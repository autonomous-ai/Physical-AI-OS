# Bootstrap & OTA System

OrangePi images defer the vendor MOTD update-count job (`orangepi-apt-updates`) by 120 seconds after boot to avoid competing with HAL for storage reads. Only its exact `@reboot root /usr/lib/orangepi/orangepi-apt-updates` cron entry changes; the daily count and APT/security update schedules remain unchanged. Existing devices can apply the same tuning with `sudo python3 scripts/imager/lib/defer_orangepi_update_count.py` after copying the script onto the device. The helper is idempotent, skips missing/customized entries, and keeps the original at `/var/backups/autonomous/orangepi-updates.before-boot-delay`. Restore that file to `/etc/cron.d/orangepi-updates` to undo the tuning. This is an image/device configuration change, not part of a HAL-only OTA.

## 1. Overview

The device runs **5 software components** on a supported board (Raspberry Pi 4, Pi 5, or OrangePi). All components are installed via an initial setup script and kept up-to-date by a background OTA worker.

| Component | Type | Install Method | Service Name | Install Path |
|---|---|---|---|---|
| **OS Server** | Go binary (ARM64) | Download zip from OTA | `os-server.service` | `/usr/local/bin/os-server` |
| **Bootstrap Server** | Go binary (ARM64) | Download zip from OTA | `bootstrap.service` | `/usr/local/bin/bootstrap-server` |
| **Web (Setup SPA)** | React/Vite bundle | Download zip from OTA | nginx serves static | `/usr/share/nginx/html/setup/` |
| **OpenClaw** | Node.js package | `npm install -g` | `openclaw.service` | Global npm |
| **HAL** | Python package | Download zip from OTA | `hal.service` | `/opt/hal/` |

HAL supports CPython **3.12.x** (`requires-python = ">=3.12,<3.13"`).
`uv sync --python 3.12` selects the interpreter but still resolves the project's
entire supported Python range and optional extras. An unbounded `>=3.12` can
therefore fail an ARM64 update while resolving Python 3.14 (the pinned LeRobot
requires Torch <2.8, without compatible wheels for that split). Keep the Python
range and `hal/uv.lock` aligned; a staging resolution failure restores the old HAL.

HAL release packaging includes `uv.lock` and validates it with
`uv lock --python 3.12 --check` before bumping the version or uploading. The
updater uses `uv sync --locked` when the archive contains a lockfile; older
archives without one retain their legacy resolution path. Excluding the lock
forced device-side fresh resolution: on uv 0.12.15, HAL 0.1.169 failed on a
Python >=3.13 ARM64/Reachy split despite a 3.12-only project range; the same
archive with the validated lock passed a locked install dry-run on the device.

Image builders for OrangePi, Pi 4, and Pi 5 also use `--locked` when the
downloaded HAL archive contains `uv.lock`. They fetch HAL from the OTA metadata,
not from the local checkout; rerun the build after publishing the fixed archive.

Lamp setup and Pi/OrangePi images install HAL with `--extra hardware --extra aec
--extra pipecat`. Non-Reachy OTA selects the same extras, so Smart Turn is
installed automatically without a separate device command. Reachy retains
`--extra hardware --extra reachy`: its ONNX runtime requirement conflicts with
Pipecat. The `pipecat` extra remains optional for bare developer `uv sync`.

### Architecture Diagram

```
                    ┌──────────────────────────────┐
                    │   OTA Metadata (GCS JSON)     │
                    │                                │
                    │  os-server: {version, url}     │
                    │  bootstrap: {version, url}     │
                    │  web:       {version, url}     │
                    │  openclaw:  {version}          │
                    │  hal:       {version, url}     │
                    └───────────────┬────────────────┘
                                    │ poll every 5m
                                    ▼
┌───────────────────────────────────────────────────────────────────┐
│                    Bootstrap Server (Go, port 8080)               │
│                                                                   │
│  checkLoop() → for each component:                                │
│    1. Detect current installed version                            │
│    2. Compare to OTA metadata target version                      │
│    3. If mismatch → applyUpdate()                                 │
│       → download zip / npm install                                │
│       → extract to install path                                   │
│       → systemctl restart {service}                               │
│    4. Persist state to /root/bootstrap/state.json                 │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

---

## 2. OTA Metadata Format

Single JSON file hosted on GCS. All components reference this file.

> In the URLs below, `{BUCKET}` and `{PREFIX}` are the bucket + path namespace:
> `GCS_BUCKET` (default `s3-autonomous-upgrade-3`) and `BUCKET_PREFIX` (default
> `os`), both set in `scripts/release/ota-config.sh`. Upload scripts read them from there;
> on-device consumers derive the same paths from the provisioned `ota_metadata_url`.

**URL**: `https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/metadata.json`

The published document retains legacy component entries at the top level and
adds a `signed` `autonomous-ota/v1` Ed25519 envelope. `signed.payload` is
base64-encoded JSON and `signed.signature.value` signs the decoded payload
bytes; the 32-byte base64 public key is provisioned locally in `bootstrap.json`,
never taken from the feed. Its decoded payload has this shape:

```json
{
  "os-server": {
    "version": "1.2.3",
    "min_version": "1.2.0",
    "url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/os-server/1.2.3/os-server-1.2.3.zip",
    "sha256": "<64 lowercase hex characters>"
  },
  "bootstrap": {
    "version": "1.0.5",
    "url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/bootstrap/1.0.5/bootstrap-1.0.5.zip"
  },
  "web": {
    "version": "0.9.0",
    "url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/web/0.9.0/setup-0.9.0.zip"
  },
  "openclaw": {
    "version": "2026.6.10"
  },
  "hal": {
    "version": "1.0.0",
    "url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/hal/1.0.0/hal-1.0.0.zip"
  }
}
```

### Generate an OTA signing key

Run this once on the release operator's machine:

```bash
make ota-keygen
```

It writes an Ed25519 private PEM outside the repository by default, at
`~/.config/autonomous/ota/ota-YYYYMMDD.pem`, and prints three export lines. Keep
`OTA_SIGNING_PRIVATE_KEY` private; use it with `OTA_SIGNING_KEY_ID` for
`make upload-*`. Provision the printed `OTA_SIGNING_PUBLIC_KEY` into new devices.
Override the path or ID when needed:

```bash
make ota-keygen OTA_SIGNING_KEY_DIR=/secure/ota-keys OTA_SIGNING_KEY_ID=prod-2026-08
```

**Domain types** — `domain/ota.go`:

```go
const (
    OTAKeyOSServer  = "os-server"
    OTAKeyBootstrap = "bootstrap"
    OTAKeyWeb       = "web"
    OTAKeyOpenClaw  = "openclaw"
    // Agent-runtime CLIs. Each value is also the runtime name in config.json
    // `agent_runtime` — that equality is how bootstrap updates only the CLI the
    // device actually runs. Hermes rides along only when its entry is
    // commit-pinned (OTAComponent.Commit) — see OTAKeyHermes.
    OTAKeyHermes     = "hermes"
    OTAKeyCodex      = "codex"
    OTAKeyClaudeCode = "claudecode"
    OTAKeyOpenCode   = "opencode"
    OTAKeyPicoClaw   = "picoclaw"
    // OTAKeyLeLamp's value is "hal" — the HAL OTA metadata key
)

type OTAMetadata map[string]OTAComponent

