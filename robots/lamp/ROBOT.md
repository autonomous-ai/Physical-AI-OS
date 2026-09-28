---
schema: autonomous.device.v1
id: lamp
name: Autonomous Lamp
type: desk_robot
# `sim` is an inert laptop-only profile selected by `make sim`; it carries no
# wiring and is refused unless HAL_SIMULATE explicitly opts in.
boards: [sim, raspberry_pi_4, raspberry_pi_5, orangepi_sun60]
gateway:
  default: hermes
  protocol: websocket
voice:
  tts_provider: elevenlabs
  #tts_voice: Rachel      # optional
  # Out-of-the-box wake-word gate: a lamp sits in a shared room and hears every
  # conversation in it, so it waits to be addressed ("hey lamp" / "hey
  # autonomous" / "hey <agent name>") instead of answering ambient speech.
  # Adopted only while config.json has no wakeword key (a device os-server is
  # setting up for the first time); Settings owns the value from then on.
  wakeword: true
capabilities:
  audio:        { routes: [audio, speaker, voice], required: true }
  vision:       { routes: [camera], driver: opencv, required: true }
  sensing:      { routes: [sensing], required: true }
  # environment:  { routes: [environment], driver: composite, required: false }
  presence:     { required: true }
  motion:       { routes: [servo], driver: feetech, required: true, safety: SAFETY.md#motion }
  light:        { routes: [led, scene], driver: ws2812, required: true, safety: SAFETY.md#light }
  # display: lamp has NO screen — do not re-declare. Declaring it (even
  # required:false) makes HAL mount /display and run DisplayService in
  # framebuffer-only mode: a render loop drawing eyes nobody sees, plus an
  # "Unknown expression" warning on every emotion.
  expression:   { routes: [emotion], required: true }
  # lifelike: routeless — opts into the os-server idle "living creature" suite
  # (breathing LED, servo micro-movements, TTS self-talk). Omit it to keep a
  # device silent when idle (e.g. intern-v2 declares audio+light but not this).
  lifelike:     { required: false }
  media:        { routes: [music], required: true }
  connectivity: { routes: [bluetooth], required: true }
  companion:    { routes: [buddy], required: false }
  system:       { routes: [system], required: true }
soul_ref:   SOUL.md
safety_ref: SAFETY.md
urdf_ref:   urdf/lamp.urdf
memory:     { backend: local }
startup_volume: 75
---

# Autonomous Lamp

The maximal reference device — a 5-DOF desk robot that sees, hears, speaks, moves,
glows, and displays expression. Lamp exists to exercise every subsystem of the OS: if
a capability works on Lamp, it works.

## Body

A weighted base, a 5-servo articulated arm (Feetech bus servos over `/dev/ttyACM0`), a
warm LED ring head (WS2812), a camera, a microphone, and a
speaker. Compute is a Raspberry Pi 4/5 or OrangePi (sun60). The body is wired per
`hal/board/board.py`; the agent never addresses hardware directly.

## What the agent should assume

- The user is likely physically near the device, in a private space.
- Camera and microphone are sensitive — prefer local processing, ask before new uses.
- Movement can surprise people. Move gently, legibly, and stop on command.
- Light and motion are communication channels, not decoration.

## Soul and memory references

`soul_ref` points at the character that inhabits this body. It resolves to a soul
artifact — a path read relative to this device folder (here, `SOUL.md`), or an
`http(s)://` URL the runtime downloads. A body with no `soul_ref` (e.g. Intern) keeps
the gateway's default soul. `memory` names the continuity layer by backend. `ROBOT.md`
describes the body; the soul is referenced here, not embedded in the front matter.
