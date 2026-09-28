"""presence.enter must say who is NEW and who was ALREADY there (#426).

A stranger walking in while the user sat at the desk used to produce
"1 face(s) visible (stranger (stranger_2))" over a snapshot with two boxes, and
the agent greeted the visitor while the user watched. The count was the number
of new arrivals, not of faces, and nothing in the text said the user was in
frame — `current_user=momo` is presence-window state, identical whether she is
sitting there or left two minutes ago.
"""

from hal.drivers.sensing.perceptions.models import Face, PersonKind
from hal.drivers.sensing.perceptions.processors.faceid.enter_message import (
    build_enter_message,
    frame_labels,
    has_new_friend,
)


def _face(kind: PersonKind, pid: str) -> Face:
    return Face(bbox=[0, 0, 10, 10], kind=kind, person_id=pid, confidence=0.9)


FRIEND = _face(PersonKind.FRIEND, "momo")
STRANGER = _face(PersonKind.STRANGER, "stranger_2")
UNSURE = _face(PersonKind.UNSURE, "?")


# -- wording -------------------------------------------------------------------


def test_stranger_joining_a_present_user_names_both():
    msg = build_enter_message(
        new_friends=set(),
        new_strangers={"stranger_2"},
        present_friends=["momo"],
        frame_labels=["momo", "stranger_2"],
    )
    assert msg == (
        "Person detected — new: stranger (stranger_2); "
        "already present: momo (friend); faces in frame: 2 (momo, stranger_2)"
    )


def test_lone_friend_enter_keeps_the_friend_label():
    msg = build_enter_message({"leo"}, set(), [], ["leo"])
    assert msg == "Person detected — new: friend (leo); faces in frame: 1 (leo)"


def test_friend_and_stranger_new_together_list_friend_first():
    msg = build_enter_message({"leo"}, {"stranger_2"}, [], ["stranger_2", "leo"])
    assert msg == (
        "Person detected — new: friend (leo), stranger (stranger_2); "
        "faces in frame: 2 (stranger_2, leo)"
    )


def test_ids_are_sorted_so_the_text_is_deterministic():
    msg = build_enter_message(set(), {"stranger_9", "stranger_10"}, [], ["stranger_9"])
    assert "new: stranger (stranger_10, stranger_9);" in msg


def test_face_count_is_the_frame_not_the_arrivals():
    """Stranger ids (arrivals) need not match the frame's face count."""
    msg = build_enter_message(set(), {"stranger_2", "stranger_3"}, [], ["momo"])
    assert msg.endswith("faces in frame: 1 (momo)")
    assert "already present" not in msg


def test_unsure_boxes_are_counted_and_labelled_like_the_snapshot():
    assert frame_labels([FRIEND, UNSURE, STRANGER]) == ["momo", "unsure", "stranger_2"]


# -- wake-focus contract -------------------------------------------------------


def test_already_present_friend_does_not_read_as_a_new_friend():
    """`<name> (friend)` must not open the voice gate — only `friend (<name>)` does."""
    msg = build_enter_message(set(), {"stranger_2"}, ["momo"], ["momo", "stranger_2"])
    assert not has_new_friend(msg)


def test_new_friend_reads_as_a_new_friend():
    assert has_new_friend(build_enter_message({"momo"}, set(), [], ["momo"]))
    assert has_new_friend(build_enter_message({"Momo"}, set(), [], ["Momo"]))


def test_familiar_stranger_hint_does_not_read_as_a_new_friend():
    msg = build_enter_message(set(), {"stranger_37"}, [], ["stranger_37"])
    msg += (
        " (familiar stranger stranger_37 — seen 2 times, ask user if they want "
        "to remember this face; image saved at /root/local/strangers/snapshots/x.jpg)"
    )
    assert not has_new_friend(msg)
