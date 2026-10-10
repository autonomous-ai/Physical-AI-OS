use lamp_voice_eval::{
    Result,
    annotations::{self, Annotations},
    canary::{self, CanaryResult},
    evaluate::{self, AttemptScore},
    fake::FakeBackend,
    import::{ImportOptions, import_trace},
    invalid,
    ledger::{create_run_directory, read_rows, unix_ms, write_new_json},
    physical::{
        fake_lamp,
        mac::{HumanPrompter, LampMode, ObserverPlayer, PhysicalBackend, PhysicalConfig, Player},
        session::{self, SessionOptions},
    },
    plan::LoadedPlan,
    record::{AttemptRecord, StimulusSource},
    report,
    runner::{SuiteOptions, attempt_order, run_suite},
    stimulus::{AssetIndex, StimulusCatalog},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
};

const USAGE: &str = "usage:
  lamp-voice-eval list | validate | self-test [--plan FILE]
  lamp-voice-eval fake-run --out NEW_DIR [--profile ID] [--scenario ID]... [--repetitions N]
      [--seed N] [--no-shuffle] [--attempt-seed N] [--cache DIR --manifest FILE...] [--plan FILE]
  lamp-voice-eval evaluate RUN_DIR [--annotations FILE] [--plan FILE]
  lamp-voice-eval import-trace --events FILE --scenario ID --out NEW_DIR [--turn STEP=TURN|none]...
      [--room-metadata FILE] [--room-independent] [--source loudspeaker_synthetic|direct_human] [--plan FILE]
  lamp-voice-eval annotation-template RUN_DIR NEW_FILE
  lamp-voice-eval assets --cache DIR --manifest FILE...
  lamp-voice-eval render-stimuli CACHE NEW_REPORT.json
  lamp-voice-eval physical-run --config FILE --cache DIR --manifest FILE... --out NEW_DIR
      [--scenario ID]... [--repetitions N] [--seed N] [--attempt-seed N] [--execute]
  lamp-voice-eval lamp-session --runtime LAMP_LIVE --work NEW_ABS_DIR --seconds N
      (--fixture-reply WAV --fixture-sha256 SHA | --provider-config FILE) [--noise-suppression on|off]
      [--diagnostics] [--allowance-seconds N]";

fn main() {
    match run() {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("lamp-voice-eval: {error}");
            std::process::exit(1);
        }
    }
}

struct Args {
    values: Vec<String>,
}
impl Args {
    fn take_flag(&mut self, flag: &str) -> bool {
        match self.values.iter().position(|v| v == flag) {
            Some(index) => {
                self.values.remove(index);
                true
            }
            None => false,
        }
    }
    fn take_all(&mut self, flag: &str) -> Result<Vec<String>> {
        let mut found = Vec::new();
        while let Some(index) = self.values.iter().position(|v| v == flag) {
            if index + 1 >= self.values.len() {
                return Err(invalid(&format!("{flag} needs a value")));
            }
            found.push(self.values.remove(index + 1));
            self.values.remove(index);
        }
        Ok(found)
    }
    fn take(&mut self, flag: &str) -> Result<Option<String>> {
        let mut all = self.take_all(flag)?;
        if all.len() > 1 {
            return Err(invalid(&format!("{flag} given more than once")));
        }
        Ok(all.pop())
    }
    fn positional(&mut self) -> Result<String> {
        if self.values.is_empty() || self.values[0].starts_with("--") {
            return Err(invalid(USAGE));
        }
        Ok(self.values.remove(0))
    }
    fn done(&self) -> Result<()> {
        if self.values.is_empty() {
            Ok(())
        } else {
            Err(invalid(&format!(
                "unexpected arguments {:?}\n{USAGE}",
                self.values
            )))
        }
    }
}

fn load(args: &mut Args) -> Result<(StimulusCatalog, LoadedPlan)> {
    let catalog = StimulusCatalog::load()?;
    let plan = match args.take("--plan")? {
        Some(path) => LoadedPlan::parse(&fs::read_to_string(path)?, &catalog)?,
        None => LoadedPlan::default_plan(&catalog)?,
    };
    Ok((catalog, plan))
}

