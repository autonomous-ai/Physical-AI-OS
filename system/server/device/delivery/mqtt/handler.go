package mqtthandler

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"path/filepath"
	"strings"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/runtimes/openclaw"
	"go.autonomous.ai/os/system/buddy"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/lib/mqtt"
	"go.autonomous.ai/os/system/network"
	"go.autonomous.ai/os/system/schedule"
	"go.autonomous.ai/os/system/server/config"
)

// publishTimeout bounds the fd_channel reply publish. Only matters at QoS 1+
// (below), where conn.Publish blocks until the broker PUBACKs — a dead/slow
// connection must not hang this forever.
const publishTimeout = 15 * time.Second

// DeviceMQTTHandler handles incoming MQTT messages and dispatches to command handlers.
type DeviceMQTTHandler struct {
	config         *config.Config
	mqttFactory    *mqtt.Factory
	deviceService  *device.Service
	networkService *network.Service
	agentGateway   domain.AgentGateway
	buddyService   *buddy.Service
	harnessService *harness.Service
	harnessVoice   *atomic.Pointer[harness.VoiceController]
	// connectorWriter is the data-driven writer for the connector.set.<code> /
	// connector.remove.<code> flow and the refresh loop. Routing (is it an MCP
	// connector? which auth header?) is decided per-message from the payload's
	// credentials map, with a compiled-in fallback for codes that shipped before
	// the contract moved to the wire. Handles every connector EXCEPT those in
	// specialConnectorWriters. Built once at startup; never mutated at runtime.
	connectorWriter *connectorWriter
	// specialConnectorWriters holds bespoke writers for connectors that can't be
	// expressed as a simple http mcp_url entry — e.g. figma-api, a local stdio
	// MCP server that drops a Node wrapper on disk. A code with no special writer
	// and no mcp_url falls through to connectorWriter. Built once at startup.
	specialConnectorWriters map[string]ConnectorWriter
	// oauthAlertStatus tracks the last-alerted refresh outcome per provider
	// ("ok"/"fail") so the OAuth refresh loop pings the maintainer chat only on
	// state changes, not every tick. Mutated only from the single refresh-loop
	// goroutine; initialised in ProvideDeviceMQTTHandler.
	oauthAlertStatus map[string]string
	// chatStream mirrors the monitor events of chat.send runs onto fd_channel.
	// Nil only in tests that build the handler directly; handleChatSend nil-checks
	// so a missing stream degrades to "turn runs, backend sees only the ack".
	chatStream *ChatStream
	// scheduleStore persists the "Scheduled" feature's task list to
	// schedules.json — a SIBLING of config.json (config.Dir()), never inside it
	// (see schedule.Store's doc comment). Shared between handleScheduleSync /
	// handleScheduleRun and scheduleRunner below.
	scheduleStore *schedule.Store
	// scheduleRunner is the once-a-minute ticker that fires due schedules
	// through agentGateway.SendSystemChatMessage. Built once at startup;
	// started by StartScheduleRunnerLoop from config_watch.go alongside the
	// other background loops (OAuth/connector refresh).
	scheduleRunner *schedule.Runner

	// scheduleIntents queues device-originated schedule changes until the
	// backend confirms them. Deliberately a SEPARATE store from scheduleStore:
	// the runner fires only from scheduleStore, so a task the backend has not
	// yet confirmed is not merely flagged un-runnable, it is absent from the
	// file the runner reads. See system/schedule/intent.go.
	scheduleIntents *schedule.IntentStore

	// scheduleAlert, when non-nil, receives every schedule ops alert
	// (alertScheduleEvent) synchronously INSTEAD of the real transport. Tests
	// set it to capture exact titles; production leaves it nil, which sends
	// through alertOps on its own goroutine (see alertScheduleEvent for why
	// schedule alerts must never run inline). Never mutated after construction.
	scheduleAlert func(title, detail string)
}

// SetHarnessService attaches the server-owned Harness service after Wire construction.
func (h *DeviceMQTTHandler) SetHarnessService(s *harness.Service) { h.harnessService = s }

