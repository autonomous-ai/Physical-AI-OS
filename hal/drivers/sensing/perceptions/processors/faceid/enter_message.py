"""presence.enter text — the one place its wording is decided and parsed."""

from collections.abc import Iterable
from typing import NamedTuple

from hal.drivers.sensing.perceptions.models import Face, PersonKind


class FrameFacts(NamedTuple):
    """What one frame says about itself, captured on the tick it was seen."""

    labels: list[str]
    present_friends: list[str]


def copresence_ticks(prev: int, faces: list[Face]) -> int:
    """Consecutive sensing ticks in which a friend and a non-friend were boxed together.

    Two friends alone do not count; a recognized friend is a positive match and needs no
    corroboration.
    """
    has_friend = any(f.kind == PersonKind.FRIEND for f in faces)
    has_other = any(f.kind != PersonKind.FRIEND for f in faces)
    return prev + 1 if (has_friend and has_other) else 0


def frame_labels(faces: Iterable[Face]) -> list[str]:
    """One label per box, matching what ``_annotate_frame`` draws on the snapshot."""
    return [f.person_id if f.kind != PersonKind.UNSURE else "unsure" for f in faces]


def build_enter_message(
    new_friends: Iterable[str],
    new_strangers: Iterable[str],
    present_friends: Iterable[str],
    frame_labels: list[str],
) -> str:
    new_parts: list[str] = []
    friends = sorted(new_friends)
    strangers = sorted(new_strangers)
    if friends:
        new_parts.append(f"friend ({', '.join(friends)})")
    if strangers:
        new_parts.append(f"stranger ({', '.join(strangers)})")
    segments = [f"new: {', '.join(new_parts)}"]
    present = sorted(present_friends)
    if present:
        segments.append(
            "already present: " + ", ".join(f"{p} (friend)" for p in present)
        )
    segments.append(
        f"faces in frame: {len(frame_labels)} ({', '.join(frame_labels)})"
    )
    return "Person detected — " + "; ".join(segments)


def has_new_friend(message: str) -> bool:
    """True when the event announces a NEWLY visible friend.

    Only the ``new:`` segment carries the ``friend (<name>)`` label.
    """
    head = message.split(";", 1)[0]
    return "friend (" in head.lower()
