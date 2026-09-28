.PHONY: install install-cpu install-gpu install-tensorrt install-lb install-dev \
       start start-dlserver start-lbserver \
       start-runpod-master start-runpod-master-ssl start-runpod-slave start-runpod-slave-ssl \
       start-nginx start-nginx-ssl stop-nginx gen-ssl-cert \
       start-runpod-dlserver start-runpod-lbserver stop-runpod-dlserver stop-runpod-lbserver start-jupyter \
       info \
       test test-api test-local \
       test-action test-action-api test-action-local \
       test-fer test-fer-api test-fer-local \
       test-ser test-ser-unit test-ser-api test-ser-local \
       test-pose test-pose-api test-pose-local \
       test-object test-object-api test-object-local \
       test-audio test-audio-api test-audio-local \
       test-crypto test-crypto-api test-crypto-local \
       test-lb test-lb-local \
       test-benchmark test-benchmark-http test-benchmark-ws test-benchmark-mixed \
       lint \
       deploy-master deploy-slave

# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

NGINX_CONF ?= nginx.conf
NGINX_SSL_CONF ?= nginx-ssl.conf
SSL_DIR ?= /workspace/ssl
SSL_CERT ?= $(SSL_DIR)/cert.pem
SSL_KEY ?= $(SSL_DIR)/key.pem
HOST ?= 0.0.0.0

# Port layout:
#   8899 — nginx (external, HTTP or SSL)
#   7999 — lbserver (master) or dlserver (slave)
#   8001 — dlserver (master only, behind LB)
#   8890 — jupyter
LBSERVER_PORT ?= 7999
DLSERVER_PORT ?= 8001
JUPYTER_PORT ?= 8890

DLSERVER_PID ?= /tmp/dlserver.pid
DLSERVER_WRAPPER_PID ?= /tmp/dlserver-wrapper.pid
LBSERVER_PID ?= /tmp/lbserver.pid
LBSERVER_WRAPPER_PID ?= /tmp/lbserver-wrapper.pid

# Resolve TensorRT/cuDNN LD_LIBRARY_PATH (evaluated once, cached)
define SET_TRT_LD_PATH
VENV_SP=$$(python -c "import site; print(site.getsitepackages()[0])" 2>/dev/null); \
CUDNN_LIB=$$(find "$$VENV_SP" -name "libcudnn.so.9" -exec dirname {} \; 2>/dev/null | head -1); \
TRT_LIB=$$(find "$$VENV_SP" -name "libnvinfer.so.10" -exec dirname {} \; 2>/dev/null | head -1); \
export LD_LIBRARY_PATH="$$CUDNN_LIB:$$TRT_LIB:$$LD_LIBRARY_PATH"
endef

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

install:
	@if command -v nvidia-smi > /dev/null 2>&1 && nvidia-smi > /dev/null 2>&1; then \
		echo "CUDA detected — installing onnxruntime-gpu"; \
		pip install -e ".[dl,gpu]"; \
		$(MAKE) install-tensorrt; \
	else \
		echo "No CUDA detected — installing onnxruntime (CPU)"; \
		pip install -e ".[dl,cpu]"; \
	fi

install-cpu:
	pip install -e ".[dl,cpu]"

install-gpu:
	pip install -e ".[dl,gpu]"
	$(MAKE) install-tensorrt

