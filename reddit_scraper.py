import csv
import json
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

FIELDS = [
    "id",
    "type",
    "post_id",
    "parent_id",
    "title",
    "body",
    "subreddit",
    "created_at",
    "url",
]

BASE_URL = "https://www.reddit.com"
OUTPUT = Path("komentari.csv")
CHROME_PROFILE = Path(__file__).resolve().parent / ".reddit_chrome"


class ScrapeError(Exception):
    """Fetching cannot be completed or the response has an unexpected format."""


def fullname(data, prefix):
    value = data.get("name") or data.get("id", "")

    if not isinstance(value, str):
        raise ScrapeError("Invalid Reddit ID.")

    if not value.startswith(prefix + "_"):
        value = prefix + "_" + value

    if not re.fullmatch(prefix + r"_[a-z0-9]+", value):
        raise ScrapeError("Invalid Reddit ID: " + value)

    return value


def make_row(data, kind, post_id):
    is_post = kind == "t3"
    item_id = fullname(data, kind)
    parent_id = "" if is_post else data.get("parent_id", "")

    if is_post and item_id != post_id:
        raise ScrapeError("Reddit returned a different post than requested.")

    if not is_post:
        if not re.fullmatch(r"t[13]_[a-z0-9]+", parent_id or ""):
            raise ScrapeError("Missing original parent_id for " + item_id)

        if data.get("link_id", post_id) != post_id:
            raise ScrapeError("Comment belongs to a different post: " + item_id)

        if parent_id.startswith("t3_") and parent_id != post_id:
            raise ScrapeError("Comment has an incorrect parent post.")

        if parent_id == item_id:
            raise ScrapeError("A comment cannot be its own parent.")

    created = data.get("created_utc")
    created_at = ""

    if created is not None:
        created_at = (
            datetime.fromtimestamp(float(created), tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    fallback_path = "/comments/" + post_id[3:] + "/"

    if not is_post:
        fallback_path += "_/" + item_id[3:] + "/"

    path = data.get("permalink") or fallback_path

    return {
        "id": item_id,
        "type": "post" if is_post else "comment",
        "post_id": post_id,
        "parent_id": parent_id,
        "title": data.get("title", "") if is_post else "",
        "body": data.get("selftext", "") if is_post else data.get("body", ""),
        "subreddit": data.get("subreddit", ""),
        "created_at": created_at,
        "url": path if path.startswith("https://") else BASE_URL + path,
    }


def listing_children(listing):
    if not isinstance(listing, dict):
        raise ScrapeError("Invalid JSON comment listing.")

    children = listing.get("data", {}).get("children")

    if not isinstance(children, list):
        raise ScrapeError("The JSON response is missing the children list.")

    return children


class RedditClient:
    def __init__(self, page):
        self.page = page
        self.last_request = 0.0
        self.requested = set()
        self.found = set()
        self.login_prompted = False

    def wait_for_login(self):
        self.login_prompted = True

        print("Sign in to Reddit in the Chrome window opened by this script.")
        self.page.goto(BASE_URL + "/login/", wait_until="domcontentloaded", timeout=45000)

        input("After signing in, press ENTER here to retry the JSON URL...")

    def get_json(self, url, params=None):
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)

        for attempt in range(3):
            # Send requests sequentially, at least one second apart.
            time.sleep(max(0, 1.0 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()

            response = self.page.goto(url, wait_until="domcontentloaded", timeout=45000)

            if response is None:
                raise ScrapeError("Chrome did not receive an HTTP response.")

            if response.status in (401, 403) and not self.login_prompted:
                self.wait_for_login()
                continue

            retryable = response.status == 429 or 500 <= response.status < 600

            if retryable and attempt < 2:
                try:
                    delay = float(response.headers.get("retry-after", 2 ** (attempt + 1)))
                except (TypeError, ValueError):
                    delay = 2 ** (attempt + 1)

                if delay > 60:
                    raise ScrapeError(
                        "Reddit requires a longer wait; run the script again later."
                    )

                print(
                    f"HTTP {response.status}; retrying in {max(1, delay):g} s.",
                    file=sys.stderr,
                )

                time.sleep(max(1, delay))
                continue

            if not response.ok:
                raise ScrapeError(f"Reddit returned HTTP {response.status} in Chrome.")

            try:
                return json.loads(response.text())

            except json.JSONDecodeError as error:
                if not self.login_prompted:
                    self.wait_for_login()
                    continue

                raise ScrapeError("Reddit did not return valid JSON in Chrome.") from error

        raise ScrapeError("Unable to fetch JSON after three attempts.")

    def iter_rows(self, url):
        """Iteratively traverse the available comment tree, including 'more' entries."""

        self.requested.clear()
        self.found.clear()

        json_url = url + ".json"
        payload = self.get_json(json_url)

        if not isinstance(payload, list) or len(payload) != 2:
            raise ScrapeError("Expected a post with a comment listing.")

        posts = listing_children(payload[0])

        if len(posts) != 1 or posts[0].get("kind") != "t3":
            raise ScrapeError("The requested post was not found in the response.")

        post_id = fullname(posts[0]["data"], "t3")
        yield make_row(posts[0]["data"], "t3", post_id)

        things = deque(listing_children(payload[1]))
        more = deque()
        branches = set()

        while things or more:
            if things:
                thing = things.popleft()
                kind, data = thing.get("kind"), thing.get("data", {})

                if kind == "t1":
                    row = make_row(data, kind, post_id)

                    # A thread continuation may repeat a parent with new replies.
                    replies = data.get("replies")

                    if replies:
                        things.extend(listing_children(replies))

                    if row["id"] in self.found:
                        continue

                    self.found.add(row["id"])
                    yield row

                elif kind == "more":
                    more.append(data)

                else:
                    raise ScrapeError(f"Unexpected record type: {kind!r}")

                continue

            extra = more.popleft()
            child_ids = extra.get("children", [])

            if not isinstance(child_ids, list) or any(
                    not isinstance(child, str) or not re.fullmatch(r"[a-z0-9]+", child)
                    for child in child_ids
            ):
                raise ScrapeError("Invalid list of additional comment IDs.")

            remaining = sorted(
                {"t1_" + child for child in child_ids}
                - self.found
                - self.requested
            )

            if remaining:
                batch = remaining[:100]

                if len(remaining) > 100:
                    more.append({
                        "children": [child[3:] for child in remaining[100:]]
                    })

                response = self.get_json(
                    BASE_URL + "/api/morechildren.json",
                    {
                        "api_type": "json",
                        "raw_json": 1,
                        "sort": "old",
                        "link_id": post_id,
                        "children": ",".join(child[3:] for child in batch),
                    },
                )

                result = response.get("json", {})

                if result.get("errors"):
                    raise ScrapeError(f"Reddit API error: {result['errors']}")

                children = result.get("data", {}).get("things")

                if not isinstance(children, list):
                    raise ScrapeError("Invalid response for additional comments.")

                self.requested.update(batch)
                things.extend(children)

            elif not child_ids:
                # 'Continue this thread' may have an empty children list.
                parent = extra.get("parent_id", "")

                if re.fullmatch(r"t1_[a-z0-9]+", parent):
                    if parent in branches:
                        raise ScrapeError(
                            "Unable to expand the thread continuation: " + parent
                        )

                    branches.add(parent)
                    response = self.get_json(
                        json_url,
                        {
                            "raw_json": 1,
                            "sort": "old",
                            "limit": 500,
                            "comment": parent[3:],
                            "context": 0,
                        },
                    )

                    if not isinstance(response, list) or len(response) != 2:
                        raise ScrapeError("Invalid response for the thread continuation.")

                    things.extend(listing_children(response[1]))

                elif extra.get("count", 0):
                    raise ScrapeError("Additional comments have no available IDs.")


def read_existing_ids(path):
    if not path.exists() or path.stat().st_size == 0:
        return set()

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)

        if reader.fieldnames != FIELDS:
            raise ValueError("The existing CSV has unexpected columns; check komentari.csv.")

        ids = set()

        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Invalid CSV row near line {reader.line_num}.")

            if not re.fullmatch(r"t[13]_[a-z0-9]+", row["id"]):
                raise ValueError(f"Invalid ID in the CSV near line {reader.line_num}.")

            ids.add(row["id"])

        return ids


def append_rows(path, rows):
    """Append only new IDs without overwriting existing rows."""

    existing = read_existing_ids(path)
    new_rows = [row for item_id, row in rows.items() if item_id not in existing]

    has_content = path.exists() and path.stat().st_size > 0
    needs_newline = False

    if has_content:
        with path.open("rb") as file:
            file.seek(-1, 2)
            needs_newline = file.read(1) not in (b"\r", b"\n")

    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8", newline="") as file:
        if needs_newline and new_rows:
            file.write("\n")

        writer = csv.DictWriter(file, fieldnames=FIELDS)

        if not has_content:
            writer.writeheader()

        writer.writerows(new_rows)

    all_ids = existing | rows.keys()
    missing = {
        row["parent_id"]
        for row in rows.values()
        if row["parent_id"] and row["parent_id"] not in all_ids
    }

    return new_rows, len(rows) - len(new_rows), missing


def main():
    if len(sys.argv) != 2:
        print(
            'Wrong arguments, please run with: python3 reddit_scraper.py "REDDIT_LINK"',
            file=sys.stderr,
        )

        return 2

    url = sys.argv[1]

    try:
        read_existing_ids(OUTPUT)  # Validate the format before sending network requests.
    except (ValueError, OSError, UnicodeError, csv.Error) as error:
        print(f"Error: {error}", file=sys.stderr)

        return 2

    print(f"Fetching {url}...")
    client = None
    rows = {}
    exit_code = 0

    try:
        with sync_playwright() as playwright:
            print("Opening Chrome with the script's saved browser profile...")

            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(CHROME_PROFILE),
                channel="chrome",
                headless=False,
                timeout=30000,
            )

            try:
                page = context.pages[0] if context.pages else context.new_page()
                client = RedditClient(page)

                for row in client.iter_rows(url):
                    rows.setdefault(row["id"], row)

                    if len(rows) % 100 == 0:
                        print(f"Records fetched: {len(rows)}", flush=True)

            finally:
                context.close()

    except KeyboardInterrupt:
        print(
            "\nFetching interrupted; saving records collected so far.",
            file=sys.stderr,
        )
        exit_code = 130

    except (
            ScrapeError,
            PlaywrightError,
            OSError,
            EOFError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OverflowError,
    ) as error:
        print(f"Fetching was not completed: {error}", file=sys.stderr)
        exit_code = 1

    try:
        new_rows, skipped, missing = append_rows(OUTPUT, rows)

    except (OSError, ValueError, UnicodeError, csv.Error) as error:
        print(f"Unable to save the CSV: {error}", file=sys.stderr)
        return 1

    print(f"Posts added: {sum(row['type'] == 'post' for row in new_rows)}")
    print(f"Comments added: {sum(row['type'] == 'comment' for row in new_rows)}")
    print(f"Existing records skipped: {skipped}")
    print(f"Parent records missing from the CSV: {len(missing)}")

    unresolved = client.requested - client.found if client else set()

    if unresolved:
        print(f"Additional comments not returned by Reddit: {len(unresolved)}")
        exit_code = exit_code or 1

    print(f"CSV: {OUTPUT.resolve()}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
