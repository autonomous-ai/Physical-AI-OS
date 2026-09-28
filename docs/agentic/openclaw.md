# OpenClaw Jev skill preloading

When enabled, OS onboarding installs the native `autonomous-jev` plugin under
`<OpenclawConfigDir>/extensions/autonomous-jev`. It registers
`before_prompt_build`, covering both OS-dispatched requests and channels handled
directly by OpenClaw. **Disabled by default** by `const jevEnabled = false` in `runtimes/openclaw/jev_plugin.go`. When disabled, bridges construct no selector; native onboarding installs no assets and creates no Jev registration. If an older Jev registration exists, Go only disables that registration and preserves unrelated settings. Disabled integrations do not read skills, call the provider, or add Jev content to requests. Enabling after validation requires changing the Go switch, rebuilding and restarting through runtime management.

If `plugins.allow` is configured, include `autonomous-jev` in that list too.
`plugins.deny`, plugin disables and `plugins.enabled: false` remain authoritative.
Opt-in authorizes sending the **current request and eligible skill names and
descriptions** to the existing OS `llm_base_url` plus `/jev/decisions`, using the
configured OS API key. There is no alternate provider or endpoint fallback.
The on-disk plugin sidecar contains only the OS config file path, not credentials.
Full skill contents stay local and are added to the current native request.

The plugin reads the host-produced session skill snapshot, checks the active
session ID, and intersects resolved skills with the native advertised roster.
It rechecks config and files after selection. Initial support deliberately covers
simple skills in the active workspace's `skills/` directory. Skills with
dependency/platform metadata, custom invocation policy, unknown tool policies,
active sandboxing, symlinks or oversized files defer to normal native loading.
Missing/incompatible snapshots also defer, including runtimes that do not expose
the required native session accessors. There is no filesystem-wide skill scan.

The provider receives at most 32 candidates. More than 32 defers instead of
arbitrarily truncating. Choice probability must be at least 0.70, margin at least
0.20 and independent fit at least 0.60, matching Hermes skill selection. The
complete operation has a 3-second deadline, no retry, one in-flight operation,
and a 30-second error cooldown. Timeout, malformed responses, abstention or
changed eligibility leave the original request unchanged.

Only the current hook request is classified; history is never inspected to find
a replacement request. Explicit skill/slash commands, system/sensing markers,
known image/attachment markers and context-only follow-ups such as `brighter`,
`continue`, or `make it brighter` bypass preloading. The main agent retains the
original conversation context. The selected full skill, source path and reference
directory are added through native `prependContext`, without replacing the system
prompt or granting tool permission. Native history may retain that skill read;
the selector never reuses a previous turn's decision. OS run correlation removes
only the validated preload envelope before matching its original pending request.

Logs use `[openclaw-jev] outcome=preloaded reason=accepted skill=...`, or
`abstained`, `skipped`, and `error`, without logging prompts or credentials.

Verified locally with mocked provider responses and native snapshot fixtures:

```sh
node --test runtimes/openclaw/plugins/jev/index.test.mjs
go test ./runtimes/openclaw -run 'TestJev|TestPending' -count=1
```

The native contract was inspected in installed OpenClaw 2026.2.23 and the
[upstream hook types](https://github.com/openclaw/openclaw/blob/main/src/plugins/hook-before-agent-start.types.ts).
No live Jev, device deployment or full gateway/channel integration test is implied
by these local checks. Older hook versions do not expose structured attachment
metadata; only supplied attachment fields and textual markers can be recognized.
