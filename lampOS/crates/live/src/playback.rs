//! Reply bookkeeping for a continuous physical PCM clock. This module grants
//! no output authority and never fabricates render coverage: callers report only
//! actual accepted writes after final-boundary checks and before a later reset.
use crate::wire::{PlaybackGap, PlaybackGapPhase, WorkerEvent};
use lamp_audio::{
    RENDER_SAMPLES,
    ownership::{PlaybackCursor, QueuedRange},
};
use lamp_interaction::{MonoTime, Snapshot, TurnOwner};
use std::io;

#[derive(Clone, Copy, Debug)]
pub(crate) struct FinalSpeech {
    pub owner: TurnOwner,
    pub sequence: u64,
    pub cursor: PlaybackCursor,
}

#[derive(Default)]
pub(crate) struct SpeechPlayback {
    unfinished: Option<TurnOwner>,
    final_speech: Option<FinalSpeech>,
    gap: Option<PlaybackGap>,
}

impl SpeechPlayback {
    pub fn unfinished_owner(&self) -> Option<TurnOwner> {
        self.unfinished
    }

    /// Includes final PCM that is still awaiting its device-queue receipt.
    pub fn owner(&self) -> Option<TurnOwner> {
        self.unfinished
            .or(self.final_speech.map(|speech| speech.owner))
    }

    /// Logical bookkeeping only. The speaker independently checks every
    /// accepted permit and its fixed tail. Call after successful installation
    /// of this snapshot, never from an unvalidated or stale data packet.
    pub fn can_supersede(
        &self,
        snapshot: Snapshot,
        microphone_generation: Option<u64>,
        now: MonoTime,
    ) -> bool {
        let (Some(old), Some(new)) = (self.owner(), snapshot.owner()) else {
            return false;
        };
        old.boot() == new.boot()
            && new.turn() > old.turn()
            && new.generation() > old.generation()
            && snapshot.input_active()
            && snapshot.listening_ready(now)
            && microphone_generation == Some(snapshot.microphone_generation())
    }

    /// A provider pause does not end its reply. Keep writing real zeros through
    /// the zero-only speaker API, but retain the debt and label the gap. A write
    /// blocked by ALSA (zero accepted frames) must not call this method.
    pub fn zeros_accepted(
        &mut self,
        first: PlaybackCursor,
        end: PlaybackCursor,
        at_us: u64,
    ) -> io::Result<Option<WorkerEvent>> {
        let frames = accepted_frames(first, end)?;
        let Some(owner) = self.unfinished else {
            return Ok(None);
        };
        if let Some(gap) = self.gap.as_mut() {
            if gap.owner != owner || gap.end_sample != first || at_us < gap.last_zero_accepted_at_us
            {
                return Err(io::Error::other("noncontiguous accepted playback gap"));
            }
            gap.end_sample = end;
            gap.last_zero_accepted_at_us = at_us;
            gap.zero_frames = gap
                .zero_frames
                .checked_add(frames)
                .ok_or_else(|| io::Error::other("playback gap counter exhausted"))?;
            Ok(None)
        } else {
            let gap = PlaybackGap {
                owner,
                first_sample: first,
                end_sample: end,
                started_at_us: at_us,
                last_zero_accepted_at_us: at_us,
                zero_frames: frames,
            };
            self.gap = Some(gap);
            Ok(Some(WorkerEvent::PlaybackGap {
                phase: PlaybackGapPhase::Started,
                gap,
                observed_at_us: at_us,
            }))
        }
    }

