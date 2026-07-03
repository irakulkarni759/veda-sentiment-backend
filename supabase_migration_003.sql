-- Run this in Supabase SQL editor (adds to the schema from migration 002).

-- Tags each cached claim with the summarizer logic version that produced it,
-- so a future change to summarize.py (e.g. improving relevance filtering)
-- can auto-invalidate old cache rows instead of silently serving stale
-- quotes for up to CACHE_TTL_DAYS after the fix ships.
alter table claims add column if not exists summarizer_version int default 0;
