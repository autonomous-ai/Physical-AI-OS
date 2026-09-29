"""Stripping the reply's own tail off the front of a transcript."""

from hal.drivers.voice._internal.session_finalize import strip_echo_prefix


def test_strips_the_word_the_reply_was_cut_on():
    said = (
        "This is a subjective question that depends entirely on the specific "
        "situation, which I don't have."
    )
    heard = "situation. I mean, should the fine be heavy?"
    assert strip_echo_prefix(heard, said) == "I mean, should the fine be heavy?"


def test_strips_a_multi_word_run():
    said = "They vary a lot by location, the type of fish you're going after."
    heard = "the type of fish do you mean salmon?"
    assert strip_echo_prefix(heard, said) == "do you mean salmon?"


def test_keeps_punctuation_and_case_of_what_remains():
    said = "Are you traveling somewhere with a lot of bears?"
    heard = "bears. Can I hunt them?"
    assert strip_echo_prefix(heard, said) == "Can I hunt them?"


def test_leaves_a_pure_echo_whole_for_the_similarity_filter():
    """Not this function's call — dropping the turn is, and it logs why."""
    said = "with your friends. Why do you think they like doing that?"
    heard = "with your friends. Why do you think they like doing"
    assert strip_echo_prefix(heard, said) == heard


def test_does_not_strip_on_a_short_common_word():
    """"the" appears in every reply ever spoken — that is not evidence."""
    said = "There isn't one season for the whole country."
    heard = "the fine should be heavy"
    assert strip_echo_prefix(heard, said) == heard


def test_strips_a_single_long_word():
    said = "Fishing seasons are definitely a thing!"
    heard = "seasons? When do they start?"
    assert strip_echo_prefix(heard, said) == "When do they start?"


def test_untouched_when_the_user_shares_no_opening_with_the_reply():
    said = "It really depends on the state and the specific animal!"
    heard = "Which animals can be hunted?"
    assert strip_echo_prefix(heard, said) == heard


def test_no_spoken_text_is_a_no_op():
    assert strip_echo_prefix("Can I hunt bears?", "") == "Can I hunt bears?"
    assert strip_echo_prefix("", "anything") == ""


def test_never_returns_empty():
    """Stripping everything but punctuation must not manufacture an empty turn."""
    said = "Hello there friend"
    assert strip_echo_prefix("hello there friend .", said) == "hello there friend ."


def test_matches_ignore_case_and_punctuation():
    said = "Is there a particular region you're interested in?"
    heard = "Region... I was thinking Alaska."
    assert strip_echo_prefix(heard, said) == "I was thinking Alaska."
