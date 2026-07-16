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

import html
import re
import time
import random
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import tls_client

# Common words that carry no topic signal, so they shouldn't count as a
# keyword match when deciding whether a post is on-topic for the query.
_STOPWORDS = {
    "for", "the", "a", "an", "and", "or", "with", "to", "of", "in", "on",
    "is", "are", "does", "do", "vs", "how", "what", "best", "good", "help",
    "my", "your", "it", "that", "this",
}


def _keywords(query):
    """Significant lowercase words from the query, used to gauge whether a
    post is actually about the topic vs. only matching on incidental words."""
    return [
        w for w in re.split(r"[^a-z0-9]+", query.lower())
        if len(w) > 2 and w not in _STOPWORDS
    ]


def _post_is_relevant(post, keywords):
    """Keep a post only if its title or body shares a significant word with
    the query. Fails open (True) when there are no usable keywords."""
    if not keywords:
        return True
    hay = f"{post.get('title', '')} {post.get('selftext', '')}".lower()
    return any(k in hay for k in keywords)

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
# Fallback host: Reddit's WAF treats www and old differently — datacenter
# egress IPs (Railway et al) that get empty/blocked responses from www
# often still get real JSON from old.reddit.com. Same .json endpoints.
OLD_BASE = "https://old.reddit.com"

# If low-volume datacenter requests start getting blocked, add a residential
# proxy here, e.g. "http://user:pass@host:port". Try TLS first.
PROXIES = None

DEBUG = True  # prints Reddit's raw response on failure so we can see what's blocking us
_warmed_up = False

# Once Reddit's WAF 403s a .json request it 403s them all — the block keys on
# the client fingerprint, not the URL. Remember the block so later calls in
# the same job skip straight to the RSS fallback instead of burning seconds
# re-discovering it per request. Re-probe after the cooldown in case the
# block lifts (fingerprint-based blocks have come and gone before).
_JSON_BLOCK_COOLDOWN_S = 600
_json_blocked_until = 0.0


def _mark_json_blocked():
    global _json_blocked_until
    _json_blocked_until = time.time() + _JSON_BLOCK_COOLDOWN_S


def _json_available():
    return time.time() >= _json_blocked_until


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
        elif resp.status_code == 403:
            # WAF block page: deterministic for this client fingerprint, so
            # retrying is wasted time — flag it and let callers fall back to RSS.
            _mark_json_blocked()
            print("[warn] HTTP 403 (WAF block on .json); switching to RSS fallback")
            if DEBUG:
                print(f"[debug] body: {resp.text[:300]}".replace("\n", " "))
            return None
        else:
            print(f"[warn] HTTP {resp.status_code}; retry {attempt + 1}")
            if DEBUG:
                snippet = resp.text[:300].replace("\n", " ")
                print(f"[debug] body: {snippet}")
                print(f"[debug] headers: {dict(resp.headers)}")
            time.sleep(2 ** attempt)
    return None


# ---------------------------------------------------------------------------
# RSS fallback transport. As of mid-2026 Reddit's WAF 403s every
# unauthenticated .json endpoint for non-browser clients (even with TLS
# impersonation), but the same content is still served over the documented
# .rss feeds — to plain HTTP clients, no fingerprint tricks needed. RSS lacks
# post/comment scores and comment counts, so .json stays the preferred
# transport whenever it works and RSS only kicks in on a WAF block.
# ---------------------------------------------------------------------------

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
_POST_PERMALINK_RE = re.compile(r"reddit\.com(/r/([^/]+)/comments/([a-z0-9]+)[^?\s]*)")


def _strip_html(fragment):
    """Atom content is escaped HTML; flatten it to plain comment text."""
    text = re.sub(r"<[^>]+>", " ", html.unescape(fragment or ""))
    return re.sub(r"\s+", " ", text).strip()


