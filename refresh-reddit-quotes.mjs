#!/usr/bin/env node
/**
 * Refreshes real Reddit quotes for every stored trend, run LOCALLY from
 * your own computer — not deployed, not run on Cloudflare Workers.
 *
 * Why this exists: Reddit's public search.json endpoint (the "just append
 * .json to any Reddit URL" trick, no API key needed) works fine, but
 * Reddit blocks it from datacenter/cloud IPs — which is exactly what
 * Cloudflare Workers is, so it doesn't work from the deployed site. Your
 * home internet connection is a residential IP, not a datacenter one, so
 * this should get through where the deployed version couldn't.
 *
 * This writes directly to the same Supabase table the site reads from
 * (generated_trends: opinions / community_verdict / sentiment_score), so
 * once it finishes, the results show up on your live site automatically —
 * no redeploy needed.
 *
 * USAGE:
 *   1. npm install (if you haven't already, from the project root)
 *   2. Set three env vars (see below), then run:
 *        node scripts/refresh-reddit-quotes.mjs
 *
 * REQUIRED ENV VARS (same values as your Lovable env vars):
 *   VEDA_SUPABASE_URL          — your Supabase project URL
 *   VEDA_SUPABASE_SECRET_KEY   — your Supabase service-role key
 *   ANTHROPIC_API_KEY          — used for the quality-filtering step
 *
 * Example (macOS/Linux):
 *   VEDA_SUPABASE_URL=https://xxxx.supabase.co \
 *   VEDA_SUPABASE_SECRET_KEY=eyJ... \
 *   ANTHROPIC_API_KEY=sk-ant-... \
 *   node scripts/refresh-reddit-quotes.mjs
 *
 * Contract, same as the deployed version: real quotes only. If nothing
 * usable/relevant/genuine turns up for a trend, it gets an empty list —
 * never a fabricated fallback.
 */

import { createClient } from "@supabase/supabase-js";

const SUPABASE_URL = process.env.VEDA_SUPABASE_URL;
const SUPABASE_KEY = process.env.VEDA_SUPABASE_SECRET_KEY;
const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY;

if (!SUPABASE_URL || !SUPABASE_KEY) {
  console.error("Missing VEDA_SUPABASE_URL / VEDA_SUPABASE_SECRET_KEY env vars.");
  process.exit(1);
}
if (!ANTHROPIC_API_KEY) {
  console.error("Missing ANTHROPIC_API_KEY env var (needed for quality filtering).");
  process.exit(1);
}

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

const USER_AGENT = "veda-wellness-app-local-script/1.0 (personal project, contact via github)";
const DELAY_MS = 1200; // polite pause between Reddit requests

const STOPWORDS = new Set([
  "for", "the", "a", "an", "and", "or", "with", "to", "of", "in", "on",
  "is", "are", "does", "do", "vs", "how", "what", "best", "good", "help",
]);

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function extractKeywords(query) {
  return query
    .toLowerCase()
    .split(/\s+/)
    .map((w) => w.replace(/[^\w]/g, ""))
    .filter((w) => w.length > 2 && !STOPWORDS.has(w));
}

function isRelevant(text, keywords) {
  if (keywords.length === 0) return true;
  const lower = text.toLowerCase();
  return keywords.some((k) => lower.includes(k));
}

function isUsable(text) {
  return !!text && text !== "[deleted]" && text !== "[removed]" && text.trim().length >= 40;
}

function looksLikeSpam(text) {
  return /https?:\/\/|www\.|\.com\b/i.test(text);
}

function looksEnglish(text) {
  const lower = ` ${text.toLowerCase()} `;
  const commonWords = [" the ", " and ", " is ", " was ", " this ", " with ", " for ", " i ", " my ", " have ", " it ", " you ", " to ", " a ", " that ", " so ", " but ", " me "];
  const hits = commonWords.filter((w) => lower.includes(w)).length;
  const nonAsciiCount = (text.match(/[^\x00-\x7F]/g) ?? []).length;
  const nonAsciiRatio = nonAsciiCount / text.length;
  return hits >= 1 && nonAsciiRatio < 0.03;
}