// mcpConnectorSpec lists the remote-MCP connectors that the generic writer
// recognises via its compiled-in fallback table. apiKey:true selects the
// static-API-key header builder (Ahrefs); the rest use the default OAuth Bearer
// access-token builder. The MCP URL is pulled from the openclaw catalog so there
// is a single source of truth. The fallback only fills the gap until the backend
// pushes mcp_url/mcp_auth_header in the connector.set payload — payload always
// wins, and a brand-new connector needs no entry here at all.
var mcpConnectorSpecs = []struct {
	name   string
	apiKey bool
}{
	{name: "notion"},
	{name: "asana"},
	{name: "linear"},
	{name: "github"},
	{name: "ahrefs", apiKey: true},
}

// specialConnectorCodes is the set of connector codes handled by a bespoke
// writer (newSpecialConnectorWriters) instead of the generic data-driven one.
// The generic writer's refresh loop skips these so it never re-Writes them in
// the wrong (http) shape — see connectorWriter.reserved.
var specialConnectorCodes = map[string]bool{
	"figma-api": true,
}

// newSpecialConnectorWriters builds the bespoke writers for connectors that
// can't be expressed as a simple http mcp_url entry. Today that is only
// figma-api: a local stdio MCP server that drops a Node wrapper on disk and
// passes the Figma OAuth token via the entry's env (the hosted Figma MCP is
// allowlist-gated, so this REST wrapper is the only Figma wiring).
func newSpecialConnectorWriters(cfg *config.Config, gw domain.AgentGateway) map[string]ConnectorWriter {
	configsDir := filepath.Join(cfg.OpenclawConfigDir, "workspace", "configs")
	wrapperPath := openclaw.FigmaMCPServerPath(cfg.OpenclawConfigDir)
	ocDir := cfg.OpenclawConfigDir
	return map[string]ConnectorWriter{
		"figma-api": newMCPConnectorWriter(mcpConnectorConfig{
			name: "figma-api",
			entry: func(c ConnectorCreds) map[string]any {
				return figmaStdioEntry(wrapperPath, c)
			},
			ensureAssets: func() error {
				// Wrapper script is required — fail the connector.set if it can't drop.
				if _, err := openclaw.EnsureFigmaMCPServer(ocDir); err != nil {
					return err
				}
				// Skill (SKILL.md from GCS) is best-effort: the figma_* tools still
				// work via the MCP tool list without it. Idempotent — only downloads
				// when missing, so token refreshes don't re-fetch.
				if err := openclaw.EnsureMCPSkill(ocDir, "figma-api"); err != nil {
					slog.Warn("figma-api: skill install failed (continuing)", "component", "mqtt", "error", err)
				}
				return nil
			},
		}, configsDir, gw),
	}
}

// figmaStdioEntry builds the mcp.servers.figma-api stdio entry for the figma-api
// connector. Token + auth header come from the connector's mcp_auth_header
// descriptor: OAuth -> access_token via Authorization/Bearer; PAT -> api_key via
// the connector's custom header (e.g. X-Figma-Token). FIGMA_ACCESS_TOKEN is kept
// as a back-compat alias for wrappers shipped before FIGMA_TOKEN existed.
func figmaStdioEntry(wrapperPath string, c ConnectorCreds) map[string]any {
	hdrName, _, token := connectorAuthHeader(c.Credentials[credentialMCPAuthHeader], c)
	return map[string]any{
		"command": "node",
		"args":    []any{wrapperPath},
		"env": map[string]any{
			"FIGMA_TOKEN":        token,
			"FIGMA_AUTH_HEADER":  hdrName,
			"FIGMA_ACCESS_TOKEN": token,
		},
	}
}

// connectorWriterFor routes a connector code to its writer: a special writer
// when one is registered (figma-api), otherwise the generic data-driven writer.
func (h *DeviceMQTTHandler) connectorWriterFor(code string) ConnectorWriter {
	if w, ok := h.specialConnectorWriters[code]; ok {
		return w
	}
	if h.connectorWriter == nil {
		return nil
	}
	return h.connectorWriter
}

