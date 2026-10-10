# Answer playback and content review

`lamp-voice-eval` retains the software `AnswerOutcome` separately from a
validated content review. A reply can finish playing yet be irrelevant,
incomplete, or one of several replies attributed to a single request. Those
failures must not improve the complete-answer rate.

Each scored step now has optional `answer_review` fields `complete` and
`relevant`, copied only from an annotation accepted by `reviewed_step`.
`null` means that judgment is absent. This does not change the recording
validation rules or turn attribution. Re-evaluate the ledger with its
annotations to populate positive review fields in an old report; existing
negative annotation findings still disqualify completion in old saved scores.

The JSON summary preserves its existing fields with these definitions:

| Field | Meaning |
|---|---|
| `complete_answers` | Software-complete playback with no known answer failure. Explicit negative completeness/relevance review and confirmed duplicate/split findings for that step remove numerator credit. |
| `complete_answers_without_gaps` | The same, additionally requiring gap-free software playback. |
| `complete_playbacks` | Raw software-complete outcome, including gapped playback, without judging content or duplicate/split failures. |
| `complete_playbacks_with_answer_failure` | Complete playbacks removed from the answer numerator by a known answer failure. |
| `complete_answers_reviewed` | Counted complete answers with both completeness and relevance positively reviewed. |
| `complete_answers_unreviewed` | Counted complete answers lacking one or both positive content judgments. Includes partial reviews. |

All three ratios share the existing answer-opportunity denominator. Reviewed
failures, missing/truncated/late answers, and confirmed duplicate/split steps
stay in it. Invalid attempts remain listed outside rates; withheld steps,
planned yields, and unsupported answers retain their existing explicit
exclusions. An unrelated ring failure does not alter answer-completion credit.
A positive review cannot repair missing or truncated transport.

Human-readable reports label the accepted numerator **“Complete playback, no
known answer failure”**, show the review counts, and keep transport outcome
visible on each attempt. Unreviewed playback is not evidence of semantic
success. Even a positive completeness/relevance review does not independently
establish factual correctness, naturalness, or acoustic latency. Multiple
attributed reply turns can establish duplicate/split findings; repetition
within one spoken reply still needs content review and is not automatically
detected by this rule.

Focused regression coverage is in `crates/voice-eval/tests/answer_review.rs`.
It includes irrelevant/incomplete/positive/absent reviews, gaps, preserved
failure denominators, duplicate attributed replies, legacy saved scores, and
an unrelated ring defect. Its trace and silent WAV fixtures exercise evidence
accounting only; they are not physical voice tests.