fn suite_options(args: &mut Args, run_id: String) -> Result<SuiteOptions> {
    Ok(SuiteOptions {
        run_id,
        scenarios: args.take_all("--scenario")?,
        repetitions: args.take("--repetitions")?.map_or(Ok(1), |v| v.parse())?,
        seed: args.take("--seed")?.map_or(Ok(1), |v| v.parse())?,
        shuffle: !args.take_flag("--no-shuffle"),
        self_test: Value::Null,
        attempt_seed: args
            .take("--attempt-seed")?
            .map(|v| v.parse())
            .transpose()?,
    })
}

fn run_id(out: &Path) -> String {
    let name = out
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let clean: String = name
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' {
                c.to_ascii_lowercase()
            } else {
                '-'
            }
        })
        .take(40)
        .collect();
    if clean.is_empty() {
        format!("run-{}", unix_ms())
    } else {
        clean
    }
}

fn assets(args: &mut Args, catalog: &StimulusCatalog) -> Result<Option<AssetIndex>> {
    let cache = args.take("--cache")?;
    let manifests: Vec<PathBuf> = args
        .take_all("--manifest")?
        .into_iter()
        .map(PathBuf::from)
        .collect();
    match (cache, manifests.is_empty()) {
        (None, true) => Ok(None),
        (Some(cache), false) => Ok(Some(AssetIndex::load(
            Path::new(&cache),
            &manifests,
            catalog,
        )?)),
        _ => Err(invalid("--cache and at least one --manifest go together")),
    }
}

