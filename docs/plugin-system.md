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

Plugins install from any git URL — GitHub, GitLab, Gitea, a Hugging Face
Space repo, or self-hosted repos. There is no in-app plugin store today (see
*Web UI* below):

```bash
# Install from GitHub
POST /api/plugin/install {"url": "https://github.com/user/my-plugin"}

# Install from any git repo
POST /api/plugin/install {"url": "https://git.example.com/my-plugin.git"}
```

### API Endpoints

All endpoints require admin authentication.

```
POST   /api/plugin/install       — clone git repo, create venv, generate systemd unit
GET    /api/plugin               — list installed plugins with status
POST   /api/plugin/:name/start   — start plugin (systemctl start)
POST   /api/plugin/:name/stop    — stop plugin (systemctl stop)
DELETE /api/plugin/:name         — uninstall (stop + remove files + systemd unit)
```

`GET /api/plugin/browse` (a Hugging Face Spaces proxy for the
`autonomous-os-plugin` tag) is **parked, not registered** (#213): the handler is
commented out in `system/server/plugin/delivery/http/handler.go` and the route in
`system/server/server.go`, pending a `plugins` collection in the OS's own catalog.

### Systemd Integration

Each plugin runs as a systemd service (`os-plugin-<name>.service`):

- `Restart=on-failure` — automatic crash recovery
- `MemoryMax=256M` — memory limit on constrained devices (the only resource
  limit in the generated unit, `system/plugin/service.go` `writeSystemdUnit`)
- `WorkingDirectory` set to plugin dir
- `HAL_URL` env var injected

### Web UI

**Settings > Plugins** tab provides:
- **Installed** — list of installed plugins with status (running/stopped/failed),
  Start/Stop/Uninstall controls
- **Install from URL** — paste any git URL (GitHub, GitLab, etc.)

The former **Browse** pane (Hugging Face Spaces discovery) is parked with the
browse endpoint (#213) and not rendered (`system/web/src/pages/settings/PluginsSection.tsx`).

## Roadmap

### v1 — Pipeline (implemented)

Git URL → venv → systemd unit → HAL HTTP. Full plugin lifecycle:
- Install from any git URL (GitHub, GitLab, Hugging Face, etc.)
- systemd lifecycle (start/stop/restart on crash)
- Web UI management (Installed / Install from URL)
- Plugin template in `integrations/community-apps/plugin-template/`

The Hugging Face Spaces plugin store (browse + one-click install) was a
prototype and is parked (#213); plugin discovery is planned to move to the OS's
own catalog next to skills.

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
- systemd `MemoryMax=256M` caps plugin memory (no `CPUQuota` is set today)
- Future: container/seccomp sandboxing if ecosystem scales

## Template

Fork `integrations/community-apps/plugin-template/` to start building. It contains a working
hello-world plugin with LED + voice demo.

## References

- Pollen ecosystem analysis: `robots/reachy-mini/docs/pollen-ecosystem-analysis.md`
- HAL API routes: `hal/routes/`
- Device capabilities: `robots/contract/capabilities.md`
- Plugin template: `integrations/community-apps/plugin-template/`
- Former Hugging Face template (prototype, from the parked store): https://huggingface.co/spaces/autonomous-os/autonomous-os-hello-robot
