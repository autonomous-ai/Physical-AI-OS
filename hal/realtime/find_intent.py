"""Deterministic find/search intent check for the realtime `look` tool (#481).

Runs only after the model chose `look`; patterns stay narrow and verb-anchored.
"""
import re

_FIND_RE = re.compile(
    r"""
      \b(?:find|finding|locate|locating|search|searching)\b
    | \blook(?:ing)?\s+(?:for|around)\b
    | \bwhere(?:['’]s|\s+is|\s+are|\s+did|\s+was)\b
    | \b(?:do|can)\s+you\s+see\s+my\b
    | \btìm\b
    | ở\s+đâu
    | đâu\s+rồi
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_find_request(transcript: str) -> bool:
    """True when the utterance asks the device to find/locate something."""
    return bool(transcript) and _FIND_RE.search(transcript) is not None
