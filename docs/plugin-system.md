# Plugin System

Standalone Python apps that extend Autonomous OS device capabilities. Plugins
run as independent processes managed by systemd, accessing hardware through
HAL's HTTP API.

Written July 2026. Status: **v1 implemented.**

## Architecture

HAL is the kernel — plugins are userspace. The OS mediates all hardware access:

```
┌─────────────────────────────────────────────┐
│  Agent Runtime (brain, always running)      │
├─────────────────────────────────────────────┤
│  Plugin A    Plugin B    Plugin C           │  ← userspace apps
│    ↓ HTTP      ↓ HTTP      ↓ HTTP          │
├─────────────────────────────────────────────┤
│  HAL :5001 (hardware service, always on)    │  ← kernel
├─────────────────────────────────────────────┤
│  LED  Servo  Audio  Camera  GPIO  Sensing   │
└─────────────────────────────────────────────┘
```

Plugins coexist with HAL and the agent runtime. HAL serializes hardware access
so multiple plugins can run without resource conflicts.

Plugin installation and lifecycle endpoints require admin authorization. Manifest and lifecycle names allow letters, digits, underscores, hyphens and dots (including existing uppercase names). Empty names, `.` and `..`, paths, spaces, globs and unit-file directives are rejected before filesystem operations. Install URLs beginning with `-` are rejected, and Git receives `--` before the URL so it cannot be parsed as an option. Plugins still run trusted third-party code; name validation is not a sandbox.

## Plugin Format

A plugin is a directory (git repo) with:

```
my-plugin/
  plugin.json         # metadata (required)
  main.py             # entry point (default)
  requirements.txt    # pip dependencies (optional)
  README.md           # description + demo
```

### plugin.json

```json
{
  "name": "dance-party",
  "version": "1.0.0",
  "description": "LED dance synced to music beats",
  "entry": "main.py"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier (used as directory name + systemd unit name) |
| `version` | No | Semver string |
| `description` | No | One-line summary |
| `entry` | No | Python entry point, defaults to `main.py` |

### main.py

Plugins access hardware through HAL's HTTP API. The `HAL_URL` environment
variable is injected by the systemd unit (default `http://localhost:5001`):

```python
import os, time, requests

HAL = os.environ.get("HAL_URL", "http://localhost:5001")

requests.post(f"{HAL}/led/effect", json={"effect": "rainbow"})
requests.post(f"{HAL}/voice/speak", json={"text": "Let's dance!"})
time.sleep(30)
requests.post(f"{HAL}/led/off")
```

## Installation & Lifecycle

### Distribution

Plugins install from any git URL — HuggingFace Spaces, GitHub, GitLab, Gitea,
or self-hosted repos:

```bash
# Install from HuggingFace
POST /api/plugin/install {"url": "https://huggingface.co/spaces/user/my-plugin"}

# Install from GitHub
POST /api/plugin/install {"url": "https://github.com/user/my-plugin"}

# Install from any git repo
POST /api/plugin/install {"url": "https://git.example.com/my-plugin.git"}
```

### API Endpoints

All endpoints require admin authentication.

```
GET    /api/plugin/browse        — discover community plugins (proxies HuggingFace Spaces API)
POST   /api/plugin/install       — clone git repo, create venv, generate systemd unit
GET    /api/plugin               — list installed plugins with status
POST   /api/plugin/:name/start   — start plugin (systemctl start)
POST   /api/plugin/:name/stop    — stop plugin (systemctl stop)
DELETE /api/plugin/:name         — uninstall (stop + remove files + systemd unit)
```

### Systemd Integration

Each plugin runs as a systemd service (`os-plugin-<name>.service`):

- `Restart=on-failure` — automatic crash recovery
- `MemoryMax=256M` — resource limits on constrained devices
- `WorkingDirectory` set to plugin dir
- `HAL_URL` env var injected

### Web UI

**Settings > Plugins** tab provides:
- **Installed** — list of installed plugins with status (running/stopped/failed),
  Start/Stop/Uninstall controls
- **Browse** — discover community plugins from HuggingFace Spaces tagged
  `autonomous-os-plugin`, one-click install. Backend proxies HF API to avoid
  CORS (`GET /api/plugin/browse`)
- **Install from URL** — paste any git URL for non-HF plugins (GitHub, GitLab, etc.)

## Roadmap

### v1 — Pipeline + Store (implemented)

Git URL → venv → systemd unit → HAL HTTP. Full plugin lifecycle:
- Install from any git URL (HuggingFace, GitHub, GitLab, etc.)
- systemd lifecycle (start/stop/restart on crash)
- Plugin store — browse community plugins from HuggingFace Spaces
  (`autonomous-os-plugin` tag), one-click install
- Manual URL install for non-HF plugins
- Web UI management (Installed / Browse / Install from URL)
- Plugin template on HuggingFace for community forking

### v2 — SDK + Agent Integration

- **`autonomous-sdk` Python package** — wraps HAL HTTP into a clean API:
  ```python
  from autonomous import Robot

  class RadioPlayer(AutonomousApp):
      async def play_radio(self, robot: Robot, genre: str = "lofi"):
          """Play internet radio."""
          await robot.audio.stream_url(STATIONS[genre])
          robot.led.visualize_audio()
  ```
- **Auto MCP tool registration** — plugin methods with docstrings become
  agent-callable tools via local MCP server. Agent can invoke plugins by voice.
- **Cross-device capability routing** — `capabilities` in plugin.json gates
  which devices can run a plugin (reuses `robots/contract/capabilities.md`
  vocabulary).

### v3 — Ecosystem

- **Resource manager** — HAL audio mixer, camera multiplexing for true
  multi-plugin coexistence
- **Exclusive mode** — `"exclusive": true` in manifest parks HAL, gives plugin
  full hardware control (power-user escape hatch)
- **JS plugins** — browser-based plugins via WebRTC (zero-install, open URL
  and go), inspired by Pollen's JS app model

## Security

- **Install is admin-gated** — all plugin API endpoints require authentication
- **Trust model: local execution = full trust.** Installing a plugin means
  trusting its author. Same model as Pollen's app ecosystem.
- Plugins access HAL via HTTP — no direct filesystem access to HAL internals
- systemd resource limits (`MemoryMax`, `CPUQuota`) prevent resource exhaustion
- Future: container/seccomp sandboxing if ecosystem scales

## Template

Fork `integrations/plugin-template/` to start building. It contains a working
hello-world plugin with LED + voice demo.

## References

- Pollen ecosystem analysis: `robots/reachy-mini/docs/pollen-ecosystem-analysis.md`
- HAL API routes: `hal/routes/`
- Device capabilities: `robots/contract/capabilities.md`
- Plugin template: `integrations/community-apps/plugin-template/`
- HuggingFace template: https://huggingface.co/spaces/autonomous-os/autonomous-os-hello-robot
