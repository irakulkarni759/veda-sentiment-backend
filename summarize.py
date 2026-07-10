"""
Summarizes a batch of Reddit comments about a wellness claim into the
structured summary Veda shows at the top of a claim page, AND selects which
specific comments are actually relevant/representative enough to quote.

Picking quotes by score alone surfaces off-topic comments when Reddit's own
search returns loosely-related posts for a niche claim (e.g. "vibration
plate for weight loss" pulling in an unrelated meme thread that happened to
rank). Claude picks by INDEX into the numbered comment list, so we never
need to fuzzy-match text back — the index is a real comment or it isn't.

Requires: pip install anthropic
Env var:  ANTHROPIC_API_KEY
"""

import os
import json
from anthropic import Anthropic

client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You analyze Reddit community discussion about wellness claims for Veda, \
a platform that summarizes research and community sentiment on wellness claims.

Given a claim and a NUMBERED list of Reddit comments discussing it, return ONLY a JSON object \
(no markdown, no preamble) with this exact shape:

{
  "summary": "2-3 sentence plain-language summary of what the community says about this claim",
  "sentiment": "positive" | "mixed" | "negative" | "insufficient_data",
  "sentiment_score": <float 0.0-1.0, where 1.0 is overwhelmingly positive>,
  "key_themes": ["theme 1", "theme 2", "theme 3"],
  "caveats": "1 sentence noting any safety concerns, mixed results, or methodology gaps mentioned by commenters, or empty string if none",
  "top_quote_indices": [<int>, <int>, <int>]
}

Base this ONLY on the provided comments. Do not invent studies or claims not present in the text. \
If comments are too sparse or off-topic to judge, use sentiment "insufficient_data".

For "top_quote_indices": pick UP TO 3 comment numbers that are reasonably relevant to the claim. \
Real Reddit discussion exists for almost any wellness topic — if comments were gathered, some are \
very likely usable. Default to INCLUDING a comment unless you have a real, specific reason to \
exclude it; don't reject something just because it's imperfect, brief, or doesn't restate the claim \
word-for-word. A comment only needs to clear these:

1. It's genuinely ABOUT the claim's product/ingredient/practice AND its outcome — not a different \
product entirely, and not a clearly different purpose. If the claim is "Jojoba Oil for Hair Growth", \
a comment about jojoba oil for acne is a real mismatch (different outcome). But don't over-apply \
this: minor product-variant/formula/scent/size differences are NOT a mismatch (a comment about \
"Neutrogena On-The-Spot" is relevant to a claim about "Neutrogena On-The-Spot Acne Treatment \
(Vanishing Formula)" — people rarely repeat a product's full marketing name), and a comment doesn't \
need to use the exact outcome wording either (a comment about "clearing up my skin" is relevant to \
an "acne treatment" claim). When a comment is in the right general territory, include it rather than \
finding a technicality to exclude it.

2. It shares the person's own experience, result, or opinion, not just a bare question with zero \
substance. A comment can mention a question or mixed feelings and still count, as long as there's a \
real opinion or experience in it somewhere.

3. It isn't PRIMARILY a list of other products instead of the queried one. A comment that's mostly \
"try X, Y, Z instead" with only a passing mention of the queried product is off-topic. But a comment \
that's mainly about the queried product and just mentions an alternative in passing is fine — don't \
exclude it over one extra sentence.

Each comment shows the title of the post it came from in [brackets] as context. Use that title to \
resolve vague references (a comment saying "it worked great" under a post titled "Did rosemary oil \
regrow your hairline?" is on-topic; the same comment under an unrelated post is not). But base the \
final judgment on what the COMMENT actually says, not just the post title. Score is Reddit upvotes, \
not relevance, so ignore it for this decision. Prefer a mix of perspectives when the community is \
split. If FEWER than 3 comments clear this bar, return only those that do. If NONE do, return an \
empty array — never force an irrelevant or off-topic pick just to fill the list."""


def summarize_comments(claim: str, comments: list[dict], max_comments_in_prompt: int = 150) -> dict:
    """
    comments: list of dicts with 'body', 'score', 'subreddit', 'author', 'url' keys
              (output of reddit_sentiment.gather_sentiment)
    Returns the structured summary dict, plus "top_quotes": a list of full
    comment dicts (not just indices) for the quotes Claude judged relevant.
    """
    empty_result = {
        "summary": f"Not enough community discussion found for '{claim}' to generate a summary.",
        "sentiment": "insufficient_data",
        "sentiment_score": None,
        "key_themes": [],
        "caveats": "",
        "top_quotes": [],
    }

    if not comments:
        return empty_result

    # Highest-score first just for prompt ordering/truncation — relevance
    # selection below is what actually decides which ones get shown.
    ranked = sorted(comments, key=lambda c: c.get("score", 0), reverse=True)[:max_comments_in_prompt]

    def _fmt(i: int, c: dict) -> str:
        # Include the title of the post each comment came from. Without it,
        # the relevance judge sees a bare comment ("this changed everything
        # for me") with no anchor and can't tell whether it's actually about
        # the claim or about something unrelated in a loosely-matched thread.
        title = (c.get("post_title") or "").strip()
        context = f' [from post: "{title[:120]}"]' if title else ""
        return f"[{i}] (score:{c.get('score', 0)}) r/{c.get('subreddit', '')}{context}: {c['body'][:500]}"

    comment_block = "\n".join(_fmt(i, c) for i, c in enumerate(ranked))

    message = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Claim: {claim}\n\nComments ({len(ranked)} of {len(comments)} total):\n{comment_block}",
        }],
    )

    text_block = next((b for b in message.content if b.type == "text"), None)
    if text_block is None:
        print(f"[debug] stop_reason={message.stop_reason}, block_types={[b.type for b in message.content]}")
        return {**empty_result, "summary": "Summary generation returned no text content; raw comments are still available below."}

    text = text_block.text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[4:] if text.lower().startswith("json") else text

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {**empty_result, "summary": "Summary generation failed to parse; raw comments are still available below."}

    indices = parsed.pop("top_quote_indices", [])
    top_quotes = []
    for i in indices:
        if isinstance(i, int) and 0 <= i < len(ranked):
            top_quotes.append(ranked[i])

    parsed["top_quotes"] = top_quotes
    return parsed
