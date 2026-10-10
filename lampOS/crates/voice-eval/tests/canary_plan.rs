//! Self-tests use the evaluator's embedded fixture, not the plan under review.
//! These are bounded fake-runtime checks; no audio, devices, or cloud calls.
use lamp_voice_eval::{
    canary,
    plan::{DEFAULT_PLAN, LoadedPlan},
    stimulus::{DESK_SOURCE, StimulusCatalog},
};
use serde_json::{Value, json};

fn subset_plan(catalog: &StimulusCatalog, custom_profile: bool) -> LoadedPlan {
    let mut source: Value = serde_json::from_str(DEFAULT_PLAN).unwrap();
    source["id"] = json!("caller-single-scenario");
    source["scenarios"]
        .as_array_mut()
        .unwrap()
        .retain(|scenario| scenario["id"] == "quick-chat");
    if custom_profile {
        let profile = source["profiles"]["v2-directed-current"].clone();
        source["profiles"] = json!({"caller-only":profile});
        // A valid run-specific deadline must not change canary expectations.
        source["answer_deadline_ms"] = json!(1000);
    }
    LoadedPlan::parse(&source.to_string(), catalog).unwrap()
}

#[test]
fn subset_plan_does_not_need_the_named_canary_scenarios() {
    let catalog = StimulusCatalog::load().unwrap();
    let caller = subset_plan(&catalog, false);
    assert!(caller.scenario("noise-only").is_err());
    assert!(caller.scenario("disconnect-between-turns").is_err());
    let result = canary::run(&caller, &catalog);
    assert!(
        result.is_ok(),
        "valid subset plan must be accepted: {result:?}"
    );
    let results = result.unwrap();
    assert!(!results.is_empty());
    assert!(canary::all_detected(&results), "{results:#?}");
}

#[test]
fn custom_catalog_profiles_and_deadline_do_not_change_canary_inputs() {
    let mut desk: Value = serde_json::from_str(DESK_SOURCE).unwrap();
    desk["scenes"]
        .as_array_mut()
        .unwrap()
        .retain(|scene| scene["id"] == "quick-chat");
    let catalog = StimulusCatalog::merge(
        &desk.to_string(),
        r#"{"version":1,"utterances":{},"scenes":[]}"#,
    )
    .unwrap();
    assert!(catalog.scene("noise-only").is_err());
    let caller = subset_plan(&catalog, true);
    assert!(caller.profile("v2-directed-current").is_err());
    let result = canary::run(&caller, &catalog);
    assert!(
        result.is_ok(),
        "valid custom input must not supply canary fixtures: {result:?}"
    );
    let results = result.unwrap();

    let canonical_catalog = StimulusCatalog::load().unwrap();
    let canonical = LoadedPlan::default_plan(&canonical_catalog).unwrap();
    let reference = canary::run(&canonical, &canonical_catalog).unwrap();
    assert!(canary::all_detected(&results), "{results:#?}");
    assert_eq!(
        serde_json::to_value(&results).unwrap(),
        serde_json::to_value(&reference).unwrap()
    );
    assert_ne!(catalog.merged_sha256, canonical_catalog.merged_sha256);
    assert_ne!(caller.sha256, canonical.sha256);
}

#[test]
fn every_result_identifies_the_embedded_canonical_plan_and_catalog() {
    let catalog = StimulusCatalog::load().unwrap();
    let canonical = LoadedPlan::default_plan(&catalog).unwrap();
    let results = canary::run(&canonical, &catalog).unwrap();
    assert!(!results.is_empty());
    assert!(canary::all_detected(&results), "{results:#?}");
    let expected = json!({
        "suite_version":2,
        "plan_id":canonical.plan.id,
        "plan_version":canonical.plan.version,
        "plan_sha256":canonical.sha256,
        "desk_catalog_sha256":catalog.desk_file_sha256,
        "extension_catalog_sha256":catalog.extension_file_sha256,
        "merged_catalog_sha256":catalog.merged_sha256,
    });
    for result in results {
        let actual = serde_json::to_value(result).unwrap();
        assert_eq!(actual["canonical_plan"], expected);
    }
}
