---
schema: autonomous.device.v1
id: intern-v2
name: Autonomous Intern
type: desk_agent
boards: [raspberry_pi_4, raspberry_pi_5, orangepi_sun60]
gateway:
  default: openclaw
  protocol: websocket
voice:
  tts_provider: elevenlabs
capabilities:
  audio:        { routes: [audio, speaker, voice], required: true }
  sensing:      { routes: [sensing], required: false }
  companion:    { routes: [buddy], required: false }
  system:       { routes: [system], required: true }
  light:        { routes: [led], driver: ws2812, required: true, safety: SAFETY.md#light }
  media:        { routes: [music], required: true }
  connectivity: { routes: [bluetooth], required: true }
soul_ref:   SOUL.md
safety_ref: SAFETY.md
memory:     { backend: local }
startup_volume: 100
---

# Autonomous Intern

The deliberate inverse of Lamp: an always-on desk agent with voice, ambient (sound)
sensing, and an LED ring, but **no camera, no actuation, no display**. Intern proves the
platform is real — it is not a fork of Lamp. It is the same OS image declaring fewer
capabilities.

## What makes Intern "not Lamp"

Intern declares `audio`, `sensing`, `companion` (it pairs the Buddy app to drive a
computer), `light` (the LED ring), and `system` — Lamp's `vision`, `motion`, `display`,
`presence`, `media`, and `connectivity` are simply absent. The OS
boots the same runtime, mounts only the audio/sensing/light/system routes Intern declares,
and never brings up the camera, servo, or display drivers. Ambient sensing is sound-only
(the mic) — presence and motion perceptions need a camera Intern does not have. There
is no `if device == intern` anywhere in the OS.

Intern v2 adds mic + speaker — capabilities Lamp already has — so the two devices share
their entire audio/voice stack verbatim. That overlap is the whole argument for one OS.

## What the agent should assume

- No camera, no body to move, and no screen to draw on — it expresses through voice and the LED ring; vision (camera) is unavailable.
- The job is agentic work (mail, calendar, tasks, research), not expressive companionship.
- Same privacy posture as Lamp: local-first, ask before sensitive sensing.
