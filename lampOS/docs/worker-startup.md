# Private socket startup

A full offline voice test run exposed a startup race before any microphone or
cloud work: `Worker::spawn` failed with `peer must be a private socket owned by
this user`. The parent waited only for both worker pathnames to exist.
`Endpoint::bind` creates a Unix socket and then sets its mode to 0600, so the
path can exist before that final permission change. Connecting in that interval
correctly fails the endpoint's privacy check.

The parent now waits for both paths to be actual sockets owned by its effective
user, with no group/other access. A missing path or a socket still becoming
private is not ready. A regular file, symlink or different owner remains an
error. A permanently unprivate socket times out; it is never accepted.

The original three-second startup deadline, five-millisecond poll interval,
child-exit checks and supervisor heartbeat callback remain unchanged. This
wait happens before listening readiness. Final `Endpoint::connect` validation
and every runtime transport freshness check still run normally. There is no
permission relaxation, process-wide umask change, or reconnect of a partially
connected channel pair.

Deterministic tests exercise missing paths, both non-private sockets, only one
private socket, the fully ready pair, files and symlinks. Existing real child
handshake and fixture-provider tests cover the completed startup path. The
combined source's commands and receipts are recorded in
[provider flow control](provider-flow-control.md); host results are not a
Linux/ARM64 or physical-device qualification.