    /// `final_chunk` is true only after the whole declared final block was
    /// accepted. Silence samples inside real provider PCM remain owned speech.
    pub fn speech_accepted(
        &mut self,
        owner: TurnOwner,
        sequence: u64,
        first: PlaybackCursor,
        end: PlaybackCursor,
        at_us: u64,
        final_chunk: bool,
    ) -> io::Result<Option<WorkerEvent>> {
        accepted_frames(first, end)?;
        if sequence == 0 || self.unfinished.is_some_and(|old| old != owner) {
            return Err(io::Error::other("speech changed owner without reset"));
        }
        if self.gap.is_some_and(|gap| {
            gap.owner != owner || gap.end_sample != first || at_us < gap.last_zero_accepted_at_us
        }) {
            return Err(io::Error::other(
                "speech did not resume the accepted playback gap",
            ));
        }
        let resumed = self.gap.take().map(|gap| WorkerEvent::PlaybackGap {
            phase: PlaybackGapPhase::Resumed,
            gap,
            observed_at_us: at_us,
        });
        self.unfinished = (!final_chunk).then_some(owner);
        if final_chunk {
            self.final_speech = Some(FinalSpeech {
                owner,
                sequence,
                cursor: end,
            });
        }
        Ok(resumed)
    }

    /// Only an explicit final speech cursor retired within the same live epoch
    /// can end playback. Following zeros, idle time and a discarded old cursor
    /// cannot complete speech which never reached the speaker.
    pub fn take_retired(&mut self, range: QueuedRange) -> Option<FinalSpeech> {
        let final_speech = self.final_speech?;
        if final_speech.cursor.epoch == range.epoch
            && final_speech.cursor.frame > 0
            && final_speech.cursor.frame <= range.retired_through
        {
            self.final_speech.take()
        } else {
            None
        }
    }

    /// Cancels reply debt, including a not-yet-retired final cursor. This is
    /// logical only; ordinary supersession must not reset the physical clock.
    pub fn reset(&mut self, at_us: u64) -> Option<WorkerEvent> {
        self.unfinished = None;
        self.final_speech = None;
        self.gap.take().map(|gap| WorkerEvent::PlaybackGap {
            phase: PlaybackGapPhase::Cancelled,
            gap,
            observed_at_us: at_us,
        })
    }
}

