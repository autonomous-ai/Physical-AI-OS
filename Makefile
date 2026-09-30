# Autonomous — Makefile
# 4 components: Go (os + bootstrap + buddy), Python (hal), TypeScript (web)

VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")

# Directories
OS_DIR         := system
HAL_DIR        := hal
BUDDY_DIR      := integrations/companions/claude-desktop-buddy
TWITCH_DIR     := integrations/chat-bridges/twitch-chat-hook
AUTONOMOUS_DIR := integrations/chat-bridges/autonomous-chat-hook
WEB_DIR        := $(OS_DIR)/web

# Go build
MODULE         := go.autonomous.ai/os
# os-server version injected into config.OSVersion (internal build var).
LDFLAGS_OS     := -X $(MODULE)/system/server/config.OSVersion=$(VERSION)
LDFLAGS_BOOT   := -X $(MODULE)/system/bootstrap/config.BootstrapVersion=$(VERSION)
LDFLAGS_IRC    := -X main.Version=$(VERSION)
LDFLAGS_AUTONOMOUS_CHAT := -X main.Version=$(VERSION)

# HAL
HAL_PORT       := 5001

# ============================================================================
# OS services (Go) — build | generate | lint | test
# ============================================================================

.PHONY: os-build os-build-bootstrap os-generate os-lint os-test

os-build:
	cd $(OS_DIR) && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w $(LDFLAGS_OS)" -o os-server ./cmd/os-server


os-build-bootstrap:
	cd $(OS_DIR) && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w $(LDFLAGS_BOOT)" -o bootstrap-server ./cmd/bootstrap


os-generate:
	GOFLAGS=-mod=mod go generate ./...

os-lint:
	golangci-lint run

os-test:
	go test ./...

# ── Off-device run (laptop) ──────────────────────────────────────────────────
# Runs the SAME binary that ships to the board — no build tag, no second code
# path. Only the device-absolute paths move, via the env vars system/lib/syspath
# reads (unset = board defaults). Needs three terminals for a full stack:
#   make sim        HAL on :5001
#   make codex-dev  codex bridge on $(CODEX_PORT)
#   make os-dev     API on :5000
# `make os-dev-all` runs the last two in one terminal (bridge in the background).
# The runtime itself (codex CLI, its skills, AGENTS.md) is expected to be
# installed already — nothing here provisions it.
OS_STATE_DIR     ?= $(HOME)/.autonomous-os
OS_AGENT_RUNTIME ?= codex
CODEX_HOME       ?= $(HOME)/.codex
CODEX_PORT       ?= 18792
CODEX_BIN        ?= $(shell command -v codex 2>/dev/null)
# The backend identifies a device by its llm_api_key, not by device_id, so a
# laptop running a copy of a device's config.json IS that device as far as the
# backend can tell: the 15s ping overwrites the board's local_ip/mac/version and
# the MQTT client ids collide, evicting each other from the broker roughly once a
# second. Off by default here; `make os-dev OS_BACKEND_UPLINK=on` is deliberate.
OS_BACKEND_UPLINK ?= off

# The device-absolute paths os-server owns, one variable each.
DEVICES_DIR         ?= $(CURDIR)/robots
OS_AGENT_HOME       ?= $(OS_STATE_DIR)
OS_AGENT_STATE_PATH ?= $(OS_STATE_DIR)/config/agent_state.json
OS_BOOTSTRAP_CONFIG ?= $(OS_STATE_DIR)/config/bootstrap.json
OS_LOG_FILE         ?= $(OS_STATE_DIR)/os-server.log
# Unshipped device log records (system/lib/logger spool); /var/lib/autonomous on a device.
OS_GELF_SPOOL_DIR   ?= $(OS_STATE_DIR)/gelf-spool
# `make codex-dev` tees the bridge here; os-server reads it for the Agent tab.
OS_AGENT_BRIDGE_LOG ?= $(OS_STATE_DIR)/codex-gatewayd.log
# HAL writes it (HAL_LOG_DIR, HAL section below); os-server reads it for the HAL tab.
OS_HAL_LOG_FILE     ?= $(HAL_LOG_DIR)/server.log