// refreshableConnectorWriters returns every writer the refresh loop must scan:
// the generic writer plus each special writer. The generic writer already skips
// codes owned by a special writer (its `reserved` set), so connectors are never
// double-refreshed.
func (h *DeviceMQTTHandler) refreshableConnectorWriters() []ConnectorWriter {
	out := make([]ConnectorWriter, 0, 1+len(h.specialConnectorWriters))
	if h.connectorWriter != nil {
		out = append(out, h.connectorWriter)
	}
	for _, w := range h.specialConnectorWriters {
		out = append(out, w)
	}
	return out
}

// ProvideDeviceMQTTHandler creates DeviceMQTTHandler with all command handlers.
func ProvideDeviceMQTTHandler(cfg *config.Config, mqttFactory *mqtt.Factory, ds *device.Service, ns *network.Service, gw domain.AgentGateway, chatStream *ChatStream, buddyService *buddy.Service) DeviceMQTTHandler {
	configsDir := filepath.Join(cfg.OpenclawConfigDir, "workspace", "configs")
	// schedules.json is a SIBLING of config.json, never inside it — see
	// schedule.Store's doc comment and config.Dir().
	scheduleStore := schedule.NewStore(filepath.Join(config.Dir(), "schedules.json"))
	scheduleIntents := schedule.NewIntentStore(filepath.Join(config.Dir(), "schedule-intents.json"))

	h := DeviceMQTTHandler{
		harnessVoice:   &atomic.Pointer[harness.VoiceController]{},
		config:         cfg,
		mqttFactory:    mqttFactory,
		deviceService:  ds,
		networkService: ns,
		agentGateway:   gw,
		buddyService:   buddyService,
		// `reserved` excludes codes owned by a special writer so the generic
		// refresh loop never clobbers their (non-http) openclaw entry.
		connectorWriter:         newConnectorWriter(configsDir, gw, specialConnectorCodes),
		specialConnectorWriters: newSpecialConnectorWriters(cfg, gw),
		oauthAlertStatus:        map[string]string{},
		chatStream:              chatStream,
		scheduleStore:           scheduleStore,
		scheduleIntents:         scheduleIntents,
	}
	// h.publishScheduleRunReport is bound to THIS local h, not to whatever copy
	// the caller eventually stores (wire_gen.go copies the return value into
	// Server.deviceMQTTHandler) — that is safe here because publishDataResult
	// only ever reads h.config/h.mqttFactory, and both are pointers shared
	// identically by every copy of this struct. The run's ops alert
	// (alertScheduleEvent) adds reads of h.scheduleAlert (nil in production,
	// never mutated) and, through alertOps, h.config again — the same holds.
	// Do not add any other mutable-by-value state to this closure without
	// re-checking that holds.
	h.scheduleRunner = schedule.NewRunner(scheduleStore, gw, cfg.DeviceID, h.publishScheduleRunReport)
	// The connector guard for template tasks (schedule.Schedule.Requires):
	// the runner asks connectorInstalled whether each required code has
	// credentials on this device, and skips the run — reported as "skipped" —
	// when one does not. Bound to the local h for the same reason, and just as
	// safely: connectorInstalled reads only h.config (pointer),
	// h.connectorWriter (pointer) and h.specialConnectorWriters (map), all set
	// in the literal above and never mutated afterwards.
	h.scheduleRunner.SetConnectorChecker(schedule.ConnectorCheckerFunc(h.connectorInstalled))
	return h
}

