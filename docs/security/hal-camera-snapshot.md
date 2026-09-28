# Camera snapshot privacy

`GET /camera/snapshot` returns 409 when the user manually disabled the camera or the physical privacy lock is active. Automatic camera pauses can still temporarily start capture. Snapshot requests serialize temporary start/capture/stop so one request cannot stop another snapshot. The snapshot mutex does not hold the privacy lock; manual/physical disable remains available during capture, and a disabled result is discarded. If another action enabled the camera during capture, snapshot cleanup does not stop it.
