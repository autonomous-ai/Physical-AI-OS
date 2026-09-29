# Troubleshooting

How to work out why the stack stopped serving, and which of the several very
different causes you are actually looking at. All paths assume a RunPod master
node; adjust `/workspace` for other hosts.

Run everything from the repo root — several checks read `.env`:

```bash
cd /workspace/autonomous-os/integrations/perception-service
```

> `/workspace/autonomous-os/dlbackend/` is a stale pre-rename copy of the source
> (no Makefile). Never run from it. See
> [migration-dlbackend-rename.md](migration-dlbackend-rename.md).

## Symptom → likely cause

| Symptom | Most likely cause | Jump to |
|---------|-------------------|---------|
| Clients get 502, GPU shows 0 MiB used | dlserver not running at all | [1](#1-what-is-actually-running) |
| Clients get 401 | Wrong/missing `X-API-Key`, service is fine | [1](#1-what-is-actually-running) |
| Service vanished, no error in logs | Container was recreated | [4](#4-was-the-container-recreated) |
| Service restarts every few seconds | Application crash loop | [3](#3-crash-or-group-kill) |
| Process killed with no traceback | Out of memory | [5](#5-out-of-memory) |
| `make` fails on start | Disk full on `/` | [6](#6-disk) |
| `make deploy-dlserver` prints `ABORT: …` | A refusal or a failed new slot; traffic did not move | [8](#8-a-deploy-failed-or-refused) |
| dlserver runs on both 8001 and 8002 after a deploy | Deploy interrupted, or its rollback was not confirmed | [8](#8-a-deploy-failed-or-refused) |

## 1. What is actually running

```bash
make info                      # dlserver / lbserver / nginx + PIDs
ps -eo pid,lstart,etime,rss,args | grep -E "[d]lserver|[l]bserver|run-with-restart"
ss -lntp | grep -E "8899|8001|8002|7999"
cat /tmp/dlserver-active       # serving dlserver slot; no file = 8001
```

`make info` reads the PID files, which can be stale. Trust `ps` and `ss` over it
when they disagree. After a [two-slot deploy](deployment.md#zero-downtime-deploy-two-slots)
the serving slot may be `8002`; `make info` shows only the serving slot.

Then check whether the stack is actually serving, not just listening:

```bash
API_KEY=$(grep -E '^DL_API_KEY=' .env | head -1 | cut -d= -f2-)
curl -s -H "X-API-Key: $API_KEY" http://127.0.0.1:8899/hal/api/dl/health
```

| Response | Meaning |
|----------|---------|
| `{"status":"ok","models":{…}}` | Healthy, models loaded |
| `401 Invalid or missing API key` | **Service is up** — the auth layer answered. Your key is wrong |
| `502` from nginx | nginx is up, dlserver/lbserver behind it is down |
| Connection refused / empty | Nothing is listening — nginx is down too |

A 401 is a liveness signal, not a failure. Only the `200` body confirms models
are actually loaded, which is why the autostart probe checks for it.

## 1b. Triaging a reported 5xx

Start from the request id in the report. One command decides which layer to look at:

```bash
grep -r "$REQUEST_ID" /var/log/nginx/ /workspace/logs/ || echo "NEVER REACHED THIS HOST"
```

| nginx access line | Meaning | Next step |
|---|---|---|
| *(no match anywhere)* | Never arrived -- died at the caller, Cloudflare or the ingress | Nothing to do here. Check the caller's own logs. |
| `client=504 upstream=-` | Backend **hung**: accepted the connection, never replied | `cat /proc/$(cat /tmp/dlserver.pid)/wchan` -- `pipe_write` means a frozen event loop |
| `client=502 upstream=-` | Backend **down**: connect refused | Did it restart? Check `watchdog.log` |
| `client=200 upstream=200` | Healthy here | Failure is downstream -- client timeout or transport |

`urt=` is the upstream response time; compare it against the ladder in
[deployment.md](deployment.md#timeout-ladder).

A full access line carries the timestamp, both status codes, both timings and the
request id -- enough to bound an incident in time without correlating against the
service logs:

```
1.2.3.4 [21/Aug/2026:08:15:21 +0000] lelamp-rp.autonomous.ai "POST /api/dl/owlv2 HTTP/1.1" \
  client=200 upstream=200 rt=0.393 urt=0.373 addr=127.0.0.1:7999 rid=79b0250114c29f05
```

## 1c. A log file stopped growing

Both servers use `ResilientRotatingFileHandler` (`src/core/logging_ext.py`). The
stock handler never reopens a dead stream, so one failed write silenced a log
until the next restart -- `lbserver.log` stopped at 07:59:35 on 2026-08-17 and
stayed dead for the rest of the day while its mtime kept advancing.

On a write failure the handler now closes and reopens; if the file itself is
unwritable it rolls over **once** to a fresh inode; if that still fails it mutes
for `retry_after` (30s) and tries again. Failure notices on stderr are
rate-limited to one per window instead of a ~40-line traceback per record.

So a log that is *permanently* frozen now means the fault outlasted every retry.
Check:

```bash
# is it really frozen, or just quiet?
stat -c '%s %y' /workspace/logs/lbserver/lbserver.log     # sample twice

# NUL bytes at the tail = the storage lost that region (reads return zeros)
S=$(stat -c %s FILE); N=$(tr -d '\000' < FILE | wc -c); echo "NUL=$((S-N))"

# read what survives -- less/editors call it binary and garble it
tr -d '\000' < FILE | less

# is the fd still on a live inode, or a deleted one?
ls -l /proc/$(cat /tmp/lbserver.pid)/fd | grep -a workspace
```

| Symptom | Meaning |
|---|---|
| fd shows `(deleted)` | An unlinked-but-open file. Should not happen since the handlers were merged -- if it does, the double-handler bug is back |
| fd on a live inode, NUL tail, size frozen | Storage-side fault on that file's backing chunks. Not preventable from the app; the handler should have rolled over |
| `[logging] write failed` on stderr | The handler is retrying. One line per 30s is normal during a fault |

> `/workspace` is MooseFS. The client tools that would identify a bad chunk
> (`mfsfileinfo`, `mfscheckfile`) are **not installed** -- worth adding, since
> without them a storage fault can only be inferred from symptoms.

## 2. Reading the logs

Logs are plain text files. No decoding is needed:

```bash
tail -100 /workspace/logs/dlserver/watchdog.log
tail -200 /workspace/logs/dlserver/stderr.log
tail -100 /workspace/logs/lbserver/watchdog.log
tail -50  /workspace/logs/autostart/autostart.log
```

| File | Contents |
|------|----------|
| `<svc>/stdout.log` | server stdout |
| `<svc>/stderr.log` | server stderr (library warnings, tracebacks) |
| `<svc>/watchdog.log` | restart events from `run-with-restart.sh` |
| `<svc>/<svc>.log` | application log (`RotatingFileHandler`, 1 MB × 3) |
| `<svc>/uvicorn.log` | uvicorn error **and** access log (one shared handler) |

Rotation keeps 3 generations as `.1`, `.2`, `.3`:

- `stdout/stderr/watchdog.log` are rotated by `run-with-restart.sh` — once at
  startup, and whenever a file exceeds `MAX_LOG_BYTES` (default 8 MiB, checked
  every `GUARD_INTERVAL` seconds, default 60). The size guard **copies then
  truncates in place**; it must never rename, because the server holds an
  `O_APPEND` fd and would keep writing to the renamed inode.
- `<svc>.log` and `uvicorn.log` are rotated by Python's `RotatingFileHandler`.

> **Historical note.** Before 2026-08-18 these were
> [multilog](https://cr.yp.to/daemontools/multilog.html) *directories* (`stdout/`,
> `stderr/`, `watchdog/`) whose live file was `current`, TAI64N-prefixed and read
> via `tai64nlocal`. Those directories may still exist with old data — decode them
> with `tai64nlocal` as before. They are no longer written to.
>
> multilog was removed because its documented reaction to a write error is to
> *pause and retry forever*, which stops it draining its input pipe. The server on
> the other end then blocks in `pipe_write` with no timeout and no way to run signal
> handlers — and that writer is the asyncio event loop. This froze lbserver on
> 2026-08-10 and dlserver on 2026-08-17. A plain file redirect cannot block the
> writer: a failed write returns an error instead.

## 3. Crash, or group kill?

**The watchdog log answers this, and it is the single most useful check.**
[run-with-restart.sh](../scripts/run-with-restart.sh) writes
`[watchdog] Process exited (code=N), restarting in 5s...` on *every* crash.

```bash
ls -l /workspace/logs/dlserver/watchdog/
cat /workspace/logs/dlserver/watchdog/current | tai64nlocal
```

| What you see | What it means | Next step |
|--------------|---------------|-----------|
| Repeated `Process exited (code=N)` | Application **crash loop**. The watchdog is doing its job | Read `stderr/current` for the traceback |
| `code=137` | SIGKILL — almost always OOM | [5](#5-out-of-memory) |
| `code=1` + traceback | Application error (missing model, port in use, bad config) | Fix the traceback |
| **Log is 0 bytes / empty** | The process **never exited while being watched**. The watchdog died at the same instant | [4](#4-was-the-container-recreated) |

An empty watchdog log across *every* generation is conclusive: the service did
not crash. Something killed the whole tree at once — a container recreate, an
OOM kill of the cgroup, or a manual `kill` of the process group.

## 4. Was the container recreated?

RunPod recreates containers without notice for host maintenance or migration.
This kills the watchdog too, so nothing restarts anything.

```bash
uptime -s                        # NOTE: usually the HOST's boot time, not this container's
ps -o lstart= -p 1               # PID 1 birth = this container's real start time
```

Use `ps -o lstart= -p 1`. Inside Docker, `/proc/uptime` is generally the host
kernel's, so `uptime -s` can be days off from when your container started.

If PID 1 is younger than your process's last log line, the container was
recreated and that is your answer.

Confirm against RunPod's own control plane:

```bash
K=$(tr '\0' '\n' < /proc/1/environ | grep '^RUNPOD_API_KEY=' | cut -d= -f2)
P=$(tr '\0' '\n' < /proc/1/environ | grep '^RUNPOD_POD_ID='  | cut -d= -f2)
curl -s -X POST "https://api.runpod.io/graphql?api_key=$K" \
  -H 'Content-Type: application/json' \
  -d "{\"query\":\"query { pod(input: {podId: \\\"$P\\\"}) { desiredStatus lastStatusChange runtime { uptimeInSeconds } } }\"}"
```

Read the result like this:

- **`lastStatusChange`** — the last *user* action ("Rented by User: …"). If this
  is weeks old, nobody stopped or resumed the pod.
- **`runtime.uptimeInSeconds`** — how long the current runtime has been up.
  Subtract from now to get the restart moment.
- A restart moment with **no matching user action** means RunPod restarted it,
  not you. Nothing on the pod will tell you why; check the console's pod events.

The pod reports `desiredStatus: RUNNING` and keeps billing throughout, and SSH,
Jupyter and nginx all still answer — so nothing looks wrong from outside while
the service is entirely gone. See
[deployment.md#autostart-after-a-container-recreate](deployment.md#autostart-after-a-container-recreate)
for the mechanism that prevents this.

## 5. Out of memory

**`free -h` is misleading inside a container** — it reports the host's RAM (e.g.
503 GiB) while you are limited to a fraction of it. Read the cgroup instead:

```bash
cat /sys/fs/cgroup/memory/memory.limit_in_bytes      # your real ceiling
cat /sys/fs/cgroup/memory/memory.usage_in_bytes      # current
cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes  # high-water mark
cat /sys/fs/cgroup/memory/memory.failcnt             # times the limit was hit
cat /sys/fs/cgroup/memory/memory.oom_control         # oom_kill counter
# cgroup v2: memory.max / memory.current / memory.events
echo $RUNPOD_MEM_GB                                  # RunPod's advertised limit
```

| Reading | Meaning |
|---------|---------|
| `failcnt` > 0 or `oom_kill` > 0 | The limit *was* hit — OOM is your cause |
| `max_usage` close to `limit` | No kill yet, but you are one request away |
| Both counters 0 | No OOM **since this container started** — they reset on recreate |

`dmesg -T | grep -i -E "oom|killed process"` gives host-side confirmation with
timestamps, but is often unreadable from inside an unprivileged container.

The single biggest consumer is dlserver (~16 GB RSS with all models loaded).
Running a second dlserver for testing roughly doubles that — check for stray
instances before blaming a leak:

```bash
ps -eo pid,rss,args --sort=-rss | head -5
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
```

## 6. Disk

Two filesystems with very different behaviour:

| Mount | Type | Size | Holds |
|-------|------|------|-------|
| `/workspace` | MooseFS network volume | huge | Repo, `.env`, logs, models. **Persists** across container recreates |
| `/` | Container overlay | 50 GB | OS, pip packages, `~/.cache`, `/post_start.sh` |

```bash
df -h / /workspace
du -sh /root/.cache /root/.vscode-server /root/.cursor-server 2>/dev/null
```

`/` is the one that fills up. `make start-runpod-master` runs `install` first,
which pip-installs the CUDA/TensorRT stack ([deployment.md#install](deployment.md#install)) —
if `/` is full, startup fails before anything serves. Deleting large directories
on `/workspace` does **not** help `/`; they are separate disks.

## 7. GPU

```bash
nvidia-smi
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
```

**0 MiB used means no dlserver is running** — the models are the only thing that
allocates GPU memory. It is a symptom of [1](#1-what-is-actually-running), not a
GPU fault. If `nvidia-smi` itself fails or hangs, the driver or device is the
problem and no restart will help until the pod is recreated.

## 8. A deploy failed or refused

`make deploy-dlserver` logs to `/workspace/logs/deploy/deploy.log`. Every
`ABORT: … -- traffic unchanged` line means devices are still served by the old
slot, and a new slot it started has already been stopped. Slot logs:
`/workspace/logs/dlserver/` (8001), `/workspace/logs/dlserver-8002/` (8002).
lbserver logs each switch as a `[switch]` line in
`/workspace/logs/lbserver/lbserver.log`.

| Message | Meaning | Do |
|---------|---------|----|
| `another deploy is running` | Another `deploy-dlserver` holds the lock | Wait for it; check with `ps -ef \| grep [d]eploy-dlserver` |
| `nothing listens on :7999 (lbserver down)` | lbserver is not running | `make start-runpod-lbserver`, then deploy again |
| `lbserver (pid N) has not acked /tmp/dlserver-active, or its ack does not name slot …` | lbserver predates the switch or runs without `LB__STATE_FILE` | `make start-runpod-lbserver` (a few seconds of 502), then deploy again |
| `active slot N is not running` | The serving slot is down: an outage, not a deploy problem | [Recovering](#recovering) |
| `idle port N is in use` | A slot is left over from an interrupted deploy | Confirm it is not the serving one (`cat /tmp/dlserver-active`), then `make stop-runpod-dlserver DLSERVER_PORT=N` |
| `no /tmp/dlserver….rev: unknown commit on slot N` | The serving slot was started by a build without this feature | One in-place restart: `make start-runpod-master` |
| `pyproject.toml changed since slot N started` | Dependency change (packages, TensorRT, ONNX Runtime) | In-place restart: `make start-runpod-master` |
| `cannot compare pyproject.toml with <rev> (git exit N)` | git no longer knows the commit the slot started from | In-place restart: `make start-runpod-master` |
| `only N MB GPU free, a second copy needs 12000` | No room for a second copy on the GPU | Check `nvidia-smi` for other GPU users; otherwise in-place restart |
| `cannot read /hal/api/dl/health on active slot N` | The serving slot does not answer health (or the API key is wrong) | [1](#1-what-is-actually-running) |
| `slot N is up but missing models: …` | The new code failed to load a model | Read the new slot's `stderr.log`; fix the code |
| `slot N not ready after 900s` | The new slot crash-loops or loads too slowly | Read the new slot's `stderr.log` and `watchdog.log` |
| `lbserver did not confirm the switch; state file restored to N` | lbserver did not apply the switch; it was rolled back | Check the `[switch]` lines in `lbserver.log`, then deploy again |
| `rollback NOT confirmed` | Both slots are left running; lbserver may use either | Below |
| `interrupted after the switch: traffic is on N` | Deploy stopped after the switch | Run the `make stop-runpod-dlserver DLSERVER_PORT=…` it prints |
| `ERROR: old slot N did not stop cleanly` | Traffic is on the new slot; the old one survived | Run the stop command it prints; read the `stop-tree` output above it |

**Both slots running.** Find the slot lbserver really uses, keep it, stop the
other:

```bash
API_KEY=$(grep -E '^DL_API_KEY=' .env | head -1 | cut -d= -f2-)
cat /tmp/dlserver-active            # slot the files say is serving
cat /tmp/dlserver-active.applied    # what lbserver applied: {"pid": …, "backends": […]}
curl -s -H "X-API-Key: $API_KEY" http://127.0.0.1:8001/hal/api/dl/health
curl -s -H "X-API-Key: $API_KEY" http://127.0.0.1:8002/hal/api/dl/health
```

If both files name the same healthy slot, stop the other one
(`make stop-runpod-dlserver DLSERVER_PORT=<other>`). If they disagree, point
lbserver at the healthy slot first and check it acked, then stop the other:

```bash
printf 'http://127.0.0.1:%s\n' 8001 > /tmp/dlserver-active    # the healthy slot
kill -HUP "$(ss -lntpH 'sport = :7999' | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)"
cat /tmp/dlserver-active.applied                              # must name that slot
make stop-runpod-dlserver DLSERVER_PORT=8002                  # the other slot
```

To undo a bad deploy, deploy the previous commit:
`git checkout <sha> && make deploy-dlserver`.

## Recovering

```bash
cd /workspace/autonomous-os/integrations/perception-service
make stop-runpod-dlserver DLSERVER_PORT=8001
make stop-runpod-dlserver DLSERVER_PORT=8002
make stop-runpod-lbserver stop-nginx
make start-runpod-master
make info
```

Always stop before starting. Stop both dlserver slots: a bare
`make stop-runpod-dlserver` stops only the serving slot. `make start-runpod-master`
restarts the slot named in `/tmp/dlserver-active` (8001 if there is none).

> The stop targets still `rm -f /workspace/logs/*/stderr/lock`. That is now a
> no-op left over from multilog and will be removed — the lock files are not
> recreated. Note the stop targets only kill the PIDs recorded in `/tmp/*.pid`,
> so multilogs or other tree members started by an older build can survive as
> orphans; check with
> `ps -eo pid,ppid,args | grep -aE 'multilog|run-with-restart'` before starting.

Two things that silently produce a *misconfigured* server rather than a failure:

- **Run `make` from the repo root.** `config.py` declares `env_file=".env"` as a
  relative path, so starting from any other directory makes every setting fall
  back to its default.
- **`LB__BACKENDS` and `DL_API_KEY` come from `.env`**, not your shell. If you
  export them by hand, make sure they match `.env` or you will get a stack whose
  load balancer points somewhere unexpected.
