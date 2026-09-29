# DL Backend

GPU-accelerated perception backend for Autonomous devices. It runs the
deep-learning models a device's HAL can't run locally (action recognition, facial
and speech emotion, pose + ergonomics, object detection, speaker embedding) and
exposes them over WebSocket and HTTP behind an optional encrypting load balancer.

## Documentation

| Doc | Covers |
|-----|--------|
| [docs/README.md](docs/README.md) | Overview, component map, ports, quick start |
| [docs/architecture.md](docs/architecture.md) | Process topology, ports, URL prefixes, request lifecycle |
| [docs/api.md](docs/api.md) | Every endpoint: method, path, request/response schema, auth |
| [docs/perceptions.md](docs/perceptions.md) | The perception subsystems, models, enums, output types |
| [docs/crypto-and-loadbalancer.md](docs/crypto-and-loadbalancer.md) | `lbserver` round-robin proxy + RSA/AES encryption + nginx |
| [docs/configuration.md](docs/configuration.md) | All environment variables with defaults |
| [docs/deployment.md](docs/deployment.md) | Install, Makefile targets, watchdog, RunPod, zero-downtime deploy |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Diagnosing an outage or a failed deploy |

The platform-level overview lives at [`docs/perception-service.md`](../../docs/perception-service.md)
(and the Vietnamese [`docs/vi/perception-service_vi.md`](../../docs/vi/perception-service_vi.md)).

## Quick start (single node, no encryption)

```bash
cd integrations/perception-service
# install deps (see pyproject.toml / Dockerfile for the CUDA stack)
export DL_API_KEY=dev-secret
python -m dlserver --host 0.0.0.0 --port 8001
```

```bash
curl -H "X-API-Key: dev-secret" http://localhost:8001/hal/api/dl/health
```

For the full proxied + encrypted stack (nginx → lbserver → dlserver), see
[docs/architecture.md](docs/architecture.md) and
[docs/crypto-and-loadbalancer.md](docs/crypto-and-loadbalancer.md).

## Updating the code on the server

New dlserver code (models, preprocessing, routes) deploys without an outage:

```bash
cd /workspace/autonomous-os/integrations/perception-service
git pull
nohup make deploy-dlserver > /dev/null 2>&1 &   # or run it in tmux
tail -f /workspace/logs/deploy/deploy.log       # until "done: serving from <port>"
```

Use the in-place restart (`make start-runpod-master`, a short outage) instead
when `pyproject.toml` changed, when lbserver changed, or for the first start
after installing the two-slot deploy. Details:
[docs/deployment.md](docs/deployment.md#zero-downtime-deploy-two-slots); when a
deploy refuses or fails: [docs/troubleshooting.md](docs/troubleshooting.md#8-a-deploy-failed-or-refused).
</content>
