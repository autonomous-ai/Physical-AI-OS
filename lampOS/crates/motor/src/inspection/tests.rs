use super::*;
use std::{cell::Cell, collections::VecDeque, rc::Rc};

struct TestRuntime {
    time: Rc<Cell<Duration>>,
    cancel_at: Option<Duration>,
}
impl Runtime for TestRuntime {
    fn now(&self) -> Duration {
        self.time.get()
    }
    fn cancelled(&mut self) -> io::Result<bool> {
        Ok(self.cancel_at.is_some_and(|at| self.now() >= at))
    }
}

struct SyntheticPort {
    time: Rc<Cell<Duration>>,
    incoming: VecDeque<u8>,
    accepted: Vec<u8>,
    requests: Vec<Vec<u8>>,
    chunk: usize,
    write_chunk: usize,
    reply_delay: Duration,
    ready_at: Duration,
    no_reply: bool,
    corrupt: bool,
    noise: bool,
    omit_id: Option<u8>,
    bad_model: bool,
    late_read: bool,
    write_zero: bool,
    would_block_writes: bool,
    wait_advances: bool,
    read_calls: usize,
    max_write_seen: usize,
    early_reply: bool,
    always_noise: bool,
    truncate_reply: bool,
    duplicate_reply: bool,
    write_delay: Duration,
}

fn status(id: u8, data: &[u8]) -> Vec<u8> {
    let mut bytes = vec![255, 255, id, (data.len() + 2) as u8, 0];
    bytes.extend(data);
    bytes.push(!bytes[2..].iter().fold(0u8, |a, b| a.wrapping_add(*b)));
    bytes
}

fn fixture() -> (SyntheticPort, TestRuntime) {
    let time = Rc::new(Cell::new(Duration::ZERO));
    (
        SyntheticPort {
            time: time.clone(),
            incoming: VecDeque::new(),
            accepted: Vec::new(),
            requests: Vec::new(),
            chunk: 64,
            write_chunk: 13,
            reply_delay: Duration::ZERO,
            ready_at: Duration::ZERO,
            no_reply: false,
            corrupt: false,
            noise: false,
            omit_id: None,
            bad_model: false,
            late_read: false,
            write_zero: false,
            would_block_writes: false,
            wait_advances: true,
            read_calls: 0,
            max_write_seen: 0,
            early_reply: false,
            always_noise: false,
            truncate_reply: false,
            duplicate_reply: false,
            write_delay: Duration::ZERO,
        },
        TestRuntime {
            time,
            cancel_at: None,
        },
    )
}

impl SyntheticPort {
    fn make_replies(&mut self) {
        let request = self.accepted.clone();
        self.requests.push(request.clone());
        self.accepted.clear();
        if self.no_reply {
            return;
        }
        let ids: Vec<u8> = if request[4] == 1 {
            vec![request[2]]
        } else {
            request[7..request.len() - 1].to_vec()
        };
        if self.noise {
            self.incoming.push_back(0x55);
        }
        for id in ids.into_iter().rev() {
            if self.omit_id == Some(id) {
                continue;
            }
            let data = if request[4] == 1 {
                vec![]
            } else {
                match request[5] {
                    3 => {
                        if self.bad_model {
                            vec![1, 0]
                        } else {
                            vec![9, 3]
                        }
                    }
                    9 => vec![0, 0, 0xff, 0x0f],
                    31 => vec![1, 8, 0],
                    56 => vec![0; 15],
                    _ => panic!("arbitrary register read"),
                }
            };
            let mut response = status(id, &data);
            if self.corrupt {
                *response.last_mut().unwrap() ^= 1;
            }
            if self.duplicate_reply {
                self.incoming.extend(response.clone());
            }
            if self.truncate_reply {
                response.pop();
            }
            self.incoming.extend(response);
        }
        self.ready_at = self.time.get() + self.reply_delay;
    }
}

impl PortIo for SyntheticPort {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        self.read_calls += 1;
        assert!(bytes.len() <= 64);
        if self.always_noise {
            bytes[0] = 0x55;
            return Ok(1);
        }
        if self.incoming.is_empty() || self.time.get() < self.ready_at {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        let count = bytes.len().min(self.chunk).min(self.incoming.len());
        for byte in bytes.iter_mut().take(count) {
            *byte = self.incoming.pop_front().unwrap();
        }
        if self.late_read && !self.requests.is_empty() {
            self.time.set(self.time.get() + TRANSACTION_DEADLINE);
        }
        Ok(count)
    }
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.max_write_seen = self.max_write_seen.max(bytes.len());
        assert!(bytes.len() <= 13);
        if self.write_zero {
            return Ok(0);
        }
        if self.would_block_writes {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        let count = bytes.len().min(self.write_chunk);
        self.accepted.extend(&bytes[..count]);
        if self.early_reply {
            self.incoming.extend(status(1, &[]));
        }
        if self.accepted.len() >= 4 && self.accepted.len() == usize::from(self.accepted[3]) + 4 {
            self.make_replies();
        }
        self.time.set(self.time.get() + self.write_delay);
        Ok(count)
    }
    fn wait(&mut self, interest: Interest, maximum: Duration) -> io::Result<()> {
        assert!(maximum <= POLL_SLICE);
        let ready = match interest {
            Interest::Read => {
                self.always_noise || (!self.incoming.is_empty() && self.time.get() >= self.ready_at)
            }
            Interest::Write => !self.would_block_writes,
        };
        // poll returns immediately when the requested operation is ready.
        if self.wait_advances && !ready {
            self.time.set(self.time.get() + maximum);
        }
        Ok(())
    }
}