function trimExcess(text, max = 600) {
  const clean = text.trim();
  if (clean.length <= max) return clean;
  const cut = clean.slice(0, max);
  const lastSpace = cut.lastIndexOf(" ");
  return `${cut.slice(0, lastSpace > 0 ? lastSpace : max)}...`;
}

async function searchReddit(query, type) {
  try {
    const res = await fetch(
      `https://old.reddit.com/search.json?q=${encodeURIComponent(query)}&type=${type}&sort=relevance&limit=25&raw_json=1`,
      {
        headers: {
          "User-Agent": USER_AGENT,
          "Accept": "application/json",
          "Accept-Language": "en-US,en;q=0.9",
        },
      },
    );
    if (!res.ok) {
      const bodySnippet = await res.text().catch(() => "");
      console.log(`\n    [DEBUG] Reddit ${type} search HTTP ${res.status} for "${query}": ${bodySnippet.slice(0, 200)}`);
      return null;
    }
    const json = await res.json();
    const rawCount = json?.data?.children?.length ?? 0;
    console.log(`\n    [DEBUG] Reddit ${type} search for "${query}": HTTP ${res.status}, ${rawCount} raw results`);
    return json;
  } catch (err) {
    console.log(`\n    [DEBUG] Reddit ${type} search threw: ${err.message}`);
    return null;
  }
}

async function fetchRealCandidates(query) {
  const keywords = extractKeywords(query);

  const commentResults = await searchReddit(query, "comment");
  await sleep(DELAY_MS);

  const rawComments = commentResults?.data?.children ?? [];
  const commentCandidates = rawComments
    .map((c) => c.data)
    .filter(
      (d) =>
        d?.author &&
        d.author !== "[deleted]" &&
        isUsable(d.body) &&
        d.permalink &&
        !looksLikeSpam(d.body) &&
        looksEnglish(d.body) &&
        isRelevant(d.body, keywords),
    )
    .map((d) => ({
      handle: `u/${d.author}`,
      text: trimExcess(d.body),
      url: `https://www.reddit.com${d.permalink}`,
      score: d.score ?? 0,
    }));

  console.log(`    [DEBUG] ${rawComments.length} raw comments -> ${commentCandidates.length} passed filters`);

  if (commentCandidates.length > 0) {
    return commentCandidates.sort((a, b) => b.score - a.score);
  }

  // Fall back to post text when comment search comes up empty.
  const postResults = await searchReddit(query, "link");
  await sleep(DELAY_MS);

  const rawPosts = postResults?.data?.children ?? [];
  const postCandidates = rawPosts
    .map((p) => p.data)
    .filter(
      (d) =>
        d?.author &&
        d.author !== "[deleted]" &&
        isUsable(d.selftext) &&
        d.permalink &&
        !looksLikeSpam(d.selftext) &&
        looksEnglish(d.selftext) &&
        isRelevant(d.selftext, keywords),
    )
    .map((d) => ({
      handle: `u/${d.author}`,
      text: trimExcess(d.selftext),
      url: `https://www.reddit.com${d.permalink}`,
      score: d.score ?? 0,
    }));

  console.log(`    [DEBUG] ${rawPosts.length} raw posts -> ${postCandidates.length} passed filters`);

  return postCandidates.sort((a, b) => b.score - a.score);
}

/** Same combined judgment as the deployed YouTube version: genuine
 *  experience/opinion AND relevant to the specific claim, not just the
 *  general ingredient. Only selects from real candidates, never rewrites. */
async function selectGenuineReactions(candidates, topic) {
  if (candidates.length === 0) return candidates;

  const pool = candidates.slice(0, 25);
  const listText = pool.map((c, i) => `[${i}] "${c.text}"`).join("\n\n");

  const prompt = `Here are real Reddit comments/posts found while researching "${topic}". Decide which ones should be KEPT, on two conditions that both must hold:

1. It reads as someone sharing their OWN experience, result, or opinion — not just asking a question, requesting advice, or asking for a comparison without sharing their own result.
2. It's actually relevant to "${topic}" as a SPECIFIC claim, not just mentioning the same general ingredient/practice for a different, unrelated purpose.

${listText}

Return ONLY a JSON array of the indices to keep, e.g. [0,2,4]. No other text.`;

  try {
    const res = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
      },
      body: JSON.stringify({
        model: "claude-haiku-4-5-20251001",
        max_tokens: 200,
        messages: [{ role: "user", content: prompt }],
      }),
    });
    if (!res.ok) return candidates;
    const json = await res.json();
    const text = json.content?.[0]?.text ?? "[]";
    const indices = JSON.parse(text.replace(/```json|```/g, "").trim());
    if (!Array.isArray(indices)) return candidates;
    const kept = indices
      .filter((i) => typeof i === "number" && i >= 0 && i < pool.length)
      .map((i) => pool[i]);
    return kept;
  } catch {
    return candidates;
  }
}