type OTAComponent struct {
    Version    string `json:"version"`
    MinVersion string `json:"min_version,omitempty"`
    URL        string `json:"url,omitempty"`
    SHA256     string `json:"sha256,omitempty"`
}
```

### Staged rollout — `version` vs `min_version`

`version` is the latest build; `min_version` is the **approved floor** the
automatic worker rolls the fleet up to. They decouple "published" from
"auto-pushed":

- **Auto OTA (bootstrap worker)** updates a device only when its current version
  is **strictly below `min_version`**. If `min_version` is absent it defaults to
  `version` (so the worker simply tracks the latest — legacy behavior).
- **Manual `software-update <key>`** (run over SSH) ignores `min_version` and
  always installs `version` — for testing a build on a few devices first.

Workflow:

1. `scripts/release/upload-<component>.sh` bumps `version` and **preserves**
   `min_version` (skills/hooks have no `min_version`). The fleet does **not**
   move — only `version` changed.
2. SSH into a device, run `software-update <key>` → it pulls `version`. Test it.
3. Happy? `make promote-<component> [V=<version>]` (e.g. `make promote-hal`,
   `make promote-os-server V=1.4.0`, `make promote-device DT=lamp`) raises
   `min_version` (default: up to `version`). Bootstrap now auto-updates every
   device below the new floor on its next check.

Version comparison is numeric per dotted segment (`bootstrap.compareVersions`):
`2026.6.10 > 2026.5.9`; pre-release/build suffixes are ignored; an empty or
unparseable current version sorts lowest (always below any floor → updates).

---

## 3. Initial Setup (`scripts/provision/setup.sh`)

One-time provisioning script run on a fresh Raspberry Pi. Executes stages sequentially.

**Quick install from CDN:**
```bash
curl -fsSL https://cdn.autonomous.ai/os/install.sh | sudo bash
```

### Stage Overview

| Stage | Name | Description |
|---|---|---|
| -1 | Locale fix | Ensure `C.UTF-8` encoding |
| 0 | Prerequisites | System packages, Node.js 22 |
| 0a | WiFi stability | Disable IPv6, WiFi power saving (RPi5) |
| 0b | Enable SPI + I2C | For WS2812 LED driver; I2C bus for peripherals |
| 1 | Fetch OTA metadata | Download metadata.json, extract versions and URLs |
| 1b | Install binaries | Download + install os-server, bootstrap-server, create systemd services |
| 2 | Install OpenClaw | `npm install -g openclaw`, create config, create systemd service |
| **2b** | **Install HAL** | **Download + install HAL Python runtime, create systemd service** (NEW) |
| 3 | Setup nginx | Download web bundle, configure reverse proxy + captive portal |
| 4 | Setup WiFi AP | Configure hostapd, dnsmasq, start AP mode for provisioning |

### Stage 2b: Install HAL Runtime (NEW)

This stage installs the HAL Python runtime that provides hardware drivers for servos, LEDs, and audio.

```bash
stage_install_hal() {
    echo "=== Stage 2b: Install HAL Runtime ==="

    # 1. Install Python dependencies
    apt-get install -y python3 python3-pip python3-venv

    # 2. Create install directory
    mkdir -p /opt/hal

    # 3. Download from OTA metadata
    HAL_URL=$(echo "$OTA_JSON" | jq -r '.hal.url')
    HAL_VERSION=$(echo "$OTA_JSON" | jq -r '.hal.version')

    curl -fsSL "$HAL_URL" -o /tmp/hal.zip
    unzip -o /tmp/hal.zip -d /opt/hal/
    rm /tmp/hal.zip

    # 4. Install Python dependencies in venv
    python3 -m venv /opt/hal/venv
    /opt/hal/venv/bin/pip install -r /opt/hal/requirements.txt

    # 5. Create systemd service
    cat > /etc/systemd/system/hal.service << 'UNIT'
[Unit]
Description=HAL Python Runtime — Hardware Drivers
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/hal
ExecStart=/opt/hal/venv/bin/python -m hal.server
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

    systemctl daemon-reload
    systemctl enable hal.service
    systemctl start hal.service

    echo "HAL $HAL_VERSION installed at /opt/hal/"
}
```

### Systemd Services Created by Setup

| Service | ExecStart | Port | Notes |
|---|---|---|---|
| `os-server.service` | `/usr/local/bin/os-server` | 5000 | Main HTTP API, always running |
| `bootstrap.service` | `/usr/local/bin/bootstrap-server` | 8080 | OTA worker, polls for updates. Exposes `POST /force-check` to trigger an immediate OTA check and `GET /security` for the OTA trust posture |
| `openclaw.service` | `xvfb-run ... openclaw gateway run` | — | AI brain, memory limit 1500M |
| `hal.service` | `uvicorn hal.server:app --host 127.0.0.1 --port 5001` | 5001 | Hardware drivers (servo, LED, camera, audio) |
| nginx | `nginx` | 80 | Setup SPA + reverse proxy (`/api/` → OS Server 5000, `/hw/` → HAL 5001) |

### Service Dependency Order

```
boot
  → os-server.service   (system layer, LED boot animation)
  → bootstrap.service   (starts polling for updates)
  → hal.service          (hardware drivers ready)
  → openclaw.service    (AI brain, connects to os-server via HTTP)
  → nginx               (web UI for setup)