# One env set shared by both processes so the bridge and its client can never
# disagree about where codex's state lives.
OS_DEV_ENV = \
	DEVICE_TYPE=$(DEVICE_TYPE) \
	DEVICES_DIR=$(DEVICES_DIR) \
	CODEX_HOME=$(CODEX_HOME) \
	CODEX_PORT=$(CODEX_PORT) \
	OS_AGENT_HOME=$(OS_AGENT_HOME) \
	OS_AGENT_STATE_PATH=$(OS_AGENT_STATE_PATH) \
	OS_BOOTSTRAP_CONFIG=$(OS_BOOTSTRAP_CONFIG) \
	OS_BACKEND_UPLINK=$(OS_BACKEND_UPLINK) \
	OS_HAL_LOG_FILE=$(OS_HAL_LOG_FILE) \
	OS_AGENT_BRIDGE_LOG=$(OS_AGENT_BRIDGE_LOG) \
	OS_GELF_SPOOL_DIR=$(OS_GELF_SPOOL_DIR) \
	OS_LOG_FILE=$(OS_LOG_FILE)

.PHONY: os-dev os-dev-all os-dev-build os-dev-seed os-dev-config codex-dev

os-dev-build:
	@mkdir -p $(OS_STATE_DIR)
	go build -ldflags "$(LDFLAGS_OS)" -o $(OS_STATE_DIR)/os-server ./system/cmd/os-server

os-dev-seed:
	@bash scripts/dev/os-dev-seed.sh $(OS_STATE_DIR) $(DEVICE_TYPE) $(OS_AGENT_RUNTIME) $(CODEX_HOME)

# Config only, no build, no server: create the file (from the template on a
# fresh machine) and hand the dev its path to edit. `make os-dev` runs the same
# seed step anyway — this is for doing it first, before touching a terminal.
os-dev-config: os-dev-seed
	@echo "edit it: $${EDITOR:-nano} $(OS_STATE_DIR)/config/config.json"

# cd into the state dir: config.json is resolved relative to the cwd, exactly as
# systemd's WorkingDirectory=/root does it on the board.
os-dev: os-dev-build os-dev-seed
	@echo "os-server: http://127.0.0.1:5000/api/health/live (runtime=$(OS_AGENT_RUNTIME))"
	cd $(OS_STATE_DIR) && $(OS_DEV_ENV) ./os-server

# Bridge + API in one terminal. The bridge runs in the background — its output
# goes only to $(OS_AGENT_BRIDGE_LOG) — and is killed when os-server exits. Use
# the two separate targets when you want the bridge's output live.
os-dev-all: os-dev-build os-dev-seed
	@test -n "$(CODEX_BIN)" || { echo "codex CLI not found on PATH — set CODEX_BIN=<path>"; exit 1; }
	@echo "codex bridge: ws://127.0.0.1:$(CODEX_PORT)/codex/ws/ (log: $(OS_AGENT_BRIDGE_LOG))"
	@echo "os-server: http://127.0.0.1:5000/api/health/live (runtime=$(OS_AGENT_RUNTIME))"
	@cd $(OS_STATE_DIR) && { $(OS_DEV_ENV) CODEX_BIN=$(CODEX_BIN) ./os-server codex-gatewayd > $(OS_AGENT_BRIDGE_LOG) 2>&1 & \
	  bridge=$$!; trap 'kill $$bridge 2>/dev/null' EXIT INT TERM; \
	  $(OS_DEV_ENV) ./os-server; }

codex-dev: os-dev-build
	@test -n "$(CODEX_BIN)" || { echo "codex CLI not found on PATH — set CODEX_BIN=<path>"; exit 1; }
	@echo "codex bridge: ws://127.0.0.1:$(CODEX_PORT)/codex/ws/ (CODEX_HOME=$(CODEX_HOME))"
	@mkdir -p $(OS_STATE_DIR)
	$(OS_DEV_ENV) CODEX_BIN=$(CODEX_BIN) $(OS_STATE_DIR)/os-server codex-gatewayd 2>&1 | tee $(OS_AGENT_BRIDGE_LOG)

# ============================================================================
# HAL (Python) — dev | run | test
# ============================================================================

.PHONY: hal hal-dev hal-install sim hal-run hal-lint hal-test hal-clean

hal: hal-dev

hal-install:
	cd $(HAL_DIR) && uv sync

