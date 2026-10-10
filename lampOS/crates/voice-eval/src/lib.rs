//! Automated voice acceptance runner and evaluator for lampOS.
//!
//! Scenarios are pre-registered, triggered from runtime/playback events with
//! bounded deadlines, and every attempt (including failures, withheld attempts
//! and expected silence) is retained in an append-only ledger.
//!
//! Evidence strata are never pooled. Fake-runtime results describe the turn
//! policy against declared or digital stimuli; they are not acoustic results.
//! Software timestamps stay separate from room-audio measurements, and a
//! missing acoustic annotation is unscored, never a pass. One loudspeaker
//! playing several synthetic voices cannot establish spatial speaker
//! discrimination.
pub mod annotations;
pub mod canary;
pub mod evaluate;
pub mod events;
pub mod fake;
pub mod import;
pub mod ledger;
pub mod physical;
pub mod plan;
mod playback;
pub mod record;
pub mod report;
pub mod runner;
pub mod stats;
pub mod stimulus;

use std::{error::Error, io};

pub type Result<T> = std::result::Result<T, Box<dyn Error + Send + Sync>>;

pub fn invalid(message: &str) -> Box<dyn Error + Send + Sync> {
    io::Error::new(io::ErrorKind::InvalidInput, message.to_owned()).into()
}

/// Deterministic xorshift64* generator for seeded order and fake jitter.
#[derive(Clone, Debug)]
pub struct Rng(u64);
impl Rng {
    pub fn new(seed: u64) -> Self {
        Self(seed.max(1) ^ 0x9e37_79b9_7f4a_7c15)
    }
    pub fn next_u64(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_f491_4f6c_dd1d)
    }
    /// Uniform in `0..=bound` (bound 0 returns 0).
    pub fn below_inclusive(&mut self, bound: u64) -> u64 {
        if bound == 0 {
            0
        } else {
            self.next_u64() % (bound + 1)
        }
    }
    pub fn shuffle<T>(&mut self, items: &mut [T]) {
        for index in (1..items.len()).rev() {
            let other = self.below_inclusive(index as u64) as usize;
            items.swap(index, other);
        }
    }
}

/// Stable 64-bit seed derived from a run seed and an attempt identity.
pub fn derive_seed(seed: u64, label: &str) -> u64 {
    let digest = lamp_acoustic::hash(format!("{seed}:{label}").as_bytes());
    u64::from_str_radix(&digest[..16], 16).unwrap_or(seed)
}