def _get_rss(url, params=None, max_retries=3):
    """GET an Atom feed, returning the parsed root or None.

    Deliberately a plain stdlib client, NOT the tls_client session: the WAF
    flags the imperfect Chrome TLS impersonation even on .rss (403), while a
    boring vanilla client gets clean 200s — RSS is a documented feed feature,
    not gated on looking like a browser."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url, headers={"User-Agent": HEADERS["User-Agent"], "Accept": "*/*"}
    )
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return ET.fromstring(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = min(int(e.headers.get("retry-after") or 5), 30)
                print(f"[warn] rss rate limited; sleeping {wait}s")
                time.sleep(wait)
                continue
            print(f"[warn] rss HTTP {e.code} from {url.split('?')[0]}")
            return None
        except Exception as e:
            print(f"[warn] rss fetch/parse failed ({e}) from {url.split('?')[0]}")
            time.sleep(2 ** attempt)
    return None


def _entry_fields(entry):
    """(permalink_match, author, title, content_text) for one Atom entry."""
    link = entry.find("atom:link", _ATOM_NS)
    href = (link.get("href") if link is not None else "") or ""
    m = _POST_PERMALINK_RE.search(href)
    author_el = entry.find("atom:author/atom:name", _ATOM_NS)
    author = (author_el.text or "").strip() if author_el is not None else ""
    if author.startswith("/u/"):
        author = author[3:]
    title_el = entry.find("atom:title", _ATOM_NS)
    content_el = entry.find("atom:content", _ATOM_NS)
    return (
        m,
        author,
        (title_el.text or "") if title_el is not None else "",
        _strip_html(content_el.text if content_el is not None else ""),
    )


def _search_posts_rss(query, limit, sort, time_filter, subreddit):
    path = f"/r/{subreddit}/search.rss" if subreddit else "/search.rss"
    params = {"restrict_sr": 1} if subreddit else {}
    params.update({"q": query, "limit": limit, "sort": sort, "t": time_filter, "type": "link"})
    for base in (BASE, OLD_BASE):
        root = _get_rss(f"{base}{path}", params=params)
        if root is None:
            continue
        out = []
        for entry in root.findall("atom:entry", _ATOM_NS):
            m, _author, title, content = _entry_fields(entry)
            if not m:  # subreddit suggestions etc., not posts
                continue
            out.append({
                "id": m.group(3),
                "title": title,
                "selftext": content,  # includes feed boilerplate; only used for keyword matching
                "subreddit": m.group(2),
                "permalink": m.group(1),
                "score": 0,           # not exposed in RSS
                "num_comments": None,  # unknown — deliberately not 0, see gather_sentiment
            })
        if out:
            print(f"[info] rss search served {len(out)} posts from {base}")
            return out
    return []


def _fetch_comments_rss(permalink, max_comments):
    post_path = permalink.rstrip("/")
    for base in (BASE, OLD_BASE):
        root = _get_rss(f"{base}{permalink}.rss", params={"limit": 200})
        if root is None:
            continue
        comments = []
        for entry in root.findall("atom:entry", _ATOM_NS):
            m, author, _title, body = _entry_fields(entry)
            if not m or m.group(1).rstrip("/") == post_path:
                continue  # the post's own entry, not a comment
            if not body or body in ("[deleted]", "[removed]"):
                continue
            comments.append({
                "body": body,
                "score": 0,  # RSS doesn't expose comment scores
                "subreddit": m.group(2),
                "author": author or None,
                "url": f"{BASE}{m.group(1)}",
            })
            if len(comments) >= max_comments:
                break
        if comments:
            return comments
    return []


def search_posts(query, limit=25, sort="relevance", time_filter="all", subreddit=None):
    """Find posts relevant to a wellness claim. Optionally scope to a subreddit.

    Tries www.reddit.com first, then old.reddit.com — an empty result from
    www very often means the egress IP is being soft-blocked (observed on
    Railway: instant empty responses for queries with plenty of real
    results), and old.reddit frequently still serves real JSON there.
    When the WAF blocks .json entirely, falls back to the RSS feeds."""
    path = f"/r/{subreddit}/search.json" if subreddit else "/search.json"
    params = {"restrict_sr": 1} if subreddit else {}
    params.update({
        "q": query,
        "limit": limit,
        "sort": sort,       # relevance | top | new | comments
        "t": time_filter,   # all | year | month | week
        "type": "link",
        "raw_json": 1,
    })

    for base in (BASE, OLD_BASE):
        if not _json_available():
            break
        data = _get_json(f"{base}{path}", params=params)
        out = []
        for child in (data or {}).get("data", {}).get("children", []):
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
        if out:
            return out
        print(f"[warn] empty search from {base} for '{query}'"
              + ("; trying old.reddit.com" if base == BASE else ""))
    return _search_posts_rss(query, limit, sort, time_filter, subreddit)


def fetch_comments(permalink, min_score=1, max_comments=100):
    """Fetch and flatten one post's comment tree (top-level + nested replies).
    Same www -> old.reddit.com fallback as search_posts; displayed comment
    permalinks always use www regardless of which host served the data.
    On a WAF block, falls back to the post's RSS feed — which has no comment
    scores, so min_score doesn't apply on that path."""
    data = None
    if _json_available():
        data = _get_json(f"{BASE}{permalink}.json", params={"raw_json": 1, "limit": 200})
    if (not data or len(data) < 2) and _json_available():
        data = _get_json(f"{OLD_BASE}{permalink}.json", params={"raw_json": 1, "limit": 200})
    if not data or len(data) < 2:
        return _fetch_comments_rss(permalink, max_comments)
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


