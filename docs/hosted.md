# What we host, what stays on your network, what happens offline

Autonomous OS phones home to three things and nothing else.

- **Skill Store** — our own catalog, behind one-tap install in the app. It
  carries more than this repo does: 70 skills as of 2026-08-16, of which the 25 in `skills/` are
  the robot skills that ship with the OS. The other 45 are first-party and
  user-published *workflow* skills (code review, standups, campaign plans) that
  need no hardware; they live only in the store, and between them they have
  been installed 14 times. Nothing in the store is paid — every entry is free. Plugins are not on this
  list: plugins install from the same store. (The plugin browser still queries
  Hugging Face Spaces — that was a prototype, and removing it is
  [#213](https://github.com/autonomous-ai/autonomous-os/issues/213).)
- **AI gateway** — the default OpenClaw brain and the voice, face and mood
  models call it. The key comes with the app account, self-built bodies
  included. No account? Swap the brain to Claude Code or Codex with your own key:
  you get chat and every skill; voice, face and mood still need the gateway.
  Pointing OpenClaw at your own endpoint (Ollama, any OpenAI-compatible server on
  your LAN) is not built yet — it is [#198](https://github.com/autonomous-ai/autonomous-os/issues/198).
- **Release feed** — the robot auto-updates from our CDN every 5 min
  (`bootstrap/`, staged by `min_version`, so we can hold a floor). Metadata is
  Ed25519-signed and ZIPs are SHA-256 checked before extraction. Running a
  fleet? Point `OTA_METADATA_URL` and `OTA_SIGNING_PUBLIC_KEY` at your own
  feed/key; os-server and bootstrap retain one local rollback copy. There is no fleet view yet — one robot per **Add
  robot**, and every robot pulls the same skill feed and OTA floor.

## Bring your own LLM endpoint

Point the robot at any OpenAI-compatible server — Ollama, vLLM, LM Studio,
llama.cpp, OpenRouter — and it lists models from *that* endpoint instead of our
catalog:

1. Set `llm_base_url` (and `llm_api_key`, if your server wants one) — in the
   browser setup wizard's AI-brain step, or directly in the robot's
   `config.json`.
2. Restart `os-server`. On boot it calls `GET {llm_base_url}/models` and writes
   the models it finds into `openclaw.json`, so the brain only ever advertises
   models that endpoint actually serves.

Any base URL that is not an `autonomous.ai` / `autonomousdev.xyz` host takes
this path (`runtimes/openclaw/byo_models.go`); ours keeps the hosted catalog. If
the endpoint cannot list models the robot logs which URL failed and falls back
to the built-in list rather than advertising models nothing can serve.

Speech and perception have their own overrides — `stt_base_url`, `tts_base_url`,
and `DL_BACKEND_URL` for the perception service — so a robot with all four set
talks to nothing of ours.

The backend channel is separate too. By default the status `/ping` (which also
delivers MQTT endpoint updates) and ops `/alert` go to `llm_base_url` with
`llm_api_key`, so pointing the brain at Ollama silently drops them. To keep them
on the Autonomous backend while the brain runs locally, set `backend_base_url`
(for example the shipped value, kept in `autonomous_defaults.base_url`) and
`backend_api_key`. Leave both empty to keep the old one-endpoint behavior.

**Verified in unit tests, not yet on a robot against a local Ollama.** If you
run one, say what happened in
[Discussions](https://github.com/autonomous-ai/autonomous-os/discussions).

## Offline

Local intents (~50 ms), recorded moves and the safety gate keep working;
conversation, voice, face and mood stop until the network is back, because the
brain and those models call the gateway. A fully local robot needs the
BYO-endpoint PR.

## On the network

HAL (:5001), the brain and OTA listen on localhost only; nginx exposes setup,
monitor and chat on the LAN behind the 4-character login. Nothing listens on
the internet — [`SECURITY.md`](../SECURITY.md) has the audit.

## Running more than one robot

Keep the fleet on its own `OTA_METADATA_URL` and pinned signing key.
