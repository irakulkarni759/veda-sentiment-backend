-- Run this in Supabase SQL editor (adds to the schema you already created).

alter table claims add column if not exists top_quotes jsonb default '[]'::jsonb;
alter table claim_comments add column if not exists author text;
alter table claim_comments add column if not exists url text;
