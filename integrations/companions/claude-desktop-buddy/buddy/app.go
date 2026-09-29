package buddy

import (
	"bytes"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"claude-desktop-buddy/httpapi"
)

// compactPreview returns the first max bytes of data with control characters replaced by spaces.
func compactPreview(data []byte, max int) string {
	if len(data) > max {
		data = data[:max]
	}
	out := make([]byte, len(data))
	for i, b := range data {
		if b < 0x20 || b == 0x7f {
			out[i] = ' '
		} else {
			out[i] = b
		}
	}
	return string(out)
}

// Config is loaded from buddy.json.
type Config struct {
	Enabled            bool   `json:"enabled"`
	DeviceName         string `json:"device_name"`
	HTTPPort           int    `json:"http_port"`
	HALURL             string `json:"hal_url"`
	DeviceURL          string `json:"device_url"`
	ApprovalTimeoutSec int    `json:"approval_timeout_sec"`
	// NarrationLang selects the narrator language; unsupported values fall back to English.
	NarrationLang string `json:"narration_lang"`
	// CodeApprovalTTLSec bounds a Claude Code approval long-poll; keep it under the hook's 60s timeout.
	CodeApprovalTTLSec int `json:"code_approval_ttl_sec"`
	// OSConfigPath is the OS server config.json holding the admin password hash; defaults to a sibling of buddy.json.
	OSConfigPath string `json:"os_config_path"`
}

// Run loads config, starts the daemon (BlueZ agent, BLE, state machine, narrator, HTTP API) and blocks.
func Run(configPath string) {
	cfg := loadConfig(configPath)
	if !cfg.Enabled {
		log.Println("[buddy] disabled in config, exiting")
		return
	}

	cfg.DeviceName = resolveDeviceName(cfg.DeviceName, cfg.DeviceURL)

	// BlueZ rejects pairing without a registered agent.
	if err := registerBluezAgent(); err != nil {
		log.Printf("[buddy] WARN: register agent failed: %v (pairing will likely fail)", err)
	}

	bridge := NewBridge(cfg.HALURL, cfg.DeviceURL)
	startTime := time.Now()

	narrator := NewNarrator(cfg.NarrationLang, bridge.speakTTS)
	go func() {
		time.Sleep(8 * time.Second)
		narrator.Warmup(bridge.prerenderTTS)
		log.Println("[narrator] prerender warmup dispatched")
	}()

	persisted := LoadStats()

	sm := NewStateMachine(func(old, next BuddyState, hb *Heartbeat) {
		bridge.OnStateChange(old, next, hb)
		switch {
		case old == StateSleep && next != StateSleep:
			narrator.StartTurn()
			narrator.Say(NarrateConnected)
		case old != StateSleep && next == StateSleep:
			narrator.Say(NarrateDisconnected)
		case old != StateBusy && next == StateBusy:
			narrator.StartTurn()
			narrator.Say(NarrateBusyStart)
		case old == StateBusy && next == StateIdle:
			narrator.Say(NarrateDone)
			bridge.expressEmotion("happy", 0.7)
		}
	})
	sm.SeedStats(persisted.Approved, persisted.Denied)

	// Assign the package-level ble (not :=) so the onMessage closure doesn't see nil.
	ble = NewBLEServer(cfg.DeviceName, func(data []byte) {
		handleBLEMessage(data, sm, ble, bridge, narrator, cfg.DeviceName, startTime)
	}, func(connected bool) {
		sm.SetConnected(connected)
		if !connected {
			xfer.Abort()
		}
	})

	go func() {
		ticker := time.NewTicker(500 * time.Millisecond)
		defer ticker.Stop()
		for range ticker.C {
			sm.CheckTransientExpiry()
		}
	}()

	codeApprovals := NewCodeApprovals(bridge, time.Duration(cfg.CodeApprovalTTLSec)*time.Second)

	httpSrv := httpapi.New(
		cfg.HTTPPort,
		NewAuthenticator(cfg.OSConfigPath),
		NewStatusReader(sm),
		NewApprovalService(sm, ble),
		NewHALActivitySink(bridge), // logs each event + speaks via HAL :5001
		codeApprovals,
	)
	go func() {
		if err := httpSrv.Start(); err != nil {
			log.Fatalf("[buddy] http server error: %v", err)
		}
	}()

	log.Printf("[buddy] starting Claude Desktop Buddy plugin (%s)", cfg.DeviceName)
	log.Printf("[buddy] HAL: %s, Device: %s, HTTP: :%d", cfg.HALURL, cfg.DeviceURL, cfg.HTTPPort)

	if err := ble.Start(); err != nil {
		log.Fatalf("[buddy] BLE start error: %v", err)
	}

	log.Println("[buddy] BLE advertising started, waiting for Claude Desktop connection...")

	select {}
}

