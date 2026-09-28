# Wiring

Pin-by-pin map for everything connected to the SBC. Two columns: **Raspberry Pi 5** and **OrangePi 4 Pro (Allwinner A733 / sun60iw2)**. Numbers come straight from the code so this stays verifiable — `hal/drivers/...` references are noted in each row.

Keep this in sync when wiring changes. Mismatch between this file and the code is a bug.

---

## Compute board pinouts

- Raspberry Pi 5 — https://pinout.xyz/
- OrangePi 4 Pro — see vendor wiki; 40-pin header, Allwinner A733 GPIO scheme: `PA=0..31, PB=32..63, PC=64..95, PD=96..127, PE=128..159, PL=352..383`

---

## Button (single)

Single momentary tactile, normally-open. Single-click = stop / unmute. Triple-click = reboot. Long press 3 s = shutdown. 200 ms debounce in software.

| | Raspberry Pi 5 | OrangePi 4 Pro |
|---|---|---|
| Header pin | 11 | 11 |
| Signal | BCM 17 | PL9 |
| Char device | `/dev/gpiochip0` line 17 | `/dev/gpiochip1` line 9 |
| GND | pin 9 | pin 9 |
| Code | `hal/drivers/gpio_button.py:22-24` | `hal/drivers/gpio_button.py:30-32` |

---

## WS2812 RGB LED ring (32 pixels)

5 V data + power + GND. Common ground with SBC. Brightness capped in software — see `power.md` for current budget rationale.

| | Raspberry Pi 5 | OrangePi 4 Pro |
|---|---|---|
| Driver | `spidev` raw WS2812 over SPI0.0 | `spidev` raw WS2812 over SPI3.0 |
| Data pin | GPIO 10 (SPI0 MOSI) — header pin 19 | SPI3 MOSI — header pin 19 |
| Clock | n/a (MOSI-only) | n/a (MOSI-only) |
| Bus speed | 6.4 MHz | 6.4 MHz |
| 5 V | external 5 V rail (not header) | external 5 V rail (not header) |
| GND | header pin 6 (common with rail) | header pin 6 (common with rail) |
| Code | `hal/drivers/rgb/rgb_service.py:102-111` | `hal/drivers/rgb/rgb_service.py:176-180` |

> **Pi 4 fallback**: PWM driver on GPIO 12 (header pin 32). See `hal/drivers/rgb/rgb_service.py:182-186`.

> **Power note**: 32 px × 60 mA white worst-case = 1.92 A. Software caps brightness; do not test at full white without a 5 A-capable rail.

---

## Servo bus (5× STS3215)

Feetech STS3215 servos on a TTL daisy chain, driven by a USB-to-TTL servo control board. Same wiring on both SBCs — it's just USB.

| | Raspberry Pi 5 | OrangePi 4 Pro |
|---|---|---|
| Connection | USB-A → USB control board | USB-A → USB control board |
| Enumeration | `/dev/ttyACM0` | `/dev/ttyACM0` |
| Servo count | 5 (chained) | 5 |
| Servo power | external 5 V (NOT from USB) | external 5 V (NOT from USB) |
| Protocol | Feetech SCS via `scservo_sdk` | same |
| `P_Coefficient` | 16 (do **not** override — see `hal/UPSTREAM.md:37`) | 16 |
| Code | `hal/config.py:13`, `hal/routes/servo.py:383` | same |

> Servo and camera **share serialization** in software because of bus contention (`hal/UPSTREAM.md:26-27`). Mechanically they're independent — this is purely a runtime concern.

---

## Speaker amplifier (PAM8610 v2) + 2× 3 W speakers

Stereo class-D amp driven by a **USB audio board (DAC)** plugged into the SBC — since 2026-09-18 this is the line-out of the C-Media adapter (`0d8c:0014`) that also carries the sensing mic; the earlier TTGK USB speaker is no longer fitted. The onboard codec → PAM8610 path was hissing / picking up static, so we moved the audio source off the SBC's onboard codec entirely. The onboard codec is no longer used for audio at all: sensing moved to a USB mic on 2026-09-17 (see Microphones).

