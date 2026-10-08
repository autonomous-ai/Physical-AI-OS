"""Post-capture speech evidence policy, shared by automatic and manual input."""


def accepts_speech_metrics(metrics, *, min_ratio, min_voiced_ms):
    """Require density and cumulative voiced time without waiting for more audio.

    Silero reports (1, 1, 1, 1, 0) when inference is unavailable or fails.
    Preserve that fail-open contract; it is not a measured zero-duration turn.
    """
    if tuple(metrics) == (1.0, 1.0, 1.0, 1.0, 0.0):
        return True
    _, _, _, span_ratio, span_seconds = metrics
    voiced_ms = span_ratio * span_seconds * 1000
    return (span_ratio >= min_ratio
            and voiced_ms + 1e-6 >= min_voiced_ms)
