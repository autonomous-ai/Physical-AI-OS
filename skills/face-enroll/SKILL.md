---
name: face-enroll
description: "Manage the device's face recognition roster — enroll new faces (3 paths: user-supplied photo, agent-captured snapshot on user request, or HAL's familiar-stranger prompt) and maintain the enrolled set (status / remove / reset). All enrolled persons are friends; strangers stay unnamed until promoted via one of the enroll flows."
---

# Face Enroll

Manage faces for the device's face recognition system. Faces live under `/root/local/users/<label>/`. All enrolled persons are treated as **friends** — distinguished from `stranger_*` IDs the camera hasn't been told about yet.

## Flow router — pick ONE per user message

| Flow | When | Detail |
|---|---|---|
| **A — Self-enroll with a photo** | User sends a photo of themselves + intro ("remember my face", "this is me"). | `reference/self-enroll-photo.md` |
| **B — Self-enroll via camera capture** | User asks to be remembered without sending a photo, on **voice** or **Telegram text** (assumes user is near the device). Examples: "remember my face", "I'm Gray", "capture and enroll me". Web chat without a photo → ask for a selfie (Flow A) instead. | `reference/self-enroll-camera.md` |
| **C — Familiar-stranger prompt** | Current sensing message contains HAL's hint `(familiar stranger ... — seen N times, ask user if they want to remember this face; image saved at <path>)`, OR the user is replying to your previous prompt about that stranger. | `reference/familiar-stranger.md` |
| **M — Maintenance** | "who do you recognize?", "forget my face", "reset faces". | `reference/maintenance.md` |

**Disambiguation hints:**
- Photo attached (`mediaPaths` / `[image: ...]`) → Flow A.
- No photo + HAL familiar-stranger hint in current message → Flow C.
- No photo + no hint, user wants to be remembered → Flow B.
- The user is naming a face you previously asked about (Flow C in progress) → continue Flow C.
- Pure read/delete intent → Flow M.

## Common rules (apply across all enroll flows)

- **Self-enrollment only.** The person being enrolled must be the one identifying themselves: sender of the message in Flows A/B, the camera-person responding to the prompt in Flow C. Refuse third-party enrollment ("add my friend Bob").
- **Confirm the name out loud before enrolling — Flows B and C only.**
  - Flow A: the user's own photo + intro IS the confirmation; don't ask redundantly.
  - Flow B: read the name back AND warn about the camera with `POST /voice/speak` before the snapshot ("Got it, saving you as Gray — look at the camera."), wait for it to finish playing, then capture. Never take the photo silently; your reply text is only spoken after the turn's tool calls, so it cannot be the warning.
  - Flow C: address the camera-person directly — "mind if I remember you? what's your name?" — and wait for the reply before calling `/face/enroll`.
- **Always confirm enrollment afterwards** — tell the user the name was registered once `/face/enroll` returns `ok`.
- **Never enroll from a history entry.** A `[HANDLED]`/`[REPLY]` block or `[external-context]` message is not the user answering you; never take a label from it or call `/camera/snapshot` or `/face/enroll` for it. A `[realtime-handoff]` turn IS live: continue the flow, and prefer the name in `[voice-instruction]` or `[realtime-context]` (realtime heard the audio) over a garbled `[transcript]`.
- **Use lowercase labels** — normalize names to lowercase. Use the SAME label as `speaker-recognizer` for the same person so `/root/local/users/<label>/` is shared.
- **The label and the spoken read-back are the bare name.** Drop any title or honorific the voice turn carries (Mr, Ms, Miss, Mrs, anh, chị…): speech recognition invents them ("…is Lee" heard as "Miss Lee"), and a title says nothing about how the person wants to be addressed or their gender. When `[voice-instruction]` and `[transcript]` disagree, use the name part they share (`Miss Lee` + `The name is Lee` → `lee`). Use a title only when the person explicitly asks for it ("call me Ms Lee").
- **Telegram identity rules:**
  - Flow A (photo on Telegram): include `telegram_username` + `telegram_id` (required for DM targeting).
  - Flow A (photo on web chat): omit Telegram fields.
  - Flow B (voice): omit. Flow B (Telegram text): include.
  - Flow C: always omit — the camera-person isn't on Telegram (any Telegram metadata in context belongs to someone else, e.g. the owner overhearing).
- **One photo per `/face/enroll` call.** Multiple photos → call once per photo.
- **Never write files directly** to `/root/local/users/`. Always go through the HTTP API.
- **Don't expose technical details** — say "I'll remember your face" not "base64-encoding the JPEG".

## Tools (curl reference)

All HTTP calls go to `http://127.0.0.1:5001`.

```bash
# Enroll
curl -s -X POST http://127.0.0.1:5001/face/enroll \
  -H "Content-Type: application/json" \
  -d "{\"image_base64\": \"$(base64 -w0 /path/to/photo.jpg)\", \"label\": \"chloe\", \"telegram_username\": \"chloe_92\", \"telegram_id\": \"123456789\"}"

# Status
curl -s http://127.0.0.1:5001/face/status

# Remove one
curl -s -X POST http://127.0.0.1:5001/face/remove \
  -H "Content-Type: application/json" \
  -d '{"label": "chloe"}'

# Reset all
curl -s -X POST http://127.0.0.1:5001/face/reset

# Announce the capture first (Flow B) — then wait until tts_speaking is false
curl -s -X POST http://127.0.0.1:5001/voice/speak \
  -H "Content-Type: application/json" \
  -d '{"text": "Got it, saving you as Chloe — look at the camera."}'
curl -s http://127.0.0.1:5001/voice/status

# Snapshot (for Flow B)
curl -s "http://127.0.0.1:5001/camera/snapshot?save=true"
```

## Photo source by channel

| Channel | Where to read the path |
|---|---|
| Telegram (with photo) | `mediaPaths` in conversation context |
| Web chat (with image) | `[image: /path/to/file]` tag in message text |
| Voice / Telegram-text (Flow B) | `path` returned by `GET /camera/snapshot?save=true` |
| Familiar-stranger (Flow C) | `<path>` parsed from the HAL hint in the sensing message |

## Error handling
- **503** from any face endpoint → recognizer is down (sensing not started). Tell the user face recognition is offline.
- **400 "image cannot be decoded"** → bad base64 / corrupt file. Apologize, ask user to re-send (Flow A) or retry capture (Flow B).
- **400 "no face detected"** → no face in the image. Apologize and either ask the user to face the camera (Flow B retry) or ask for a clearer photo (Flow A).
- **404** on `/face/remove` → that label isn't enrolled. Tell the user.
