"""OpenAI wrapper for meeting summary and topic section generation."""
from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_MIN_WORDS = 80
_MAX_TRANSCRIPT_CHARS = 16_000
_MAX_RETRIES = 2

_SYSTEM_PROMPT = (
    "You are a senior business analyst reviewing a meeting transcript. "
    "Produce two things:\n\n"
    "1. A structured markdown summary with these H2 sections (omit any with no content): "
    "Key Discussion Points, Decisions Made, Action Items, Next Steps. "
    "Be factual, concise, third-person. No filler phrases.\n\n"
    "2. A list of chronological topic sections the meeting covered.\n\n"
    "Respond ONLY with JSON (no markdown fences):\n"
    "{\"summary\": \"# Meeting Summary\\n\\n## Key Discussion Points\\n...\", "
    "\"topics\": [{\"title\": \"Intro\", \"emoji\": \"👋\", \"summary\": \"...\", "
    "\"start\": \"00:00:00\", \"end\": \"00:03:00\", \"tags\": [\"intro\"]}]}\n\n"
    "Use a relevant emoji for each topic. "
    "Return {\"summary\": \"\", \"topics\": []} if the transcript is too brief."
)


def _strip_vtt(text: str) -> str:
    """Remove WebVTT timestamps and headers, leaving only spoken text."""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("WEBVTT") or re.match(r"^\d+$", line):
            continue
        if re.match(r"^\d{2}:\d{2}:\d{2}", line):
            continue
        lines.append(line)
    return " ".join(lines)


class SummaryAnalyzer:
    """Analyze a meeting transcript and produce a summary + topic list."""

    def __init__(self) -> None:
        from openai import OpenAI
        self._client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    def analyze(self, transcript: str) -> dict:
        """Return dict with 'summary' (markdown str) and 'topics' (list[dict]).

        Each topic dict has: title, emoji, summary, start, end, tags.
        Returns {"summary": "", "topics": []} on failure or too-brief transcript.
        """
        clean = _strip_vtt(transcript)
        if len(clean.split()) < _MIN_WORDS:
            logger.info("summary_analyzer.too_brief words=%d", len(clean.split()))
            return {"summary": "", "topics": []}

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = self._client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": clean[:_MAX_TRANSCRIPT_CHARS]},
                    ],
                    temperature=0.3,
                    max_tokens=3000,
                    response_format={"type": "json_object"},
                )
                raw = response.choices[0].message.content or "{}"
                parsed = json.loads(raw)
                summary = (parsed.get("summary") or "").strip()
                topics = parsed.get("topics") or []
                if summary:
                    return {"summary": summary, "topics": topics}
                logger.warning("summary_analyzer.empty_retry attempt=%d", attempt)
            except Exception as exc:
                logger.error(
                    "summary_analyzer.failed attempt=%d error=%s", attempt, exc, exc_info=True
                )
                if attempt == _MAX_RETRIES:
                    return {"summary": "", "topics": []}

        return {"summary": "", "topics": []}
