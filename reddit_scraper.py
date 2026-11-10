
import asyncio
import csv
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright


URL = (
    "https://www.reddit.com/r/DC_Cinematic/comments/"
    "1xsewbp/this_was_james_gunn_dc_after_4_years_as_coceo/"
)

MAX_DEPTH = 3
MAX_PASSES = 100
MAX_IDLE_PASSES = 8
OUTPUT = Path("reddit_output")
OUTPUT.mkdir(exist_ok=True)

MORE_PATTERN = re.compile(
    r"view more|more replies|more comments|"
    r"load more|show more replies|"
    r"continue this thread|see more replies",
    re.IGNORECASE,
)

COMMENT_FIELDS = [
    "post_id",
    "comment_id",
    "parent_id",
    "depth",
    "author",
    "score",
    "created",
    "body",
    "permalink",
]


async def wait_for_reddit(page):
    """Wait for post content, allowing manual CAPTCHA completion."""

    await page.wait_for_timeout(2000)

    for attempt in range(3):
        post_count = await page.locator("shreddit-post").count()
        comment_count = await page.locator("shreddit-comment").count()

        if post_count > 0 and comment_count > 0:
            return True

        print("\nReddit content is not fully loaded.")
        print("Check the browser for 'Prove you are human'.")
        print("Complete the verification manually.")

        await asyncio.to_thread(
            input,
            "Press ENTER when the post and comments are visible..."
        )

        await page.wait_for_timeout(2000)

    return False


async def extract_post(page):
    """Extract original post metadata."""

    return await page.evaluate(
        """() => {
            const post = document.querySelector("shreddit-post");

            if (!post) return null;

            const attr = (...names) => {
                for (const name of names) {
                    const value = post.getAttribute(name);
                    if (value !== null) return value;
                }
                return null;
            };

            const bodyElement =
                post.querySelector('[slot="text-body"]') ||
                post.querySelector('[slot="post-text"]');

            return {
                id: attr("id", "thingid"),
                title: attr("post-title") ||
                    document.querySelector("h1")?.innerText || "",
                author: attr("author"),
                subreddit: attr("subreddit-prefixed-name"),
                body: bodyElement?.innerText?.trim() || "",
                score: attr("score"),
                created: attr("created-timestamp"),
                permalink: attr("permalink")
            };
        }"""
    )


async def extract_comments(page):
    """Extract comments from Reddit HTML."""

    return await page.locator("shreddit-comment").evaluate_all(
        """elements => elements.map(el => {
            const attr = (...names) => {
                for (const name of names) {
                    const value = el.getAttribute(name);
                    if (value !== null) return value;
                }
                return null;
            };

            const bodyElement =
                el.querySelector(':scope > [slot="comment"]') ||
                el.querySelector('[slot="comment"]') ||
                el.querySelector('div.md') ||
                el.querySelector('[data-testid="comment"]');

            const timeElement = el.querySelector("time");

            return {
                comment_id: attr("thingid", "thing-id"),
                parent_id: attr(
                    "parentid",
                    "parent-id",
                    "parent_id"
                ),
                depth: Number(attr("depth") ?? -1),
                author: attr("author"),
                score: attr("score"),
                created: attr(
                    "created-timestamp",
                    "created-utc"
                ) || timeElement?.getAttribute("datetime"),
                body: bodyElement?.innerText?.trim() || "",
                permalink: attr("permalink")
            };
        })"""
    )


def normalize_comment_id(value):
    """Ensure Reddit comment IDs use the t1_ prefix."""

    if not value:
        return None

    if value.startswith(("t1_", "t3_")):
        return value

    return f"t1_{value}"