fn run() -> Result<i32> {
    let mut all: Vec<String> = std::env::args().skip(1).collect();
    if all.is_empty() {
        return Err(invalid(USAGE));
    }
    let command = all.remove(0);
    match command.as_str() {
        "lamp-session" => {
            let options = SessionOptions::parse(&all)?;
            let stdout = std::io::stdout();
            return session::run(&options, std::io::stdin(), &mut stdout.lock());
        }
        "fake-lamp-live" => return fake_lamp::run(&all),
        _ => {}
    }
    let mut args = Args { values: all };
    match command.as_str() {
        "list" | "validate" => {
            let (catalog, plan) = load(&mut args)?;
            args.done()?;
            if command == "validate" {
                println!(
                    "{}",
                    json!({"valid": true, "plan": plan.plan.id, "plan_sha256": plan.sha256,
                    "scenarios": plan.plan.scenarios.len(), "merged_catalog_sha256": catalog.merged_sha256,
                    "desk_catalog_file_sha256": catalog.desk_file_sha256,
                    "new_utterances_needing_render": catalog.extension_utterances})
                );
            } else {
                for scenario in &plan.plan.scenarios {
                    let steps: Vec<String> = scenario
                        .steps
                        .iter()
                        .map(|s| format!("{}:{:?}", s.id, s.expect))
                        .collect();
                    println!(
                        "{:<30} {:<20} {:<12} physical={:<5} {}",
                        scenario.id,
                        format!("{:?}", scenario.cohort),
                        format!("{:?}", scenario.provider),
                        scenario.physical,
                        steps.join(" -> ")
                    );
                }
            }
        }
        "fake-run" => {
            let (catalog, plan) = load(&mut args)?;
            let out = PathBuf::from(
                args.take("--out")?
                    .ok_or_else(|| invalid("--out NEW_DIR is required"))?,
            );
            let profile_id = args
                .take("--profile")?
                .unwrap_or_else(|| "v2-directed-current".into());
            let options = suite_options(&mut args, run_id(&out))?;
            let assets = assets(&mut args, &catalog)?;
            args.done()?;
            let profile = plan.profile(&profile_id)?.clone();
            let self_test = canary::run(&plan, &catalog)?;
            let mut options = options;
            options.self_test = serde_json::to_value(&self_test)?;
            create_run_directory(&out)?;
            let mut backend = FakeBackend::new(
                &profile_id,
                profile,
                plan.plan.fixed_reply.clone(),
                assets.as_ref(),
            );
            let records = run_suite(&plan, &catalog, &mut backend, &options, &out)?;
            let scores: Vec<AttemptScore> = records
                .iter()
                .map(|r| evaluate::score(r, plan.plan.answer_deadline_ms, None))
                .collect();
            let directory = write_evaluation(
                &out,
                &plan,
                &options.run_id,
                &records,
                &scores,
                Vec::new(),
                self_test.clone(),
            )?;
            print_summary(&scores, &directory);
            return Ok(trust_exit(&self_test));
        }
        "self-test" => {
            let (catalog, plan) = load(&mut args)?;
            args.done()?;
            let results = canary::run(&plan, &catalog)?;
            println!("{}", serde_json::to_string_pretty(&results)?);
            return Ok(trust_exit(&results));
        }
        "evaluate" => {
            let run = PathBuf::from(args.positional()?);
            let annotations = args
                .take("--annotations")?
                .map(|path| {
                    fs::read(path)
                        .map_err(Into::into)
                        .and_then(|b| Annotations::parse(&b))
                })
                .transpose()?;
            let (_, plan) = load(&mut args)?;
            args.done()?;
            let (records, problems, run_id) = read_records(&run, &plan)?;
            let scores: Vec<AttemptScore> = records
                .iter()
                .map(|r| evaluate::score(r, plan.plan.answer_deadline_ms, annotations.as_ref()))
                .collect();
            let self_test = canary::run(&plan, &StimulusCatalog::load()?)?;
            let directory = write_evaluation(
                &run,
                &plan,
                &run_id,
                &records,
                &scores,
                problems,
                self_test.clone(),
            )?;
            print_summary(&scores, &directory);
            return Ok(trust_exit(&self_test));
        }
        "import-trace" => {
            let (catalog, plan) = load(&mut args)?;
            let events = PathBuf::from(
                args.take("--events")?
                    .ok_or_else(|| invalid("--events is required"))?,
            );
            let scenario = args
                .take("--scenario")?
                .ok_or_else(|| invalid("--scenario is required"))?;
            let out = PathBuf::from(
                args.take("--out")?
                    .ok_or_else(|| invalid("--out NEW_DIR is required"))?,
            );
            let turn_map = args
                .take_all("--turn")?
                .iter()
                .map(|pair| {
                    let (step, turn) = pair
                        .split_once('=')
                        .ok_or_else(|| invalid("--turn takes STEP=TURN or STEP=none"))?;
                    let turn = match turn {
                        "none" => None,
                        turn => Some(turn.parse::<u64>()?),
                    };
                    Ok((step.to_owned(), turn))
                })
                .collect::<Result<Vec<_>>>()?;
            let room = args.take("--room-metadata")?.map(PathBuf::from);
            let room_independent = args.take_flag("--room-independent");
            let source = match args.take("--source")?.as_deref() {
                None => StimulusSource::Unknown,
                Some("loudspeaker_synthetic") => StimulusSource::LoudspeakerSynthetic,
                Some("direct_human") => StimulusSource::DirectHuman,
                Some(_) => {
                    return Err(invalid("--source is loudspeaker_synthetic or direct_human"));
                }
            };
            args.done()?;
            create_run_directory(&out)?;
            let run_id = run_id(&out);
            let record = import_trace(
                &plan,
                &catalog,
                &ImportOptions {
                    run_id: run_id.clone(),
                    events_path: &events,
                    scenario,
                    turn_map,
                    room_metadata: room.as_deref(),
                    room_independent,
                    source,
                },
                &out,
            )?;
            let scores = vec![evaluate::score(&record, plan.plan.answer_deadline_ms, None)];
            let records = vec![record];
            let self_test = canary::run(&plan, &catalog)?;
            let directory = write_evaluation(
                &out,
                &plan,
                &run_id,
                &records,
                &scores,
                Vec::new(),
                self_test.clone(),
            )?;
            print_summary(&scores, &directory);
            return Ok(trust_exit(&self_test));
        }
        "annotation-template" => {
            let run = PathBuf::from(args.positional()?);
            let destination = PathBuf::from(args.positional()?);
            let (_, plan) = load(&mut args)?;
            args.done()?;
            let (records, _, run_id) = read_records(&run, &plan)?;
            let template = annotations::template(&run_id, &records);
            write_new_json(&destination, &serde_json::to_value(&template)?)?;
            println!(
                "{}",
                json!({"attempts": template.attempts.len(), "template": destination})
            );
        }
        "assets" => {
            let catalog = StimulusCatalog::load()?;
            let assets = assets(&mut args, &catalog)?
                .ok_or_else(|| invalid("--cache and --manifest are required"))?;
            args.done()?;
            let resolved: BTreeSet<&str> = assets.scenes().map(|a| a.scene.as_str()).collect();
            let missing: Vec<&str> = catalog
                .catalog
                .scenes
                .iter()
                .map(|s| s.id.as_str())
                .filter(|id| !resolved.contains(id))
                .collect();
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({"resolved": resolved, "missing": missing}))?
            );
        }
        "render-stimuli" => {
            // Renders only objects absent from the cache; existing verified speech
            // and mixes are reused by lamp-acoustic's content-addressed keys.
            let cache = PathBuf::from(args.positional()?);
            let manifest = PathBuf::from(args.positional()?);
            args.done()?;
            if manifest.exists() {
                return Err(invalid("use a new report path for every render"));
            }
            let catalog = StimulusCatalog::load()?;
            let cache = lamp_acoustic::Cache::new(cache)?;
            let report =
                lamp_acoustic::render(&catalog.catalog, &cache, &mut lamp_acoustic::MacSay)?;
            lamp_acoustic::save_report(&manifest, &report)?;
            println!(
                "{}",
                json!({"generated_speech": report.generated_speech, "reused_speech": report.reused_speech,
                "generated_scenes": report.generated_scenes, "reused_scenes": report.reused_scenes, "manifest": manifest})
            );
        }
        "physical-run" => {
            let (catalog, plan) = load(&mut args)?;
            let config_path = PathBuf::from(
                args.take("--config")?
                    .ok_or_else(|| invalid("--config FILE is required"))?,
            );
            let out = PathBuf::from(
                args.take("--out")?
                    .ok_or_else(|| invalid("--out NEW_DIR is required"))?,
            );
            let options = suite_options(&mut args, run_id(&out))?;
            let assets = assets(&mut args, &catalog)?
                .ok_or_else(|| invalid("--cache and --manifest are required"))?;
            let execute = args.take_flag("--execute");
            args.done()?;
            let file: PhysicalFile = serde_json::from_slice(&fs::read(&config_path)?)?;
            let config = file.into_config()?;
            let mut backend = PhysicalBackend::new(config.0, &assets, config.1);
            if !execute {
                use lamp_voice_eval::runner::Backend;
                let order = attempt_order(&plan, &options)?;
                let rows: Vec<Value> = order
                    .iter()
                    .map(|(repetition, id)| {
                        let scenario = plan.scenario(id).expect("validated");
                        json!({"repetition": repetition, "scenario": id,
                            "status": backend.unsupported(scenario).map_or("would run".to_owned(), |r| format!("withheld: {r}"))})
                    })
                    .collect();
                println!(
                    "{}",
                    serde_json::to_string_pretty(&json!({
                        "dry_run": true,
                        "note": "nothing was started; add --execute after the operator readiness checks",
                        "backend": backend.describe(),
                        "attempts": rows,
                    }))?
                );
                return Ok(0);
            }
            // Lamp time is scarce: never run a physical suite with an evaluator
            // that fails to detect its own injected failures.
            let self_test = canary::run(&plan, &catalog)?;
            if !canary::all_detected(&self_test) {
                eprintln!("{}", serde_json::to_string_pretty(&self_test)?);
                return Err(invalid(
                    "evaluator self-test failed; refusing to start a physical run",
                ));
            }
            let mut options = options;
            options.self_test = serde_json::to_value(&self_test)?;
            create_run_directory(&out)?;
            let records = run_suite(&plan, &catalog, &mut backend, &options, &out)?;
            let scores: Vec<AttemptScore> = records
                .iter()
                .map(|r| evaluate::score(r, plan.plan.answer_deadline_ms, None))
                .collect();
            let directory = write_evaluation(
                &out,
                &plan,
                &options.run_id,
                &records,
                &scores,
                Vec::new(),
                self_test,
            )?;
            print_summary(&scores, &directory);
        }
        _ => return Err(invalid(USAGE)),
    }
    Ok(0)
}

