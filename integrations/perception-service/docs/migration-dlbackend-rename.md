# Migration: `dlbackend/` → `integrations/perception-service/`

**Date:** 2026-07-20
**Scope:** The GPU perception backend source tree moved from the repo-root
`dlbackend/` directory to `integrations/perception-service/`. This is a
directory move **plus** a rename of the service concept from `dlbackend` to
`perception-service`. This note tracks every setup/config/doc change made so
the change and its operational impact are auditable.

> The `dlserver` and `lbserver` Python module names are **unchanged** — only
> the enclosing directory and the `dlbackend` brand name were renamed.

---

## 1. Deployment-breaking change (had to fix)

| File | Line | Before | After |
|------|------|--------|-------|
| `Makefile` | `DEPLOY_DL` | `$(DEPLOY_ROOT)/dlbackend` | `$(DEPLOY_ROOT)/integrations/perception-service` |

The `deploy-master` / `deploy-slave` targets clone the repo to
`$(DEPLOY_ROOT)` (`/workspace/autonomous-os`) then `cd $(DEPLOY_DL)`. After the
source move, the old path no longer exists, so **every** deploy step (`make
install`, `.env` copy, health check) targeted a dead directory. This is the
only change that outright broke automated deployment.

## 2. Docker image rename

| File | Before | After |
|------|--------|-------|
| `docker-compose.yml` | `tnk2908/dlbackend` | `tnk2908/perception-service` |
| `docker-compose.slave.yml` | `tnk2908/dlbackend` | `tnk2908/perception-service` |
| `Dockerfile` (comment) | `... dlbackend` | `... perception-service` |
| `docker-compose.yml` (header comment) | `... dlbackend` | `... perception-service` |

The master builds locally (`build: .`) so it is unaffected until pushed. **Slaves
pull the image by name** (no local build), so they will fail until the renamed
image is published — see operational steps below.

## 3. nginx temp config filename

| File | Before | After |
|------|--------|-------|
| `Makefile` (`start-nginx`, `start-nginx-ssl`) | `/etc/nginx/dlbackend-nginx[-ssl].conf` | `/etc/nginx/perception-service-nginx[-ssl].conf` |
| `scripts/docker-entrypoint.sh` | `/etc/nginx/dlbackend-nginx.conf` | `/etc/nginx/perception-service-nginx.conf` |

Self-contained (the `cp` target and `nginx -c` arg use the same name). No
operational action required.

## 4. Runtime cache / key directories

| File | Setting | Before | After |
|------|---------|--------|-------|
| `src/config.py` | `CryptoSetting.key_dir` | `~/.dlbackend/keys` | `~/.perception-service/keys` |
| `src/config.py` | `Settings.cache_dir` | `~/.cache/dlbackend` | `~/.cache/perception-service` |
| `src/config.py` | `Settings.model_cache_dir` | `~/.cache/dlbackend/models` | `~/.cache/perception-service/models` |
| `.env.example` | commented `CACHE_DIR` / `MODEL_CACHE_DIR` hints | `~/.cache/dlbackend...` | `~/.cache/perception-service...` |

⚠️ **These have live-server impact** — see operational steps. In Docker the
container overrides `MODEL_CACHE_DIR=/workspace/models` (a mounted volume), so
**containerized deployments are unaffected** by the model-cache rename.

## 5. Documentation

Path prefixes (`dlbackend/src/...`), `cd dlbackend` commands, cache paths, the
conceptual backend name, and one pre-existing broken relative link were updated
across:

- `integrations/perception-service/docs/` — `deployment.md`, `configuration.md`,
  `perceptions.md`, `README.md`
- `docs/face-emotion/` + `docs/vi/face-emotion/` (EN + VI) — these doc sets have
  since been removed; facial emotion is now covered in
  [perceptions.md § Facial emotion (FER)](perceptions.md#2-facial-emotion-fer)
- `docs/pose/` + `docs/vi/pose/` (EN + VI) — since removed; see
  [perceptions.md § Pose estimation](perceptions.md#3-pose-estimation)
- `skills/speaker-recognizer/reference/api.md`, `skills/wellbeing/reference/posture.md`

Fixed as part of this pass: the relative link to `deployment.md` in
`emoaffectnet-setup.md` (EN + VI, part of the since-removed `docs/face-emotion/` set) had the wrong depth (`../../` reached `docs/`,
not the repo root) — corrected to `../../../integrations/perception-service/docs/deployment.md`.

`integrations/README.md` intentionally keeps the phrase "formerly `dlbackend`"
as a historical pointer.

---

## Operational steps for the live cloud server(s)

Run these on each GPU node **once**, at the deploy that picks up this change:

1. **Publish the renamed Docker image** (so slaves can pull):
   ```bash
   cd integrations/perception-service
   docker build -t tnk2908/perception-service:latest .
   docker push tnk2908/perception-service:latest
   ```

2. **Migrate the `.env`** if you deploy from a git checkout (it is not tracked,
   so the move does not carry it over):
   ```bash
   cp /workspace/autonomous-os/dlbackend/.env \
      /workspace/autonomous-os/integrations/perception-service/.env
   ```
   (Or let `make deploy-master DEPLOY_API_KEY=<key>` recreate it from
   `.env.example`.)

3. **Preserve the model cache** to avoid re-downloading weights from the CDN on
   first call (bare-metal / non-Docker nodes only — Docker uses the
   `/workspace/models` volume):
   ```bash
   mv ~/.cache/dlbackend ~/.cache/perception-service   # if the old cache exists
   ```

4. **Preserve crypto keys** if payload encryption is enabled and keys must
   persist across the rename:
   ```bash
   mv ~/.dlbackend ~/.perception-service               # if the old key dir exists
   ```
   (If keys are auto-generated on start and rotation is acceptable, skip this.)

5. **Redeploy** with the updated Makefile:
   ```bash
   make deploy-master DEPLOY_HOST=root@<ip> DEPLOY_API_KEY=<key>
   ```