func (h *DeviceMQTTHandler) publish(data interface{}) error {
	ctx, cancel := context.WithTimeout(context.Background(), publishTimeout)
	defer cancel()
	mqttClient := h.mqttFactory.GetClient("device-" + h.config.DeviceID)
	if err := mqttClient.Connect(ctx); err != nil {
		return err
	}
	defer mqttClient.Close()
	payload, err := json.Marshal(data)
	if err != nil {
		return err
	}
	// QoS 1 (was 0): a QoS-0 publish is fire-and-forget with zero delivery
	// guarantee, and larger replies (e.g. a full SKILL.md via skills.files) were
	// observed being silently lost on real device network conditions — the
	// broker itself carries far larger payloads fine, so this is loss on the
	// device's own uplink, not a broker limit. At QoS 1 the paho client blocks
	// this call until the broker PUBACKs (or ctx above times out), so a lost ack
	// is retried by the client's session rather than silently dropped.
	if err := mqttClient.Publish(ctx, h.config.FDChannel, byte(1), payload); err != nil {
		slog.Error("PublishToFD failed", "component", "mqtt", "channel", h.config.FDChannel, "error", err)
		return err
	}
	slog.Debug("PublishToFD ok", "component", "mqtt", "channel", h.config.FDChannel, "payload", string(payload))
	return nil
}

// handleData routes a generic cmd:"data" envelope by its delivery Type:
//
//   - default (Type == "")  → Data is inline; dispatch immediately.
//   - Type == "privacy"     → Data lives on the backend; ack "received" then
//     async-fetch it over TLS before re-entering dispatchData with Data
//     populated (see privacy_fetch.go).
//
// Per-kind handlers don't care which path the data took — they read env.Data
// after the routing layer has populated it.
func (h *DeviceMQTTHandler) handleData(cmd domain.MQTTMessage) error {
	var env domain.MQTTDataCommand
	if err := json.Unmarshal(cmd.Raw(), &env); err != nil {
		slog.Error("data: invalid envelope", "component", "mqtt", "error", err)
		return h.publishDataResult("", "failure", "invalid envelope: "+err.Error(), nil)
	}

	if env.Type == domain.MQTTDataTypePrivacy {
		return h.handlePrivacyEnvelope(env)
	}
	return h.dispatchData(env)
}

