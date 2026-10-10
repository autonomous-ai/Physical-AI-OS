# Voice evaluation: prompt and source boundaries

The integration of Agent 2's `3eaa0365a` exposed an evidence problem: the direct
human player correctly displayed a prompt without playing audio, but the scorer
treated that display time plus cached synthetic clip timings and a 2.5-second
allowance as a delivered human utterance. Silence could pass when nobody spoke;
an echo admission in the guessed window could pass as a natural interruption;
a delayed person could be reported as missed input. None follows from a prompt.

Timed attempts whose source is `direct_human` now remain **incomplete/unscored**.
Their step receipts, raw events and recordings remain in the ledger. A prompt's
`valid` field means the prompt was displayed; `speech_delivery_verified: false`
explicitly states its limit. There is no guessed human path allowance. The
evaluator does not derive delivery, overlap, restraint or latency from these
prompt timestamps, even when the resulting runtime trace looks successful.

After reviewing the actual recording, an operator can import the trace with
explicit `--turn STEP=TURN` or `--turn STEP=none` attribution and declare
`--source direct_human`. That attribution remains an operator declaration; it
does not verify the source by itself. Acoustic numbers still require a matching,
usable WAV and reviewed in-recording boundaries. Automatic promotion of prompted
attempts from reviewed speech intervals is not implemented. See
[recording verification](voice-eval-recording-evidence.md).

Reports group by `(stratum, source)`: JSON `strata` is now an array of
`{stratum, source, summary}`, replacing the old object keyed by stratum. Older
attempts and saved scores without `source` deserialize as `unknown`, not human
or loudspeaker. Physical headings name the runtime; source labels separately
identify the stimulus. Known negative content review continues to remove
completion credit within each group while preserving the answer denominator.

An admitted background-scene input with neither an endpoint nor a terminal
event has an unknown captured interval. The evaluator records missing evidence
and does not label it background by inventing an infinite endpoint. Known
interval comparisons use saturating tolerance arithmetic. Runtime completion
checks still report an unterminated turn separately.

`tests/acceptance_boundaries.rs` uses synthetic records to cover no human speech,
a delayed human response, ambiguous echo versus interruption, explicitly
attributed imports, and an unfinished background input. Before the fix, four
regressions failed: two false passes, one false rejection and one integer
overflow panic. The test records and silent WAV fixtures are software evidence,
not real human speech or acoustic qualification.

The corrected integration passed 38 focused tests (5 acceptance boundaries,
14 answer accounting, 19 recording evidence). These counts describe the host
only. Full-workspace qualification is recorded in the integration checkpoint.
