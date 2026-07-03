"""
Summarizes a batch of Reddit comments about a wellness claim into the
structured summary Veda shows at the top of a claim page.

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

Given a claim and a set of Reddit comments discussing it, return ONLY a JSON object \
(no markdown, no preamble) with this exact shape:

{
  "summary": "2-3 sentence plain-language summary of what the community says about this claim",
  "sentiment": "positive" | "mixed" | "negative" | "insufficient_data",
  "sentiment_score": <float 0.0-1.0, where 1.0 is overwhelmingly positive>,
  "key_themes": ["theme 1", "theme 2", "theme 3"],
  "caveats": "1 sentence noting any safety concerns, mixed results, or methodology gaps mentioned by commenters, or empty string if none"
}

Base this ONLY on the provided comments. Do not invent studies or claims not present in the text. \
If comments are too sparse or off-topic to judge, use sentiment "insufficient_data"."""


def summarize_comments(claim: str, comments: list[dict], max_comments_in_prompt: int = 150) -> dict:
    """
    comments: list of dicts with 'body', 'score', 'subreddit' keys
              (output of reddit_sentiment.gather_sentiment)
    Returns the structured summary dict described in SYSTEM_PROMPT.
    """
    if not comments:
        return {
            "summary": f"Not enough community discussion found for '{claim}' to generate a summary.",
            "sentiment": "insufficient_data",
            "sentiment_score": None,
            "key_themes": [],
            "caveats": "",
        }

    # Highest-score comments first, they're the ones actually surfaced/upvoted by the community
    ranked = sorted(comments, key=lambda c: c.get("score", 0), reverse=True)[:max_comments_in_prompt]

    comment_block = "\n".join(
        f"[score:{c.get('score', 0)}] r/{c.get('subreddit', '')}: {c['body'][:500]}"
        for c in ranked
    )

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
        return {
            "summary": "Summary generation returned no text content; raw comments are still available below.",
            "sentiment": "insufficient_data",
            "sentiment_score": None,
            "key_themes": [],
            "caveats": "",
        }
    text = text_block.text.strip()
    # strip accidental markdown fences
    if text.startswith("```"):
        text = text.strip("`")
        text = text[4:] if text.lower().startswith("json") else text

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "summary": "Summary generation failed to parse; raw comments are still available below.",
            "sentiment": "insufficient_data",
            "sentiment_score": None,
            "key_themes": [],
            "caveats": "",
        }