// dispatchData is the per-kind switch, shared by the inline and privacy paths.
// New sub-handlers go in this switch — adding one does NOT require touching the
// privacy fetch flow, which only cares about env.Kind/env.Type.
func (h *DeviceMQTTHandler) dispatchData(env domain.MQTTDataCommand) error {
	// Connector kinds carry the connector code as suffix (e.g.
	// "connector.set.notion"), so prefix-match before the exact-kind switch.
	if strings.HasPrefix(env.Kind, domain.DataKindConnectorSetPrefix) {
		return h.handleConnectorSet(env)
	}
	if strings.HasPrefix(env.Kind, domain.DataKindConnectorRemovePrefix) {
		return h.handleConnectorRemove(env)
	}
	switch env.Kind {
	case domain.KindEnvironmentStatus:
		return h.handleEnvironmentStatus(env)
	case domain.KindBuddyStatus:
		return h.handleBuddyStatus(env)
	case domain.KindBuddyPairStart:
		return h.handleBuddyPairStart(env)
	case domain.KindBuddyPairRevoke:
		return h.handleBuddyPairRevoke(env)
	case domain.KindHarnessVoiceModeGet, domain.KindHarnessVoiceModeSet:
		return h.handleHarnessVoiceMode(env)
	case domain.KindHarnessPairStart, domain.KindHarnessStatus, domain.KindHarnessPairCancel, domain.KindHarnessPairRevoke:
		return h.handleHarnessPair(env)
	case domain.KindTTSSet:
		return h.handleTTSSet(env)
	case domain.KindRealtimeSet:
		return h.handleRealtimeSet(env)
	case domain.KindWakeWordGate:
		return h.handleWakeWordGate(env)
	case domain.KindTimezoneSet:
		return h.handleTimezoneSet(env)
	case domain.KindHermesSetup:
		return h.handleRuntimeSetup(env, domain.AgentRuntimeHermes)
	case domain.KindPicoclawSetup:
		return h.handleRuntimeSetup(env, domain.AgentRuntimePicoclaw)
	case domain.KindClaudecodeSetup:
		return h.handleRuntimeSetup(env, domain.AgentRuntimeClaudeCode)
	case domain.KindOpenclawSetup:
		return h.handleRuntimeSetup(env, domain.AgentRuntimeOpenClaw)
	case domain.KindCodexSetup:
		return h.handleRuntimeSetup(env, domain.AgentRuntimeCodex)
	case domain.KindOpenCodeSetup:
		return h.handleRuntimeSetup(env, domain.AgentRuntimeOpenCode)
	case domain.KindTTSPreview:
		return h.handleTTSPreview(env)
	case domain.KindDeviceRename:
		return h.handleDeviceRename(env)
	case domain.KindDeviceSoftReset:
		return h.handleDeviceSoftReset(env)
	case domain.KindOAuthSet:
		return h.handleOAuthSet(env)
	case domain.KindOAuthRemove:
		return h.handleOAuthRemove(env)
	case domain.KindSystemInfo:
		return h.handleSystemInfo(env)
	case domain.KindSystemVersion:
		return h.handleSystemVersion(env)
	case domain.KindSystemNetwork:
		return h.handleSystemNetwork(env)
	case domain.KindSystemReboot:
		return h.handleSystemReboot(env)
	case domain.KindSystemShutdown:
		return h.handleSystemShutdown(env)
	case domain.KindSystemOTAVersions:
		return h.handleSystemOTAVersions(env)
	case domain.KindSystemSoftwareUpdate:
		return h.handleSystemSoftwareUpdate(env)
	case domain.KindSkillsInstall:
		return h.handleSkillsInstall(env)
	case domain.KindSkillsSave:
		return h.handleSkillsSave(env)
	case domain.KindSkillsUpload:
		return h.handleSkillsUpload(env)
	case domain.KindSkillsInstallStore:
		return h.handleSkillsInstallStore(env)
	case domain.KindSkillsFiles:
		return h.handleSkillsFiles(env)
	case domain.KindSkillsUninstall:
		return h.handleSkillsUninstall(env)
	case domain.KindChannelRefreshConfig:
		return h.handleChannelRefreshConfig(env)
	case domain.KindChatSend:
		return h.handleChatSend(env)
	case domain.KindChatFileGet:
		return h.handleChatFileGet(env)
	case domain.KindScheduleSync:
		return h.handleScheduleSync(env)
	case domain.KindScheduleMutateAck:
		return h.handleScheduleMutateAck(env)
	case domain.KindScheduleRun:
		return h.handleScheduleRun(env)
	default:
		slog.Warn("unknown data kind", "component", "mqtt", "kind", env.Kind)
		return h.publishDataResult(env.Kind, "failure", "unknown kind: "+env.Kind, nil)
	}
}

// HandleMessage processes an incoming MQTT message (called from MQTT subscription callback or GWS HTTP).
func (h *DeviceMQTTHandler) HandleMessage(topic string, payload []byte) error {
	slog.Debug("HandleMessage", "component", "mqtt", "topic", topic, "payload", string(payload))

	var cmd domain.MQTTMessage
	if err := json.Unmarshal(payload, &cmd); err != nil {
		slog.Error("invalid payload", "component", "mqtt", "error", err)
		return fmt.Errorf("unmarshal mqtt command: %w", err)
	}

	switch cmd.Cmd {
	case domain.CommandInfo:
		return h.handleInfo(cmd)
	case domain.CommandAddChannel:
		return h.handleAddChannel(cmd)
	case domain.CommandSlackEvent:
		return h.handleSlackEvent(cmd)
	case domain.CommandSlackCommand:
		return h.handleSlackCommand(cmd)
	case domain.CommandWhatsappPair:
		return h.handleWhatsappPair(cmd)
	case domain.CommandClaudeCodeLogin:
		return h.handleClaudeCodeLogin(cmd)
	case domain.CommandClaudeCodeLoginCode:
		return h.handleClaudeCodeLoginCode(cmd)
	case domain.CommandData:
		return h.handleData(cmd)
	default:
		slog.Warn("unknown command", "component", "mqtt", "cmd", cmd.Cmd)
		return nil
	}
}