/// Reviewable physical-run configuration. Contains no credentials: the Lamp
/// command reuses the operator's existing authorized SSH setup.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PhysicalFile {
    lamp_command: Vec<String>,
    lamp_work_root: String,
    fixture_reply: Option<String>,
    fixture_sha256: Option<String>,
    provider_config: Option<String>,
    #[serde(default = "default_noise")]
    noise_suppression: String,
    #[serde(default)]
    diagnostics: bool,
    room_recorder: Option<Vec<String>>,
    #[serde(default)]
    room_independent: bool,
    /// Required for loudspeaker replay; unused for direct human speech.
    #[serde(default)]
    output_device: Option<String>,
    /// "loudspeaker_synthetic" (default) or "direct_human".
    #[serde(default = "default_source")]
    stimulus_source: StimulusSource,
    /// Label (not a name) for the person speaking in direct-human runs.
    #[serde(default)]
    human_speaker: Option<String>,
    #[serde(default = "default_allowance")]
    allowance_seconds: u16,
    #[serde(default)]
    ring_channel_ceiling: Option<u16>,
}
fn default_source() -> StimulusSource {
    StimulusSource::LoudspeakerSynthetic
}
fn default_noise() -> String {
    "on".into()
}
fn default_allowance() -> u16 {
    40
}
impl PhysicalFile {
    fn into_config(self) -> Result<(PhysicalConfig, Box<dyn Player>)> {
        if self.lamp_command.is_empty() || !self.lamp_work_root.starts_with('/') {
            return Err(invalid(
                "lamp_command must be nonempty and lamp_work_root absolute",
            ));
        }
        let player: Box<dyn Player> = match self.stimulus_source {
            StimulusSource::LoudspeakerSynthetic => {
                let output_device = self
                    .output_device
                    .ok_or_else(|| invalid("loudspeaker replay needs output_device"))?;
                lamp_observer::playback::validate_output_name(&output_device)?;
                Box::new(ObserverPlayer { output_device })
            }
            StimulusSource::DirectHuman => Box::new(HumanPrompter {
                speaker_label: self.human_speaker.unwrap_or_else(|| "person-1".into()),
            }),
            _ => {
                return Err(invalid(
                    "stimulus_source must be loudspeaker_synthetic or direct_human",
                ));
            }
        };
        let mode = match (
            self.fixture_reply,
            self.fixture_sha256,
            self.provider_config,
        ) {
            (Some(reply), Some(sha256), None) => LampMode::Fixture { reply, sha256 },
            (None, None, Some(provider_config)) => LampMode::Directed { provider_config },
            _ => {
                return Err(invalid(
                    "give fixture_reply + fixture_sha256, or provider_config",
                ));
            }
        };
        Ok((
            PhysicalConfig {
                lamp_command: self.lamp_command,
                lamp_work_root: self.lamp_work_root,
                mode,
                noise_suppression: self.noise_suppression,
                diagnostics: self.diagnostics,
                room_recorder: self.room_recorder,
                room_independent: self.room_independent,
                allowance_seconds: self.allowance_seconds,
                ring_channel_ceiling: self.ring_channel_ceiling,
                stimulus_source: self.stimulus_source,
            },
            player,
        ))
    }
}