install-tensorrt:
	@echo "Installing TensorRT 10.8 (cu12) + cuDNN 9..."
	@pip uninstall tensorrt tensorrt_cu13 tensorrt_cu13_bindings tensorrt_cu13_libs -y 2>/dev/null || true
	@pip install -e ".[dl,tensorrt]" || \
		echo "WARNING: TensorRT/cuDNN installation failed — falling back to CUDA/CPU"
	@VENV_SITE_PACKAGES=$$(python -c "import site; print(site.getsitepackages()[0])") && \
	CUDNN_LIB=$$(find "$$VENV_SITE_PACKAGES" -name "libcudnn.so.9" -exec dirname {} \; 2>/dev/null | head -1) && \
	TRT_LIB=$$(find "$$VENV_SITE_PACKAGES" -name "libnvinfer.so.10" -exec dirname {} \; 2>/dev/null | head -1) && \
	if [ -z "$$CUDNN_LIB" ]; then echo "WARNING: libcudnn.so.9 not found in venv"; fi && \
	if [ -z "$$TRT_LIB" ]; then echo "WARNING: libnvinfer.so.10 not found in venv"; fi && \
	if [ -n "$$TRT_LIB" ] || [ -n "$$CUDNN_LIB" ]; then \
		BASHRC_LINE="export LD_LIBRARY_PATH=$$CUDNN_LIB:$$TRT_LIB:\$$LD_LIBRARY_PATH"; \
		if ! grep -q "libnvinfer" ~/.bashrc 2>/dev/null; then \
			echo "$$BASHRC_LINE" >> ~/.bashrc; \
			echo "Added LD_LIBRARY_PATH to ~/.bashrc"; \
		else \
			echo "LD_LIBRARY_PATH already in ~/.bashrc"; \
		fi; \
		export LD_LIBRARY_PATH="$$CUDNN_LIB:$$TRT_LIB:$$LD_LIBRARY_PATH"; \
	fi && \
	echo "  cuDNN lib: $$CUDNN_LIB" && \
	echo "  TRT lib:   $$TRT_LIB"

install-lb:
	pip install -e ".[lb]"

install-dev:
	pip install -e ".[dl,cpu,dev]"

# ---------------------------------------------------------------------------
# Local dev (foreground, blocking)
# ---------------------------------------------------------------------------

start: install
	@$(SET_TRT_LD_PATH); python -m dlserver --host $(HOST) --port $(DLSERVER_PORT)

start-dlserver: install
	@$(SET_TRT_LD_PATH); python -m dlserver --host $(HOST) --port $(DLSERVER_PORT)

start-lbserver: install-lb
	python -m lbserver --host $(HOST) --port $(LBSERVER_PORT)

# ---------------------------------------------------------------------------
# Server (background, non-blocking)
# ---------------------------------------------------------------------------

start-runpod-master: start-nginx start-runpod-dlserver start-runpod-lbserver

start-runpod-master-ssl: start-nginx-ssl start-runpod-dlserver start-runpod-lbserver

start-runpod-slave: start-nginx stop-runpod-dlserver install
	@$(SET_TRT_LD_PATH); nohup setsid bash scripts/run-with-restart.sh \
		--pid-file $(DLSERVER_PID) --wrapper-pid-file $(DLSERVER_WRAPPER_PID) --cooldown 5 \
		--probe-url http://127.0.0.1:$(LBSERVER_PORT)/livez \
		--log-dir /workspace/logs/dlserver -- \
		python -m dlserver --host 127.0.0.1 --port $(LBSERVER_PORT) --log-dir /workspace/logs/dlserver \
		</dev/null >/dev/null 2>&1 &
	@echo "dlserver started on port $(LBSERVER_PORT) (slave, no LB)"

start-runpod-slave-ssl: start-nginx-ssl stop-runpod-dlserver install
	@$(SET_TRT_LD_PATH); nohup setsid bash scripts/run-with-restart.sh \
		--pid-file $(DLSERVER_PID) --wrapper-pid-file $(DLSERVER_WRAPPER_PID) --cooldown 5 \
		--probe-url http://127.0.0.1:$(LBSERVER_PORT)/livez \
		--log-dir /workspace/logs/dlserver -- \
		python -m dlserver --host 127.0.0.1 --port $(LBSERVER_PORT) --log-dir /workspace/logs/dlserver \
		</dev/null >/dev/null 2>&1 &
	@echo "dlserver started on port $(LBSERVER_PORT) (slave-ssl, no LB)"

gen-ssl-cert:
	@if [ ! -f $(SSL_CERT) ] || [ ! -f $(SSL_KEY) ]; then \
		mkdir -p $(SSL_DIR); \
		openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
			-keyout $(SSL_KEY) -out $(SSL_CERT) \
			-subj "/CN=perception-service/O=autonomous/C=US"; \
		echo "Self-signed SSL cert generated at $(SSL_DIR)"; \
	else \
		echo "SSL cert already exists at $(SSL_DIR)"; \
	fi

