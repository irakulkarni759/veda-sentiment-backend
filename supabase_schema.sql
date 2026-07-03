-- Run this in your Supabase SQL editor.
-- Adjust table/column names if they collide with your existing Veda schema.

create table if not exists claims (
  id uuid primary key default gen_random_uuid(),
  claim_text text not null,
  normalized_claim text not null unique,  -- lowercased/trimmed, used for cache lookup
  summary text,
  sentiment text,               -- positive | mixed | negative | insufficient_data
  sentiment_score numeric,      -- 0.0 - 1.0
  key_themes text[],
  caveats text,
  post_count int default 0,
  comment_count int default 0,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create index if not exists idx_claims_normalized on claims (normalized_claim);

create table if not exists claim_comments (
  id uuid primary key default gen_random_uuid(),
  claim_id uuid references claims(id) on delete cascade,
  body text not null,
  score int default 0,
  subreddit text,
  post_title text,
  created_at timestamptz default now()
);

create index if not exists idx_claim_comments_claim_id on claim_comments (claim_id);

-- Optional: enable RLS and allow public read (adjust to your auth model)
alter table claims enable row level security;
alter table claim_comments enable row level security;

create policy "Public read claims" on claims for select using (true);
create policy "Public read claim_comments" on claim_comments for select using (true);

-- Writes should go through your backend service using the service_role key,
-- which bypasses RLS, so no insert/update policy is needed for anon users.
