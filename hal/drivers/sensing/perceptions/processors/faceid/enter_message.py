"""presence.enter text — the one place its wording is decided and parsed.

Three consumers read this string and must agree on its shape: the agent
(skills/sensing/SKILL.md greets from it), the wake-focus gate in
sensing_service (``has_new_friend``) and people grepping the flow log
(skills/sensing-track). The ``friend (<name>)`` / ``stranger (<id>)`` labels
for NEW arrivals are that contract and stay byte-identical.

Shape (#426):

    Person detected — new: stranger (stranger_2); already present: momo (friend); faces in frame: 2 (momo, stranger_2)

- ``new:`` — who just became visible, friend part first. This is what
  presence.enter has always meant: NEWLY visible, not visible.
- ``already present:`` — friends boxed in the SAME frame whose last_seen was
  still inside the forget window. It is the co-presence signal the sensing
  skill keys its "looks like you've got company" aside on. It lists every
  friend face-ID matched in that frame; the stranger it sits next to is
  already corroborated (FACE_STRANGER_MIN_TICKS) and the friend is a
  positive match, so no extra tick guard is needed (#531). Written
  ``<name> (friend)`` on purpose: ``friend (<name>)`` means "a friend just
  arrived" to ``has_new_friend`` and would open the voice gate for a
  stranger's arrival.
- ``faces in frame:`` — the number of boxes in the frame the snapshot
  shows and their labels in detection order, ``unsure`` for a box without
  an identity yet. Same labels ``_annotate_frame`` draws. It is not the
  number of arrivals: flushed stranger ids may come from several buffered
  frames; the count and ``already present:`` describe the newest one, the
  frame the agent is looking at (``FrameFacts``).
"""

from collections.abc import Iterable
from typing import NamedTuple

from hal.drivers.sensing.perceptions.models import Face, PersonKind


class FrameFacts(NamedTuple):
    """What one frame says about itself, captured on the tick it was seen.

    A stranger snapshot is buffered on its mint tick and flushed up to
    FACE_STRANGER_FLUSH_S later; the frame at flush time can look nothing
    like the one attached (device-observed: friend blurred out for two ticks
    and the text said ``1 (unsure)`` over a two-box picture). So the facts
    travel with the snapshot and the message is built from the frame the
    agent actually sees.
    """

    labels: list[str]
    present_friends: list[str]


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

    Only the ``new:`` segment carries the ``friend (<name>)`` label; an
    already-present friend is written ``<name> (friend)`` and the trailing
    familiar-stranger hint never contains it, but the check is limited to the
    first segment anyway so a future segment cannot open the voice gate by
    accident.
    """
    head = message.split(";", 1)[0]
    return "friend (" in head.lower()
