# Audio diagnostic privacy

`POST /audio/record` returns 409 when software mute or the hardware mic switch blocks capture. Duration is limited to 100–30000 ms; invalid values return 422. If mute is observed after capture, the recording is discarded. `POST /audio/play-tone` returns 409 for speaker mute or privacy lock. Tone duration is 1–5000 ms and frequency is 20–20000 Hz (422 outside these bounds). These checks apply before opening audio devices, including simulation. They do not promise immediate cancellation of a recording already in progress.

Diagnostic tone behavior during quiet hours is unchanged; the existing music quiet-hours policy is not broadened by this fix.