fn accepted_frames(first: PlaybackCursor, end: PlaybackCursor) -> io::Result<u64> {
    let frames = end
        .frame
        .checked_sub(first.frame)
        .filter(|frames| *frames > 0 && *frames <= RENDER_SAMPLES as u64);
    match frames {
        Some(frames) if first.epoch != 0 && first.epoch == end.epoch => Ok(frames),
        _ => Err(io::Error::other("invalid accepted playback range")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        reference::ReferenceAssembly,
        wire::{ClockPrimed, RenderPayload, RenderReference},
    };
    use lamp_audio::{EchoProcessor, ownership::PlaybackOwnership};
    use lamp_interaction::{
        AdmissionState, AdmittedInput, BootId, BoundaryGuard, CaptureState, Controller, Error,
        MonoTime, OutputKind, OutputPermit, Permission,
    };

    fn at(us: u64) -> MonoTime {
        MonoTime::from_micros(us)
    }
    fn fixture() -> (Controller, BoundaryGuard, OutputPermit) {
        let boot = BootId::new([17; 16]).unwrap();
        let mut controller = Controller::new(boot, at(0));
        controller
            .set_microphone_permission(at(0), Permission::Allowed)
            .unwrap();
        controller
            .set_capture(at(0), CaptureState::RetainingUntil(at(500_000)))
            .unwrap();
        controller
            .set_admission(at(0), AdmissionState::OpenUntil(at(500_000)))
            .unwrap();
        let owner = controller.admit(at(0), AdmittedInput::NewTurn).unwrap();
        controller.input_ended(at(0), owner).unwrap();
        let plan = controller
            .plan_output(at(0), owner, OutputKind::Speech, None)
            .unwrap();
        let permit = controller.issue_output(at(0), plan, 100_000).unwrap();
        let mut guard = BoundaryGuard::new(boot, at(0));
        guard
            .install(at(0), controller.snapshot(at(0)).unwrap())
            .unwrap();
        (controller, guard, permit)
    }

    #[test]
    fn ordinary_supersession_cancels_final_debt_without_completing_or_resetting_epoch() {
        let (mut controller, mut guard, permit) = fixture();
        let microphone = guard.zero_authority(at(0)).unwrap();
        let mut ledger = PlaybackOwnership::new();
        ledger.push_clock(permit, microphone, 240).unwrap();
        let mut speech = SpeechPlayback::default();
        let first = PlaybackCursor { epoch: 1, frame: 0 };
        let end = ledger.accepted_cursor();
        speech
            .speech_accepted(permit.owner(), 1, first, end, 0, true)
            .unwrap();
        assert_eq!(speech.owner(), Some(permit.owner()));
        let successor = controller
            .admit(at(1), AdmittedInput::Interruption)
            .unwrap();
        let snapshot = controller.snapshot(at(1)).unwrap();
        guard.install(at(1), snapshot).unwrap();
        ledger.check_clock_owned(at(1), &mut guard).unwrap();
        assert!(speech.can_supersede(snapshot, Some(microphone.microphone_generation()), at(1)));
        assert!(!speech.can_supersede(snapshot, Some(999), at(1)));
        assert!(!speech.can_supersede(snapshot, None, at(1)));
        assert!(!speech.can_supersede(
            snapshot,
            Some(microphone.microphone_generation()),
            snapshot.expires_at()
        ));
        speech.reset(1);
        ledger.retire_to(0);
        ledger.check_clock_owned(at(2), &mut guard).unwrap();
        assert_eq!(ledger.take_retired_tail().unwrap().superseded_by, successor);
        assert!(speech.take_retired(ledger.queued_range()).is_none());
        assert_eq!(ledger.queued_range().epoch, 1);
        let next = PlaybackCursor {
            epoch: 1,
            frame: 480,
        };
        speech
            .speech_accepted(successor, 2, end, next, 3, true)
            .unwrap();
        assert_eq!(
            speech
                .take_retired(QueuedRange {
                    epoch: 1,
                    retired_through: 480,
                    accepted_through: 720,
                })
                .unwrap()
                .owner,
            successor
        );
    }

    #[test]
    fn stopped_or_ended_successor_never_requests_logical_clock_preservation() {
        let (mut controller, _, permit) = fixture();
        let mut speech = SpeechPlayback::default();
        speech
            .speech_accepted(
                permit.owner(),
                1,
                PlaybackCursor { epoch: 1, frame: 0 },
                PlaybackCursor {
                    epoch: 1,
                    frame: 240,
                },
                0,
                false,
            )
            .unwrap();
        controller.cancel_turn(at(1), permit.owner()).unwrap();
        assert!(!speech.can_supersede(controller.snapshot(at(1)).unwrap(), Some(2), at(1)));
        let successor = controller
            .admit(at(2), AdmittedInput::Interruption)
            .unwrap();
        controller.input_ended(at(3), successor).unwrap();
        assert!(!speech.can_supersede(controller.snapshot(at(3)).unwrap(), Some(2), at(3)));
        assert_eq!(speech.owner(), Some(permit.owner()));
    }

    #[test]
    fn observed_7v7_nominal_exhaustion_is_avoided_by_actual_zero_writes_not_reset_grace() {
        // Preserve the observed 456-frame queue and 20,490 us queue age at the
        // failed capture boundary. These are software observations, not sound.
        const WRITE: u64 = 183_637_586_619;
        const QUEUE: u64 = 183_637_586_628;
        const ADMIT: u64 = 183_637_590_615;
        const FAULT: u64 = 183_637_607_118;
        let (mut controller, mut guard, permit) = fixture();
        let microphone = guard.zero_authority(at(0)).unwrap();
        let mut ledger = PlaybackOwnership::new();
        let mut reference = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        ledger.push_silence(microphone, 480).unwrap();
        reference
            .prime(
                ClockPrimed {
                    playback_epoch: 1,
                    privacy_generation: 2,
                    accepted_zero_frames: 480,
                    accepted_at_us: WRITE - 1_000,
                    queue_observed_at_us: WRITE - 1_000,
                    queued_frames: 480,
                    discarded: None,
                },
                WRITE - 999,
                2,
                &mut echo,
            )
            .unwrap();
        reference
            .started(1, WRITE - 998, WRITE - 998, WRITE - 997)
            .unwrap();
        ledger.retire_to(216);
        let first = ledger.accepted_cursor();
        ledger.push_clock(permit, microphone, 240).unwrap();
        reference
            .accept(
                RenderReference {
                    playback_epoch: 1,
                    privacy_generation: 2,
                    sequence: 1,
                    accepted_at_us: WRITE,
                    queue_observed_at_us: QUEUE,
                    first_sample: first.frame,
                    queued_frames: 456,
                    payload: RenderPayload::Speech {
                        owner: permit.owner(),
                        samples: vec![100; 240],
                    },
                },
                QUEUE,
                2,
                &mut echo,
            )
            .unwrap();
        reference.before_capture(0, QUEUE + 1).unwrap();
        reference.captured(QUEUE + 1, QUEUE + 1).unwrap();
        assert!(
            reference.before_capture(0, FAULT).is_err(),
            "the old observation really exhausts"
        );
        controller
            .admit(at(ADMIT - WRITE), AdmittedInput::Interruption)
            .unwrap();
        guard
            .install(
                at(ADMIT - WRITE),
                controller.snapshot(at(ADMIT - WRITE)).unwrap(),
            )
            .unwrap();
        ledger
            .check_clock_owned(at(ADMIT - WRITE), &mut guard)
            .unwrap();
        // No prime/reset call occurs after admission. Two actual accepted zero
        // writes supply the same epoch/cursor sequence to the existing AEC.
        for index in 1..=2 {
            let time = WRITE + index * 10_000;
            ledger.retire_to(216);
            ledger
                .check_clock_owned(at(time - WRITE), &mut guard)
                .unwrap();
            let first = ledger.accepted_cursor();
            ledger.push_silence(microphone, 240).unwrap();
            reference
                .accept(
                    RenderReference {
                        playback_epoch: 1,
                        privacy_generation: 2,
                        sequence: index + 1,
                        accepted_at_us: time,
                        queue_observed_at_us: time,
                        first_sample: first.frame,
                        queued_frames: 456,
                        payload: RenderPayload::Silence { frames: 240 },
                    },
                    time,
                    2,
                    &mut echo,
                )
                .unwrap();
            reference.before_capture(0, time + 1).unwrap();
            reference.captured(time + 1, time + 1).unwrap();
        }
        assert!(ledger.take_retired_tail().is_some());
        reference.before_capture(0, FAULT).unwrap();
        assert_eq!(reference.timing().playback_epoch, 1);
        assert_eq!(reference.timing().capture_blocks_processed, 3);
        assert_eq!(reference.timing().clock_started_at_us, Some(WRITE - 998));
    }

    #[test]
    fn nine_speech_blocks_then_provider_gap_keep_actual_reference_and_capture_running() {
        let (_, mut guard, permit) = fixture();
        let zero = guard.zero_authority(at(0)).unwrap();
        let mut ledger = PlaybackOwnership::new();
        ledger.push_silence(zero, 480).unwrap();
        let mut speech = SpeechPlayback::default();
        let mut reference = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        reference
            .prime(
                ClockPrimed {
                    playback_epoch: 1,
                    privacy_generation: 2,
                    accepted_zero_frames: 480,
                    accepted_at_us: 1_000,
                    queue_observed_at_us: 1_000,
                    queued_frames: 480,
                    discarded: None,
                },
                1_001,
                2,
                &mut echo,
            )
            .unwrap();
        reference.started(1, 1_002, 1_002, 1_003).unwrap();
        let mut gap_events = Vec::new();
        // Model the observed nine-block burst, a 50 ms provider pause, then the
        // retained final block. The ledger represents actual accepted frames;
        // no empty transport tick is submitted as an imaginary render block.
        for index in 0..17 {
            let tick_us = 11_002 + index * 10_000;
            ledger.retire_to(240);
            let first = ledger.accepted_cursor();
            let payload = if index < 9 || index == 14 {
                ledger.push(permit, 240).unwrap();
                if let Some(event) = speech
                    .speech_accepted(
                        permit.owner(),
                        index + 1,
                        first,
                        ledger.accepted_cursor(),
                        tick_us,
                        index == 14,
                    )
                    .unwrap()
                {
                    gap_events.push(event);
                }
                RenderPayload::Speech {
                    owner: permit.owner(),
                    samples: vec![123; 240],
                }
            } else {
                ledger.push_silence(zero, 240).unwrap();
                if let Some(event) = speech
                    .zeros_accepted(first, ledger.accepted_cursor(), tick_us)
                    .unwrap()
                {
                    gap_events.push(event);
                }
                RenderPayload::Silence { frames: 240 }
            };
            reference
                .accept(
                    RenderReference {
                        playback_epoch: 1,
                        privacy_generation: 2,
                        sequence: index + 1,
                        accepted_at_us: tick_us,
                        queue_observed_at_us: tick_us,
                        first_sample: first.frame,
                        queued_frames: ledger.pending_frames(),
                        payload,
                    },
                    tick_us + 1,
                    2,
                    &mut echo,
                )
                .unwrap();
            reference.before_capture(0, tick_us + 2).unwrap();
            reference.captured(tick_us + 2, tick_us + 3).unwrap();
            assert_eq!(ledger.pending_frames(), 480);
            if index < 14 {
                assert_eq!(speech.unfinished_owner(), Some(permit.owner()));
                assert!(speech.take_retired(ledger.queued_range()).is_none());
            }
        }
        assert_eq!(reference.timing().capture_blocks_processed, 17);
        assert_eq!(gap_events.len(), 2);
        assert!(matches!(
            gap_events[0],
            WorkerEvent::PlaybackGap {
                phase: PlaybackGapPhase::Started,
                ..
            }
        ));
        let WorkerEvent::PlaybackGap { phase, gap, .. } = gap_events[1] else {
            panic!()
        };
        assert_eq!(phase, PlaybackGapPhase::Resumed);
        assert_eq!(gap.zero_frames, 5 * 240);
        let retired = speech.take_retired(ledger.queued_range()).unwrap();
        assert_eq!(retired.owner, permit.owner());
        assert_eq!(retired.sequence, 15);
        assert_eq!(retired.cursor.frame, 480 + 15 * 240);
        assert!(
            ledger.has_pending(),
            "idle zeros need not drain to retire final speech"
        );
    }

    #[test]
    fn following_zeros_never_complete_unaccepted_or_discarded_final_speech() {
        let (_, mut guard, permit) = fixture();
        let zero = guard.zero_authority(at(0)).unwrap();
        let mut ledger = PlaybackOwnership::new();
        let mut speech = SpeechPlayback::default();
        // Idle zeros with no speech never create a final boundary.
        ledger.push_silence(zero, 240).unwrap();
        speech
            .zeros_accepted(
                PlaybackCursor { epoch: 1, frame: 0 },
                ledger.accepted_cursor(),
                1,
            )
            .unwrap();
        ledger.retire_to(0);
        assert!(speech.take_retired(ledger.queued_range()).is_none());
        let first = ledger.accepted_cursor();
        ledger.push(permit, 240).unwrap();
        speech
            .speech_accepted(permit.owner(), 1, first, ledger.accepted_cursor(), 2, true)
            .unwrap();
        let first = ledger.accepted_cursor();
        ledger.push_silence(zero, 240).unwrap();
        assert!(
            speech
                .zeros_accepted(first, ledger.accepted_cursor(), 3)
                .unwrap()
                .is_none()
        );
        ledger.retire_to(241);
        assert!(
            speech.take_retired(ledger.queued_range()).is_none(),
            "one final speech frame still queued"
        );
        // Reset invalidates the entire old final cursor; new silence cannot
        // retire it even when its numeric sample count becomes larger.
        ledger.clear().unwrap();
        ledger.push_silence(zero, 720).unwrap();
        ledger.retire_to(0);
        assert!(speech.take_retired(ledger.queued_range()).is_none());
        speech.reset(4);
        assert!(speech.take_retired(ledger.queued_range()).is_none());
    }

    #[test]
    fn a_partial_or_empty_final_write_cannot_declare_completion() {
        let (_, _, permit) = fixture();
        let mut speech = SpeechPlayback::default();
        let start = PlaybackCursor { epoch: 1, frame: 0 };
        assert!(
            speech
                .speech_accepted(permit.owner(), 1, start, start, 1, true)
                .is_err()
        );
        assert!(speech.zeros_accepted(start, start, 1).is_err());
        let partial = PlaybackCursor {
            epoch: 1,
            frame: 73,
        };
        speech
            .speech_accepted(permit.owner(), 1, start, partial, 2, false)
            .unwrap();
        assert!(
            speech
                .take_retired(QueuedRange {
                    epoch: 1,
                    retired_through: 73,
                    accepted_through: 73
                })
                .is_none()
        );
        let end = PlaybackCursor {
            epoch: 1,
            frame: 240,
        };
        speech
            .speech_accepted(permit.owner(), 1, partial, end, 3, true)
            .unwrap();
        assert!(
            speech
                .take_retired(QueuedRange {
                    epoch: 1,
                    retired_through: 239,
                    accepted_through: 240
                })
                .is_none()
        );
        assert_eq!(
            speech
                .take_retired(QueuedRange {
                    epoch: 1,
                    retired_through: 240,
                    accepted_through: 240
                })
                .unwrap()
                .cursor,
            end
        );
    }

    #[test]
    fn a_gap_cannot_renew_expired_speech_or_bypass_privacy() {
        let (mut controller, mut guard, permit) = fixture();
        let zero = guard.zero_authority(at(0)).unwrap();
        let mut ledger = PlaybackOwnership::new();
        ledger.push(permit, 240).unwrap();
        ledger.push_silence(zero, 240).unwrap();
        guard
            .install(at(100_000), controller.snapshot(at(100_000)).unwrap())
            .unwrap();
        assert_eq!(
            ledger
                .check_owned(at(100_000), &mut guard)
                .unwrap_err()
                .reason,
            Error::ExpiredPermit
        );
        ledger.check_silence(at(100_000), &mut guard).unwrap();
        controller
            .set_microphone_permission(at(100_001), Permission::Denied)
            .unwrap();
        guard
            .install(at(100_001), controller.snapshot(at(100_001)).unwrap())
            .unwrap();
        assert_eq!(
            ledger.check_silence(at(100_001), &mut guard),
            Err(Error::PrivacyClosed)
        );
    }

    #[test]
    fn cancel_during_gap_preserves_old_identity_and_allows_new_turn_after_reset() {
        let (mut controller, _, old) = fixture();
        let mut speech = SpeechPlayback::default();
        let c = |epoch, frame| PlaybackCursor { epoch, frame };
        speech
            .speech_accepted(old.owner(), 1, c(1, 0), c(1, 240), 1, false)
            .unwrap();
        speech.zeros_accepted(c(1, 240), c(1, 480), 2).unwrap();
        controller.cancel_turn(at(3), old.owner()).unwrap();
        let new = controller
            .admit(at(3), AdmittedInput::Interruption)
            .unwrap();
        assert!(
            speech
                .speech_accepted(new, 2, c(1, 480), c(1, 720), 3, true)
                .is_err()
        );
        let WorkerEvent::PlaybackGap { phase, gap, .. } = speech.reset(3).unwrap() else {
            panic!()
        };
        assert_eq!(phase, PlaybackGapPhase::Cancelled);
        assert_eq!(gap.owner, old.owner());
        assert_eq!(gap.zero_frames, 240);
        speech
            .speech_accepted(new, 2, c(2, 480), c(2, 720), 4, true)
            .unwrap();
        let retired = speech
            .take_retired(QueuedRange {
                epoch: 2,
                retired_through: 720,
                accepted_through: 960,
            })
            .unwrap();
        assert_eq!(retired.owner, new);
    }
}
