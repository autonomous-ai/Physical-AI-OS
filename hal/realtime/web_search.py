"""Grounded web search for the Pipecat provider via the campaign-api Google-Search relay.

`parse_interaction` is pure; `grounded_search` adds the HTTP call and error envelope.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Cut at a sentence boundary after this many chars (voice answers).
MAX_ANSWER_CHARS: int = 1200
MAX_SOURCES: int = 5


class WebSearchError(Exception):
    """The search produced no answer; the message is safe to hand back to the model."""


@dataclass
class SearchResult:
    answer: str
    sources: list[str] = field(default_factory=list)  # distinct citation titles, e.g. "wikipedia.org"
    queries: list[str] = field(default_factory=list)  # what Google actually searched (diagnostic)
    elapsed_s: float = 0.0


_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_EMPHASIS = re.compile(r"(\*\*|__|\*|_|`)")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s*[-*•]\s+", re.MULTILINE)
_WS = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{2,}")


def strip_markdown(text: str) -> str:
    """Flatten Gemini markdown into plain prose so TTS doesn't read the markup."""
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_BULLET.sub("", text)
    text = _MD_EMPHASIS.sub("", text)
    text = _WS.sub(" ", text)
    text = _BLANK_LINES.sub("\n", text)
    return text.strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for mark in (". ", "! ", "? ", ".\n", "!\n", "?\n"):
        idx = cut.rfind(mark)
        if idx > limit // 2:
            return cut[: idx + 1].rstrip()
    return cut.rstrip() + "…"


def parse_interaction(payload: dict[str, Any]) -> SearchResult:
    """Extract the grounded answer from an Interaction; raises WebSearchError if none."""
    status = str(payload.get("status") or "").lower()
    if status and status != "completed":
        raise WebSearchError(f"search interaction ended with status {status!r}")

    # The relay answers with `steps`; the canonical Interactions API uses `outputs`.
    steps: list[Any] = payload.get("steps") or payload.get("outputs") or []
    if not isinstance(steps, list):
        raise WebSearchError("search interaction has no steps")

    texts: list[str] = []
    sources: list[str] = []
    queries: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        kind = step.get("type")
        if kind == "google_search_call":
            args = step.get("arguments") or {}
            for q in args.get("queries") or []:
                if isinstance(q, str) and q:
                    queries.append(q)
        elif kind == "model_output":
            for part in step.get("content") or []:
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
                for ann in part.get("annotations") or []:
                    title = ann.get("title") if isinstance(ann, dict) else None
                    if isinstance(title, str) and title and title not in sources:
                        sources.append(title)

    answer = strip_markdown("\n".join(texts))
    if not answer:
        raise WebSearchError("search returned no answer text")
    return SearchResult(
        answer=_truncate(answer, MAX_ANSWER_CHARS),
        sources=sources[:MAX_SOURCES],
        queries=queries,
    )


def grounded_search(
    query: str,
    *,
    url: str,
    api_key: str,
    model: str,
    timeout_s: float,
) -> SearchResult:
    """POST the question to the Google-Search relay and return its grounded answer (blocking).

    `timeout_s` must stay below HAL_PIPECAT_TOOL_RESULT_TIMEOUT_S.
    """
    if not url:
        raise WebSearchError("search endpoint not configured")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {"model": model, "input": query, "tools": [{"type": "google_search"}]}
    t0 = time.monotonic()
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=timeout_s)
    except requests.Timeout as e:
        raise WebSearchError(f"search timed out after {timeout_s:.0f}s") from e
    except requests.RequestException as e:
        raise WebSearchError(f"search request failed: {e.__class__.__name__}") from e
    if resp.status_code != 200:
        raise WebSearchError(f"search relay returned HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise WebSearchError("search relay returned non-JSON") from e
    if not isinstance(payload, dict):
        raise WebSearchError("search relay returned an unexpected body")
    result = parse_interaction(payload)
    result.elapsed_s = time.monotonic() - t0
    return result


__all__ = [
    "MAX_ANSWER_CHARS",
    "SearchResult",
    "WebSearchError",
    "grounded_search",
    "parse_interaction",
    "strip_markdown",
]