```

---

## 4. Bootstrap OTA Worker

### Config (`/root/config/bootstrap.json`)

The bootstrap worker keeps its own config file, separate from os-server's
`config.json`, but it lives in the same `/root/config/` directory.

```json
{
  "httpPort": 8080,
  "metadata_url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/metadata.json",
  "signing_public_key": "<base64 32-byte Ed25519 public key>",
  "rollback_versions": {"os-server": "1.2.3"},
  "poll_interval": "5m",
  "state_file": "/root/bootstrap/state.json"
}
```

`metadata_url` has **no compiled-in default** — it is a per-deployment value
seeded into this file by `setup.sh` (`stage_ota_metadata`) at provisioning. The
file is loaded as an overlay on operational defaults (`httpPort` 8080,
`poll_interval` 5m, `state_file`), so a partial file (just `metadata_url`) works
and a missing file yields defaults with an empty URL.

When supplied, `setup.sh` and golden-image builders persist
`OTA_SIGNING_PUBLIC_KEY` as `signing_public_key`. Bootstrap then verifies the
envelope before reading any component; the provisioned updater verifies it
again and hashes every ZIP before extraction. When a release operator supplies
`OTA_SIGNING_PRIVATE_KEY` and `OTA_SIGNING_KEY_ID`, release writers re-sign the
envelope; without them they retain the legacy unsigned format. For the two self-contained
binaries, each update retains `/root/bootstrap/rollback/<component>.previous`;
run `software-update rollback os-server` or `software-update rollback bootstrap`
to restore it. The updater records the version it removed in
`rollback_versions`; bootstrap then skips only that exact target, so the failed
release is not reinstalled on the next poll. Publishing a different version
automatically resumes OTA for that component. Rollback itself does not need the
metadata URL or network access.

Before `os-server`, `web`, or `device` updates, the updater ensures the nginx
Harness WebSocket route exists. It searches both `/etc/nginx/conf.d/*.conf` and
all `/etc/nginx/sites-enabled/*` entries, including Reachy's `reachy-spike`.
The route reuses the existing `/api/` HTTP upstream (`backend` or
`spike_backend`); unsupported proxy block shapes or URI rewriting are rejected.
Symlinked sites are updated at their real target, preserving the enabled link.
The updater validates with `nginx -t` and reloads nginx only after adding a route.

For a manual repair of older images that already have `/api/buddy/ws`, run
`scripts/maintenance/patch-nginx-harness-ws.sh` as root on the device. It resolves
symlinked sites before editing, saves a separate copy of the original file under
`/var/backups/nginx-harness-ws/`, and restores that file if `nginx -t` fails,
preserving the enabled symlink. An existing Harness route is left unchanged.

Directory installs have the same recovery contract. Before a web update, the
updater stops nginx, swaps the fully unpacked staged bundle into place, and
retains the previous bundle at `/root/bootstrap/rollback/web.previous` together
with nginx's prior active/inactive state. It then requires `index.html`, a valid
`nginx -t`, and—when nginx had been running—a successful loopback `GET /`.
Failure automatically restores the saved bundle and service state. An operator
can also run `software-update rollback web`; the rejected version is recorded in
`rollback_versions` just like a binary rollback.

For a device profile, the updater stages the ZIP, stops only the `os-server` and
`hal` services that were active, and retains the old profile at
`/root/bootstrap/rollback/device.previous`. It also snapshots the exact files
covered by the old or new `rootfs/` overlay in `device.previous.rootfs`; rollback
therefore restores overwritten files and removes files introduced only by the
rejected profile. Successful OTA replaces the generated `/opt/hal/.env`. The profile
must contain `ROBOT.md`; each service that was active must recover and answer
its loopback health endpoint. A failed check restores the known-good profile and
its prior service state automatically. Use `software-update rollback device` for
an operator rollback; the rejected device-profile version is then blocked.

### Optional hardware overrides

The OS reads the single-line name in `/etc/autonomous/hardware-profile`.
Missing, empty or `standard` selects the fresh base device package without
an override: no new USB checks or changed legacy audio/Live defaults. Other names must match
`[a-z][a-z0-9_-]{0,63}` and select `overrides/<name>/` inside that device's package.
The identity file is machine-owned and must not be shipped in an overlay.

`scripts/provision/apply-overrides.py` is included in every released device ZIP.
It merges the selected `rootfs/opt/hal/.env` over the shared env, copies other
selected `rootfs/` files into the staged rootfs, and applies the optional integer
`startup_volume`/`max_volume` fields from `profile.json` to `ROBOT.md`/`SAFETY.md`.
Its optional `capabilities` map accepts boolean values and activates or comments
existing capability entries in `ROBOT.md` frontmatter; it cannot invent a
capability declaration. Selected `device/*.json` files replace existing
top-level device JSON files in full, without merging their contents. The
renderer validates these inputs before writing changes and rejects invalid
capabilities or replacement files. Product-specific values live only in the
device package. For example:

| Profile | Voice microphone | Sensing microphone | Speaker | Startup / maximum volume | SEN63C |
|---|---|---|---|---|---|
| `standard` | Jieli `device_micro2` | CMedia `device_cmedia` | Base speaker | Base defaults | Disabled |
| `pro` | Jieli `device_micro2` | CMedia `device_cmedia` | Base speaker | Base defaults | Enabled on `orangepi_sun60`, bus `0` |
| `pro-respeaker-lite` | ReSpeaker Lite processed left input | Onboard ES8389 (`sndi2s4`) | ReSpeaker Lite softvol `Speaker` | 35 / 35 | Enabled on `orangepi_sun60`, bus `0` |
| `pro-xvf3800` | XVF3800 (`Array`) | Onboard ES8389 (`sndi2s4`) | XVF3800 | 77 / 77 | Enabled on `orangepi_sun60`, bus `0` |

Each Pro directory contains `profile.json` with
`capabilities: {"environment": true}`, plus `device/sen63c.json` enabling the
OrangePi sensor. Only the Lite and XVF3800 variants override volume defaults. The base Standard package comments that capability and
disables SEN63C, so it does not start acquisition, write the SEN63C bus clock,
poll the environment UI or qualify for the environment skill. SEN55/SCD41
and boards without matching entries remain disabled even on Pro.

`pro-respeaker-lite` preserves the former `pro` overlay in full, including its
ALSA, udev and softvol service files: HAL AEC enabled, Silero threshold `0.10`,
Live off, and always-on uplink. `pro` is Standard audio plus SEN63C only:
it has no `rootfs/`, `.env`, ALSA, udev, service or volume override. Its rendered
ALSA configuration and safety bounds are byte-identical to Standard;
microphones, speaker, processing and volume defaults all use the base package.
The common renderer only adds the selected profile's saved-volume namespace
(`HAL_VOLUME_STATE_PATH=/root/config/.volume-pro`) to the generated HAL environment. `pro-xvf3800` keeps XMOS AEC, software
AEC off, Live on and always-on uplink.

ReSpeaker Lite (XMOS XU316, USB `2886:0019`, ALSA card `Lite`) uses S16_LE
2 ch 16 kHz audio. `pro-respeaker-lite` requires its speaker wired through Lite. This card has no ALSA mixer; a udev-triggered oneshot opens
the PCM before `hal.service` to create the softvol control for boot volume
restore. The 35% setting was tuned on the Lite assembly, not calibrated as an
acoustic equivalence across devices. Standard retains its audio defaults and
ceiling. The renderer does not detect, flash or retune attached hardware.

Existing `pro` machines using the Lite microphone must explicitly select
`pro-respeaker-lite` before installing the updated device package to preserve
their full audio configuration. There is no automatic marker migration. Changing the
marker alone does not replace installed files; reinstall the device package.

Image builders/fresh setup apply a selected override before installing rootfs.
OTA starts from the fresh base archive, not an already rendered Pro tree, so
switching to Standard restores its disabled sensor and capability defaults.
OTA renders before stopping services and taking the rootfs snapshot; missing
helper/selected overlay or render failure leaves the installed package running. Copy
or health failure rolls back the profile and live rootfs together. Rollback does
not change the machine's hardware-profile selection.

For new images, select the assembly at build time:

```bash
make -C scripts/imager build TARGET=opi DEVICE_TYPE=lamp VARIANT=pro OTA_METADATA_URL=...
```

`VARIANT` is the only build input for hardware selection. The overlay stage writes
`/etc/autonomous/hardware-profile` before applying `overrides/pro`; OTA later reads
that file. Empty or `VARIANT=standard` removes any stale selection from the image
and uses the unchanged base package. The selection is not baked into the reusable
base cache. Nonstandard variants add a suffix to the final image/release filename.
Unknown variants or packages without the override helper fail the build.

Machines without a hardware-profile marker remain Standard, including machines
that previously received the global SEN63C default; no automatic Pro migration
is performed. To retain SEN63C on Pro hardware, explicitly choose `pro`, `pro-respeaker-lite` or
`pro-xvf3800` and reinstall the device package. Upgrades from older releases
need both HAL (the I2C clock fix) and the device package (capability and sensor
configuration). A HAL-only update does not install these profile defaults.

Provision a selected assembly by writing its name before installation. For an
existing machine, first install the new updater and HAL/os-server, then:

```sh
sudo mkdir -p /etc/autonomous
printf 'pro\n' | sudo tee /etc/autonomous/hardware-profile
sudo software-update device
```

Check the update succeeds before using the device. If conversion fails, restore
the previous hardware-profile selection to match the restored package. To return
to legacy hardware, remove the selection and install a fresh device package via
OTA; simply deleting the file does not undo an already rendered overlay. An old
updater cannot apply overrides: automatic bootstrap refreshes its updater first,
but manual/failed-refresh deployments must update it before selecting a profile.

Every selected profile saves volume separately as `config/.volume-<name>`
(HAL and os-server agree), including the capability/device-JSON-only `pro`
profile. The common renderer generates `HAL_VOLUME_STATE_PATH` accordingly;
absent/Standard retains `config/.volume`. This prevents a
legacy speaker's saved percentage from overriding the new assembly's startup
level. HAL addresses both `PCM,0` and `PCM,1`, excluding capture controls; mixer
write failure returns 503 rather than persisting a false success. UI, voice and
startup volume remain subject to the selected ceiling. No Live UI or delegated
TTS barge-in behavior is added; direct generated `.env` edits are overwritten
by the next successful device OTA, as before.

Devices without `signing_public_key` deliberately remain in legacy mode: they
read the top-level entries and emit a warning rather than failing OTA. This is a
compatibility bridge, not a trust guarantee; pin the public key to opt in to
verified OTA.

### OTA security posture (operator-visible)

The warning above is a log line, so a device silently stuck in legacy mode is
invisible to a fleet operator. Both processes expose the posture as data:

| Endpoint | Served by | Notes |
|----------|-----------|-------|
| `GET http://127.0.0.1:8080/security` | `bootstrap-server` | Source of truth: the worker holds the pinned key and performs verification. Loopback only. |
| `GET /api/system/ota-security` | `os-server` | Proxies the above verbatim inside the standard `{"status":1,"data":…}` wrapper. `502` when the bootstrap worker is unreachable. |

```json
{
  "mode": "verified",
  "metadata_format": "autonomous-ota/v1",
  "key_fingerprint": "9f2c1ab30d4e5f60",
  "artifact_checksums": true,
  "last_metadata_fetch": {
    "at": "2026-08-24T09:12:03Z",
    "verified": true
  }
}
```

- `mode` is `verified` when `signing_public_key` is provisioned, `legacy`
  otherwise. This is the single field to alert on across the fleet.
- `key_fingerprint` is the first 16 hex characters of the SHA-256 of the pinned
  key, so operators can confirm *which* key a device trusts (and spot devices
  left on a rotated-out key) without the endpoint echoing key material. Absent
  in legacy mode.
- `artifact_checksums` reports whether per-component SHA-256 digests are
  enforced. It follows `mode`: digests are only meaningful when the metadata
  carrying them is authentic.
- `last_metadata_fetch` is the outcome of the most recent fetch, including
  transport failures (`error` is set, `verified` is `false`). It is absent until
  the first fetch completes after a restart, so a device that has never reached
  its feed is distinguishable from one whose feed is healthy.

### Signed-only cutover

Signed metadata is published as a superset: the payload's component entries stay
at the top level for already-deployed workers, with the authenticated document
under `signed`. That compatibility copy is what keeps legacy mode exploitable,
so it has to be retired. The rollout:

1. **Publish signed** (done). Release writers add the `signed` envelope whenever
   `OTA_SIGNING_PRIVATE_KEY` and `OTA_SIGNING_KEY_ID` are set. Devices ignore it
   until a key is pinned.
2. **Provision keys.** New devices get `OTA_SIGNING_PUBLIC_KEY` at setup; the
   existing fleet gets it written into `/root/config/bootstrap.json`. No
   redeploy is needed — the worker reads the key on the next config load.
3. **Confirm the fleet.** Poll `GET /api/system/ota-security` and require
   `mode == "verified"` with the expected `key_fingerprint` on every device.
   This step is the gate: do not proceed while any device reports `legacy`.
4. **Cut over.** Publish with `OTA_METADATA_SIGNED_ONLY=1`, which drops the
   top-level copy so the document contains only `signed`. An unmigrated device
   then stops updating (loudly) instead of updating from an unauthenticated
   source — the intended failure direction.

Rollback for step 4 is publishing once more without the flag; devices already in
verified mode are unaffected either way, since they read only `signed`.

**Wait-then-retry when unprovisioned**: if `metadata_url` is empty (device not
set up yet), `Serve()` does not start the poll loop or healthcheck server. It logs
`waiting for metadata_url in bootstrap config` and reloads
`/root/config/bootstrap.json` every 30s until a URL appears, then proceeds.
Nothing is silent.

### State (`/root/bootstrap/state.json`)

Tracks last known installed version per component:

```json
{
  "components": {
    "os-server": "1.2.3",
    "bootstrap": "1.0.5",
    "web": "0.9.0",
    "openclaw": "2026.6.10",
    "hal": "1.0.0"
  }
}
```

State saves are atomic: bootstrap writes and syncs a temporary file in the same
directory before renaming it into place. If an older state file is malformed
(for example, after an interrupted legacy write), bootstrap preserves it as
`state.json.corrupt-<timestamp>`, logs a warning, and resumes with empty state
instead of failing to start OTA polling.

### Core Loop (`bootstrap/bootstrap.go`)

```
checkLoop():
  1. checkOnce() immediately on startup
  2. Sleep poll_interval (default 5m)
  3. Repeat

checkOnce():
  1. Fetch OTA metadata JSON
  2. For each key [os-server, bootstrap, web, hal]:
     → reconcile(key, metadata[key])
  NOTE: OpenClaw OTA is temporarily disabled (reconcileOpenClawFromNpm commented out)
  3. Save state

reconcile(key, target):
  1. Detect current installed version
  2. If current == "" AND the component is not installed → skip silently
  3. floor = target.min_version (default target.version if empty)
  4. If current >= floor → sync state, return (at/above approved floor)
  5. If current < floor →
     a. Set LED orange breathing (OTA in progress)
     b. applyUpdate(key, target)   # installs target.version via software-update
     c. Success → green flash | Failure → red pulse for 10 s, then restore LED state
```

> Manual `software-update <key>` over SSH does NOT pass through `reconcile` — it
> installs `target.version` directly, bypassing the `min_version` floor.

#### Step 2 — components this device does not have

Metadata lists every published component; no device runs all of them. A Reachy
Mini has no `claude-desktop-buddy`; a device on a non-OpenClaw runtime has no
`openclaw`. For those `detectVersion` returns `""`, which sorts below every
floor — so without this gate the worker reports "update available", speaks the
update cue, lights the OTA LED and fails the install **every poll, forever**.

`componentInstalled(key)` asks a coarser question than `detectVersion`: *is the
artifact here at all*, not *which version*. A component that is present but
unreadable still counts as installed, so OTA self-repair keeps working (an
`os-server` whose `--version` is broken is still updated); only a genuinely
absent one is skipped.

| Component | Counts as installed when |
|---|---|
| `bootstrap` | always (it is the running worker, so it can self-update) |
| `os-server` | `os-server` is on `$PATH` |
| `openclaw` | `openclaw` is on `$PATH` |
| `web` | `/usr/share/nginx/html/setup/` exists |
| `hal` | `/opt/hal/` exists |
| `claude-desktop-buddy` | `/opt/claude-desktop-buddy/` exists |
| `device` | `$DEVICES_DIR/<type>/` exists (default `/opt/devices`) |

### OTA LED Feedback

Bootstrap uses `lib/hal` to show update status on LEDs. See [status-led.md](../robots/lamp/docs/status-led.md) for full spec.

| Phase | LED |
|-------|-----|
| Downloading + installing | Orange breathing `[16, 8, 0]` |
| Success | Green flash `[0, 12, 4]` for 1 second, then restore the user-selected LED look or the ambient resting look when none exists |
| Failure | Red pulse `[16, 2, 2]` for 10 seconds, then restore the user-selected LED look or the ambient resting look when none exists |

### Invariants the updater must keep (learned the hard way)

A device lost `/opt/hal`, its staging dir AND its rollback backup from two clicks
on the web update button 30 s apart. The five safeguards below are implemented
in `scripts/provision/software-update`, and each is easy to reintroduce:

1. **One run at a time** (`flock` on `/var/lock/software-update.lock`). Every
   branch publishes by `mv`-ing the live tree to `<name>.previous`, so a second
   concurrent run `rm -rf`s the first run's only backup and then fails on the
   missing source. The per-target 30 s rate limit in os-server does not prevent
   this.
2. **A missing install directory is a REINSTALL, not an error.** `mv "$HAL_DIR"`
   aborting on an absent `/opt/hal` meant the one command that could repair the
   device refused to run.
3. **Publish with the live tree's mode** (`publish_mode`). `mktemp -d` is 0700;
   moving such a directory onto `/usr/share/nginx/html/setup` makes nginx
   (www-data) answer 403 to everything, so the health check fails and the update
   rolls itself back — forever, on every device.
4. **A virtualenv is not relocatable** (`relocate_venv_scripts`). Building
   `.venv` inside the staging dir bakes `#!/opt/.hal.new.XXXXXX/.venv/bin/python`
   into every console script; the unit then dies with `203/EXEC` once staging is
   gone. Invisible while an old `.venv` is inherited — it only bites on a fresh
   install, i.e. exactly the recovery path.
5. **Failure feedback must be temporary.** An OTA failure may pulse red to make
   the outcome visible, but it restores the user-selected LED look or ambient
   resting look after 10 seconds; failure feedback must never latch the strip.

Plus one that made all of the above hard to see: **the saved service state is a
snapshot taken at the start of a run**, so a run that begins while a previous
failed update left the unit down records "inactive" and every later update
faithfully restores "down". `unit_wanted_active` now treats a systemd-`enabled`
unit as "must run", and `check_web`/`check_hal` probe what the unit is ACTUALLY
doing instead of skipping the probe when the snapshot said it should be down (the
old form reported success while the web UI served 403s).

### `POST /force-update/:target` vs `POST /force-check/:target` (bootstrap, loopback)

Two different acts, and mixing them up is what made the web button look broken:

| Endpoint | Meaning | `min_version` |
|---|---|---|
| `force-update/<key>` | Install the published `version` NOW — the same thing `software-update <key>` does over SSH | **ignored** (the floor stages the fleet, not one deliberate operator) |
| `force-check/<key>` | Re-run the AUTOMATIC decision for that component | **respected** — a component at or above the floor does nothing |

The web Versions card's `update` button is `force-update` (via
`POST /api/system/software-update/:target`). Both are limited to the same target
allowlist, including `bootstrap` and `device`; a Bootstrap update detaches its installer so
the replacement worker can restart safely. `componentInstalled` still refuses a
component this device does not have.

### `GET /versions` (bootstrap, loopback)

Reports `{current, target, min_version, update_available, held_by_floor}` for every
component this device actually has (`componentInstalled`), so the agent-CLI entry
is the runtime it runs and nothing else. `held_by_floor` means a newer build is
published but `min_version` was not promoted to it — the worker will refuse it,
which is why the web Versions card treats held components as "no update".
The installed device profile is reported as `device`, resolved from nested
`metadata.devices.<device_type>` rather than the flat component list.
os-server proxies this as `GET /api/system/ota-versions` and, for the cloud, as
the MQTT `data` kind `system.ota_versions` (both via `system/ota`; the cloud's
`system.software_update` is the MQTT twin of the Versions card's button).

### Version Detection Per Component

| Component | How to Detect Current Version |
|---|---|
| `os-server` | Run `os-server --version`, parse output |
| `bootstrap` | Compiled-in constant `config.BootstrapVersion` (ldflags) |
| `web` | Read file `/usr/share/nginx/html/setup/VERSION` |
| `device` | Read `/opt/devices/<device_type>/VERSION` |
| `openclaw` | Run `openclaw --version`, extract semver with regex |
| `hal` | Run `/opt/hal/venv/bin/python -m hal --version` OR read `/opt/hal/VERSION` file |
| `codex` / `claudecode` / `opencode` | Run `<cli> --version`, extract semver from line one (`cliSemver`) |
| `picoclaw` | Read `/usr/local/lib/os-runtimes/picoclaw/installed-version` — its `version` output carries no semver |
| `hermes` | Run `hermes --version` ("Hermes Agent v0.21.1 (2026.9.7)"), extract semver from line one (`cliSemver`) |

### Update Application Per Component

| Component | Update Steps |
|---|---|
| `os-server` | Run `software-update os-server` (blocks up to 10 min) |
| `bootstrap` | Spawn detached `software-update bootstrap` (self-update, survives restart) |
| `web` | Run `software-update web` |
| `device` | Run `software-update device` for `devices.<device_type>`; apply any explicitly selected hardware override, then install rootfs including generated HAL `.env` |
| `openclaw` | ~~Run `npm install -g openclaw@{version}` → `systemctl restart openclaw`~~ (temporarily disabled) |
| `hal` | Run `software-update hal` → `systemctl restart hal` |
| `codex` / `claudecode` / `opencode` / `picoclaw` | Run `software-update <key>` — only on the device whose `agent_runtime` IS that runtime |
| `hermes` | Run `software-update hermes` — only when `agent_runtime` is hermes, the metadata entry carries `commit`, AND the on-device updater is the pinning one (`updaterSupportsHermesPin`: it reads `.hermes.commit`). An unpinned entry (no `commit`) is skipped by both the loop and `/versions`, so the web button never appears for it — `hermes update` would land on upstream HEAD and the floor could never be met. |

Manual and force OpenClaw updates run `software-update openclaw`. Before
installing the version selected by OTA metadata, the updater reads that npm
package's `engines.node` range and checks it with npm's bundled semver library.
A compatible Node installation is retained. Otherwise, it installs system
Node 24.x through NodeSource and apt, then checks the range again before
installing OpenClaw and restarting its service. Missing engine metadata,
compatibility-check errors, or a failed/incompatible Node upgrade abort before
the OpenClaw install and restart. Node is a shared system dependency; a
successful Node upgrade is not rolled back if the later OpenClaw install fails.
This prerequisite handling does not change the automatic-update gate above.

OrangePi image defaults match OTA metadata checked on 2026-09-11: OpenClaw
`2026.9.3` and Hermes `0.21.1`. The builder installs the latest NodeSource 26.x
package even on reused base images and requires at least Node `26.8.2` (the
current upstream release at that check). Hermes's installer and checkout are
pinned to release `v2026.9.7`, commit `2237be355906fbe6065ce1815711eee52b2d646e`,
and a different reported CLI version fails the build. These are image-build
defaults; presync and subsequent `software-update` behavior are unchanged.

After the OpenClaw package and plugin updates, the updater stops
`openclaw.service` and runs `openclaw doctor --fix --non-interactive
--no-workspace-suggestions` with `HOME=/root` and OpenClaw home/state set to
`/root/.openclaw`. This migrates legacy workspace/state stores before the new
gateway starts. If stopping the service or running doctor fails, the updater
exits without restarting it; a failed migration leaves the gateway stopped
for repair. After restart, success requires an authenticated
`gateway status --require-rpc --timeout 5000` probe (up to 12 attempts,
5 seconds between attempts). Exhausted probes report update failure, even if
systemd considers the process active. Package/state changes are not
automatically rolled back on migration or readiness failure.

`software-update hermes` has two modes, chosen by the metadata entry:

- **Pinned** (`hermes.commit` present — written by `scripts/release/upload-hermes.sh
  <version> <upstream-tag|sha>`, which resolves the date tag upstream uses,
  e.g. `v2026.9.7` → `2237be35…` = 0.21.1): fetch the upstream installer AT
  that commit and drive its `repository`, `venv`, `python-deps`, `path` stages
  with `--commit <sha> --force-commit` — the flags `scripts/imager/build-orangepi.sh`
  bakes the image with. `--force-commit` also rolls BACK a checkout that an
  unpinned `hermes update` had moved past the release. The landed `hermes
  --version` must equal the published semver; a mismatch fails the update
  (the metadata pair is wrong). This is the mode bootstrap auto-applies.
- **Unpinned** (no `commit`, entries published before pinning existed): `hermes
  update` to upstream HEAD; the published version is only expected, so a
  mismatch warns. Manual SSH only — bootstrap never applies it.

Both modes check Node first. The supported build range follows the upstream
Hermes installer: Node 22.22+ within 22.x, 24.11+ within 24.x, or stable 26+.
Incompatible Node is upgraded to system Node 24.x with the same helper used for
OpenClaw, then rechecked; a failed check or upgrade aborts before any change.
Both modes end by restarting `hermes-gateway` and then **os-server**: os-server
patches two Hermes sources at `EnsureOnboarding` (`cache_usage.go` →
`api_server.py`, `runs_patch.go` → `api_server_runs.py`) and a fresh checkout
drops them — without the restart the next turns fall back to `/v1/responses`
and the Flow Monitor shows no cache column.
`runtimes/hermes/install.sh` (runtime switch) still installs upstream HEAD
unpinned; the reconcile loop then brings the device onto the pinned commit on
its next poll, exactly as it would for any other stale component.

**Why the agent CLIs are gated on `agent_runtime`, not on the binary:**
`scripts/imager/build-orangepi.sh` bakes every agent CLI onto every lamp /
intern-v2 image regardless of `DEFAULT_AGENT`, so `inPath("codex")` is true even
on a device running Hermes. Gating on presence would make each poll announce
"device is updating", turn the strip orange, download a CLI the device never
uses, and restart a unit that does not exist — forever.
`componentInstalled` therefore compares the key against `agent_runtime` in
`/root/config/config.json`; an unreadable/unset value means "not this runtime"
(skip), which is the safe direction.

A **second gate** guards the other direction: `updaterSupports(key)` reads
`/usr/local/bin/software-update` and requires its exact branch guard
(`[ "$APP" = "<key>" ]`) to be present. `software-update` reaches a device only
via the imager or `setup.sh` — never over OTA — so a device provisioned before
these keys existed keeps an updater that answers `Unknown app: codex` forever.
Without the gate, every poll (5m) on such a device would speak "device is
updating", breathe orange, fail the apply, and briefly pulse red before restoring
the normal LED state. With it, those devices simply never receive agent-CLI
updates — the only outcome available to them anyway — silently. The match is the
branch guard, not the bare key: the key also appears in comments and in the usage
strings of an updater that does not implement it.

**Healing a device that has an old updater.** `make upload-setup` also publishes
the updater raw at `{CDN}/software-update`. Bootstrap now refreshes the on-device
copy from there **automatically**, at the top of every check cycle, before it
reconciles any component — the updater is not an OTA component, so this is the
only automatic path it has (`system/bootstrap/updater_refresh.go`). The URL is
derived from `metadata_url` (`{base}/ota/metadata.json` → `{base}/software-update`)
rather than configured separately, so the two can never point at different
releases.

The refresh is deliberately timid. It skips while a force update is installing
(bash reads a script lazily, so replacing a *running* updater would resume
execution at an arbitrary offset — which is also why the script must never
update itself), it validates the download with `bash -n` **before** anything
touches the live file, it stages into the same directory and `rename`s so the
swap is atomic, and every failure leaves the existing updater untouched. A
device must never end up without a working updater.

The manual one-liner below still works and is still the right tool when you need
the new updater *now* rather than at the next poll:

```bash
sudo curl -fsSL https://cdn.autonomous.ai/os/software-update -o /tmp/su \
  && sudo bash -n /tmp/su \
  && sudo install -m 0755 /tmp/su /usr/local/bin/software-update
```

`bash -n` before `install` is the point: a truncated download must not replace a
working updater. After this the device picks up agent-CLI updates on its next
poll — no restart needed, bootstrap re-reads the file every cycle. OpenClaw keeps its `inPath` check: it is
npm-installed per device rather than baked, and older provisioning may leave
`agent_runtime` unset.

---

## 5. Software Update Script (`/usr/local/bin/software-update`)

Bash script installed by setup.sh (and baked into the image by the imager).
Called by bootstrap worker to apply updates.

It reads the OTA metadata URL from `metadata_url` in `/root/config/bootstrap.json`
(an explicit `OTA_METADATA_URL` env var overrides it for manual/debug runs), and
aborts with an error if neither is set — no compiled-in URL.

### HAL Case

The updater finds `uv` on `PATH`, then at `/root/.local/bin/uv`, then at
`/home/pollen/.local/bin/uv` (Reachy's installer location). Before stopping HAL,
it selects Python extras from `DEVICE_TYPE` in `/opt/hal/.env`, falling back to
`device_type` in `/root/config/config.json`: `reachy-mini` uses `hardware + reachy`
to retain the Pollen SDK; every other device uses `hardware + aec + pipecat`.

> **The uv cache lives outside the runtime tree** (`/opt/.uv-cache-hal`, next to
> `/opt/hal` so uv can hardlink into the new venv). It used to sit at
> `/opt/hal/.uv-cache`, so every update copied it — measured at 2.5 GB, beside a
> 2.3 GB `.venv` — into the staging tree before syncing: ~4.8 GB shuffled across
> eMMC before any real work, minutes of HAL downtime, and a cache duplicated per
> update. Neither directory is copied now; an in-tree cache is migrated once.
> Measured on a lamp: **5-6 min → 41 s**, and `/opt/hal` shrank from ~4.8 GB to
> 98 MB (the venv hardlinks into the shared cache).

> **The publish window is crash-safe.** Between "move the live tree aside" and
> "rename the staged tree into place" the component does not exist on disk. A
> `trap` records that pending move and puts the tree back if the process exits
> first — an interrupted SSH (HUP), a `systemctl restart bootstrap` killing the
> cgroup (TERM), or any failure path. SIGKILL and power loss cannot be trapped;
> those land on the reinstall path instead (a missing tree is installed fresh).

```bash
"hal")
    # Preserve the complete runtime and prior service state.
    systemctl stop hal
    mv /opt/hal /root/bootstrap/rollback/hal.previous

    # UV_BIN and HAL_EXTRA are resolved before stopping HAL.
    # Build a fresh venv; preserve .env and use the external shared cache.
    unzip -q "$ZIP" -d /opt/.hal.new
    cp -a /root/bootstrap/rollback/hal.previous/.env /opt/.hal.new/
    HAL_EXTRA_ARGS=(--extra "$HAL_EXTRA")
    if [ "$HAL_EXTRA" != "reachy" ]; then
        HAL_EXTRA_ARGS+=(--extra pipecat)
    fi
    (cd /opt/.hal.new && UV_CACHE_DIR=/opt/.uv-cache-hal "$UV_BIN" sync --python 3.12 --extra hardware "${HAL_EXTRA_ARGS[@]}")
    mv /opt/.hal.new /opt/hal

    systemctl restart hal
    curl -fsS http://127.0.0.1:5001/health
    # Any staging or health failure restores hal.previous and its old state.
    # Operators can also run: software-update rollback hal
    ;;
```

### Codex Case

The Codex CLI is a static musl binary published on GitHub releases, so unlike
every other component nothing of ours is hosted on GCS for it: metadata carries
only `codex.version` (no `url`/`sha256`), and the release URL is composed from
that version. Metadata stores the **bare semver** (`0.149.1`) because that is
what `codex --version` prints; the upstream tag prefix (`rust-v`) is re-added
when downloading. Keep this download in step with the same one in
`runtimes/codex/install.sh` and `scripts/imager/build-orangepi.sh`.

```bash
"codex")
    curl -fsSL https://github.com/openai/codex/releases/download/rust-v${VERSION}/codex-aarch64-unknown-linux-musl.tar.gz
    tar -xzf …            # the extract IS the integrity check (no sha256 for upstream artifacts)
    install -D -m 0755 /usr/local/bin/codex /root/bootstrap/rollback/codex.previous
    install -m 0755 …     /usr/local/bin/codex
    codex --version       # abort if the new binary is not runnable
    # codex.service only exists once the runtime was switched to codex; every
    # lamp/intern-v2 image bakes the BINARY regardless, so the restart is
    # conditional. Operators can roll back: software-update rollback codex
    systemctl restart codex
    ;;
```

Publish a version with `make upload-codex <bare-semver>`, release it to the
fleet with `make promote-codex`.

Only the binary is touched: `config.toml`, `.env`, and the persona stay owned by
the presync hook, so an update cannot clobber a device's Codex configuration.

### Claude Code Case

Unlike Codex, Claude Code is **not** a binary we place: Anthropic's installer
owns `/root/.local/share/claude/versions/<ver>` and repoints
`/root/.local/bin/claude` at it, and `/usr/local/bin/claude` is only our symlink
into that. The update therefore re-runs the installer pinned to the published
version (it takes the version as a positional arg: `install.sh [stable|latest|VERSION]`).

```bash
"claudecode")
    HOME=/root curl -fsSL https://claude.ai/install.sh | bash -s -- "$VERSION"
    ln -sf /root/.local/bin/claude /usr/local/bin/claude   # installer may repoint its own symlink
    claude --version                                       # abort if not runnable
    systemctl restart claudecode                           # conditional: unit exists only when the runtime is active
    ;;
```

There is deliberately **no `.previous` backup / rollback target**: the installer
keeps the old `versions/<ver>` directory, so going back is
`software-update claudecode` with an older version published — not a restore of
a file we saved. Publish with `make upload-claudecode <bare-semver>`, release
with `make promote-claudecode`.

### OpenCode Case

OpenCode uses its official installer (arch detection + extraction are upstream's
job); we only pin the version and force the install dir. Mirrors
`runtimes/opencode/install.sh` and the imager pre-bake — keep the three in step.

```bash
"opencode")
    install -D -m 0755 /usr/local/bin/opencode /root/bootstrap/rollback/opencode.previous
    curl -fsSL https://opencode.ai/install | OPENCODE_INSTALL_DIR=/usr/local/bin bash -s -- --version "$VERSION"
    # …then the same belt-and-suspenders copy install.sh does, in case the
    # installer ignored OPENCODE_INSTALL_DIR and used ~/.opencode/bin.
    opencode --version
    systemctl restart opencode      # conditional: unit exists only when the runtime is active
    ;;
```

`OPENCODE_INSTALL_DIR` MUST prefix `bash`, not `curl` — in a `VAR=x curl … | bash`
pipeline the variable binds to `curl` only. Because this one really is a binary
at a stable path, it does get a `.previous` backup: `software-update rollback
opencode` works, unlike claudecode. Publish with `make upload-opencode
<bare-semver>`, release with `make promote-opencode`.

### Hermes Case (SSH-only — NOT pinnable, so never auto-applied)

Hermes is a git install; `runtimes/hermes/install.sh` stamps
`/usr/local/lib/hermes-agent/.install_method=git` precisely so the upstream
updater recognizes it. That updater takes **no target version** — `hermes update`
always moves to upstream HEAD. The published `hermes.version` therefore decides
*when* the fleet updates (via `min_version`), not *which* build it gets.

```bash
"hermes")
    hermes update                       # no version arg exists upstream
    hermes --version                    # abort if not runnable
    # Landed version != metadata version → WARN, not fail: nothing on the
    # device could have pinned it.
    systemctl restart hermes-gateway    # unit is hermes-gateway.service, not hermes.service
    ;;
```

The unit name is the one declared in `/usr/local/lib/os-runtimes/hermes/service`
(`hermes-gateway`), and the run reports the version Hermes actually landed on,
not the requested one. No `.previous` backup: like claudecode, the install is
owned by the upstream tool, not by a file we copy.

### PicoClaw Case (the odd one out on versioning)

PicoClaw is a raw binary from our OWN GitHub releases (no tarball, unlike codex).
Its published `version` is the release **TAG** (`v0.3.1-fixvision`), not a semver:
`picoclaw version` prints an unrelated build description
(`nightly-44-g1959045c-dirty`), so the tag cannot be recovered from the binary.
The update therefore stamps the installed tag to
`/usr/local/lib/os-runtimes/picoclaw/installed-version` — that stamp, not
`picoclaw version`, is what any version check must read.

```bash
"picoclaw")
    curl -fsSL https://github.com/autonomous-ai/picoclaw/releases/download/${VERSION}/picoclaw-linux-arm64
    picoclaw --no-color version         # run it FROM THE TEMP DIR first: the only integrity gate
    install -D -m 0755 /usr/local/bin/picoclaw /root/bootstrap/rollback/picoclaw.previous
    install -m 0755 …                   /usr/local/bin/picoclaw
    echo "$VERSION" > /usr/local/lib/os-runtimes/picoclaw/installed-version
    systemctl restart picoclaw          # conditional: unit exists only when the runtime is active
    ;;
```

`make upload-picoclaw <release-tag>` verifies the tag actually has a
`picoclaw-linux-arm64` asset before publishing — a typo would otherwise only
surface as a failed OTA on every polling device. Rollback works
(`software-update rollback picoclaw`).

---

## 6. HAL Runtime — Source & Integration

### Source Strategy: Copy + Track Manually

HAL runtime code is **copied** from the upstream open-source project into this mono-repo, then modified heavily.

**Why copy, not submodule/subtree:**
- We need to **remove** LiveKit/OpenAI integration (replaced by OpenClaw)
- We need to **add** HTTP API server (Flask/FastAPI) for OS Server to bridge to
- We need to **add** DisplayService (GC9A01 eyes + info, not in original)
- We need to **modify** services to work with our architecture
- The overlap is drivers only (~30-40% of their code), the rest is rewritten

**Upstream tracking:**
- Source: `https://github.com/humancomputerlab/lelamp_runtime`
- Record the upstream commit hash in `hal/UPSTREAM.md` when copying
- Periodically check upstream for driver-level fixes (servo protocol, LED timing, etc.)
- Cherry-pick relevant driver changes manually
- Ignore upstream AI/LiveKit changes (we replaced that entirely)

**Implementation steps:**
1. Clone `humancomputerlab/lelamp_runtime` to a temp directory
2. Copy driver code (`services/motors.py`, `services/rgb.py`, `services/audio.py`, `services/service_base.py`) into `hal/services/`
3. Remove all LiveKit, OpenAI, and conversation code
4. Add `hal/server.py` — new HTTP API server (FastAPI)
5. Add `hal/services/display.py` — new DisplayService for GC9A01
6. Create `hal/UPSTREAM.md` with source commit hash and date
7. Test on device with actual hardware

### Mono-repo Layout

HAL lives inside this repo as a Python subfolder alongside Go and TypeScript:

```
autonomous/
├── system/          # Go code (forked from lobster)
│   ├── cmd/              # Go entrypoints
│   ├── server/           # Go HTTP layer
│   ├── internal/         # Go business logic
│   ├── bootstrap/        # Go OTA worker
│   └── domain/           # Shared structs
├── system/web/      # TypeScript/React SPA (copied from lobster, renamed intern→lamp)
├── hal/               # Python hardware drivers (NEW)
│   ├── __init__.py       # Package init, exposes __version__
│   ├── server.py         # HTTP API server (FastAPI) — NEW, not from upstream
│   ├── system/
│   │   ├── motors.py     # MotorsService — 5x Feetech servo (from upstream)
│   │   ├── rgb.py        # RGBService — 64x WS2812 LED (from upstream)
│   │   ├── audio.py      # Audio — amixer, playback (from upstream)
│   │   ├── display.py    # DisplayService — GC9A01 LCD (NEW, not from upstream)
│   │   └── service_base.py  # Event-driven ServiceBase (from upstream)
│   ├── config.py         # Runtime config
│   ├── requirements.txt  # Python dependencies
│   ├── VERSION           # Plain text version string
│   └── UPSTREAM.md       # Tracks source commit from humancomputerlab/lelamp_runtime
├── resources/
│   └── openclaw-skills/  # SKILL.md files
├── scripts/
│   └── setup.sh
├── go.mod
├── Makefile
└── CLAUDE.md
```

3 languages (Go, Python, TypeScript), 3 folders, 1 repo. Each has its own build, but managed together.

### HAL OTA Package

For OTA distribution, HAL is zipped from the `hal/` folder:

```
hal-{version}.zip
├── hal/                  # Full Python package
├── requirements.txt
└── VERSION
```

### HAL HTTP API (FastAPI on port 5001)

The HAL Python runtime exposes its own HTTP API on `127.0.0.1:5001`. OS Server (Go, port 5000) bridges OpenClaw skill requests to this API. Nginx proxies `/hw/*` for same-machine callers only — external clients receive 403. Swagger UI at `/hw/docs` is not accessible from LAN.

```
OpenClaw LLM → curl 127.0.0.1:5000/api/servo → OS Server → http://127.0.0.1:5001/servo → HAL Python → Hardware
External     → http://<device-ip>/hw/docs    → nginx → 403 Forbidden
```

#### Endpoints (v0.2.0)

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Hardware availability (servo, led, camera, audio) |
| `/servo` | GET | Available recordings + current state |
| `/servo/play` | POST | Play animation by name |
| `/led` | GET | LED strip info |
| `/led/solid` | POST | Fill with single color |
| `/led/paint` | POST | Set per-pixel colors |
| `/led/off` | POST | Turn off all LEDs |
| `/camera` | GET | Camera info (resolution, availability) |
| `/camera/snapshot` | GET | Capture single JPEG frame |
| `/camera/stream` | GET | MJPEG stream |
| `/audio` | GET | Audio device info (Seeed mic/speaker) |
| `/audio/volume` | GET | Get current volume |
| `/audio/volume` | POST | Set volume (0-100%) |
| `/audio/play-tone` | POST | Play test tone |
| `/audio/record` | POST | Record from mic, return WAV |

---

## 7. Upload / Publish Scripts

### `scripts/release/upload-hal.sh` (NEW)

```bash
#!/usr/bin/env bash
# Upload HAL runtime to OTA

set -euo pipefail

VERSION_FILE="VERSION_HAL"
BUCKET="s3-autonomous-upgrade-3"
OTA_PATH="os/ota/hal"
METADATA_PATH="os/ota/metadata.json"

# Auto-increment patch version
CURRENT=$(cat "$VERSION_FILE" 2>/dev/null || echo "0.0.0")
MAJOR=$(echo "$CURRENT" | cut -d. -f1)
MINOR=$(echo "$CURRENT" | cut -d. -f2)
PATCH=$(echo "$CURRENT" | cut -d. -f3)
NEW_VERSION="$MAJOR.$MINOR.$((PATCH + 1))"
echo "$NEW_VERSION" > "$VERSION_FILE"

# Package
echo "Packaging HAL $NEW_VERSION..."
cd path/to/hal-source
echo "$NEW_VERSION" > VERSION
zip -r "/tmp/hal-${NEW_VERSION}.zip" hal/ requirements.txt VERSION

# Upload zip
gsutil cp "/tmp/hal-${NEW_VERSION}.zip" \
    "gs://${BUCKET}/${OTA_PATH}/${NEW_VERSION}/hal-${NEW_VERSION}.zip"

# Update metadata
DOWNLOAD_URL="https://storage.googleapis.com/${BUCKET}/${OTA_PATH}/${NEW_VERSION}/hal-${NEW_VERSION}.zip"
gsutil cp "gs://${BUCKET}/${METADATA_PATH}" /tmp/metadata.json
jq --arg v "$NEW_VERSION" --arg u "$DOWNLOAD_URL" \
    '.hal = {"version": $v, "url": $u}' /tmp/metadata.json > /tmp/metadata-updated.json
gsutil cp /tmp/metadata-updated.json "gs://${BUCKET}/${METADATA_PATH}"

echo "HAL $NEW_VERSION published."
```

### All Upload Scripts

| Script | Component | Pattern |
|---|---|---|
| `scripts/release/upload-os-server.sh` | OS Server binary | Build → zip → GCS → update metadata |
| `scripts/release/upload-bootstrap.sh` | Bootstrap Server binary | Build → zip → GCS → update metadata |
| `scripts/release/upload-web.sh` | Web SPA bundle | Build → zip → GCS → update metadata |
| `scripts/release/upload-hal.sh` | HAL Python runtime (NEW) | Package → zip → GCS → update metadata |
| `scripts/release/upload-setup.sh` | Setup script | Upload to GCS |
| `scripts/release/upload-setup-ap.sh` | AP setup script | Upload to GCS |
| `scripts/release/upload-skills.sh` | OpenClaw skill files | Upload to GCS |
| `scripts/release/upload-openclaw.sh` | OpenClaw npm version | Metadata only (`npm install -g` on device) |
| `scripts/release/upload-codex.sh` | Codex CLI version | Metadata only (GitHub release tarball on device) |
| `scripts/release/upload-claudecode.sh` | Claude Code CLI version | Metadata only (Anthropic installer on device) |
| `scripts/release/upload-opencode.sh` | OpenCode CLI version | Metadata only (opencode.ai installer on device) |
| `scripts/release/upload-picoclaw.sh` | PicoClaw release TAG | Metadata only (GitHub asset on device); verifies the tag exists |
| `scripts/release/upload-hermes.sh` | Hermes version + upstream tag/commit | Metadata only: `hermes.version` + `hermes.commit` (device checks the commit out via the upstream installer) |
| `scripts/provision/install.sh` | CDN install shortcut | `curl ... \| sudo bash` on Pi |
| `scripts/release/tag-release.sh` | Git release tag with OTA metadata snapshot | Fetch metadata.json → annotated tag → `git push origin <tag>` |

### `scripts/release/tag-release.sh` — GPL v3 §6 traceability

After component uploads succeed (`make upload-os-server upload-hal upload-web ...`), this script anchors the resulting OTA metadata snapshot to a single git tag:

```bash
make tag-release v0.0.8
# → curl https://cdn.autonomous.ai/os/ota/metadata.json
# → git tag -a v0.0.8 -F - (annotation = pretty-printed metadata JSON)
# → git push origin v0.0.8
```

Buyers run `os-server --version` on the device — value comes from `git describe --tags --always --dirty` at build time (`Makefile:VERSION`), so it resolves to the closest tag. They then open the public repo (`github.com/autonomous-ai/autonomous-os`), find the matching tag, read the annotation for the exact `os-server`/`hal`/`web`/`bootstrap` versions baked at release time, and checkout that commit for corresponding source.

Guards in the script: refuses if tag already exists locally or on remote, refuses if metadata fetch fails or JSON is invalid (`set -euo pipefail` + `jq .`). Overrides via env vars: `OTA_METADATA_URL` (default: `https://cdn.autonomous.ai/os/ota/metadata.json`), `TAG_REMOTE` (default: `origin`).

---

## 8. Build & Version Injection

### Go Binaries (ldflags)

```makefile
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")

# Go symbol stays config.LampVersion (internal, not part of deploy identity).
LDFLAGS_OS   := -X go.autonomous.ai/os/system/server/config.OSVersion=$(VERSION)
LDFLAGS_BOOT := -X go.autonomous.ai/os/system/bootstrap/config.BootstrapVersion=$(VERSION)

os-build-bootstrap:
	GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS_BOOT)" -o bootstrap-server ./cmd/bootstrap

os-build:
	GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS_OS)" -o os-server ./cmd/os-server
```

### HAL (VERSION file)

HAL version is a plain text `VERSION` file in the package root. Read by bootstrap via file or `python -m hal --version`.

---

## 9. Key Differences from Lobster

| Aspect | Lobster (original) | Autonomous (this project) |
|---|---|---|
| Components | 4 (lamp, bootstrap, web, openclaw) | **5** (+ hal) |
| OTA keys | lamp, bootstrap, web, openclaw | + **hal** |
| Setup stages | 7 (stages -1 to 4) | **8** (+ stage 2b: HAL) |
| Systemd services | 4 | **5** (+ hal.service) |
| Python runtime | None | **HAL** at /opt/hal/ with venv |
| Hardware bridge | N/A | OS server HTTP → HAL HTTP (localhost proxy) |
| SPI usage | LED only | LED + **Display (GC9A01)** |

---

## 10. Open Questions

- [x] **HAL source**: Mono-repo. Driver code copied from `humancomputerlab/lelamp_runtime` into `hal/`, with LiveKit/OpenAI removed and HTTP API + DisplayService added. Upstream tracked manually via `hal/UPSTREAM.md`.
- [x] **HAL HTTP port**: `5001` (OS Server is `5000`).
- [x] **Bridge protocol**: Simple HTTP proxy. HAL runs FastAPI on `127.0.0.1:5001`, OS Server proxies from port 5000.
- [x] **Python version**: Pinned to Python 3.12.x (`pyproject.toml`, `.python-version`, `setup.sh` uses `uv sync --python 3.12`).
- [x] **HAL packaging**: On-device venv via `uv sync --python 3.12 --extra hardware` plus `--extra reachy` for Reachy Mini or `--extra aec --extra pipecat` for other devices. OTA builds a fresh venv using the shared cache, preserves `.env`, and retains the old runtime for rollback.
- [x] **Display driver**: DisplayService (GC9A01) is part of HAL Python at `hal/service/display/display_service.py`.
- [x] **HAL config**: Environment variable-based (`config.py` reads from env vars). `.env` file support via `python-dotenv`. No separate config file needed.

---

*This document describes the full OTA and bootstrap system. For architecture decisions, see [architecture-decision.md](../robots/lamp/docs/architecture-decision.md). For product vision, see [product-vision.md](../robots/lamp/docs/product-vision.md).*
