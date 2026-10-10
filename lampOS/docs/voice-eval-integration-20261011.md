# Voice evaluator integration checkpoint — 2026-10-11

This checkpoint combines the previously verified `ad6c26423` integration with
Agent 2's four newer commits through `3eaa0365a`, preserving both histories.
There are 42 scenarios and 21 injected-failure self-tests. Their coverage is
software coverage, not 42 successful physical conversations.

Integration fixes preserve actual-WAV verification and reviewed-answer
accounting while adding these evidence boundaries:

- Prompted human trials cannot pass based on display time or synthetic speech
  windows. Unverified timed attempts remain incomplete and unscored; declared
  reviewed imports remain separate. See [acceptance boundaries](voice-eval-acceptance-boundaries.md).
- Self-tests use their own versioned, hashed embedded plan and catalog, allowing
  old/custom plans to be evaluated without new canary scenario names. See
  [canonical canary plan](voice-eval-canary-plan.md).
- Background inputs without an endpoint remain unknown rather than overflowing
  the timestamp arithmetic. Missing source fields deserialize as `unknown`.
- Report groups keep source cohorts separate, preserving negative-review
  denominators. JSON `strata` is an array of source-specific groups.

## Verification

A source-only copy outside the parent repository passed **712 host tests,
zero failed and zero ignored**, plus all four gates, run sequentially:

```sh
cargo fmt --all -- --check
cargo clippy --locked --offline --workspace --all-targets -- -D warnings
cargo test --locked --offline --workspace --no-fail-fast -- --test-threads=1
cargo build --locked --offline --release -p lamp-live -p lamp-voice-eval
```

Host: macOS x86-64. Source manifest: `30263f217d4e9ccb550e1f4a29a1fa1c0764bd489989b56079968dda7501aeda`
(163 Rust, Cargo and toolchain files; documentation excluded).
The 696-test receipt in [provider flow control](provider-flow-control.md)
belongs to the earlier source and is preserved separately.

The first full attempt under the filesystem/network sandbox failed local
socket tests (`Operation not permitted`); its logs are retained separately.
The unchanged source was then rerun with local IPC/loopback socket access.

Before correction, four focused evidence regressions failed: two false passes,
one false rejection and an integer overflow panic. Canonical self-test work
also reproduced three focused failures and an unchanged historical-plan CLI
failure (`unknown scenario disconnect-between-turns`); all now pass. The actual
historical plan then detected all 21 canaries. These are synthetic/software
checks, with no microphone, speaker, camera or motor activity.

Logs, exact commands, source identities, source-only archives and failed
reproductions are retained privately in
`artifacts/voice-eval-integration-20261011/`, alongside a separate durable
handoff backup. The reported simulation table in [voice evaluation](voice-eval.md)
is Agent 2's earlier baseline, not a new acoustic result.

## Remaining work

No new cloud call, Linux/ARM64 build, real Lamp run or acoustic latency result
was performed. Upstream playback-paced Gemini delivery, directed-mode cue
integration, bounded relay framing, robust echo/addressee admission, and
physical duplex/choreography qualification remain open. Follow
[workstream ownership](development-workstreams.md); physical work is deferred
until the owner checks the device. This checkpoint is not release acceptance.