def reconstruct_parent_ids(rows, post_id):
    """
    Reconstruct missing parent IDs using comment depth.

    This is a fallback only. It assumes the extracted rows
    follow the original tree order. If that assumption fails,
    the parent ID must remain unresolved.
    """

    stack = {}
    result = []

    for comment in rows:
        depth = comment["depth"]
        comment_id = normalize_comment_id(
            comment["comment_id"]
        )

        if not comment_id or depth < 0:
            continue

        comment["comment_id"] = comment_id

        parent_id = comment.get("parent_id")

        if parent_id:
            comment["parent_id"] = normalize_comment_id(parent_id)

        elif depth == 0:
            comment["parent_id"] = f"t3_{post_id}"

        elif depth - 1 in stack:
            # Inferred, not directly verified.
            comment["parent_id"] = stack[depth - 1]
            comment["parent_inferred"] = True

        else:
            comment["parent_id"] = None
            comment["parent_inferred"] = False

        stack[depth] = comment_id

        for old_depth in list(stack):
            if old_depth > depth:
                del stack[old_depth]

        result.append(comment)

    return result


async def expand_comments(page):
    """Click visible controls for loading more comments."""

    clicked = 0

    buttons = page.get_by_role(
        "button",
        name=MORE_PATTERN
    )

    count = await buttons.count()

    for index in range(count - 1, -1, -1):
        button = buttons.nth(index)

        try:
            if not await button.is_visible():
                continue

            depth = await button.evaluate(
                """el => {
                    const parent = el.closest("shreddit-comment");

                    if (!parent) return -1;

                    return Number(
                        parent.getAttribute("depth") ?? -1
                    );
                }"""
            )

            if depth >= MAX_DEPTH - 1:
                continue

            await button.click(timeout=2500)
            clicked += 1

            await page.wait_for_timeout(500)

        except Exception:
            continue

    return clicked


async def get_continuation_links(page):
    """Collect Reddit continuation links."""

    return await page.locator("a[href]").evaluate_all(
        """elements => elements
            .filter(el =>
                /continue this thread/i.test(
                    el.innerText || ""
                )
            )
            .map(el => el.href)
        """
    )


def merge_comment(existing, incoming):
    """Keep the most complete version of a comment."""

    if existing is None:
        return incoming

    for key in COMMENT_FIELDS:
        if not existing.get(key) and incoming.get(key):
            existing[key] = incoming[key]

    return existing