Signal chain:

```
SBC → USB → USB audio board (DAC) → 3.5 mm line-out → PAM8610 L/R in → speakers
```

| | Raspberry Pi 5 | OrangePi 4 Pro |
|---|---|---|
| Audio source | USB audio board (line-out) | USB audio board (line-out) |
| Connection | USB-A | USB-A |
| ALSA alias | `plug:device_speaker` (mapped to USB DAC card) | `plug:device_speaker` = softvol stage (-30..0 dB) in front of card `device_cmedia`, whose hardware mixer is pinned at -31 dB by `lamp-cmedia-speaker.service`; the PAM8610 is hot enough that the raw mixer's bottom step was already audible, so the slider needed its own range |
| DAC out → amp | 3.5 mm TRS → PAM8610 L/R inputs (twisted pair, short run) | same |
| Speaker A | PAM8610 L+ / L− → speaker A | same |
| Speaker B | PAM8610 R+ / R− → speaker B | same |
| Amp Vcc | 12 V (do not feed 5 V — under-driven) | 12 V |
| Amp GND | star-ground at buck output | same |

> The onboard codec (WM8960 on Pi via Seeed HAT, ES8389 on OPi) is no longer used: its line-out is not connected to the amp and sensing capture moved to a USB mic (Mic 1). Only `ctl.!default` still points at `sndi2s4`.

> Keep the DAC → amp lead short and twisted. Run it away from the 12 V power harness — the prior hiss was partly induced from the SBC's switching supply.

---

## Microphones

Two microphones are active, both USB: the Jieli mic handles voice capture and
the C-Media adapter handles ambient sensing.

| Role | Device | ALSA alias | Code |
|---|---|---|---|
| Voice (Mic 2) | Jieli USB Composite Device | `plug:device_micro2` | `/opt/hal/.env` (`HAL_AUDIO_INPUT_ALSA`) |
| Sensing (Mic 1) | C-Media USB Audio Adapter (Unitek Y-247A, `0d8c:0014`) | `plug:device_micro1` | `/opt/hal/.env` (`HAL_AUDIO_SENSING_DEVICE`) |

The OPENAICAM audio endpoint (`device_micro3`) is not selected: a direct tone
test returned near-digital silence, so it needs a hardware or firmware repair
before it can be used for voice capture.

> Mic 1 was the onboard ES8389 MEMS mic until 2026-09-17; it is now the capture side of the C-Media USB adapter (card `device_cmedia` by a udev rule keyed on `0d8c:0014`, alias `device_micro1` in asound.conf) (`/etc/udev/rules.d/99-lamp-device.rules`, tracked at `robots/lamp/rootfs/etc/udev/rules.d/`). No desoldering is needed anymore.

> ALSA aliases live in `/etc/asound.conf`. Their tracked source is `robots/lamp/rootfs/etc/asound.conf`, installed onto `/` at image build and on every device-profile OTA. Cards are addressed by **name**, not index, since USB card numbers reorder across boots.

> On Raspberry Pi the wm8960 capture gain has a watchdog that clamps it to 160 — see `project_lamp_pcm_watchdog.md`.

---

## Camera (USB IMX307)

USB UVC. Plug into any free USB port (prefer USB 3 if available for headroom; 1080p30 fits in USB 2 fine).

| | Raspberry Pi 5 | OrangePi 4 Pro |
|---|---|---|
| Connection | USB-A | USB-A |
| Enumeration | `/dev/video0` (first UVC device) | `/dev/video0` |
| Pixel format | MJPG @ 1080p / 30 fps | MJPG @ 1080p / 30 fps |
| Notes | Camera + servo serialize in software | same |
| Code | `hal/routes/camera.py`, `hal/server.py` | same |

---

## TTP223 capacitive touch (optional, OPi only)

