"""
Veda claim-sentiment API.

Two ways to get a claim's sentiment:

  1. GET /api/claim — synchronous. Fine for a cache hit (instant), but a
     cache miss means scraping Reddit + a Claude summarization call, which
     can take anywhere from a few seconds to over a minute for a
     heavily-discussed claim. Kept for backward compatibility (the manual
     refresh-reddit-quotes.mjs script, the admin bulk-refresh tool) — but a
     browser calling this directly is at the mercy of whatever request
     duration limit sits between it and here (e.g. Cloudflare Workers'
     limits), which is shorter than a slow scrape can need.

  2. POST /api/claim/start + GET /api/claim/status — asynchronous. start
     returns instantly: either the cached result directly, or a job_id and
     kicks the real work off in the background. The frontend then polls
     status every few seconds — each poll is a fast, trivial lookup, so no
     individual request is ever slow, and the actual scrape can take as
     long as it genuinely needs without anything timing out. This is what
     the Veda frontend uses.

Requires:
  pip install fastapi uvicorn supabase python-dotenv

Env vars (put in a .env file, loaded via python-dotenv):
  SUPABASE_URL=
  SUPABASE_SERVICE_ROLE_KEY=      # service_role key, NOT anon key (bypasses RLS for writes)
  ANTHROPIC_API_KEY=

Run locally:
  uvicorn api_server:app --reload --port 8787

Deploy: Railway, Render, or Fly.io all work for a small always-on Python service.
Cloudflare Workers can't run this directly (no native TLS binaries), so this
stays a separate service that your TanStack frontend calls over HTTP.
"""

import os
import uuid
import time
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks, FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from supabase import create_client

from reddit_sentiment import gather_sentiment
from summarize import summarize_comments

load_dotenv()

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_ROLE_KEY"],
)

CACHE_TTL_DAYS = 14  # re-scrape a claim after this many days

# Bump this whenever summarize.py's logic meaningfully changes (e.g. the
# relevance-filtering fix for top_quotes). Any cached row tagged with an
# older version is treated as a cache miss, regardless of CACHE_TTL_DAYS —
# otherwise a summarizer improvement silently doesn't apply to already-cached
# claims for up to two weeks, which is exactly what happened with the
# relevance-check fix and existing "cold water immersion" style claims.
SUMMARIZER_VERSION = 6  # v6: multi-pass retrieval in gather_sentiment (query variants + sort=comments fallback) — rows cached from the old single-pass scrape often have few/zero comments for claims that DO have real discussion, so force a re-scrape

app = FastAPI(title="Veda Claim Sentiment API")

# Adjust to your actual Lovable / production origin(s) before shipping.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this to your real frontend domain in production
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# In-memory job store for the async start/status flow. Deliberately simple:
# Railway runs this as a single always-on container, and jobs are short-
# lived (claimed, polled to completion or abandoned, within minutes) — a
# real job queue (Redis, etc.) would be overkill here. The one real
# limitation: a Railway restart/redeploy mid-job loses in-flight jobs, and
# this won't work if this service is ever scaled to multiple instances
# without a shared store. Fine for the current single-instance deployment.
JOBS: dict[str, dict] = {}
JOB_TTL_SECONDS = 15 * 60  # stop tracking a job's memory after this long


def normalize(claim: str) -> str:
    return " ".join(claim.strip().lower().split())


def get_cached(normalized: str):
    res = (
        supabase.table("claims")
        .select("*")
        .eq("normalized_claim", normalized)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    row = res.data[0]
    updated = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) - updated > timedelta(days=CACHE_TTL_DAYS):
        return None  # stale, treat as cache miss
    # An empty scrape (0 comments) is never a valid cache entry — it's what
    # a Reddit block/outage looks like, and serving it for 14 days turns a
    # transient failure into a permanent "limited discussion". Always retry.
    if (row.get("comment_count") or 0) == 0:
        return None
    # Version gate, but only for THIN old rows (no picked quotes). A rich
    # older-version row with real comments beats re-scraping it through a
    # possibly-blocked datacenter egress and losing data we already had —
    # blanket invalidation on version bump is exactly what mass-converted
    # previously-good claims into cached "limited discussion" rows.
    if (row.get("summarizer_version") or 0) < SUMMARIZER_VERSION and not row.get("top_quotes"):
        return None
    comments_res = (
        supabase.table("claim_comments")
        .select("*")
        .eq("claim_id", row["id"])
        .order("score", desc=True)
        .execute()
    )
    row["comments"] = comments_res.data
    return row


