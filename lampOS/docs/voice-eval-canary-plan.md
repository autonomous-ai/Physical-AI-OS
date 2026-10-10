# Canonical evaluator self-test plan

The evaluator tests itself with the plan and stimulus catalog embedded in its
own binary. It does not borrow profiles, scenarios, deadlines, or stimulus
timing from the run being evaluated. This lets a historical ledger use its
original plan and lets an operator provide a valid subset/custom plan without
also copying every current canary scenario into it.

`canary::run(plan, catalog)` keeps its existing signature for callers. Those
arguments identify the caller's evaluation inputs but do not configure the
self-test. Internally it loads `StimulusCatalog::load()` and
`LoadedPlan::default_plan()` and runs the existing bounded fake-runtime
canaries against those canonical inputs. Normal run-plan validation, the
ledger's plan-hash check, and scoring of the actual attempts are unchanged.

Each `CanaryResult` adds `canonical_plan`:

| Field | Identity |
|---|---|
| `suite_version` | Version of the self-test suite contract, currently 1. |
| `plan_id`, `plan_version` | Identity and schema version of the embedded plan. |
| `plan_sha256` | SHA-256 of the exact embedded plan source bytes. |
| `desk_catalog_sha256` | SHA-256 of the embedded desk catalog source bytes. |
| `extension_catalog_sha256` | SHA-256 of the embedded extension source bytes. |
| `merged_catalog_sha256` | SHA-256 of the validated merged catalog serialization. |

The existing CLI and report serialize these fields with their self-test
results. The attempt report's `plan_id` and `plan_sha256` continue to describe
the run under review, which can differ from `canonical_plan`. Canonical means
the fixtures embedded in that evaluator build; their hashes make later
fixture changes visible. Change `suite_version` when changing the suite's
interpretation or contract. This patch does not change any canary's injected
failure, detection rule, or trust verdict.

`crates/voice-eval/tests/canary_plan.rs` covers valid plans without named
canary scenarios, a reduced caller catalog/custom profiles/deadline, and the
identity attached to every result. Historical compatibility is also checked
by running `lamp-voice-eval self-test --plan HISTORICAL_PLAN.json` with an
unchanged retained plan. These are software self-tests: no speech is generated
or played, no recording is opened, and no physical latency or voice quality
is established.
