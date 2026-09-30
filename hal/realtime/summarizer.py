"""LLM-based memory summarizer using the Anthropic Messages API."""

import logging
import threading
import time

import hal.config as app_config
from hal.realtime.constants import RESOURCES_DIR

logger = logging.getLogger(__name__)

SUMMARIZE_PROMPT_PATH = RESOURCES_DIR / "summarize_prompt.md"


class RealtimeSummarizer:
    """Summarize text entries using the Anthropic Messages API."""

    MAX_INPUT_CHARS: int = 100_000

    def __init__(
        self,
        api_key: str = app_config.REALTIME_SUMMARIZER_API_KEY,
        base_url: str | None = app_config.REALTIME_SUMMARIZER_BASE_URL or None,
        model: str = app_config.REALTIME_SUMMARIZER_MODEL,
        system_prompt: str | None = None,
        max_tokens: int = 4096,
        disable_thinking: bool = False,
        source: str = "voice_summary",
    ) -> None:
        # anthropic is imported lazily (~1.3s on device) so cold boot doesn't pay for it.
        self._api_key = api_key
        self._base_url = base_url
        # Sent as X-Auto-Source to name where this call came from (logging
        # only); it is not an agent turn and carries no [via:] marker.
        self._source = source
        self._client = None
        self._client_lock = threading.Lock()
        self._model: str = model
        self._retries: int = app_config.REALTIME_SUMMARIZER_RETRIES
        self._retry_backoff_s: float = app_config.REALTIME_SUMMARIZER_RETRY_BACKOFF_S
        self._max_tokens: int = max_tokens
        self._disable_thinking = disable_thinking
        if system_prompt is not None:
            self._system_prompt = system_prompt
            return
        try:
            # The real cap, not a word count: "under 2000 words" (~12k chars)
            # against a 5k cap meant every summary was cut (#449).
            self._system_prompt: str = (
                SUMMARIZE_PROMPT_PATH.read_text(encoding="utf-8").strip()
                .replace("{max_chars}", str(app_config.REALTIME_SUMMARY_MAX_CHARS))
            )
        except FileNotFoundError:
            logger.warning("[realtime] Summarize prompt not found at %s", SUMMARIZE_PROMPT_PATH)
            self._system_prompt = "Summarize the following entries concisely."

    def _get_client(self):
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    import anthropic
                    self._client = anthropic.Anthropic(
                        api_key=self._api_key,
                        base_url=self._base_url,
                        timeout=120.0,
                        default_headers={"X-Auto-Source": self._source},
                    )
        return self._client

    @staticmethod
    def _log_failure_evidence(exc: BaseException) -> None:
        """Log what the server actually sent when a call fails (best-effort; never masks the failure)."""
        try:
            if isinstance(exc, UnicodeDecodeError):
                raw = exc.object or b""
                logger.warning(
                    "[realtime] Summarizer: undecodable response body "
                    "(reason=%s, bad byte at %d of %d) first 64 bytes hex: %s",
                    exc.reason, exc.start, len(raw), raw[:64].hex(" "),
                )
                logger.warning(
                    "[realtime] Summarizer: same bytes, lossy text: %r",
                    raw[:120].decode("utf-8", "replace"),
                )
                return
            response = getattr(exc, "response", None)
            if response is not None:
                headers = getattr(response, "headers", {}) or {}
                body = getattr(response, "content", b"") or b""
                logger.warning(
                    "[realtime] Summarizer: HTTP %s %s | content-type=%r "
                    "content-encoding=%r content-length=%r | body[:200]=%r",
                    getattr(response, "status_code", "?"),
                    getattr(getattr(response, "request", None), "url", "?"),
                    headers.get("content-type"),
                    headers.get("content-encoding"),
                    headers.get("content-length"),
                    body[:200],
                )
        except Exception as diag_error:  # pragma: no cover - never mask the real failure
            logger.debug("[realtime] Summarizer: failure diagnostics unavailable: %s", diag_error)

    def summarize(self, entries: list[str]) -> str:
        """Summarize text entries; returns '' if entries are empty or the call fails."""
        entries = [e.strip() for e in entries if e.strip()]
        if not entries:
            return ""

        user_content: str = "\n\n---\n\n".join(entries)
        if len(user_content) > self.MAX_INPUT_CHARS:
            logger.info("[realtime] Truncating summarizer input: %d → %d chars", len(user_content), self.MAX_INPUT_CHARS)
            user_content = user_content[-self.MAX_INPUT_CHARS :]

        # Streaming on purpose: the gateway's non-streaming /v1/messages returns a binary body
        # labelled JSON. Retried because the gateway fails intermittently.
        attempts = max(self._retries, 0) + 1
        backoff = self._retry_backoff_s
        for attempt in range(1, attempts + 1):
            try:
                chunks: list[str] = []
                # Callers that need text, not reasoning, turn thinking off: the
                # proxy can spend the whole budget thinking and return nothing
                # (speech notifications, and realtime memory since #449).
                options = {"thinking": {"type": "disabled"}} if getattr(self, "_disable_thinking", False) else {}
                with self._get_client().messages.stream(
                    model=self._model,
                    max_tokens=getattr(self, "_max_tokens", 4096),
                    system=self._system_prompt,
                    messages=[
                        {"role": "user", "content": user_content},
                    ],
                    **options,
                ) as stream:
                    for text in stream.text_stream:
                        chunks.append(text)
                summary: str = "".join(chunks).strip()
                if not summary:
                    # HTTP 200 is not proof of a usable summary: the proxy can exhaust tokens before any text.
                    final = stream.get_final_message()
                    logger.warning(
                        "[realtime] Empty summarizer response: stop_reason=%s "
                        "output_tokens=%s max_tokens=%s (model=%s); caller fallback required",
                        final.stop_reason, final.usage.output_tokens,
                        getattr(self, "_max_tokens", 4096), self._model,
                    )
                    return ""
                logger.info(
                    "[realtime] Summarized %d entries (%d chars) → %d chars%s",
                    len(entries), len(user_content), len(summary),
                    f" (attempt {attempt}/{attempts})" if attempt > 1 else "",
                )
                return summary
            except Exception as e:
                logger.warning(
                    "[realtime] Summarization failed (attempt %d/%d): %s: %s "
                    "(model=%s, base_url=%s)",
                    attempt, attempts, type(e).__name__, e,
                    self._model, self._base_url,
                )
                self._log_failure_evidence(e)
                if attempt == attempts:
                    return ""
                time.sleep(backoff)
                backoff *= 2
        return ""
