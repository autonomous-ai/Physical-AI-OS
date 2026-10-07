# Flow B — Self-enroll via camera capture (no photo supplied)

User asks the device to remember their face WITHOUT sending a photo. Works for:

- **Voice** — user is in front of the device, says "remember my face", "I'm Gray".
- **Telegram chat (text only)** — "take a photo and remember me", "capture and enroll me". User assumed to be in front of the device (or at least someone is — they're explicitly asking the camera to capture).

Do NOT activate on **web chat without a photo** — the web user may not be in front of the device at all (remote browser). Ask them to send a selfie instead (Flow A).

The agent grabs a snapshot via `/camera/snapshot` and uses it for `/face/enroll`. This counts as **self-enrollment** because the user is the subject — not naming someone else.

## Trigger
Voice or text expressing intent to be remembered, no photo attached:

"remember my face", "enroll my face", "save my face", "capture and enroll me", "take a photo and remember me", "I'm <Name>" combined with intent ("...remember that").

Do NOT activate this flow when:
- The message has a photo attached → use Flow A instead.
- The current sensing message has a familiar-stranger hint → use Flow C instead (the user may be naming the prompted face, not themselves).
- The channel is web chat without a photo → ask for a selfie (Flow A); see warning above.

## Steps

1. Extract the **name**:
   - Prefer name spoken in the message ("I'm Gray").
   - **Voice transcript with `Speaker - <Name>:` prefix** (speaker already recognized): use that name. The user is asking to refresh/add a face for an already-known identity.
   - Voice without name and `[context: current_user=<known>]` is set: do NOT auto-use `current_user` (the user is asking to enroll, which means they're not yet recognized — `current_user` is likely `unknown` or stale). Ask: "What name should I save you under?"
   - Telegram without name → fall back to sender prefix (`[telegram:Gray]` → `gray`); confirm with the user before enrolling.
   - If still unclear, ask once.
2. **Announce the capture out loud BEFORE the snapshot — as a tool call, not in your reply.** Your reply text is only spoken after every tool call in the turn has finished, so a read-back written in the reply plays after the photo was already taken (2026-10-05, green-lamp: snapshot at +9 s, first spoken word at +26 s). Instead:
   - Call `POST /voice/speak` with the name read-back and a camera cue, e.g. `{"text": "Got it, saving you as {Name} — look at the camera."}`. The read-back still gives the user a chance to correct a mishearing.
   - Wait until it has played: poll `GET /voice/status` until `tts_speaking` is `false` (about every 0.5 s, give up after ~8 s), then pause ~1 s so the user can face the lens.
   - If `/voice/speak` returns `{"status": "suppressed"}` (speaker muted) or HTTP 409 (music playing), do NOT capture silently: stop and tell the user in your reply that you need them to unmute the speaker / pause the music so you can warn them before taking the photo.
3. Call `GET /camera/snapshot?save=true` and read `path` from the JSON response. Do NOT check `/camera` status first — the snapshot endpoint auto-enables the camera.
4. Base64-encode the saved image at `path`.
5. Call `POST /face/enroll` with `image_base64`, `label` (lowercase). Telegram identity:
   - **Voice path:** omit (no Telegram metadata available).
   - **Telegram path:** include `telegram_username` + `telegram_id` from message context (required for DM targeting).
6. Confirm enrollment to the user with the new `enrolled_count` in your normal reply. Do not repeat the camera cue there — it was already spoken.

## Error handling specific to this flow

- `/camera/snapshot` returns 503 → tell the user the camera is offline; do not retry blindly.
- `/face/enroll` returns 400 with "no face detected" → the snapshot didn't capture a face. Apologize, ask the user to face the camera, and retry once via a fresh `/camera/snapshot` call.

## Example (voice)

```
User: "Remember my face — I'm Gray."
Agent (turn 1):
  → POST /voice/speak {"text": "Got it, saving you as Gray — look at the camera."}
  → GET /voice/status → {"tts_speaking": false, ...}   (poll until false, then ~1 s pause)
  → GET /camera/snapshot?save=true → {"path": "/root/.openclaw/media/hal-snapshots/snap_171xxx.jpg"}
  → POST /face/enroll {"image_base64": "...", "label": "gray"}
  Reply: "Done — I'll remember you as Gray now."
```

## Example (Telegram, no photo)

```
User (Telegram): "Take a photo and enroll me, I'm Gray."
Agent:
  → POST /voice/speak {"text": "Got it, saving you as Gray — look at the camera."}   (someone is in front of the device; warn them)
  → GET /voice/status until tts_speaking is false
  → GET /camera/snapshot?save=true
  → POST /face/enroll {"image_base64": "...", "label": "gray", "telegram_username": "gray_dev", "telegram_id": "98765"}
  → confirm: "Captured and saved your face as Gray. Your Telegram is linked too."
```

## Notes
- **Don't narrate technical details** — say "looking now" not "calling /camera/snapshot".
- **Already-enrolled = add a fresh photo, don't refuse.** If the label is already in `/face/status`, treat the request as "refresh the face sample" — `/face/enroll` appends another JPEG to `/root/local/users/<label>/`, which keeps the embedding average up to date as appearance changes (haircut, beard, glasses). Reply matter-of-factly: "Updated your photo, Gray." instead of "You're already enrolled."
- **Pairs with speaker-recognizer.** Voice "remember my face, I'm Gray" almost always co-fires `speaker-recognizer` (Branch B / multi-turn combine). Use the SAME lowercase label so the face JPEG and voice WAV both land in `/root/local/users/<label>/`. One spoken confirm covers both: "Got you, Gray — face and voice both remembered."