$(HAL_DIR)/.venv: $(HAL_DIR)/uv.lock $(HAL_DIR)/pyproject.toml
	@command -v uv >/dev/null || { echo "uv not found — install: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
	cd $(HAL_DIR) && uv sync --inexact
	@touch $(HAL_DIR)/.venv

hal-dev: $(HAL_DIR)/.venv
	cd $(HAL_DIR) && PYTHONPATH=.. HAL_MODE=developer .venv/bin/uvicorn hal.server:app --host 0.0.0.0 --port $(HAL_PORT) --reload

# Boot any declared body on a laptop without opening its physical peripherals.
# DEVICE_TYPE remains the single body selector; Lamp is the default product body.
DEVICE_TYPE ?= lamp
SIM_STATE_DIR ?= $(HOME)/.autonomous-sim
# virtual is deterministic and permission-free; host opts into the developer
# machine's camera, microphone and speaker. host is also what turns the REAL
# voice pipeline on (STT → realtime → dispatch) instead of the inert stub —
# see the state.simulation_audio gate in hal/server.py.
SIM_MEDIA ?= virtual

# The device-absolute paths HAL owns that a laptop cannot use as-is. Same
# env-per-concern rule as system/lib/syspath: unset = the board's path.
# The first three carry meaning beyond "somewhere writable":
#   OS_CONFIG_PATH   the config.json HAL shares with os-server. Carries the LLM /
#                    STT / TTS / realtime credentials AND agent_runtime, which is
#                    what SNAPSHOT_DIR keys off — point it at the os-dev state dir
#                    so both processes read one file, as /root/config does on a board.
#   HAL_SNAPSHOT_DIR where ?save=true writes. MUST sit under the agent's own home
#                    (device: /root/.codex/media/hal-snapshots) — os-server serves
#                    it from there, and it is inside the agent's read allow-list.
#   HAL_SNAPSHOT_PERSIST_DIR  sensing's persistent copies; /var/lib is root-only.
#
# The rest are HAL's writable state, all rooted at /var/lib/hal or /root on a
# board. Each failure is silent-ish and far from its cause: the TTS cache one
# surfaced as `POST /voice/speak 409` with the real PermissionError buried in a
# thread traceback. Redirect them all rather than one at a time.
OS_CONFIG_PATH           ?= $(OS_STATE_DIR)/config/config.json
HAL_SNAPSHOT_DIR         ?= $(CODEX_HOME)/media/hal-snapshots
HAL_SNAPSHOT_PERSIST_DIR ?= $(SIM_STATE_DIR)/snapshots
HAL_TTS_CACHE_DIR        ?= $(SIM_STATE_DIR)/tts_cache
HAL_CALIBRATION_DIR      ?= $(SIM_STATE_DIR)/calibration/robots/hal_follower
HAL_USER_BEARING_PATH    ?= $(SIM_STATE_DIR)/user_bearing.json
HAL_FACE_HEIGHT_PATH     ?= $(SIM_STATE_DIR)/face_height.json
HAL_VOICE_STRANGERS_DIR  ?= $(SIM_STATE_DIR)/voice_strangers
HAL_DL_STALL_LOG         ?= $(SIM_STATE_DIR)/dl_ws_stall.log
HAL_CODEX_WORKSPACE_DIR  ?= $(CODEX_HOME)/workspace
HAL_LOG_DIR              ?= $(SIM_STATE_DIR)/log
HAL_USERS_DIR            ?= $(SIM_STATE_DIR)/users
HAL_STRANGERS_DIR        ?= $(SIM_STATE_DIR)/strangers
HAL_BT_STATE_DIR         ?= $(SIM_STATE_DIR)
HAL_VOLUME_STATE_PATH    ?= $(SIM_STATE_DIR)/volume

SIM_HAL_ENV = \
	OS_CONFIG_PATH=$(OS_CONFIG_PATH) \
	HAL_SNAPSHOT_DIR=$(HAL_SNAPSHOT_DIR) \
	HAL_SNAPSHOT_PERSIST_DIR=$(HAL_SNAPSHOT_PERSIST_DIR) \
	HAL_TTS_CACHE_DIR=$(HAL_TTS_CACHE_DIR) \
	HAL_CALIBRATION_DIR=$(HAL_CALIBRATION_DIR) \
	HAL_USER_BEARING_PATH=$(HAL_USER_BEARING_PATH) \
	HAL_FACE_HEIGHT_PATH=$(HAL_FACE_HEIGHT_PATH) \
	HAL_VOICE_STRANGERS_DIR=$(HAL_VOICE_STRANGERS_DIR) \
	HAL_DL_STALL_LOG=$(HAL_DL_STALL_LOG) \
	HAL_CODEX_WORKSPACE_DIR=$(HAL_CODEX_WORKSPACE_DIR) \
	HAL_LOG_DIR=$(HAL_LOG_DIR) \
	HAL_USERS_DIR=$(HAL_USERS_DIR) \
	HAL_STRANGERS_DIR=$(HAL_STRANGERS_DIR) \
	HAL_BT_STATE_DIR=$(HAL_BT_STATE_DIR) \
	HAL_VOLUME_STATE_PATH=$(HAL_VOLUME_STATE_PATH)

sim:
	@echo "HAL simulator: http://127.0.0.1:$(HAL_PORT)/docs"
	$(if $(filter lamp,$(DEVICE_TYPE)),@echo "Lamp visualizer: http://127.0.0.1:$(HAL_PORT)/simulator (drag to orbit; wheel to zoom)",@echo "No Lamp visualizer for DEVICE_TYPE=$(DEVICE_TYPE)")
	@if [ "$(SIM_MEDIA)" = "host" ]; then \
	  echo "Media: host — the Mac's mic/speaker/camera and the REAL voice pipeline (STT + realtime + dispatch). macOS will ask for Microphone and Camera access."; \
	else \
	  echo "Media: virtual — deterministic and permission-free; the voice pipeline stays inert. Pass SIM_MEDIA=host for the full stack."; \
	fi
	HAL_SIMULATE=1 HAL_SIM_MEDIA=$(SIM_MEDIA) HAL_BOARD=sim DEVICE_TYPE=$(DEVICE_TYPE) $(SIM_HAL_ENV) $(MAKE) hal-dev

hal-run: $(HAL_DIR)/.venv
	cd $(HAL_DIR) && PYTHONPATH=.. .venv/bin/python -m hal.server

# Catch refactor-leftover bugs (broken local imports + undefined names) that
# py_compile/tests miss off-hardware. Needs the `dev` extra (pyflakes).
hal-lint: $(HAL_DIR)/.venv
	cd $(HAL_DIR) && .venv/bin/python scripts/lint.py

hal-test: $(HAL_DIR)/.venv
	cd $(HAL_DIR) && .venv/bin/python -m pytest test/

hal-clean:
	rm -rf $(HAL_DIR)/.venv $(HAL_DIR)/__pycache__

# ============================================================================
# CTS — Compatibility Test Suite (robots/contract/cts)
# ============================================================================

.PHONY: new-device push-skill skills-catalog skills-catalog-check latency cts cts-runtime

# Scaffold a new body from robots/_template/ — ROBOT.md + SOUL.md, no SAFETY.md
# on purpose: `make cts` fails until you write the bounds for what you declared.
new-device:
	@test -n "$(NAME)" || { echo "usage: make new-device NAME=my-robot" >&2; exit 2; }
	@test -d robots/_template || { echo "robots/_template missing — is this a full clone?" >&2; exit 2; }
	@test ! -d robots/$(NAME) || { echo "robots/$(NAME) already exists" >&2; exit 2; }
	@cp -r robots/_template robots/$(NAME)
	@sed -i.bak 's/my-robot/$(NAME)/g; s/My Robot/$(NAME)/g' robots/$(NAME)/ROBOT.md robots/$(NAME)/SOUL.md && rm -f robots/$(NAME)/*.bak
	@echo "robots/$(NAME)/ — edit ROBOT.md, then: make cts"

# Regenerate the skill catalog from the skills/ tree (one folder per skill,
# capabilities in skills/<name>/skill.json) into system/skills/catalog_gen.go
# and scripts/provision/setup.sh. `make skills-catalog-check` fails if stale.
skills-catalog:
	python3 scripts/skills/gen_catalog.py

skills-catalog-check:
	python3 scripts/skills/gen_catalog.py --check

# Expands to a ProxyJump option when J=<host> is set, and to nothing otherwise.
# -o rather than -J so scp of any vintage accepts it.
SSH_JUMP_OPT = $(if $(J),-o ProxyJump=$(J),)

# Measure what a turn costs on a running robot: reads its flow log and prints
# p50/p95 per stage. PASSWORD is the 4 characters in the robot's Wi-Fi name.
#   make latency TARGET=lamp-ac82.local PASSWORD=ac82 [DATE=2026-08-16]
latency:
	@test -n "$(TARGET)" || { echo "usage: make latency TARGET=lamp-xxxx.local PASSWORD=xxxx" >&2; exit 2; }
	python3 scripts/bench/latency.py --target $(TARGET) $(if $(PASSWORD),--password $(PASSWORD),) $(if $(TOKEN),--token $(TOKEN),) $(if $(DATE),--date $(DATE),)

# Copy a skill folder onto a running body. Live on the next conversation, no
# reboot. Root SSH is off, so it lands in /tmp and moves with sudo.
# J=<host> routes both hops through an SSH jump host, as in the deploy targets:
#   make push-skill SKILL=./my-skill TARGET=pi@10.0.0.5 J=proxy-host
push-skill:
	@test -n "$(SKILL)" -a -n "$(TARGET)" || { echo "usage: make push-skill SKILL=./my-skill TARGET=pi@lamp-xxxx.local [J=jump-host]" >&2; exit 2; }
	@scp $(SSH_JUMP_OPT) -r $(SKILL) $(TARGET):/tmp/
	@ssh $(SSH_JUMP_OPT) $(TARGET) 'sudo mv /tmp/$(notdir $(SKILL)) /root/.openclaw/workspace/skills/'
	@echo "$(notdir $(SKILL)) → $(TARGET) — live on the next conversation"

# Static half: validates every robots/<id>/ROBOT.md against COMPATIBILITY.md.
# No hardware, no deps — this is what CI runs.
cts:
	python3 -m unittest discover -s robots/contract/cts -v

# Runtime half: compares a LIVE device against its own declaration.
#   make cts-runtime TARGET=lamp-ac82.local
# Add ALLOW_MOTION=1 to also exercise torque-off (it drops a raised arm).
# See robots/contract/cts/README.md for the full environment.
cts-runtime:
	@test -n "$(TARGET)" || { echo "usage: make cts-runtime TARGET=<device-host>" >&2; exit 2; }
	CTS_HAL=http://$(TARGET):5001 \
	CTS_OS=http://$(TARGET):5000 \
	CTS_ALLOW_MOTION=$(ALLOW_MOTION) \
	  python3 -m unittest discover -s robots/contract/cts -v

# ============================================================================
# Web (React/Vite/Tailwind) — install | dev | build
# ============================================================================

.PHONY: web web-install web-dev web-build

web: web-dev

# web-install stays unconditional — it is what you run after package.json
# changes. The node_modules target below is the first-run convenience web-dev
# depends on, and only fires when the directory is absent.
web-install:
	cd $(WEB_DIR) && npm install

$(WEB_DIR)/node_modules:
	cd $(WEB_DIR) && npm install

# os-server serves no HTML — on a board nginx serves web/dist and proxies /api
# and /hw to :5000. Off-device Vite plays nginx: LAMP_PROXY is the device the
# SPA talks to, and for `make os-dev` that device is this laptop. A .env in
# web/ still wins (vite.config reads it first), so pointing at a real Pi is
# unchanged.
LAMP_PROXY ?= http://127.0.0.1:5000

# Vite binds [::1] only, so the URL must say localhost — 127.0.0.1:5173 is
# refused. Admin routes need auth: log in with the device password, or append
# ?llm_api_key=<the key in config.json> once and the SPA exchanges it for a
# session cookie and scrubs it from the address bar.
web-dev: $(WEB_DIR)/node_modules
	@echo "Web UI: http://localhost:5173/monitor  (API proxied to $(LAMP_PROXY))"
	cd $(WEB_DIR) && LAMP_PROXY=$(LAMP_PROXY) npm run dev

web-build:
	cd $(WEB_DIR) && npm run build

# ============================================================================
# Claude Desktop Buddy (Go) — build
# ============================================================================

.PHONY: buddy-build

buddy-build:
	cd $(BUDDY_DIR) && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w" -o buddy-plugin .

# ============================================================================
# Twitch chat hook (Go) — build IRC fallback reader
# ============================================================================

.PHONY: twitch-build-irc

twitch-build-irc:
	cd $(TWITCH_DIR) && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w $(LDFLAGS_IRC)" -o twitch-irc ./cmd/irc

# ============================================================================
# Autonomous chat hook (Go) — MQTT subscriber bridging BE web chat → lamp
# ============================================================================

.PHONY: autonomous-build-chat

autonomous-build-chat:
	cd $(AUTONOMOUS_DIR) && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w $(LDFLAGS_AUTONOMOUS_CHAT)" -o autonomous-chat ./cmd/mqtt

# ============================================================================
# Upload (OTA to GCS) — unified format: make upload-<component>
# ============================================================================

OTA_SIGNING_KEY_DIR ?= $(HOME)/.config/autonomous/ota
OTA_SIGNING_KEY_ID ?= ota-$(shell date +%Y%m%d)

.PHONY: hal-deploy os-deploy device-deploy hal-log os-log ota-keygen upload-aec-wheel upload-os-server upload-bootstrap upload-hal upload-claude-desktop-buddy upload-autonomous-buddy upload-web upload-skills upload-hooks upload-setup upload-setup-ap upload-openclaw upload-codex upload-claudecode upload-opencode upload-hermes upload-picoclaw upload-device upload-twitch-irc upload-autonomous-chat upload-setup-remote-hermes upload-all promote-os-server promote-bootstrap promote-web promote-hal promote-claude-desktop-buddy promote-openclaw promote-codex promote-claudecode promote-opencode promote-hermes promote-picoclaw promote-device

# Generate a deployment-owned Ed25519 keypair outside the repository. The
# private PEM is for release writers only; the printed public key is provisioned
# to devices as OTA_SIGNING_PUBLIC_KEY.
ota-keygen:
	@set -eu; \
	key_dir='$(OTA_SIGNING_KEY_DIR)'; \
	key_id='$(OTA_SIGNING_KEY_ID)'; \
	case "$$key_id" in ''|*[!A-Za-z0-9._-]*) echo "ERROR: OTA_SIGNING_KEY_ID may contain only letters, digits, ., _, -" >&2; exit 1 ;; esac; \
	umask 077; mkdir -p "$$key_dir"; \
	key_path="$$key_dir/$$key_id.pem"; \
	[ ! -e "$$key_path" ] || { echo "ERROR: key already exists: $$key_path" >&2; exit 1; }; \
	command -v openssl >/dev/null || { echo "ERROR: openssl is required" >&2; exit 1; }; \
	openssl genpkey -algorithm Ed25519 -out "$$key_path"; \
	public_key=$$(openssl pkey -in "$$key_path" -pubout -outform DER | tail -c 32 | base64 | tr -d '\n'); \
	[ "$${#public_key}" -eq 44 ] || { echo "ERROR: generated public key is not 32 bytes" >&2; exit 1; }; \
	printf '\nPrivate key (keep outside the repo): %s\n' "$$key_path"; \
	printf 'export OTA_SIGNING_PRIVATE_KEY=%s\n' "$$key_path"; \
	printf 'export OTA_SIGNING_KEY_ID=%s\n' "$$key_id"; \
	printf 'export OTA_SIGNING_PUBLIC_KEY=%s\n' "$$public_key"

# ============================================================================
# Dev deploy — push the working tree to ONE device by IP.
# NOT the OTA path: upload-*/promote-* version and roll out to the whole fleet.
#
#   IP=172.168.20.255 make device-deploy   # hal + os-server
#   IP=172.168.20.255 make hal-deploy      # hal only (no build step)
#   IP=172.168.20.255 make os-deploy       # cross-compile + swap the binary
#   IP=172.168.20.255 make hal-log         # tail the device's hal journal
#   IP=172.168.20.255 make os-log          # tail the device's os-server journal
#
# J=<host> puts an SSH jump host in front of every hop (ssh, scp and rsync all
# get the same ProxyJump), for a device that is not routable from here:
#   IP=10.0.0.5 J=proxy-host make hal-deploy
#   IP=10.0.0.5 J=proxy-host make hal-log
# The hop authenticates from your SSH key/agent + ~/.ssh/config; PI_PASS is the
# DEVICE password only. Both orders work: `J=... make hal-deploy` and
# `make hal-deploy J=...`.
#
# hal-log/os-log deploy NOTHING — safe to run against a device mid-session.
# Knobs: LOG_LINES (default 200), FOLLOW=0 to dump instead of follow, GREP=<re>
# to filter on the device rather than over the link. Ctrl-C stops the follower
# on both ends.
#   IP=172.168.20.255 GREP='\[live\]|Response latency' make hal-log
#   IP=172.168.20.255 FOLLOW=0 LOG_LINES=500 make hal-log
#
# Auth: PI_USER (default orangepi), PI_PASS (default orangepi; set PI_PASS=""
# to use your SSH key). Never overwrites .env, .venv or calibration/.
# ============================================================================

# Reach the script whether these came from the environment (`IP=... make x`) or
# from make's own command line (`make x IP=...`); only the first form is
# inherited automatically. PI_PASS is deliberately NOT in this list: make
# exports a listed-but-undefined variable as an EMPTY string, and an empty
# PI_PASS is the script's opt-in to key auth — it would turn off the default
# device password for everyone. Pass it as an environment variable:
#   PI_PASS="" IP=... make hal-deploy
export IP J PI_HOST PI_JUMP PI_USER LOG_LINES FOLLOW GREP

hal-deploy:
	bash scripts/deploy-device.sh --hal

hal-log:
	bash scripts/deploy-device.sh --hal-log

os-log:
	bash scripts/deploy-device.sh --os-log

os-deploy:
	bash scripts/deploy-device.sh --os-server

device-deploy:
	bash scripts/deploy-device.sh

upload-os-server:
	bash scripts/release/upload-os-server.sh

upload-bootstrap:
	bash scripts/release/upload-bootstrap.sh

upload-hal:
	bash scripts/release/upload-hal.sh

upload-claude-desktop-buddy:
	bash scripts/release/upload-claude-desktop-buddy.sh

upload-autonomous-buddy:
	bash scripts/release/upload-autonomous-buddy.sh

upload-web:
	bash scripts/release/upload-web.sh

upload-skills:
	bash scripts/release/upload-skills.sh

# Publishes dist/aec/*.whl, built by scripts/release/build-aec-wheel.sh <ip>.
upload-aec-wheel:
	bash scripts/release/upload-aec-wheel.sh

upload-hooks:
	bash scripts/release/upload-hooks.sh

upload-setup:
	bash scripts/release/upload-setup.sh

upload-setup-ap:
	bash scripts/release/upload-setup-ap.sh

upload-twitch-irc:
	bash scripts/release/upload-twitch-irc.sh

upload-autonomous-chat:
	bash scripts/release/upload-autonomous-chat.sh

upload-setup-remote-hermes:
	bash scripts/release/upload-setup-remote-hermes.sh

# Allow positional version: `make upload-openclaw 2026.5.2`. The eval
# stub below creates a no-op rule for the version arg so make doesn't
# try to build it as a target ("no rule to make target '2026.5.2'").
# Scoped to when upload-openclaw is the first goal so this doesn't
# silence missing-target errors elsewhere.
ifeq (upload-openclaw,$(firstword $(MAKECMDGOALS)))
  OPENCLAW_VERSION_ARG := $(word 2,$(MAKECMDGOALS))
  ifneq ($(OPENCLAW_VERSION_ARG),)
    $(eval $(OPENCLAW_VERSION_ARG):;@:)
  endif
endif

upload-openclaw:
	@if [ -z "$(OPENCLAW_VERSION_ARG)" ]; then echo "Usage: make upload-openclaw <version>" >&2; exit 1; fi
	bash scripts/release/upload-openclaw.sh "$(OPENCLAW_VERSION_ARG)"

# Same positional-version trick for the Codex CLI: `make upload-codex 0.149.1`.
# Bare semver, no "rust-v" prefix (the script rejects the tag form).
ifeq (upload-codex,$(firstword $(MAKECMDGOALS)))
  CODEX_VERSION_ARG := $(word 2,$(MAKECMDGOALS))
  ifneq ($(CODEX_VERSION_ARG),)
    $(eval $(CODEX_VERSION_ARG):;@:)
  endif
endif

upload-codex:
	@if [ -z "$(CODEX_VERSION_ARG)" ]; then echo "Usage: make upload-codex <version>   (bare semver, e.g. 0.149.1)" >&2; exit 1; fi
	bash scripts/release/upload-codex.sh "$(CODEX_VERSION_ARG)"

# Claude Code CLI: `make upload-claudecode 2.1.218` (bare semver, no leading v).
ifeq (upload-claudecode,$(firstword $(MAKECMDGOALS)))
  CLAUDECODE_VERSION_ARG := $(word 2,$(MAKECMDGOALS))
  ifneq ($(CLAUDECODE_VERSION_ARG),)
    $(eval $(CLAUDECODE_VERSION_ARG):;@:)
  endif
endif

upload-claudecode:
	@if [ -z "$(CLAUDECODE_VERSION_ARG)" ]; then echo "Usage: make upload-claudecode <version>   (bare semver, e.g. 2.1.218)" >&2; exit 1; fi
	bash scripts/release/upload-claudecode.sh "$(CLAUDECODE_VERSION_ARG)"

# OpenCode CLI: `make upload-opencode 1.18.4` (bare semver, no leading v).
ifeq (upload-opencode,$(firstword $(MAKECMDGOALS)))
  OPENCODE_VERSION_ARG := $(word 2,$(MAKECMDGOALS))
  ifneq ($(OPENCODE_VERSION_ARG),)
    $(eval $(OPENCODE_VERSION_ARG):;@:)
  endif
endif

upload-opencode:
	@if [ -z "$(OPENCODE_VERSION_ARG)" ]; then echo "Usage: make upload-opencode <version>   (bare semver, e.g. 1.18.4)" >&2; exit 1; fi
	bash scripts/release/upload-opencode.sh "$(OPENCODE_VERSION_ARG)"

# Hermes CLI: `make upload-hermes 0.21.1 v2026.9.7` — the bare semver `hermes
# --version` prints, plus the upstream tag (or full commit) that builds it.
# Pinned by commit: `software-update hermes` checks that commit out through the
# upstream installer, so the fleet lands on exactly this build. See
# scripts/release/upload-hermes.sh.
ifeq (upload-hermes,$(firstword $(MAKECMDGOALS)))
  HERMES_VERSION_ARG := $(word 2,$(MAKECMDGOALS))
  HERMES_REF_ARG := $(word 3,$(MAKECMDGOALS))
  ifneq ($(HERMES_VERSION_ARG),)
    $(eval $(HERMES_VERSION_ARG):;@:)
  endif
  ifneq ($(HERMES_REF_ARG),)
    $(eval $(HERMES_REF_ARG):;@:)
  endif
endif

upload-hermes:
	@if [ -z "$(HERMES_VERSION_ARG)" ] || [ -z "$(HERMES_REF_ARG)" ]; then echo "Usage: make upload-hermes <version> <upstream-tag|commit>   (e.g. 0.21.1 v2026.9.7)" >&2; exit 1; fi
	bash scripts/release/upload-hermes.sh "$(HERMES_VERSION_ARG)" "$(HERMES_REF_ARG)"

# PicoClaw: `make upload-picoclaw v0.3.1-fixvision`. Takes the GitHub release
# TAG, not a bare semver — `picoclaw version` reports an unrelated build
# description. See scripts/release/upload-picoclaw.sh.
ifeq (upload-picoclaw,$(firstword $(MAKECMDGOALS)))
  PICOCLAW_VERSION_ARG := $(word 2,$(MAKECMDGOALS))
  ifneq ($(PICOCLAW_VERSION_ARG),)
    $(eval $(PICOCLAW_VERSION_ARG):;@:)
  endif
endif

upload-picoclaw:
	@if [ -z "$(PICOCLAW_VERSION_ARG)" ]; then echo "Usage: make upload-picoclaw <release-tag>   (e.g. v0.3.1-fixvision)" >&2; exit 1; fi
	bash scripts/release/upload-picoclaw.sh "$(PICOCLAW_VERSION_ARG)"

# Allow positional device type: `make upload-device lamp` (publishes ONE device
# profile). Per-device by design — each type versions + publishes independently,
# so it's NOT in upload-all (publishing lamp must not touch intern).
ifeq (upload-device,$(firstword $(MAKECMDGOALS)))
  DEVICE_TYPE_ARG := $(word 2,$(MAKECMDGOALS))
  ifneq ($(DEVICE_TYPE_ARG),)
    $(eval $(DEVICE_TYPE_ARG):;@:)
  endif
endif

upload-device:
	@if [ -z "$(DEVICE_TYPE_ARG)" ]; then echo "Usage: make upload-device <type>   (e.g. lamp, intern, unitree-go2w)" >&2; exit 1; fi
	bash scripts/release/upload-device.sh "$(DEVICE_TYPE_ARG)"

# Promote the auto-rollout floor (min_version) so bootstrap pushes a build to the
# fleet. One target per component (mirrors upload-*) so the names never collide
# with real targets like `hal`/`web`. Optional V=<version> pins an explicit floor
# (default: the entry's current version).
#   make promote-hal                # min_version = hal.version
#   make promote-os-server V=1.4.0  # pin floor explicitly
#   make promote-device DT=lamp     # devices.lamp profile
promote-os-server promote-bootstrap promote-web promote-hal promote-claude-desktop-buddy promote-openclaw promote-codex promote-claudecode promote-opencode promote-hermes promote-picoclaw:
	bash scripts/release/promote-ota.sh $(patsubst promote-%,%,$@) $(V)

promote-device:
	@if [ -z "$(DT)" ]; then echo "Usage: make promote-device DT=<type> [V=<min_version>]" >&2; exit 1; fi
	bash scripts/release/promote-ota.sh device "$(DT)" $(V)

# upload-openclaw / upload-codex / upload-claudecode / upload-opencode /
# upload-hermes / upload-picoclaw are intentionally NOT in upload-all — bumping an
# agent CLI version is an explicit decision, not a side effect of pushing other
# artifacts.
upload-all: upload-os-server upload-bootstrap upload-hal upload-claude-desktop-buddy upload-web upload-skills upload-hooks

# ============================================================================
# Release tagging — GPL v3 §6 compliance
# ============================================================================
# Annotated git tag with current OTA metadata.json embedded as message, then
# pushed. Lets buyers map "os-server --version" on the board back to a
# specific commit + component version set in the public repo.
#
# Usage: make tag-release v0.0.8       # after all upload-* targets succeed

ifeq (tag-release,$(firstword $(MAKECMDGOALS)))
  TAG_VERSION_ARG := $(word 2,$(MAKECMDGOALS))
  ifneq ($(TAG_VERSION_ARG),)
    $(eval $(TAG_VERSION_ARG):;@:)
  endif
endif

.PHONY: tag-release

tag-release:
	bash scripts/release/tag-release.sh "$(TAG_VERSION_ARG)"

# ============================================================================
# Clean
# ============================================================================

.PHONY: clean

clean:
	rm -f $(OS_DIR)/os-server $(OS_DIR)/bootstrap-server
	rm -f $(BUDDY_DIR)/buddy-plugin $(BUDDY_DIR)/claude-desktop-buddy
	rm -f $(TWITCH_DIR)/twitch-irc
	rm -rf $(HAL_DIR)/.venv $(HAL_DIR)/__pycache__
	rm -rf $(WEB_DIR)/dist $(WEB_DIR)/node_modules