Four touch pads for left/right swipe gesture. Factory default mode (AB pads unsoldered): momentary, active-HIGH push-pull output. **The OPi GPIO is 3.3 V only — feeding it 5 V will damage the SoC.** So the TTP223 must run on a 3.3 V VCC (push-pull output then swings 0 ↔ 3.3 V, GPIO-safe).

Signal lines are the same in both options:

| Pad | OPi header pin | GPIO line |
|---|---|---|
| S1 (leftmost) | 29 | gpiochip0 line 96 (PD0) |
| S2 | 31 | gpiochip0 line 97 (PD1) |
| S3 | 33 | gpiochip0 line 98 (PD2) |
| S4 (rightmost) | 35 | gpiochip0 line 99 (PD3) |

GND of all four modules ties to OPi header pin 25 or 39 (any GND).

Two ways to deliver 3.3 V to the four VCC pins:

### Option A — run a 3.3 V wire from the OPi to the head (recommended)

A single ~1 m wire from OPi header **pin 1 or pin 17** (3.3 V) up the neck to the head, where it fans out to the four VCC pins. Plus the GND wire. **No resistors.**

```
OPi pin 1 (3.3V)  ──── 1 m wire ──── ●───●───●───●   →  VCC of TTP1..TTP4
OPi pin 6 (GND)   ──── 1 m wire ──── ●───●───●───●   →  GND of TTP1..TTP4
```

Pros: regulated, stiff 3.3 V (the OPi's onboard LDO sources 100+ mA, plenty for ~12 mA worst case from 4 sensors). No extra parts. Multi-touch works fine.

Cons: one extra conductor through the articulated neck.

### Option B — reuse the 5 V already at the head, drop with a divider

If 5 V is already routed to the head for other things (LED ring, etc.), tap that and add a resistor divider locally to make 3.3 V for the TTP223 VCC rail. Saves a wire down the neck.

```
5V (already at head) ──[R1]──┬── 3.3V rail ── ●───●───●───●  →  VCC of TTP1..TTP4
                             │
                            [R2]
                             │
                            GND
```

Sizing: pick the divider so it doesn't sag too much when more than one pad is touched. **Use small resistors.** Recommended: **R1 = 47 Ω, R2 = 91 Ω** → no-load output ≈ 3.30 V; under 12 mA load (all 4 touched) sags to ≈ 2.95 V (still above the TTP223 minimum of 2.0 V).

Trade-offs vs Option A:
- Saves one wire to the head.
- Wastes ~180 mW continuous in the divider (5 V across 138 Ω = 36 mA always flowing).
- VCC isn't truly regulated — it shifts ~0.35 V between idle and 4-pad touch. Touch sensitivity drifts a little with that.
- If you size the divider too high (e.g. 1.8 k + 3.3 k), it works at idle but **collapses to ~0 V on a single touch** — the chip then resets, and you get phantom triggers on the other lines. Don't go bigger than ~100 Ω total.

### Multi-touch behaviour

- **Option A**: all combinations work. Touching 1, 2, 3, or 4 pads at once each register cleanly on their respective GPIO. The 3.3 V rail doesn't budge.
- **Option B with the recommended 47 Ω + 91 Ω**: single and double touches are clean. Triple/quad touches drop VCC by ~0.3–0.4 V; the chips still detect, but right at the edge of their range — expect occasional missed events on the 3rd/4th simultaneous touch.
- **Option B with high-value resistors (kΩ range)**: a single touch already collapses VCC. The chip browns out, output flickers, neighbouring channels glitch. **Avoid.**

Software side: the swipe gesture detector needs to handle a small overlap (two adjacent pads briefly HIGH together as a finger crosses). With Option A there's no extra coupling concern; with Option B the slight VCC dip during a swipe could shift edge timing by a few ms — negligible for swipe, but tighten debounce if you see issues.

---

## Power

See [`power.md`](power.md) for the full 12 V → 5 V tree, current budget, and grounding scheme.
