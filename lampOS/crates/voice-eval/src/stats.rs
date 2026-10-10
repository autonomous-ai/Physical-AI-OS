//! One percentile method for every stratum: nearest rank on the sorted values.
use serde::Serialize;

#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Distribution {
    pub n: usize,
    pub min: f64,
    pub p50: f64,
    pub p95: f64,
    pub p99: f64,
    pub max: f64,
}

/// Nearest-rank percentile: the smallest value with at least `p`% of the
/// sample at or below it. With small n, p95/p99 equal the maximum.
pub fn nearest_rank(sorted: &[f64], percentile: f64) -> f64 {
    let rank = ((percentile / 100.0) * sorted.len() as f64).ceil().max(1.0) as usize;
    sorted[rank.min(sorted.len()) - 1]
}

pub fn distribution(values: &[f64]) -> Option<Distribution> {
    let mut sorted: Vec<f64> = values.iter().copied().filter(|v| v.is_finite()).collect();
    if sorted.is_empty() {
        return None;
    }
    sorted.sort_by(f64::total_cmp);
    Some(Distribution {
        n: sorted.len(),
        min: sorted[0],
        p50: nearest_rank(&sorted, 50.0),
        p95: nearest_rank(&sorted, 95.0),
        p99: nearest_rank(&sorted, 99.0),
        max: sorted[sorted.len() - 1],
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn nearest_rank_matches_the_definition() {
        let values: Vec<f64> = (1..=20).map(f64::from).collect();
        let d = distribution(&values).unwrap();
        assert_eq!((d.p50, d.p95, d.p99, d.max), (10.0, 19.0, 20.0, 20.0));
        assert_eq!(distribution(&[3.0]).unwrap().p95, 3.0);
        assert!(distribution(&[f64::NAN]).is_none());
    }
}
