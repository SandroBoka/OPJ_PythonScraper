# Reddit Scraper

Collects a Reddit post and its available comments into a single `komentari.csv` file using Google Chrome and Playwright.

## Setup

Install Python 3 and Google Chrome, then run these commands in the project directory (macOS/Linux):

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install playwright
```

For later runs, activate the environment with `source .venv/bin/activate`.

## Run

```bash
python3 reddit_scraper.py "https://www.reddit.com/r/promet/comments/1xvyy6t/prekoracenje_brzine/"
```

Replace the example with a full post URL without `.json`, query parameters, or fragments. The script appends `.json` and opens the URL in Chrome. It sends additional requests when more comments are available.

If prompted, sign in to Reddit in the Chrome window opened by the script, then press **Enter in the terminal**. This separate browser profile remembers your session in `.reddit_chrome/`, which is excluded from Git.

## Output

`komentari.csv` is created in the current directory. Later runs append only new IDs; existing rows are kept unchanged.

Both posts and comments use these columns:

```text
id,type,post_id,parent_id,title,body,subreddit,created_at,url
```

- `id`: original Reddit ID (`t3_` for posts, `t1_` for comments).
- `type`: `post` or `comment`.
- `post_id`: original post ID, including for nested replies.
- `parent_id`: direct parent post or comment ID; empty for posts.
- `title`: populated for posts; empty for comments.
- `created_at`: creation time in UTC.

The terminal shows added records, skipped duplicates, and missing parents. Press **Ctrl+C** to stop and save records collected so far. Errors may leave a partial dataset; a successful run does not guarantee that Reddit returned every comment.