stop-nginx:
	@nginx -s stop 2>/dev/null || true
	@if [ -f /tmp/nginx.pid ]; then \
		kill $$(cat /tmp/nginx.pid) 2>/dev/null || true; \
		sleep 1; \
		if kill -0 $$(cat /tmp/nginx.pid) 2>/dev/null; then \
			kill -9 $$(cat /tmp/nginx.pid) 2>/dev/null || true; \
			sleep 1; \
		fi; \
		rm -f /tmp/nginx.pid; \
	fi
	@echo "nginx stopped"

start-nginx: stop-nginx
	@if ! command -v nginx > /dev/null 2>&1; then \
		apt-get update && apt-get install -y nginx; \
	fi
	cp $(NGINX_CONF) /etc/nginx/perception-service-nginx.conf
	nginx -c /etc/nginx/perception-service-nginx.conf

start-nginx-ssl: gen-ssl-cert stop-nginx
	@if ! command -v nginx > /dev/null 2>&1; then \
		apt-get update && apt-get install -y nginx; \
	fi
	cp $(NGINX_SSL_CONF) /etc/nginx/perception-service-nginx-ssl.conf
	nginx -c /etc/nginx/perception-service-nginx-ssl.conf

start-runpod-dlserver: install stop-runpod-dlserver
	@$(SET_TRT_LD_PATH); nohup setsid bash scripts/run-with-restart.sh \
		--pid-file $(DLSERVER_PID) --wrapper-pid-file $(DLSERVER_WRAPPER_PID) --cooldown 5 \
		--probe-url http://127.0.0.1:$(DLSERVER_PORT)/livez \
		--log-dir /workspace/logs/dlserver -- \
		python -m dlserver --host 127.0.0.1 --port $(DLSERVER_PORT) --log-dir /workspace/logs/dlserver \
		</dev/null >/dev/null 2>&1 &
	@echo "dlserver started on port $(DLSERVER_PORT) (auto-restart enabled)"

stop-runpod-dlserver:
	@bash scripts/stop-tree.sh dlserver $(DLSERVER_PORT) $(DLSERVER_WRAPPER_PID) $(DLSERVER_PID)

start-runpod-lbserver: install-lb stop-runpod-lbserver
	@nohup setsid bash scripts/run-with-restart.sh \
		--pid-file $(LBSERVER_PID) --wrapper-pid-file $(LBSERVER_WRAPPER_PID) --cooldown 5 \
		--probe-url http://127.0.0.1:$(LBSERVER_PORT)/livez \
		--log-dir /workspace/logs/lbserver -- \
		python -m lbserver --host 127.0.0.1 --port $(LBSERVER_PORT) --log-dir /workspace/logs/lbserver \
		</dev/null >/dev/null 2>&1 &
	@echo "lbserver started on port $(LBSERVER_PORT) (auto-restart enabled)"

stop-runpod-lbserver:
	@bash scripts/stop-tree.sh lbserver $(LBSERVER_PORT) $(LBSERVER_WRAPPER_PID) $(LBSERVER_PID)

start-jupyter:
	@if ! command -v jupyter-lab > /dev/null 2>&1; then \
		python -m pip install jupyter; \
	fi
	@command -v multilog >/dev/null || apt-get install -y daemontools
	@mkdir -p /workspace/logs/jupyter
	@(sleep 3 && TOKEN=$$(grep -oP 'token=\K[a-f0-9]+' /workspace/logs/jupyter/current 2>/dev/null | tail -1); \
	if [ -n "$$TOKEN" ]; then \
		echo "Jupyter Lab: https://<host>:8899/jupyter/lab?token=$$TOKEN"; \
	else \
		echo "Jupyter Lab started — grep token from /workspace/logs/jupyter/current"; \
	fi) &
	jupyter-lab --ip=127.0.0.1 --port=$(JUPYTER_PORT) --no-browser --allow-root --ServerApp.allow_remote_access=true --ServerApp.base_url=/jupyter/ --notebook-dir=/workspace 2>&1 | multilog t s1048576 n3 /workspace/logs/jupyter