/// Every attempt row of a run, including planned attempts that never finished.
fn read_records(
    run: &Path,
    plan: &LoadedPlan,
) -> Result<(Vec<AttemptRecord>, Vec<String>, String)> {
    let (rows, truncated) = read_rows(run)?;
    let mut problems = Vec::new();
    if truncated {
        problems.push("the final ledger line was truncated".to_owned());
    }
    let start = rows
        .first()
        .filter(|r| r["row"] == "run_start")
        .ok_or_else(|| invalid("ledger has no run_start"))?;
    if start["plan_sha256"] != plan.sha256 {
        return Err(invalid(
            "this run used a different plan; pass the same --plan",
        ));
    }
    let run_id = start["run_id"].as_str().unwrap_or_default().to_owned();
    let mut records = Vec::new();
    let mut planned = Vec::new();
    for row in &rows {
        match row["row"].as_str() {
            Some("attempt_planned") => {
                planned.push(row["attempt_id"].as_str().unwrap_or_default().to_owned())
            }
            Some("attempt_finished") => records.push(serde_json::from_value::<AttemptRecord>(
                row["record"].clone(),
            )?),
            _ => {}
        }
    }
    for id in planned {
        if !records.iter().any(|r| r.attempt_id == id) {
            problems.push(format!(
                "attempt {id} was planned but never finished (runner crash or kill)"
            ));
        }
    }
    if rows.last().is_none_or(|r| r["row"] != "run_end") {
        problems.push("the run has no run_end row".into());
    }
    Ok((records, problems, run_id))
}

