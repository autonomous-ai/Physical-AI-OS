"""Filter Gemini Live chain-of-thought leaks out of the reply text."""

import logging
import re

logger = logging.getLogger("hal.voice")


_TRIGGER = re.compile(
    r"(?i)(?:\bthe (?:user|speaker)s? (?:is|are|was|were|wants?|wanted|asks?"
    r"|asked|insists?|insisted|seems?|seemed|says?|said|repeats?|repeated"
    r"|claims?|claimed|mentions?|mentioned|requests?|requested|greets?|greeted"
    r"|needs?|needed)\b"
    r"|phrasing draft|delivery guidance|spoken delivery"
    r"|^\s*they named (?:a|the) song\s*:"
    r"|^\s*speaker(?: identity)? is unknown\b)"
)

_SECONDARY = re.compile(
    r"(?i)(?:\bpersonas?\b|\bsystem prompts?\b|language lock|\baudio tags?\b"
    r"|\bemotion tool\b|via tool call"
    r"|\bmy search results?\b|\bsearch results? (?:show|suggest|indicate)\b"
    r"|\bsearch quer(?:y|ies)\b"
    r"|\b\d+ (?:sentences?|words)\b)"
)

# All-ASCII label sentence ending in a colon ("Vietnamese Translation:", "Length
# Check:", "Context:") — planning scaffolding from the leak corpus.
_LABEL = re.compile(r"^[\x20-\x7e]{1,40}:$")

_OPENER = re.compile(
    r"(?i)^\s*(?:users? (?:want|wants|is|are|asked|insists?)\b"
    r"|i (?:need|should|must|will|can(?:not|'t)?) \b"
    r"|therefore,? i\b|looking at the\b|re-examining\b|plan:\s*$)"
)


_SENTENCE_SPLIT = re.compile(
    r"(?<=[.!?。！？：；:])\s+|\n+|(?<=[.!?])(?=[A-Z]|[^\x00-\x7f])"
)

_QUOTES = "\"'“”‘’«»「」『』【】＂＇"

# Quoted spans inside a sentence don't decide its language.
_QUOTED_SPAN = re.compile(
    r"\"[^\"]*\"|“[^”]*”|‘[^’]*’|«[^»]*»|「[^」]*」|『[^』]*』"
    r"|(?<!\w)'[^']*'(?!\w)"
)

_NON_ASCII_LETTER = re.compile(r"[^\x00-\x7f]")
# Leading audio/emotion tags like "[caring] " don't decide the language.
_LEADING_TAGS = re.compile(r"^(?:\s*\[[^\]]{1,30}\])+\s*")

_CJK_RANGE = "぀-ヿ㐀-䶿一-鿿豈-﫿가-힯"
_TOKEN = re.compile(rf"[{_CJK_RANGE}]|[0-9]+|[^\W\d_{_CJK_RANGE}]+")

_NON_ASCII_SCRIPTS = ("vietnamese", "chinese", "japanese", "korean", "thai")

_EN_STOPWORDS = frozenset(
    "the a an is are was were to of and or that this it its i you we they"
    " will would should must need in on at with for be as by from since".split()
)


def _word_set(sentence: str) -> frozenset[str]:
    return frozenset(_TOKEN.findall(sentence.lower()))


class CoTLeakFilter:
    """Per-turn stateful filter. Feed it reply text in arrival order."""

    def __init__(self, reply_language: str) -> None:
        lang = (reply_language or "").strip().lower()
        self._non_english: bool = bool(lang) and not lang.startswith("english")
        self._non_ascii_script: bool = lang.startswith(_NON_ASCII_SCRIPTS)
        self._cot_mode: bool = False
        self._seen: list[frozenset[str]] = []
        self.dropped: int = 0

    def _looks_english(self, sentence: str) -> bool:
        s = _LEADING_TAGS.sub("", sentence).strip()
        # Judge the sentence by its own voice, not by what it quotes (see
        # _QUOTED_SPAN): quoted reply-language text inside an English planning
        # sentence must not rescue it.
        s = _QUOTED_SPAN.sub(" ", s)
        letters = re.findall(r"[^\W\d_]", s)
        if not letters:
            return False
        non_ascii = sum(1 for ch in letters if ord(ch) > 127)
        if non_ascii / len(letters) > 0.05:
            return False
        words = re.findall(r"[A-Za-z]+", s)
        if len(words) < 3:
            return False
        if self._non_ascii_script:
            return True
        return sum(1 for w in words if w.lower() in _EN_STOPWORDS) >= 2

    def _is_leak(self, sentence: str) -> bool:
        s = sentence.strip()
        if not s:
            return False
        if _TRIGGER.search(s):
            self._cot_mode = True
            return True
        if self._non_english and _OPENER.match(s):
            self._cot_mode = True
            return True
        if self._non_english and _LABEL.match(s):
            self._cot_mode = True
            return True
        if not self._cot_mode:
            return False
        if _SECONDARY.search(s):
            return True
        if self._non_english and self._looks_english(s):
            return True
        if s[0] in _QUOTES or s[-1] in _QUOTES:
            return True
        bare = _LEADING_TAGS.sub("", s).strip()
        if bare and not _NON_ASCII_LETTER.search(bare) and len(_word_set(bare)) <= 2:
            return True
        words = _word_set(s)
        if words and any(
            len(words & seen) / len(words | seen) >= 0.7 for seen in self._seen
        ):
            return True
        return False

    def filter_text(self, text: str) -> str:
        """Drop CoT sentences from `text`, keeping the rest in order."""
        if not text:
            return text
        kept: list[str] = []
        for sentence in _SENTENCE_SPLIT.split(text):
            if self._is_leak(sentence):
                self.dropped += 1
                logger.warning(
                    "[realtime] CoT leak dropped (not spoken/forwarded): %r",
                    sentence.strip()[:120],
                )
            elif sentence.strip():
                s = sentence.strip()
                kept.append(s)
                words = _word_set(s)
                if words:
                    self._seen.append(words)
        return " ".join(kept)


def clean_transcript(text: str, reply_language: str) -> str:
    """Filter a full turn transcript with fresh state (for [REPLY]/memory)."""
    return CoTLeakFilter(reply_language).filter_text(text)