async function inferSentimentFromQuotes(name, summary, quotes) {
  const quotesText = quotes.length
    ? quotes.map((q) => `${q.handle}: "${q.text}"`).join("\n")
    : "(no real quotes found for this trend)";

  const prompt = `A wellness result titled "${name}" has this research summary: "${summary}"

Real community quotes just fetched for it:
${quotesText}

Return a JSON object with two fields:
1. "sentiment": a number 0-100 for how positive community sentiment is, based ONLY on the real quotes above (if any) — not invented. If there are no real quotes, use your general knowledge of typical reception for this kind of product/practice instead.
2. "communityVerdict": ONE sentence (max ~140 chars), plain conversational language, synthesizing the real quotes above. Do not invent a specific claim the real quotes don't support. If there are no real quotes, write a general, honest line like "Limited public discussion found — the research above gives a reasonable starting expectation" rather than fabricating specifics.

Return ONLY this JSON, no other text:
{"sentiment": 70, "communityVerdict": "..."}`;

  try {
    const res = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
      },
      body: JSON.stringify({
        model: "claude-haiku-4-5-20251001",
        max_tokens: 250,
        messages: [{ role: "user", content: prompt }],
      }),
    });
    if (!res.ok) return { communityVerdict: null, sentiment: null };
    const json = await res.json();
    const text = json.content?.[0]?.text ?? "{}";
    const parsed = JSON.parse(text.replace(/```json|```/g, "").trim());
    return {
      communityVerdict: typeof parsed.communityVerdict === "string" ? parsed.communityVerdict.trim() || null : null,
      sentiment: typeof parsed.sentiment === "number" ? parsed.sentiment : null,
    };
  } catch {
    return { communityVerdict: null, sentiment: null };
  }
}

async function main() {
  console.log("Loading trends from Supabase...");
  const { data: rows, error } = await supabase
    .from("generated_trends")
    .select("id, query, name, summary, sentiment_score")
    .neq("verdict", "unmapped")
    .limit(500);

  if (error || !rows) {
    console.error("Couldn't load trends:", error);
    process.exit(1);
  }

  console.log(`Found ${rows.length} trends. Starting...\n`);

  let updated = 0;
  let emptied = 0;

  for (const [i, row] of rows.entries()) {
    console.log(`[${i + 1}/${rows.length}] ${row.name}`);

    const rawCandidates = await fetchRealCandidates(row.query);
    const genuine = await selectGenuineReactions(rawCandidates, row.query);
    console.log(`    [DEBUG] ${rawCandidates.length} candidates -> ${genuine.length} kept after genuine/relevance check`);
    const realQuotes = genuine.map(({ handle, text, url }) => ({ handle, text, url })).slice(0, 3);

    const { communityVerdict, sentiment } = await inferSentimentFromQuotes(
      row.name,
      row.summary,
      realQuotes,
    );

    const { error: updateError } = await supabase
      .from("generated_trends")
      .update({
        opinions: realQuotes,
        community_verdict: communityVerdict ?? "",
        sentiment_score: sentiment ?? row.sentiment_score,
      })
      .eq("id", row.id);

    if (updateError) {
      console.log(`FAILED TO SAVE (${updateError.message})`);
      continue;
    }

    if (realQuotes.length > 0) {
      updated++;
      console.log(`${realQuotes.length} real quote(s) found`);
    } else {
      emptied++;
      console.log("none found — left empty");
    }
  }

  console.log(`\nDone. ${updated} of ${rows.length} trends got real quotes; ${emptied} had none found (left empty, not fabricated).`);
}

main();