def _search_variants(query):
    """Progressively broader search phrasings for one claim, most specific
    first. Reddit has threads on almost any wellness topic — when the full
    query comes up dry it's nearly always the PHRASING (a "for <purpose>"
    clause nobody writes in a post title, or product-name noise like
    "10% + Zinc 1%"), not a genuine absence of discussion. Later variants
    only run when earlier ones didn't gather enough comments."""
    variants = [query]

    # Punctuation/concentration noise: "Niacinamide 10% + Zinc 1%" -> the
    # words people actually type in a thread title.
    cleaned = re.sub(r"\d+(\.\d+)?\s*%", " ", query)
    cleaned = re.sub(r"[+/®™]", " ", cleaned)
    cleaned = " ".join(cleaned.split())
    if cleaned and cleaned.lower() != query.lower():
        variants.append(cleaned)

    # Strip a trailing "for <purpose>" clause — people discuss "rosemary
    # oil", not "rosemary oil for hair growth".
    for base in (query, cleaned):
        idx = base.lower().find(" for ")
        if idx > 0:
            core = base[:idx].strip()
            if core:
                variants.append(core)

    # Dedupe, preserving order.
    seen = set()
    out = []
    for v in variants:
        key = v.lower()
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


# Stop broadening once this many comments are in hand — enough for a real
# summary + quote selection without burning the request budget.
MIN_COMMENTS_TARGET = 12
# Hard cap on comment-tree fetches per claim across ALL passes, so the
# broadened search can't blow past the frontend's polling patience.
MAX_POSTS_TOTAL = 10


def gather_sentiment(query, post_limit=8, per_post_comments=40, subreddit=None):
    """Top-level entry for Veda: claim string -> list of relevant comments.

    Multi-pass: tries the query as given, then progressively broader
    variants (punctuation stripped, purpose clause dropped, sort=comments)
    until enough comments are gathered or the request budget runs out."""
    all_comments = []
    seen_post_ids = set()
    posts_fetched = 0

    def run_pass(search_query, sort="relevance"):
        nonlocal posts_fetched
        posts = search_posts(search_query, limit=post_limit, sort=sort, subreddit=subreddit)

        # Drop posts that don't share any significant word with the query
        # before we spend a request pulling their comment tree. Fail open:
        # if the filter would remove everything, keep the full set.
        keywords = _keywords(search_query)
        fresh = [p for p in posts if p.get("id") not in seen_post_ids]
        relevant = [p for p in fresh if _post_is_relevant(p, keywords)]
        posts_to_use = relevant or fresh
        print(
            f"[info] pass '{search_query}' (sort={sort}): {len(posts)} posts, "
            f"{len(posts_to_use)} new+relevant"
        )

        for p in posts_to_use:
            if posts_fetched >= MAX_POSTS_TOTAL or len(all_comments) >= MIN_COMMENTS_TARGET * 3:
                return
            if p["num_comments"] == 0:
                continue
            seen_post_ids.add(p.get("id"))
            posts_fetched += 1
            cs = fetch_comments(p["permalink"], max_comments=per_post_comments)
            for c in cs:
                c["post_title"] = p["title"]
            all_comments.extend(cs)
            # Still randomized and >0.5s to stay polite and dodge 429s, but
            # tight enough that an uncached request finishes inside the
            # frontend's polling window.
            time.sleep(random.uniform(0.5, 1.0))

    for variant in _search_variants(query):
        run_pass(variant)
        if len(all_comments) >= MIN_COMMENTS_TARGET or posts_fetched >= MAX_POSTS_TOTAL:
            break

    # Last resort: same broadest variant, but ranked by comment count —
    # surfaces big discussion threads that relevance sort can bury.
    if len(all_comments) < MIN_COMMENTS_TARGET and posts_fetched < MAX_POSTS_TOTAL:
        run_pass(_search_variants(query)[-1], sort="comments")

    print(f"[info] {len(all_comments)} comments gathered across {posts_fetched} posts")
    return all_comments


if __name__ == "__main__":
    results = gather_sentiment("rosemary oil for hair")
    for r in results[:10]:
        print(f"[{r['score']:+d}] r/{r['subreddit']}: {r['body'][:120]}")