# ---------------------------------------------------------------------------
# Info
# ---------------------------------------------------------------------------

info:
	@echo "=== Port Layout ==="
	@echo "  nginx:     8899"
	@echo "  lbserver:  $(LBSERVER_PORT)"
	@echo "  dlserver:  $(DLSERVER_PORT)"
	@echo "  jupyter:   $(JUPYTER_PORT)"
	@echo ""
	@echo "=== Processes ==="
	@printf "  dlserver:  "; \
	if [ -f $(DLSERVER_PID) ] && kill -0 $$(cat $(DLSERVER_PID)) 2>/dev/null; then \
		WRAPPER=""; \
		if [ -f $(DLSERVER_WRAPPER_PID) ] && kill -0 $$(cat $(DLSERVER_WRAPPER_PID)) 2>/dev/null; then \
			WRAPPER=" (watchdog=$$(cat $(DLSERVER_WRAPPER_PID)))"; \
		fi; \
		echo "running (pid=$$(cat $(DLSERVER_PID))$$WRAPPER)"; \
	else \
		echo "stopped"; \
	fi
	@printf "  lbserver:  "; \
	if [ -f $(LBSERVER_PID) ] && kill -0 $$(cat $(LBSERVER_PID)) 2>/dev/null; then \
		WRAPPER=""; \
		if [ -f $(LBSERVER_WRAPPER_PID) ] && kill -0 $$(cat $(LBSERVER_WRAPPER_PID)) 2>/dev/null; then \
			WRAPPER=" (watchdog=$$(cat $(LBSERVER_WRAPPER_PID)))"; \
		fi; \
		echo "running (pid=$$(cat $(LBSERVER_PID))$$WRAPPER)"; \
	else \
		echo "stopped"; \
	fi
	@printf "  nginx:     "; \
	if [ -f /tmp/nginx.pid ] && kill -0 $$(cat /tmp/nginx.pid) 2>/dev/null; then \
		echo "running (pid=$$(cat /tmp/nginx.pid))"; \
	else \
		echo "stopped"; \
	fi
	@echo ""
	@echo "=== Logs ==="
	@echo "  dlserver:  /workspace/logs/dlserver/"
	@echo "  lbserver:  /workspace/logs/lbserver/"
	@echo "  jupyter:   /workspace/logs/jupyter/"

# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

test: test-api test-local

test-api: test-action-api test-fer-api test-ser-api test-pose-api test-object-api test-audio-api test-crypto-api

test-local: test-action-local test-fer-local test-ser-local test-pose-local test-object-local test-audio-local test-crypto-local test-lb-local

test-action: test-action-api test-action-local

test-action-api:
	-python -m pytest tests/action_api/ -v

test-action-local:
	-python -m pytest tests/action_local/ -v

test-fer: test-fer-api test-fer-local

test-fer-api:
	-python -m pytest tests/emotion_api/ -v

test-fer-local:
	-python -m pytest tests/emotion_local/ -v

test-ser: test-ser-unit test-ser-api test-ser-local

test-ser-unit:
	-DL_API_KEY=$${DL_API_KEY:-test} python -m pytest tests/audio_emotion_unit/ -v

test-ser-api:
	-python -m pytest tests/audio_emotion_api/ -v

test-ser-local:
	-python -m pytest tests/audio_emotion_local/ -v

test-pose: test-pose-api test-pose-local

test-pose-api:
	-python -m pytest tests/pose_api/ -v

test-pose-local:
	-python -m pytest tests/pose_local/ -v

test-object: test-object-api test-object-local

test-object-api:
	-python -m pytest tests/object_api/ -v

test-object-local:
	-python -m pytest tests/object_local/ -v

test-audio: test-audio-api test-audio-local

test-audio-api:
	-python -m pytest tests/audio_api/ -v

test-audio-local:
	-python -m pytest tests/audio_local/ -v

test-crypto: test-crypto-api test-crypto-local

