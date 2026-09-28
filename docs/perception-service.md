# DL Backend

GPU-accelerated perception backend for Autonomous devices. It runs the
deep-learning models a device's HAL can't run locally (action recognition, facial
and speech emotion, pose + ergonomics, object detection, speaker embedding) and
exposes them over WebSocket and HTTP behind an optional encrypting load balancer.

This page is the **platform overview** — what the backend is and where it sits.
The **full reference lives next to the code** in
[`integrations/perception-service/docs/`](../integrations/perception-service/docs/); schemas, model lists, endpoints and env
vars stay there, not here.

| Want… | Read |
|-------|------|
| Process topology, ports, URL prefixes, request lifecycle | [`integrations/perception-service/docs/architecture.md`](../integrations/perception-service/docs/architecture.md) |
| Every endpoint with request/response schemas | [`integrations/perception-service/docs/api.md`](../integrations/perception-service/docs/api.md) |
| Perception models, enums, output types | [`integrations/perception-service/docs/perceptions.md`](../integrations/perception-service/docs/perceptions.md) |
| Load balancer + RSA/AES encryption + nginx | [`integrations/perception-service/docs/crypto-and-loadbalancer.md`](../integrations/perception-service/docs/crypto-and-loadbalancer.md) |
| Deploy: install, GPU scaling, RunPod, Docker, TLS | [`integrations/perception-service/docs/deployment.md`](../integrations/perception-service/docs/deployment.md) |
| All environment variables with defaults | [`integrations/perception-service/docs/configuration.md`](../integrations/perception-service/docs/configuration.md) |

## Architecture at a glance

Two FastAPI processes behind an nginx front door:

```
HAL / clients
   │  https / wss  :8899
   ▼
┌─────────┐   /lelamp/ → /hal/      ┌──────────┐  round-robin   ┌──────────┐
│  nginx  │ ─────────────────────▶ │ lbserver │ ─────────────▶ │ dlserver │
│  :8899  │     (WS upgrade)        │  :7999   │  decrypt →      │  :8001   │
└─────────┘                         └──────────┘   plaintext     └──────────┘
```

- **`dlserver`** (`:8001`) — loads the ML models, serves the perception endpoints.
- **`lbserver`** (`:7999`) — round-robin proxy over one or more `dlserver`
  instances; terminates RSA+AES encryption so `dlserver` stays plaintext.
- **`nginx`** (`:8899`) — public front door; maps the device-facing `/lelamp/`
  prefix onto the internal `/hal/` prefix and upgrades WebSockets.

For single-node dev you can hit `dlserver:8001` directly with encryption off.

## What it provides

Perception subsystems exposed to devices: action recognition, facial emotion,
speech emotion (SER), pose estimation (with RULA ergonomics), object detection and
speaker embedding. `/emotion-recognize` accepts `raw: true`, which returns the
ungated argmax and every class probability; HAL uses it and applies the
per-label gate on the device. Face and person detection run internally to feed
those pipelines. Requests from concurrent sessions are batched before GPU dispatch —
tune `BATCH_SIZE` and `BATCH_TIMEOUT` per model. SER input is bounded to 2–8 s
at batch 1 server-side, which stays inside the cached TensorRT engine's shape
range, so no clip length can trigger an engine rebuild during a request. Model choices and outputs:
[`integrations/perception-service/docs/perceptions.md`](../integrations/perception-service/docs/perceptions.md).

## Using it from a device

HAL is the main client. Point it at the backend with `DL_BACKEND_URL` and the
shared `DL_API_KEY` (sent as `X-API-Key`), and optionally enable client-side
encryption. Sensing streams camera frames to the action/pose/emotion endpoints;
voice posts end-of-utterance audio to the speech-emotion endpoint. For the
speaker-embedding path, HAL runs the audio filter/VAD/normalize pipeline
**on-device** and calls `/audio-recognizer/embed` with `preprocess=false`, so
the server only computes the embedding (the endpoint is embed-only and defaults
to `preprocess=false`; pass `true` only to upload raw audio for server-side
cleaning). HAL also sets `use_sliding_window`: enroll sends `false` so the whole
reference is embedded in a single shot (one clean vector), while recognize sends
`true` to slide overlapping windows and vote per chunk. The embed response (and
`/health`) also report an
`embed_model_version` fingerprint of the loaded weights, so HAL can tell when the
server model changed and re-embed its stored voice profiles instead of matching
against stale vectors (see
[`robots/lamp/docs/speaker-enrollment.md`](../robots/lamp/docs/speaker-enrollment.md)). Exact endpoints
and payloads are in [`integrations/perception-service/docs/api.md`](../integrations/perception-service/docs/api.md); every
config knob is in
[`integrations/perception-service/docs/configuration.md`](../integrations/perception-service/docs/configuration.md).

## Deployment

`integrations/perception-service/` ships a `Dockerfile` (CUDA + nginx), `nginx.conf` / `nginx-ssl.conf`
and a `Makefile`; the two processes launch via `python -m dlserver` and
`python -m lbserver`. Install, single-node vs master/slave GPU scaling, RunPod,
Docker and TLS: [`integrations/perception-service/docs/deployment.md`](../integrations/perception-service/docs/deployment.md).

---

> **Doc-sync:** this page is an overview only. When code changes, update the
> detailed docs under [`integrations/perception-service/docs/`](../integrations/perception-service/docs/) **and** the
> Vietnamese overview [`docs/vi/perception-service_vi.md`](vi/perception-service_vi.md). Code is the
> source of truth.
</content>
