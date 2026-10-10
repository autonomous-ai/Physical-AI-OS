//! JSON-line protocol between `lamp-session` (Lamp) and the runner (Mac).
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const SCHEMA: u32 = 1;
/// Upper bound for one relayed line; the final trace is streamed line by line.
pub const MAX_LINE_BYTES: usize = 64 * 1024;
pub const MAX_TRACE_LINES: usize = 25_000;

/// Lamp -> runner.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum SessionLine {
    SessionStart {
        schema: u32,
        lamp_us: u64,
        mode: String,
        cue_socket: bool,
        runtime_argv: Vec<String>,
    },
    /// A cue datagram, verbatim, with the relay's receipt time.
    Cue {
        lamp_received_us: u64,
        cue: Value,
    },
    CueInvalid {
        lamp_received_us: u64,
        error: String,
    },
    /// One stdout line printed by the runtime (for example listening readiness).
    Stdout {
        lamp_received_us: u64,
        line: String,
    },
    Pong {
        seq: u64,
        lamp_us: u64,
    },
    /// One record of the runtime's final `events.jsonl`.
    Trace {
        record: Value,
    },
    SessionEnd {
        lamp_us: u64,
        exit_code: Option<i32>,
        killed: bool,
        events_sha256: Option<String>,
        events_lines: usize,
        error: Option<String>,
    },
}

/// Runner -> Lamp.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum RunnerLine {
    Ping { seq: u64 },
}