async def scrape():
    comments = {}
    visited_pages = set()
    pending_urls = [URL]
    unresolved = []
    post_data = None

    post_id = URL.split("/comments/")[1].split("/")[0]

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=False
        )

        context = await browser.new_context(
            viewport={"width": 1440, "height": 900}
        )

        page = await context.new_page()

        while pending_urls:
            current_url = pending_urls.pop(0)

            if current_url in visited_pages:
                continue

            visited_pages.add(current_url)

            print(f"\nOpening: {current_url}")

            try:
                response = await page.goto(
                    current_url,
                    wait_until="domcontentloaded",
                    timeout=45000
                )

                if response and response.status >= 400:
                    unresolved.append({
                        "url": current_url,
                        "reason": f"HTTP {response.status}"
                    })
                    continue

                loaded = await wait_for_reddit(page)

                if not loaded:
                    unresolved.append({
                        "url": current_url,
                        "reason": "Reddit content not loaded"
                    })
                    continue

                if post_data is None:
                    post_data = await extract_post(page)

                previous_count = -1
                idle_passes = 0

                for iteration in range(MAX_PASSES):
                    await page.mouse.wheel(0, 1200)
                    await page.wait_for_timeout(700)

                    rows = await extract_comments(page)

                    rows = reconstruct_parent_ids(
                        rows,
                        post_id
                    )

                    for comment in rows:
                        depth = comment["depth"]

                        if not 0 <= depth < MAX_DEPTH:
                            continue

                        comment["post_id"] = post_id

                        cid = comment["comment_id"]

                        comments[cid] = merge_comment(
                            comments.get(cid),
                            comment
                        )

                    clicked = await expand_comments(page)

                    print(
                        f"Pass {iteration + 1}: "
                        f"{len(comments)} unique comments, "
                        f"{clicked} expansions"
                    )

                    if (
                        len(comments) == previous_count
                        and clicked == 0
                    ):
                        idle_passes += 1
                    else:
                        idle_passes = 0

                    previous_count = len(comments)

                    if idle_passes >= MAX_IDLE_PASSES:
                        print("No new comments detected.")
                        break

                    if clicked == 0:
                        await page.evaluate(
                            """() => window.scrollTo(
                                0,
                                document.body.scrollHeight
                            )"""
                        )
                        await page.wait_for_timeout(1000)

                else:
                    unresolved.append({
                        "url": current_url,
                        "reason": "MAX_PASSES reached"
                    })

                # Save HTML snapshot for debugging.
                html = await page.content()

                (OUTPUT / "debug.html").write_text(
                    html,
                    encoding="utf-8"
                )

                links = await get_continuation_links(page)

                for link in links:
                    absolute = urljoin(current_url, link)

                    if (
                        urlparse(absolute).netloc.endswith("reddit.com")
                        and absolute not in visited_pages
                        and absolute not in pending_urls
                    ):
                        pending_urls.append(absolute)

            except Exception as error:
                unresolved.append({
                    "url": current_url,
                    "reason": str(error)
                })

        await browser.close()

    # Save post.
    if post_data is None:
        post_data = {
            "id": f"t3_{post_id}",
            "url": URL,
            "error": "Post metadata unavailable"
        }

    post_data["url"] = URL

    with open(
        OUTPUT / "post.json",
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            post_data,
            file,
            ensure_ascii=False,
            indent=2
        )

    # Save comments.
    with open(
        OUTPUT / "comments.csv",
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=COMMENT_FIELDS,
            extrasaction="ignore"
        )

        writer.writeheader()
        writer.writerows(comments.values())

    # Validate.
    missing_parent = [
        c["comment_id"]
        for c in comments.values()
        if not c.get("parent_id")
    ]

    missing_body = [
        c["comment_id"]
        for c in comments.values()
        if not c.get("body")
    ]

    missing_created = [
        c["comment_id"]
        for c in comments.values()
        if not c.get("created")
    ]

    inferred_parent = [
        c["comment_id"]
        for c in comments.values()
        if c.get("parent_inferred")
    ]

    validation = {
        "post_id": post_id,
        "total_comments": len(comments),
        "max_depth": MAX_DEPTH,
        "depth_counts": dict(
            Counter(
                c["depth"]
                for c in comments.values()
            )
        ),
        "missing_parent_id": missing_parent,
        "missing_body": missing_body,
        "missing_created": missing_created,
        "inferred_parent_id": inferred_parent,
        "unresolved_pages": unresolved,
        "complete": False,
        "note": (
            "Completeness cannot be guaranteed from "
            "rendered HTML alone."
        )
    }

    with open(
        OUTPUT / "validation.json",
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            validation,
            file,
            ensure_ascii=False,
            indent=2
        )

    with open(
        OUTPUT / "unresolved.json",
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            unresolved,
            file,
            ensure_ascii=False,
            indent=2
        )

    # Results.
    print("\n========== RESULTS ==========")
    print(f"Unique comments: {len(comments)}")

    for depth in range(MAX_DEPTH):
        count = sum(
            c["depth"] == depth
            for c in comments.values()
        )
        print(f"Depth {depth}: {count}")

    print("\n========== VALIDATION ==========")
    print(f"Missing parent_id: {len(missing_parent)}")
    print(f"Missing body: {len(missing_body)}")
    print(f"Missing created: {len(missing_created)}")
    print(f"Inferred parent_id: {len(inferred_parent)}")
    print(f"Unresolved pages: {len(unresolved)}")

    print(f"\nOutput directory: {OUTPUT.resolve()}")
    print("Completeness verified: NO")


if __name__ == "__main__":
    asyncio.run(scrape())
