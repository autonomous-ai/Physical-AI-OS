//! Evaluator-only history of immutable playback occurrences, not turn authority.
//! A retired occurrence says nothing about a provider's whole-turn completion.
#[derive(Clone, Debug)]
pub(crate) struct Occurrence {
    pub sequence: Option<u64>,
    pub started_us: u64,
    pub retired_us: Option<u64>,
}

#[derive(Clone, Debug, Default)]
pub(crate) struct PlaybackHistory {
    pub occurrences: Vec<Occurrence>,
    active: Option<usize>,
    error: Option<String>,
}
impl PlaybackHistory {
    pub fn error(&self) -> Option<&str> {
        self.error.as_deref()
    }
    pub fn invalidate(&mut self, reason: &str) {
        self.error.get_or_insert_with(|| reason.to_owned());
    }
    pub fn active(&self) -> bool {
        self.active.is_some()
    }
    pub fn started(&self) -> bool {
        !self.occurrences.is_empty()
    }
    pub fn complete(&self) -> bool {
        self.error.is_none() && self.started() && !self.active()
    }
    pub fn start(&mut self, sequence: Option<u64>, at: u64) -> Result<(), &'static str> {
        let previous = self.occurrences.last();
        let problem = if sequence == Some(0) {
            Some("zero playback sequence")
        } else if self.active.is_some() {
            Some("new playback started before the active occurrence retired")
        } else if previous.is_some_and(|p| p.sequence.is_none() || sequence.is_none()) {
            Some("multiple playback occurrences have missing identity")
        } else if previous.is_some_and(|p| p.sequence >= sequence) {
            Some("duplicate or regressing playback sequence")
        } else if previous.and_then(|p| p.retired_us).is_some_and(|r| at < r) {
            Some("playback occurrences overlap or regress in time")
        } else {
            None
        };
        if let Some(reason) = problem {
            self.invalidate(reason);
            return Err(reason);
        }
        self.active = Some(self.occurrences.len());
        self.occurrences.push(Occurrence {
            sequence,
            started_us: at,
            retired_us: None,
        });
        Ok(())
    }
    pub fn retire(&mut self, sequence: Option<u64>, at: u64) -> Result<(), &'static str> {
        let problem = match self.active.map(|index| &self.occurrences[index]) {
            None => Some("retirement has no active playback occurrence"),
            Some(p) if p.sequence != sequence => {
                Some("retirement does not match the active playback sequence")
            }
            Some(p) if at < p.started_us => Some("retirement predates its playback start"),
            _ if sequence == Some(0) => Some("zero playback sequence"),
            _ => None,
        };
        if let Some(reason) = problem {
            self.invalidate(reason);
            return Err(reason);
        }
        let index = self.active.take().expect("active playback checked");
        self.occurrences[index].retired_us = Some(at);
        Ok(())
    }
    /// Known software intervals only. The cutoff is cancellation/completion or
    /// the observed run end, never an invented acoustic end.
    pub fn intervals(&self, cutoff: u64) -> impl Iterator<Item = (u64, u64)> + '_ {
        self.occurrences
            .iter()
            .map(move |p| (p.started_us, p.retired_us.unwrap_or(cutoff).min(cutoff)))
            .filter(|(start, end)| end >= start)
    }
    pub fn playing_at(&self, at: u64, cutoff: u64, tolerance: u64) -> bool {
        self.intervals(cutoff).any(|(start, end)| {
            // Playback has already ended at retirement/cancellation. Ring checks
            // may request an explicit tolerance; an empty interval proves nothing.
            start < end
                && at.saturating_add(tolerance) >= start
                && if tolerance == 0 {
                    at < end
                } else {
                    at <= end.saturating_add(tolerance)
                }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stale_retirement_cannot_close_the_next_occurrence() {
        let mut h = PlaybackHistory::default();
        h.start(Some(1), 10).unwrap();
        h.retire(Some(1), 20).unwrap();
        h.start(Some(2), 30).unwrap();
        assert!(h.retire(Some(1), 35).is_err());
        assert!(h.active());
        h.retire(Some(2), 40).unwrap();
        assert!(
            !h.complete(),
            "bad evidence stays invalid after later valid input"
        );
    }
    #[test]
    fn legacy_single_occurrence_is_readable_but_multiple_missing_ids_are_ambiguous() {
        let mut h = PlaybackHistory::default();
        h.start(None, 10).unwrap();
        h.retire(None, 20).unwrap();
        assert!(h.complete());
        assert!(h.start(None, 30).is_err());
        assert!(!h.complete());
    }
    #[test]
    fn overlap_is_half_open_and_empty_occurrences_prove_no_overlap() {
        let mut h = PlaybackHistory::default();
        h.start(Some(1), 10).unwrap();
        h.retire(Some(1), 20).unwrap();
        h.start(Some(2), 30).unwrap();
        h.retire(Some(2), 40).unwrap();
        for at in [10, 19, 30, 39] {
            assert!(h.playing_at(at, 100, 0), "{at}");
        }
        for at in [9, 20, 29, 40] {
            assert!(!h.playing_at(at, 100, 0), "{at}");
        }
        assert!(
            h.playing_at(20, 100, 1),
            "explicit ring tolerance remains distinct"
        );
        assert!(
            !h.playing_at(15, 15, 0),
            "cancellation boundary is also excluded"
        );
        let mut zero = PlaybackHistory::default();
        zero.start(Some(1), 10).unwrap();
        zero.retire(Some(1), 10).unwrap();
        assert!(!zero.playing_at(10, 100, 0));
        assert!(!zero.playing_at(10, 100, 50));
    }
}