test-crypto-api:
	-python -m pytest tests/crypto_api/ -v

test-crypto-local:
	-python -m pytest tests/crypto/ -v

test-lb: test-lb-local

test-lb-local:
	-python -m pytest tests/lb_local/ -v

test-benchmark: test-benchmark-http test-benchmark-ws test-benchmark-mixed

test-benchmark-http:
	-python -m pytest tests/benchmark_api/test_benchmark_api.py -v -s

test-benchmark-ws:
	-python -m pytest tests/benchmark_api/test_benchmark_ws.py -v -s

test-benchmark-mixed:
	-python -m pytest tests/benchmark_api/test_benchmark_mixed.py -v -s

lint:
	ruff check src/ tests/

# ---------------------------------------------------------------------------
# Deploy (run from local machine)
# ---------------------------------------------------------------------------
# Usage:
#   make deploy-master DEPLOY_HOST=root@<ip>
#   make deploy-slave  DEPLOY_HOST=root@<ip>

DEPLOY_HOST ?=
DEPLOY_REPO ?= https://github.com/autonomous-ai/autonomous-os.git
DEPLOY_BRANCH ?= main
DEPLOY_ROOT ?= /workspace/autonomous-os
DEPLOY_DL ?= $(DEPLOY_ROOT)/integrations/perception-service
DEPLOY_API_KEY ?=

define DEPLOY_SYNC
	ssh $(DEPLOY_HOST) "\
		if [ -d $(DEPLOY_ROOT) ]; then \
			cd $(DEPLOY_ROOT) && git fetch --all && git checkout $(DEPLOY_BRANCH) && git pull; \
		else \
			git clone -b $(DEPLOY_BRANCH) $(DEPLOY_REPO) $(DEPLOY_ROOT); \
		fi && \
		cd $(DEPLOY_DL) && make install"
endef

define DEPLOY_ENSURE_ENV
	@if [ -n "$(DEPLOY_API_KEY)" ]; then \
		ssh $(DEPLOY_HOST) "\
			if [ ! -f $(DEPLOY_DL)/.env ]; then \
				cp $(DEPLOY_DL)/.env.example $(DEPLOY_DL)/.env && \
				sed -i 's/DL_API_KEY=changeme/DL_API_KEY=$(DEPLOY_API_KEY)/' $(DEPLOY_DL)/.env && \
				echo '.env created from .env.example'; \
			fi"; \
	fi
endef

define DEPLOY_HEALTH
	@echo "Waiting for health check..."
	@ssh $(DEPLOY_HOST) '\
		cd $(DEPLOY_DL) && \
		KEY=$$(grep DL_API_KEY .env | cut -d= -f2-) && \
		while true; do \
			RESP=$$(curl -sf -H "X-API-Key: $$KEY" http://127.0.0.1:$(1)/hal/api/dl/health 2>/dev/null) && \
			echo "$$RESP" | grep -q '"'"'"status":"ok"'"'"' && \
			echo "Health OK: $$RESP" && break; \
			echo "Not ready yet... retrying in 5s"; \
			sleep 5; \
		done'
endef

deploy-master:
	@if [ -z "$(DEPLOY_HOST)" ]; then echo "DEPLOY_HOST not set. Usage: make deploy-master DEPLOY_HOST=root@<ip> DEPLOY_API_KEY=<key>"; exit 1; fi
	$(DEPLOY_SYNC)
	$(DEPLOY_ENSURE_ENV)
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DL) && make stop-runpod-dlserver stop-runpod-lbserver stop-nginx && make start-runpod-master"
	$(call DEPLOY_HEALTH,8001)

deploy-slave:
	@if [ -z "$(DEPLOY_HOST)" ]; then echo "DEPLOY_HOST not set. Usage: make deploy-slave DEPLOY_HOST=root@<ip> DEPLOY_API_KEY=<key>"; exit 1; fi
	$(DEPLOY_SYNC)
	$(DEPLOY_ENSURE_ENV)
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DL) && make stop-runpod-dlserver stop-nginx && make start-runpod-slave"
	$(call DEPLOY_HEALTH,7999)
