"""Deterministic find/search intent check for the realtime `look` tool (#481).

The prompt and the `look` tool description already forbid `look` for finding a
thing, but that is advice the model can ignore — "Find my mouse" went through
`look` on 3.8 extended-thinking. A find needs the servo sweep the main agent runs
(`/servo/search`); one frame from wherever the head points cannot find anything.
This check runs only after the model has ALREADY chosen `look`, so a correct
delegation is never affected. A false positive costs a main-agent round trip
(which can still answer a visual question), a false negative costs today's
behaviour — so the patterns stay narrow and verb-anchored.
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
