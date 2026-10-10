//! Physical privacy opens only after a stable permitted observation; closing is
//! immediate. Old, missing or failed observations can never keep input open.
use lamp_interaction::MonoTime;

pub const PRIVACY_FRESH_US: u64 = 50_000;
pub const REOPEN_STABLE_US: u64 = 60_000;

#[derive(Default, Debug)]
pub struct PrivacyGate {
    last: Option<(u64, MonoTime)>,
    allowed_since: Option<MonoTime>,
}

impl PrivacyGate {
    /// Source IDs are supplied by the supervisor; reset this gate when its
    /// privacy worker restarts. `muted` is already normalized for active-low.
    pub fn observe(
        &mut self,
        sequence: u64,
        acquired: MonoTime,
        now: MonoTime,
        muted: bool,
    ) -> bool {
        let fresh = now
            .as_micros()
            .checked_sub(acquired.as_micros())
            .is_some_and(|age| age < PRIVACY_FRESH_US);
        let ordered = sequence > 0
            && self
                .last
                .is_none_or(|(old_seq, old_time)| sequence > old_seq && acquired >= old_time);
        let continuous = self.last.is_none_or(|(_, old_time)| {
            acquired.as_micros().saturating_sub(old_time.as_micros()) < PRIVACY_FRESH_US
        });
        if !fresh || !ordered {
            self.close();
            return false;
        }
        if !continuous {
            self.allowed_since = None;
        }
        self.last = Some((sequence, acquired));
        if muted {
            self.allowed_since = None;
        } else if self.allowed_since.is_none() {
            self.allowed_since = Some(acquired);
        }
        self.allowed(now)
    }

    pub fn allowed(&self, now: MonoTime) -> bool {
        let fresh = self.last.is_some_and(|(_, at)| {
            now.as_micros()
                .checked_sub(at.as_micros())
                .is_some_and(|age| age < PRIVACY_FRESH_US)
        });
        fresh
            && self.allowed_since.is_some_and(|since| {
                now.as_micros()
                    .checked_sub(since.as_micros())
                    .is_some_and(|age| age >= REOPEN_STABLE_US)
            })
    }

    pub fn close(&mut self) {
        // Keep sequence high-water: a reordered packet must not reopen privacy.
        self.allowed_since = None;
    }
}
