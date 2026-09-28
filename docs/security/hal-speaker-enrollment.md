# Speaker enrollment privacy

`POST /speaker/record-enroll` returns 409 when software mute or the hardware mic switch blocks capture. It rechecks after releasing the voice listener and after capture; a newly muted recording is discarded before enrollment. The listener restarts only if it was running before enrollment and the microphone remains unmuted. Duration remains bounded to 1–60 seconds. This does not promise immediate interruption of an already running recorder.