// ble is package-level so handleBLEMessage can reach it from the closure.
var ble *BLEServer

// xfer holds the single active folder-push transfer from Claude Desktop.
var xfer Transfer

func handleBLEMessage(data []byte, sm *StateMachine, bleSrv *BLEServer, bridge *Bridge, narrator *Narrator, deviceName string, startTime time.Time) {
	msg, lost, err := ParseOrSalvage(data)
	if err != nil {
		// Write-without-response drops packets; framing is lost, so abort any char transfer.
		preview := compactPreview(data, 80)
		category := "mid-corruption"
		switch {
		case len(data) == 0 || data[0] != '{':
			category = "prefix-lost"
			xfer.Abort()
		case !bytes.HasSuffix(bytes.TrimRight(data, "\n"), []byte("}")):
			category = "truncated"
		}
		log.Printf("[ble] dropped %d-byte BLE message (%s): %v — %q", len(data), category, err, preview)
		return
	}
	if lost > 0 {
		// Write-without-response drops packets under load; salvage the line tail and keep the session alive.
		log.Printf("[ble] WARN: dropped %d corrupted prefix bytes (BLE packet loss)", lost)
		xfer.Abort()
	}

	switch m := msg.(type) {
	case *Heartbeat:
		if !sm.Connected() {
			sm.SetConnected(true)
			log.Println("[ble] Claude Desktop connected")
		}
		// Desktop pings ~1/s; only log when a meaningful field changes.
		if prev := sm.LastHeartbeat(); heartbeatChanged(prev, m) {
			log.Printf("[ble] heartbeat total=%d running=%d waiting=%d tokens=%d today=%d msg=%q entries=%d prompt=%v",
				m.Total, m.Running, m.Waiting, m.Tokens, m.TokensToday, m.Msg, len(m.Entries), m.Prompt != nil)
		}
		sm.HandleHeartbeat(m)

	case *TimeSync:
		log.Printf("[ble] time sync: epoch=%d, offset=%d", m.Time[0], m.Time[1])

	case *Event:
		log.Printf("[ble] event evt=%q role=%q content=%q", m.Evt, m.Role, m.TurnText())
		bridge.OnEvent(m)
		if m.Evt == "turn" {
			switch m.Role {
			case "user":
				narrator.StartTurn()
			case "assistant":
				for _, b := range m.Blocks() {
					switch b.Type {
					case "thinking":
						narrator.Say(NarrateThinking)
					case "tool_use":
						narrator.SayTool(b.Name)
					}
				}
			}
		}

	case *Command:
		log.Printf("[ble] command: %s", m.Cmd)
		switch m.Cmd {
		case "status":
			approved, denied := sm.ApprovalStats()
			resp := MakeStatusAck(deviceName, time.Since(startTime), approved, denied)
			if err := bleSrv.Send(resp); err != nil {
				log.Printf("[ble] send status ack error: %v", err)
			}
		case "owner":
			log.Printf("[ble] owner set to: %s", m.Name)
			if err := bleSrv.Send(MakeAck("owner", true)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}
		case "name":
			log.Printf("[ble] name set to: %s", m.Name)
			if err := bleSrv.Send(MakeAck("name", true)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}
		case "unpair":
			log.Println("[ble] unpair requested")
			xfer.Abort()
			if err := bleSrv.Send(MakeAck("unpair", true)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}
			sm.SetConnected(false)

		case "char_begin":
			ok := true
			if err := xfer.Begin(m.Name, m.Total); err != nil {
				log.Printf("[xfer] begin error: %v", err)
				ok = false
			}
			if err := bleSrv.Send(MakeAck("char_begin", ok)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}
		case "file":
			ok := true
			if err := xfer.StartFile(m.Path, m.Size); err != nil {
				log.Printf("[xfer] file error: %v", err)
				ok = false
			}
			if err := bleSrv.Send(MakeAck("file", ok)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}
		case "chunk":
			n, err := xfer.WriteChunk(m.D)
			if err != nil {
				log.Printf("[xfer] chunk error: %v", err)
				if err := bleSrv.Send(MakeAckN("chunk", false, n)); err != nil {
					log.Printf("[ble] send ack error: %v", err)
				}
				break
			}
			if err := bleSrv.Send(MakeAckN("chunk", true, n)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}
		case "file_end":
			n, err := xfer.EndFile()
			ok := err == nil
			if err != nil {
				log.Printf("[xfer] file_end error: %v", err)
			}
			if err := bleSrv.Send(MakeAckN("file_end", ok, n)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}
		case "char_end":
			xfer.End()
			if err := bleSrv.Send(MakeAck("char_end", true)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}

		default:
			log.Printf("[ble] unknown command: %s", m.Cmd)
			if err := bleSrv.Send(MakeAck(m.Cmd, false)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}
		}
	}
}