#[test]
fn finite_plan_sends_only_nine_read_only_requests_and_decodes_25_replies() {
    let (mut io, mut clock) = fixture();
    let result = run(&mut io, &mut clock);
    assert!(result.completed, "{:?}", result.fault);
    assert_eq!(result.transactions.len(), 9);
    assert_eq!(
        result
            .transactions
            .iter()
            .map(|t| t.replies.len())
            .sum::<usize>(),
        25
    );
    assert_eq!(io.requests.len(), 9);
    assert!(
        io.requests
            .iter()
            .all(|request| [1, 0x82].contains(&request[4]))
    );
    assert_eq!(result.freshness, "not_proven_wire_has_no_transaction_id");
    assert_eq!(result.telemetry_target_met, Some(true));
    assert_eq!(result.quiet.observed_quiet_us, 20_000);
    assert_eq!(result.transaction_deadline_us, 200_000);
    assert_eq!(result.telemetry_target_us, 25_000);
    serde_json::to_string(&result).unwrap();
}

#[test]
fn all_partial_write_and_read_sizes_preserve_exact_requests() {
    for write_chunk in 1..=13 {
        for chunk in 1..=21 {
            let (mut io, mut clock) = fixture();
            io.write_chunk = write_chunk;
            io.chunk = chunk;
            let result = run(&mut io, &mut clock);
            assert!(
                result.completed,
                "write={write_chunk} read={chunk} fault={:?}",
                result.fault
            );
            for (stage, bytes) in Stage::PLAN.into_iter().zip(&io.requests) {
                assert_eq!(stage.request().bytes(), bytes);
            }
        }
    }
}

#[test]
fn initial_stale_bytes_are_counted_not_accepted_as_replies() {
    let (mut io, mut clock) = fixture();
    io.incoming.extend(status(1, &[]));
    let result = run(&mut io, &mut clock);
    assert!(result.completed);
    assert_eq!(result.quiet.discarded_bytes, 6);
    assert_eq!(result.transactions[0].replies.len(), 1);
    assert!(result.transactions[0].started_us >= 20_000);
}

#[test]
fn continuous_initial_traffic_never_reaches_a_request() {
    let (mut io, mut clock) = fixture();
    io.always_noise = true;
    let result = run(&mut io, &mut clock);
    assert_eq!(result.fault, Some(Fault::OperationLimit));
    assert!(result.transactions.is_empty());
    assert!(io.requests.is_empty());
}

#[test]
fn timed_out_request_is_not_retried_and_late_reply_is_not_reused() {
    let (mut io, mut clock) = fixture();
    io.reply_delay = Duration::from_millis(201);
    let result = run(&mut io, &mut clock);
    assert!(
        matches!(result.fault, Some(Fault::Deadline { ref pending_ids, .. }) if pending_ids == &[1])
    );
    assert_eq!(io.requests.len(), 1);
    assert_eq!(result.transactions.len(), 1);
    assert!(!io.incoming.is_empty());
    assert!(result.transactions[0].replies.is_empty());
}

#[test]
fn bytes_returned_after_deadline_are_counted_but_never_decoded() {
    let (mut io, mut clock) = fixture();
    io.late_read = true;
    let result = run(&mut io, &mut clock);
    assert!(matches!(result.fault, Some(Fault::Deadline { .. })));
    assert_eq!(result.transactions[0].bytes_read, 6);
    assert!(result.transactions[0].replies.is_empty());
    assert_eq!(io.requests.len(), 1);
}

#[test]
fn cancellation_before_and_during_transaction_stops_all_future_requests() {
    for at in [0, 10, 21, 50] {
        let (mut io, mut clock) = fixture();
        io.no_reply = true;
        clock.cancel_at = Some(Duration::from_millis(at));
        let result = run(&mut io, &mut clock);
        assert_eq!(result.fault, Some(Fault::Cancelled));
        assert!(io.requests.len() <= 1);
        assert!(clock.now() <= Duration::from_millis(at) + POLL_SLICE);
    }
}

#[test]
fn checksum_noise_and_missing_joint_are_explicit_failures() {
    let (mut io, mut clock) = fixture();
    io.corrupt = true;
    let result = run(&mut io, &mut clock);
    assert!(matches!(result.fault, Some(Fault::Protocol { .. })));
    assert_eq!(io.requests.len(), 1);
    let (mut io, mut clock) = fixture();
    io.noise = true;
    let result = run(&mut io, &mut clock);
    assert!(matches!(result.fault, Some(Fault::Noise { bytes: 1 })));
    let (mut io, mut clock) = fixture();
    io.omit_id = Some(5);
    let result = run(&mut io, &mut clock);
    assert!(
        matches!(result.fault, Some(Fault::Deadline { ref pending_ids, .. }) if pending_ids == &[5])
    );
    assert_eq!(io.requests.len(), 5);
}