fn write_evaluation(
    run: &Path,
    plan: &LoadedPlan,
    run_id: &str,
    records: &[AttemptRecord],
    scores: &[AttemptScore],
    problems: Vec<String>,
    self_test: Vec<CanaryResult>,
) -> Result<PathBuf> {
    let parent = run.join("evaluations");
    if !parent.exists() {
        create_run_directory(&parent)?;
    }
    let directory = parent.join(unix_ms().to_string());
    create_run_directory(&directory)?;
    let report = report::build(
        run_id,
        &plan.plan.id,
        &plan.sha256,
        scores,
        problems,
        self_test,
    );
    write_new_json(
        &directory.join("evaluation.json"),
        &serde_json::to_value(&report)?,
    )?;
    let attempts = directory.join("attempts");
    create_run_directory(&attempts)?;
    for (record, score) in records.iter().zip(scores) {
        let mut file = lamp_voice_eval::ledger::new_private_file(
            &attempts.join(format!("{}.md", record.attempt_id)),
        )?;
        std::io::Write::write_all(
            &mut file,
            report::attempt_markdown(record, score).as_bytes(),
        )?;
    }
    let markdown = report::markdown(&report, &plan.plan.targets);
    let mut file = lamp_voice_eval::ledger::new_private_file(&directory.join("report.md"))?;
    std::io::Write::write_all(&mut file, markdown.as_bytes())?;
    Ok(directory)
}

fn print_summary(scores: &[AttemptScore], directory: &Path) {
    let mut counts = std::collections::BTreeMap::new();
    for score in scores {
        *counts
            .entry(format!("{:?}", score.outcome).to_lowercase())
            .or_insert(0) += 1;
    }
    println!(
        "{}",
        json!({"attempts": scores.len(), "outcomes": counts, "report": directory.join("report.md")})
    );
}

/// Exit code 2 when the evaluator failed its self-test: the report exists but
/// is marked untrusted.
fn trust_exit(self_test: &[CanaryResult]) -> i32 {
    if canary::all_detected(self_test) {
        0
    } else {
        eprintln!("lamp-voice-eval: evaluator self-test failed; the report is marked UNTRUSTED");
        2
    }
}
