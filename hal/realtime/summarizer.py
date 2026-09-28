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
    ) -> None:
        # anthropic imports lazily on first summarize(): the SDK costs ~1.3s of
        # import time on device and every summarize() runs on a background
        # thread, so cold boot shouldn't pay for it.
        self._api_key = api_key
        self._base_url = base_url
        self._client = None
        self._client_lock = threading.Lock()
        self._model: str = model
        self._retries: int = app_config.REALTIME_SUMMARIZER_RETRIES
        self._retry_backoff_s: float = app_config.REALTIME_SUMMARIZER_RETRY_BACKOFF_S
        self._max_tokens: int = max_tokens
        if system_prompt is not None:
            # Another task on the same endpoint (e.g. Harness speech rendering).
            self._system_prompt = system_prompt
            return
        try:
            self._system_prompt: str = SUMMARIZE_PROMPT_PATH.read_text(encoding="utf-8").strip()
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
                    )
        return self._client

    @staticmethod
    def _log_failure_evidence(exc: BaseException) -> None:
        """Say what the SERVER actually sent when a call fails.

        The bare `%s` of an exception is often useless here: a proxy that
        answers with a compressed or binary body surfaces only as
        "'utf-8' codec can't decode byte 0xc4 in position 4" (device-observed
        03/09/2026), which names neither the endpoint's status nor the body it
        choked on. Decoding fails before the SDK can wrap it in an APIError, so
        there is no status code on the exception either — the bytes are the only
        evidence, and without them the next reader cannot tell a gzip/brotli
        mismatch from an HTML error page or a truncated response.

        Best-effort by construction: diagnostics must never replace the original
        failure with one of their own.
        """
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
        """Summarize a list of text entries into a concise summary.

        Returns the summary text, or an empty string if entries are empty
        or the API call fails.
        """
        entries = [e.strip() for e in entries if e.strip()]
        if not entries:
            return ""

        user_content: str = "\n\n---\n\n".join(entries)
        if len(user_content) > self.MAX_INPUT_CHARS:
            logger.info("[realtime] Truncating summarizer input: %d → %d chars", len(user_content), self.MAX_INPUT_CHARS)
            user_content = user_content[-self.MAX_INPUT_CHARS :]

        # Streaming, deliberately — not for latency. The gateway's NON-streaming
        # /v1/messages answers with a binary body while labelling it
        # `application/json; charset=utf-8`, so the SDK dies decoding it before
        # it can even raise an APIError (device-observed 03/09/2026: every
        # summarization failed with "'utf-8' codec can't decode byte 0xc4").
        # Probed on device with Accept-Encoding identity/gzip and with/without
        # `anthropic-version`: the body is binary either way, and it is NOT
        # transport compression. The SSE path on the same endpoint returns clean
        # `event: message_start` text, which is why web chat and the delegate
        # agent never saw this. The gateway is still wrong; this stops the
        # summarizer being the one caller that has to wait for the fix.
        # Retried because the failures are the gateway's, not the input's: the
        # same payload has returned 404 once and then succeeded on the next
        # attempts, while the LARGER payload containing it went through first
        # time (measured on lamp-0c89, 03/09/2026). A dropped call otherwise
        # costs the whole summary until the next session rebuild.
        attempts = max(self._retries, 0) + 1
        backoff = self._retry_backoff_s
        for attempt in range(1, attempts + 1):
            try:
                chunks: list[str] = []
                with self._get_client().messages.stream(
                    model=self._model,
                    max_tokens=getattr(self, "_max_tokens", 4096),
                    system=self._system_prompt,
                    messages=[
                        {"role": "user", "content": user_content},
                    ],
                ) as stream:
                    for text in stream.text_stream:
                        chunks.append(text)
                summary: str = "".join(chunks).strip()
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
