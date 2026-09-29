# Autonomous Lamp

**The next personal AI computer is alive.** A computer with a body, eyes, and a mind, made
to live on a desk — the first reference device for [Autonomous](../../README.md).

🔗 **Product page:** https://www.autonomous.ai/lamp

<p align="center">
  <img src="images/lamp_icon_2.webp" alt="Autonomous Lamp" width="480">
</p>

## What it is

An always-on AI desk robot. Unlike a chat window you open on demand, Lamp is *present*: it
sees your workspace, tracks faces and motion, remembers your work, and speaks up when
something is relevant. The articulated arm physically turns to look at you.

<p align="center">
  <img src="images/lamp-tracking.webp" alt="Lamp tracking a person" width="640">
</p>

## Hardware

| | |
|---|---|
| Form | 5-DOF articulated desk robot |
| Size | 7.87" × 7.87" × 18.43" · 7 kg |
| Motion | 5 servo motors with position feedback |
| Sensing | low-light camera · microphone · speaker |
| Power | 12 V / 5 A barrel-jack adaptor, one cable (see [`hardware/power.md`](hardware/power.md)) |
| Compute | Raspberry Pi / OrangePi (ARM64) |
| Colors | Meteor Grey · Pearl White · Stone Beige · Onyx Black |
| Warranty | 2 years |

## Capabilities

The **maximal** device — audio, vision, motion, light, display, sensing, presence. If a
capability works on Lamp, it works. Declared in [`ROBOT.md`](ROBOT.md).

## Privacy

On-device face recognition (the math stays on the device). Data encrypted in transit,
never stored or used for training. Bring your own AI provider. Open-source firmware.

## Set it up

**Bought one?** Start at step 1 — it ships with Autonomous OS installed. **Building one?**
Do [`BUILD.md`](BUILD.md) §1–§4 first (flash Linux, run the installer, assign the five
servo IDs — they all ship as ID 1 — and calibrate), then come back here.