#[test]
fn unknown_model_stops_before_using_sts_specific_register_decoders() {
    let (mut io, mut clock) = fixture();
    io.bad_model = true;
    let result = run(&mut io, &mut clock);
    assert!(matches!(
        result.fault,
        Some(Fault::UnexpectedModel { model_raw: 1, .. })
    ));
    assert_eq!(io.requests.len(), 6);
}

#[test]
fn partial_write_cannot_accept_an_early_stale_response() {
    let (mut io, mut clock) = fixture();
    io.write_chunk = 1;
    io.early_reply = true;
    let result = run(&mut io, &mut clock);
    assert_eq!(
        result.fault,
        Some(Fault::UnexpectedPriorTraffic { bytes: 6 })
    );
    assert_eq!(result.transactions[0].request_bytes_accepted, 1);
    assert!(io.requests.is_empty());
}

#[test]
fn write_zero_and_busy_writer_are_bounded_not_retried_as_new_transactions() {
    let (mut io, mut clock) = fixture();
    io.write_zero = true;
    let result = run(&mut io, &mut clock);
    assert_eq!(result.fault, Some(Fault::WriteZero));
    let (mut io, mut clock) = fixture();
    io.would_block_writes = true;
    let result = run(&mut io, &mut clock);
    assert!(matches!(result.fault, Some(Fault::Deadline { .. })));
    assert!(io.requests.is_empty());
    assert_eq!(result.transactions.len(), 1);
}

#[test]
fn operation_budget_terminates_even_if_clock_and_poll_never_advance() {
    let (mut io, mut clock) = fixture();
    io.wait_advances = false;
    let result = run(&mut io, &mut clock);
    assert_eq!(result.fault, Some(Fault::OperationLimit));
    assert_eq!(result.quiet.io_operations, MAX_OPERATIONS);
}

#[test]
fn telemetry_target_reports_measured_delay_separately_from_fault_deadline() {
    let (mut io, mut clock) = fixture();
    io.reply_delay = Duration::from_millis(30);
    let result = run(&mut io, &mut clock);
    assert!(result.completed);
    assert_eq!(result.telemetry_target_met, Some(false));
    let last = result.transactions.last().unwrap();
    assert!(last.elapsed_us >= 30_000 && last.elapsed_us < 200_000);
    assert!(last.request_accepted_us.unwrap() < last.first_read_completed_us.unwrap());
}

#[test]
fn truncated_reply_deadline_keeps_partial_count_and_never_returns_a_snapshot() {
    let (mut io, mut clock) = fixture();
    io.truncate_reply = true;
    let result = run(&mut io, &mut clock);
    assert_eq!(
        result.fault,
        Some(Fault::Deadline {
            pending_ids: vec![1],
            partial_bytes: 5
        })
    );
    assert!(!result.completed);
    assert_eq!(io.requests.len(), 1);
    assert!(result.transactions[0].replies.is_empty());
}

#[test]
fn duplicate_reply_faults_instead_of_starting_another_request() {
    let (mut io, mut clock) = fixture();
    io.duplicate_reply = true;
    let result = run(&mut io, &mut clock);
    assert!(matches!(result.fault, Some(Fault::Protocol { .. })));
    assert_eq!(io.requests.len(), 1);
}

#[test]
fn cancellation_observed_after_write_keeps_accepted_count_but_discards_reply() {
    let (mut io, mut clock) = fixture();
    io.write_delay = POLL_SLICE;
    clock.cancel_at = Some(INITIAL_QUIET + Duration::from_millis(1));
    let result = run(&mut io, &mut clock);
    assert_eq!(result.fault, Some(Fault::Cancelled));
    assert_eq!(io.requests.len(), 1);
    assert_eq!(result.transactions[0].request_bytes_accepted, 6);
    assert!(result.transactions[0].replies.is_empty());
    assert!(!io.incoming.is_empty());
}

#[test]
fn clock_regression_and_failed_cancellation_source_cannot_admit_another_operation() {
    let (mut io, mut runtime) = fixture();
    let mut clock = Clock {
        last: Duration::from_millis(1),
    };
    let mut quiet_report = QuietReport::default();
    assert_eq!(
        quiet(&mut io, &mut runtime, &mut clock, &mut quiet_report),
        Err(Fault::ClockRegression)
    );
    assert_eq!(io.read_calls, 0);
    struct BrokenCancellation;
    impl Runtime for BrokenCancellation {
        fn now(&self) -> Duration {
            Duration::ZERO
        }
        fn cancelled(&mut self) -> io::Result<bool> {
            Err(io::ErrorKind::BrokenPipe.into())
        }
    }
    let result = run(&mut io, &mut BrokenCancellation);
    assert!(matches!(result.fault, Some(Fault::CancellationIo { .. })));
    assert_eq!(io.read_calls, 0);
}
