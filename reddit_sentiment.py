"""
Reddit community-sentiment fetcher for Veda.

Two fixes baked in:
  1. TLS fingerprint: tls_client impersonates a real browser so Reddit returns
     JSON instead of the 'blocked by network security' HTML page.
  2. Architecture: Reddit has no public full-text COMMENT search anymore, so we
     search POSTS for the claim, then pull each post's comment tree.

Note: uses tls_client instead of curl_cffi. curl_cffi's compiled binary
conflicts with Anaconda's Python on macOS (dlopen / _CFRelease symbol error).
tls_client uses a Go binary under the hood and avoids that entirely.

Install:  pip install tls_client
Run:      python reddit_sentiment.py
"""

import time
import random
import tls_client

# tls_client replays a browser's full TLS + HTTP2 fingerprint. If Reddit ever
# starts 403ing again, try a newer identifier, e.g. "chrome131".
IMPERSONATE = "chrome_124"
_session = tls_client.Session(client_identifier=IMPERSONATE, random_tls_extension_order=True)

# Keep the UA consistent with the impersonated TLS profile. A Python UA on top of
# a Chrome TLS handshake is itself a mismatch that can get you flagged.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Referer": "https://www.reddit.com/",
}

BASE = "https://www.reddit.com"

# If low-volume datacenter requests start getting blocked, add a residential
# proxy here, e.g. "http://user:pass@host:port". Try TLS first.
PROXIES = None

DEBUG = True  # prints Reddit's raw response on failure so we can see what's blocking us
_warmed_up = False


def _warm_up():
    """Visit the homepage first so Reddit issues session cookies before /search.json."""
    global _warmed_up
    if _warmed_up:
        return
    try:
        _session.get(BASE + "/", headers=HEADERS, timeout_seconds=20)
    except Exception as e:
        print(f"[warn] warm-up request failed: {e}")
    _warmed_up = True


def _get_json(url, params=None, max_retries=3):
    """GET a Reddit .json endpoint with browser TLS impersonation + retries."""
    _warm_up()
    for attempt in range(max_retries):
        try:
            resp = _session.get(
                url,
                params=params,
                headers=HEADERS,
                proxy=PROXIES,
                timeout_seconds=20,
            )
        except Exception as e:
            print(f"[warn] request error ({e}); retry {attempt + 1}")
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 200:
            # Guard: a 200 that returns HTML means we were still soft-blocked.
            ctype = resp.headers.get("content-type", "")
            looks_json = resp.text.lstrip()[:1] in ("{", "[")
            if "json" not in ctype and not looks_json:
                print("[warn] got HTML not JSON (still blocked); retrying")
                time.sleep(3 + attempt * 2)
                continue
            try:
                return resp.json()
            except Exception:
                print("[warn] JSON parse failed; retrying")
        elif resp.status_code == 429:
            wait = int(resp.headers.get("retry-after", 5))
            print(f"[warn] rate limited; sleeping {wait}s")
            time.sleep(wait)
        else:
            print(f"[warn] HTTP {resp.status_code}; retry {attempt + 1}")
            if DEBUG:
                snippet = resp.text[:300].replace("\n", " ")
                print(f"[debug] body: {snippet}")
                print(f"[debug] headers: {dict(resp.headers)}")
            time.sleep(2 ** attempt)
    return None


def search_posts(query, limit=25, sort="relevance", time_filter="all", subreddit=None):
    """Find posts relevant to a wellness claim. Optionally scope to a subreddit."""
    if subreddit:
        url = f"{BASE}/r/{subreddit}/search.json"
        params = {"restrict_sr": 1}
    else:
        url = f"{BASE}/search.json"
        params = {}
    params.update({
        "q": query,
        "limit": limit,
        "sort": sort,       # relevance | top | new | comments
        "t": time_filter,   # all | year | month | week
        "type": "link",
        "raw_json": 1,
    })
    data = _get_json(url, params=params)
    if not data:
        return []
    out = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        out.append({
            "id": d.get("id"),
            "title": d.get("title"),
            "selftext": d.get("selftext", ""),
            "subreddit": d.get("subreddit"),
            "permalink": d.get("permalink"),
            "score": d.get("score", 0),
            "num_comments": d.get("num_comments", 0),
        })
    return out


def fetch_comments(permalink, min_score=1, max_comments=100):
    """Fetch and flatten one post's comment tree (top-level + nested replies)."""
    data = _get_json(f"{BASE}{permalink}.json", params={"raw_json": 1, "limit": 200})
    if not data or len(data) < 2:
        return []
    comments = []

    def walk(children):
        for child in children:
            if child.get("kind") != "t1":  # t1 = comment; ignore "more"/"t3"
                continue
            c = child.get("data", {})
            body = (c.get("body") or "").strip()
            if body and body not in ("[deleted]", "[removed]") and c.get("score", 0) >= min_score:
                permalink = c.get("permalink")  # e.g. /r/sub/comments/abc123/title/def456/
                comments.append({
                    "body": body,
                    "score": c.get("score", 0),
                    "subreddit": c.get("subreddit"),
                    "author": c.get("author"),
                    "url": f"{BASE}{permalink}" if permalink else None,
                })
            replies = c.get("replies")
            if isinstance(replies, dict):
                walk(replies.get("data", {}).get("children", []))

    walk(data[1].get("data", {}).get("children", []))
    return comments[:max_comments]


def gather_sentiment(query, post_limit=15, per_post_comments=40, subreddit=None):
    """Top-level entry for Veda: claim string -> list of relevant comments."""
    posts = search_posts(query, limit=post_limit, subreddit=subreddit)
    print(f"[info] {len(posts)} posts for '{query}'")
    all_comments = []
    for p in posts:
        if p["num_comments"] == 0:
            continue
        cs = fetch_comments(p["permalink"], max_comments=per_post_comments)
        for c in cs:
            c["post_title"] = p["title"]
        all_comments.extend(cs)
        time.sleep(random.uniform(1.0, 2.0))  # be polite; dodge 429s
    print(f"[info] {len(all_comments)} comments gathered")
    return all_comments


if __name__ == "__main__":
    results = gather_sentiment("rosemary oil for hair")
    for r in results[:10]:
        print(f"[{r['score']:+d}] r/{r['subreddit']}: {r['body'][:120]}")