func loadConfig(path string) Config {
	cfg := Config{
		Enabled:            true,
		DeviceName:         "Claude-{MAC}",
		HTTPPort:           5002,
		HALURL:             "http://127.0.0.1:5001",
		DeviceURL:          "http://127.0.0.1:5000",
		ApprovalTimeoutSec: 30,
		NarrationLang:      "vi",
		CodeApprovalTTLSec: 55,
	}

	data, err := os.ReadFile(path)
	if err != nil {
		log.Printf("[buddy] config %s not found, using defaults", path)
	} else if err := json.Unmarshal(data, &cfg); err != nil {
		log.Printf("[buddy] config parse error: %v, using defaults", err)
	} else {
		log.Printf("[buddy] loaded config from %s", path)
	}

	if cfg.OSConfigPath == "" {
		cfg.OSConfigPath = filepath.Join(filepath.Dir(path), "config.json")
	}
	return cfg
}

// resolveDeviceName expands the {MAC} placeholder in name using the OS server's MAC suffix, retrying while it starts.
// The suffix is 4 chars so the name fits the 31-byte advertisement alongside the 128-bit service UUID.
func resolveDeviceName(name, deviceURL string) string {
	if name == "" {
		name = "Claude-{MAC}"
	}
	if !strings.Contains(name, "{MAC}") {
		return name
	}

	mac, reason := fetchMAC(deviceURL)
	switch {
	case mac != "":
		log.Printf("[buddy] resolved mac=%q from device", mac)
	case reason == "empty":
		log.Printf("[buddy] WARN: Device reachable at %s but mac is empty — hardware serial/MAC unreadable", deviceURL)
		mac = "unk"
	default:
		log.Printf("[buddy] WARN: failed to fetch mac from %s after %d attempts (%s)",
			deviceURL, fetchAttempts, reason)
		mac = "unk"
	}
	short := shortMAC(mac)
	if short != mac {
		log.Printf("[buddy] shortened mac %q → %q for BLE adv fit", mac, short)
	}
	return strings.ReplaceAll(name, "{MAC}", short)
}

// shortMAC returns the lowercase `<device_type>-<4hex>` BLE name, e.g. "Lamp-A1B2" -> "lamp-a1b2".
func shortMAC(mac string) string {
	if mac == "" {
		return "unk"
	}
	mac = strings.ToLower(mac)
	if i := strings.LastIndexByte(mac, '-'); i >= 0 && i+1 < len(mac) {
		prefix, suffix := mac[:i+1], mac[i+1:]
		if len(suffix) > 4 {
			suffix = suffix[len(suffix)-4:]
		}
		return prefix + suffix
	}
	if len(mac) > 4 {
		mac = mac[len(mac)-4:]
	}
	return mac
}

const fetchAttempts = 15

// fetchMAC returns the MAC suffix, or a reason ("empty" or a transport error summary) on failure.
func fetchMAC(deviceURL string) (string, string) {
	client := &http.Client{Timeout: 3 * time.Second}
	url := deviceURL + "/api/system/network"
	var lastErr string
	for i := 0; i < fetchAttempts; i++ {
		mac, ok, errStr := tryFetchMAC(client, url)
		if ok {
			if mac == "" {
				return "", "empty"
			}
			return mac, ""
		}
		lastErr = errStr
		time.Sleep(2 * time.Second)
	}
	if lastErr == "" {
		lastErr = "unknown error"
	}
	return "", lastErr
}

// tryFetchMAC fetches the MAC once; ok=false means a transport/decode failure worth retrying.
func tryFetchMAC(client *http.Client, url string) (string, bool, string) {
	resp, err := client.Get(url)
	if err != nil {
		return "", false, "http: " + err.Error()
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", false, "http " + resp.Status
	}
	var wrap struct {
		Data struct {
			MAC string `json:"mac"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&wrap); err != nil {
		return "", false, "decode: " + err.Error()
	}
	return wrap.Data.MAC, true, ""
}

// heartbeatChanged reports whether a heartbeat differs enough to log; token counts are ignored.
func heartbeatChanged(prev, curr *Heartbeat) bool {
	if prev == nil {
		return true
	}
	if prev.Running != curr.Running ||
		prev.Waiting != curr.Waiting ||
		prev.Msg != curr.Msg ||
		(prev.Prompt == nil) != (curr.Prompt == nil) {
		return true
	}
	if prev.Prompt != nil && curr.Prompt != nil && prev.Prompt.ID != curr.Prompt.ID {
		return true
	}
	return false
}