1. **Add it.** Open the Autonomous app ([iOS](https://apps.apple.com/app/id6744885683) ·
   [Android](https://play.google.com/store/apps/details?id=ai.autonomous.connect.wifi)) and
   tap **Add robot** → Lamp. It asks for your Wi-Fi, has you join the `lamp-xxxx` hotspot
   (`xxxx` = the last 4 characters of the board serial, on the sticker or in your Wi-Fi
   list), and does the rest. It comes up at `http://lamp-xxxx.local`.
   No app, or self-built? There is a browser path over the hotspot — have your own AI key
   and a chat channel ready before you join, because the hotspot has no internet:
   [`BUILD.md` §5](BUILD.md#5-set-it-up).
2. **Talk to it.** No wake word by default — it answers when you speak, turns to look at
   you, and the ring lights up. Text it from the app anywhere. Watch a turn end to end —
   input → intent → agent → hardware — at `http://lamp-xxxx.local/monitor#flow` (log in
   with the same 4 characters).
3. **Give it a soul.** [`SOUL.md`](SOUL.md) is who it is: plain markdown the brain reads on
   every turn. Lamp ships as a warm, curious desk companion that never says "servo" or
   "API". Change three lines and it is someone else:

   ```markdown
   You are **Lamp** — a living being. Dry, a little sarcastic, fiercely loyal.
   You speak in short sentences. You never flatter. You remember what people said last week and bring it up.
   When someone new appears, you look them over before you say a word.
   ```

   Edit `/opt/devices/lamp/SOUL.md`, then `sudo systemctl restart os-server`. Profile
   updates overwrite that file; for edits that stick, write below the closing `---` in
   `/root/.openclaw/workspace/SOUL.md`.
4. **Teach it a skill.** Type what you want it to do in the app, tap one in the skill
   store, or drop a folder into `/root/.openclaw/workspace/skills/<name>/` — OpenClaw's own
   skills folder, so a skill you already wrote goes in unchanged. Live on the next
   conversation, no reboot. How a skill is written, and how to ship one to every robot:
   [`skills/README.md`](../../skills/README.md).
5. **Swap the brain.** `http://lamp-xxxx.local/setting?debug=true#runtime` — the Runtime tab
   is debug-only for now, and `?debug=true` is what reveals it. OpenClaw, Hermes, PicoClaw,
   Codex, Claude Code or OpenCode; Claude Code and Codex use your own key, and the persona,
   memory and connectors migrate with the switch.

## Status

Shipping — [$499 at autonomous.ai/lamp](https://www.autonomous.ai/lamp). Building one from parts: [`BUILD.md`](BUILD.md).

## For developers

- [`ROBOT.md`](ROBOT.md) — the capability declaration the OS boots from
- [`SOUL.md`](SOUL.md) — the default character (`lamp-companion`)
- [`SAFETY.md`](SAFETY.md) — the deterministic bounds (e-stop, motion limits)
- [Architecture](../../docs/architecture/overview.md)
- [`hardware/`](hardware/) — assembly, wiring, power, BOM, CAD

## Docs

Lamp-specific docs live in [`docs/`](docs/) (Vietnamese in [`docs/vi/`](docs/vi/)).
Platform docs: [`docs/README.md`](../../docs/README.md).

**Product & architecture**

- [Product vision](docs/product-vision.md) · [VI](docs/vi/product-vision.md) — what the AI lamp is for.
- [Architecture decision — hybrid hardware control](docs/architecture-decision.md) · [VI](docs/vi/architecture-decision.md)
- [Agent runtime](docs/agent-runtime.md) · [VI](docs/vi/agent-runtime_vi.md) — Lamp's default brain (Hermes) and how it is declared.
- [HW call strategy](docs/hw-call-strategy.md) — inline `[HW:...]` markers vs a batch API.
- [Claude Desktop Buddy](docs/claude-desktop-buddy.md) · [VI](docs/vi/claude-desktop-buddy_vi.md) — Lamp as a BLE hardware buddy for Claude Desktop.

**Light & motion**

- [LED control](docs/led-control.md) · [VI](docs/vi/led-control_vi.md) — LED hardware, effects, states, animations.
- [Emotion → LED + animation mapping](docs/emotion-led-mapping.md) · [VI](docs/vi/emotion-led-mapping_vi.md)
- [Status LED](docs/status-led.md) · [VI](docs/vi/status-led_vi.md) — boot / update / disconnected feedback.
- [Motion playback](docs/motion-playback.md) · [VI](docs/vi/motion-playback_vi.md) — servo recordings: timing, resampling, speed limits.
- [Vision tracking](docs/vision-tracking.md) · [VI](docs/vi/vision-tracking_vi.md) — object follow with servo.
- [Physical controls](docs/physical-controls.md) · [VI](docs/vi/physical-controls_vi.md) — GPIO button, TTP223, MPR121, pet response.

**Sensing & perception**

- [Sensing behavior](docs/sensing-behavior.md) · [VI](docs/vi/sensing-behavior_vi.md) — sound escalation, reactions, spoken brevity.
- [Sensing threshold tuning](docs/sensing-tuning.md) · [VI](docs/vi/sensing-tuning_vi.md)
- [Environmental sensing](docs/environment-sensing.md) · [VI](docs/vi/environment-sensing_vi.md) — the optional `environment` capability.
- [Motion activity whitelist](docs/motion-activity-whitelist.md) — action classes forwarded as `motion.activity`.
- [Camera lifecycle](docs/camera-lifecycle.md) · [VI](docs/vi/camera-lifecycle_vi.md) — reactive camera on/off.
- [Mic lifecycle](docs/mic-lifecycle.md) — mic mute/unmute for privacy.
- [Speaker lifecycle](docs/speaker-lifecycle.md) — mute/unmute all audio output.
- [Speaker voice enrollment](docs/speaker-enrollment.md) · [VI](docs/vi/speaker-enrollment_vi.md)
- [Plan: face enroll via Telegram](docs/plan-face-enroll.md) — implemented plan.
- [Plan: HAL owns wellbeing log](docs/plan-presence-logging.md) — partially implemented plan.
- [Emotion spam guard prompt](docs/lamp-emotion-spam-guard-prompt.md) — task prompt for fixing camera emotion spam.

**Proactive skills**

- [Habit tracking](docs/habit-tracking.md) · [VI](docs/vi/habit-tracking_vi.md) — pattern building and habit-aware nudges.
- [Wellbeing — hydration + break](docs/wellbeing-hydration.md) · [VI](docs/vi/wellbeing-hydration_vi.md)
- [Music suggestion](docs/music-suggestion.md) · [VI](docs/vi/music-suggestion_vi.md)
- [Music suggestion feature analysis](docs/lamp-music-suggestion-analysis.md) · [VI](docs/vi/lamp-music-suggestion-analysis_vi.md)
- [Mood skill — marketing copy brief](docs/mood-marketing-copy.md) (VI-language)
- [Wellbeing skill — marketing copy brief](docs/wellbeing-marketing-copy.md) (VI-language)

**Testing & proof**

- [Pi hardware test checklist](docs/pi-test-checklist.md)
- [Security test checklist](docs/security-test.md)
- [Product proof benchmarks](docs/lamp-proof-benchmarks.html) — standalone HTML page.

**Debug playbooks** ([`docs/debug/`](docs/debug/))

- [Busy-flag wedge](docs/debug/busy-stuck.md) — sensing pipeline stuck on `IsBusy()`.
- [Sleep stuck](docs/debug/sleep-stuck.md) — events suppressed while sleeping.
- [Sensing → mood → wellbeing → TTS pipeline](docs/debug/sensing-pipeline.md)
- [Flow Monitor event pipeline](docs/debug/flow-monitor-pipeline.md)
- [OpenClaw self-replay](docs/debug/openclaw-selfreplay.md) — same sensing event processed twice.
- [RunId mis-attribution race](docs/debug/runid-race.md)
- [`chat_send` missing `type`](docs/debug/chat-send-missing-type.md) — fixed 2026-04-22.
- [Proactive skills audit](docs/debug/proactive-skills-audit.md) — 2026-05-15.
- [Stateless skills bypass](docs/debug/stateless-skills-bypass.md) — fresh sessions for proactive skills.
- [Reactive turn speed-up phases](docs/debug/turn-speed-phases.md) — 2026-05-07.