def save_result(claim: str, normalized: str, summary: dict, comments: list[dict]):
    claim_row = {
        "claim_text": claim,
        "normalized_claim": normalized,
        "summary": summary["summary"],
        "sentiment": summary["sentiment"],
        "sentiment_score": summary["sentiment_score"],
        "key_themes": summary["key_themes"],
        "caveats": summary["caveats"],
        "top_quotes": summary["top_quotes"],
        "comment_count": len(comments),
        "summarizer_version": SUMMARIZER_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    existing = (
        supabase.table("claims").select("id").eq("normalized_claim", normalized).execute()
    )
    if existing.data:
        claim_id = existing.data[0]["id"]
        supabase.table("claims").update(claim_row).eq("id", claim_id).execute()
        supabase.table("claim_comments").delete().eq("claim_id", claim_id).execute()
    else:
        inserted = supabase.table("claims").insert(claim_row).execute()
        claim_id = inserted.data[0]["id"]

    if comments:
        rows = [
            {
                "claim_id": claim_id,
                "body": c["body"],
                "score": c.get("score", 0),
                "subreddit": c.get("subreddit"),
                "post_title": c.get("post_title"),
                "author": c.get("author"),
                "url": c.get("url"),
            }
            for c in comments
        ]
        # batch insert
        supabase.table("claim_comments").insert(rows).execute()

    return claim_id


def _cached_to_response(cached: dict) -> dict:
    return {
        "claim": cached["claim_text"],
        "summary": cached["summary"],
        "sentiment": cached["sentiment"],
        "sentiment_score": cached["sentiment_score"],
        "key_themes": cached["key_themes"],
        "caveats": cached["caveats"],
        "top_quotes": cached.get("top_quotes", []),
        "comment_count": cached.get("comment_count") or len(cached.get("comments") or []),
        "comments": cached["comments"],
        "cached": True,
    }


def _run_claim_scrape(query: str) -> dict:
    """The actual slow work: scrape, summarize, persist. Shared by the
    synchronous endpoint and the background job runner."""
    normalized = normalize(query)
    comments = gather_sentiment(query, post_limit=8, per_post_comments=40)
    summary = summarize_comments(query, comments)
    # NEVER persist an empty scrape: it would overwrite whatever real
    # comments a previous scrape found (save_result deletes + reinserts)
    # and turn a transient Reddit block into a cached "limited discussion".
    # The empty result still goes back to this one caller; the next request
    # simply tries the scrape again.
    if comments:
        save_result(query, normalized, summary, comments)
    else:
        print(f"[warn] scrape for '{query}' returned 0 comments — not cached, will retry on next request")
    return {
        "claim": query,
        "summary": summary["summary"],
        "sentiment": summary["sentiment"],
        "sentiment_score": summary["sentiment_score"],
        "key_themes": summary["key_themes"],
        "caveats": summary["caveats"],
        "top_quotes": summary["top_quotes"],
        "comment_count": len(comments),
        "comments": comments,
        "cached": False,
    }


def _run_job(job_id: str, query: str):
    try:
        result = _run_claim_scrape(query)
        JOBS[job_id] = {"status": "done", "result": result, "started_at": JOBS[job_id]["started_at"]}
    except Exception as e:
        print(f"[error] job {job_id} failed: {e}")
        JOBS[job_id] = {"status": "error", "result": None, "started_at": JOBS[job_id]["started_at"]}


def _prune_old_jobs():
    now = time.time()
    stale = [jid for jid, j in JOBS.items() if now - j["started_at"] > JOB_TTL_SECONDS]
    for jid in stale:
        JOBS.pop(jid, None)


@app.get("/api/claim")
def get_claim(query: str = Query(..., min_length=2), force: bool = Query(False)):
    """Synchronous path — instant on a cache hit, but a cache miss blocks
    until the full scrape finishes. Kept for scripts/tools that can afford
    to wait; browser code should use /api/claim/start + /api/claim/status
    instead so it's never at the mercy of a request-duration limit."""
    normalized = normalize(query)
    cached = None if force else get_cached(normalized)
    if cached:
        return _cached_to_response(cached)
    return _run_claim_scrape(query)


@app.post("/api/claim/start")
def start_claim(query: str = Query(..., min_length=2), force: bool = Query(False), background_tasks: BackgroundTasks = None):
    """Returns instantly. A cache hit comes back done immediately; a cache
    miss starts the scrape in the background and returns a job_id to poll."""
    _prune_old_jobs()
    normalized = normalize(query)
    cached = None if force else get_cached(normalized)
    if cached:
        return {"status": "done", **_cached_to_response(cached)}

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "pending", "result": None, "started_at": time.time()}
    background_tasks.add_task(_run_job, job_id, query)
    return {"status": "pending", "job_id": job_id}


@app.get("/api/claim/status")
def claim_status(job_id: str = Query(..., min_length=1)):
    """Fast, trivial lookup — safe to poll every few seconds for as long as
    the frontend wants to keep waiting."""
    job = JOBS.get(job_id)
    if not job:
        # Job finished long enough ago to be pruned, or never existed
        # (e.g. a stale job_id from before a redeploy). Treat as an error
        # rather than hanging the caller forever on an unknown id.
        return {"status": "error"}
    if job["status"] == "done":
        return {"status": "done", **job["result"]}
    return {"status": job["status"]}
