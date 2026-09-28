package mqtthandler

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/schedule"
	"go.autonomous.ai/os/system/server/config"
)

// guardGateway is the AgentGateway double for the scheduled-task connector
// guard tests: it satisfies every method the Runner (IsBusy, Send/Speak) and
// the connector writers (Write/RemoveMCPEntry) call, and records sends so a
// test can prove a skipped task never reached the agent. Any other method
// panics on the nil embedded interface, which these tests never reach.
type guardGateway struct {
	domain.AgentGateway
	sent []string
}

func (g *guardGateway) IsBusy() bool { return false }

func (g *guardGateway) SendSystemChatMessage(msg string) (string, error) {
	g.sent = append(g.sent, msg)
	return "run-1", nil
}

func (g *guardGateway) Speak(string) error { return nil }

func (g *guardGateway) WriteMCPEntry(string, map[string]any) error { return nil }

func (g *guardGateway) RemoveMCPEntry(string) (bool, error) { return true, nil }

// guardHandler builds a handler whose connector writers point at a temp
// OpenclawConfigDir exactly the way ProvideDeviceMQTTHandler wires them, and
// returns it with the configs dir every token file lands in.
func guardHandler(t *testing.T) (*DeviceMQTTHandler, string) {
	t.Helper()
	ocDir := t.TempDir()
	cfg := &config.Config{OpenclawConfigDir: ocDir}
	gw := &guardGateway{}
	configsDir := filepath.Join(ocDir, "workspace", "configs")
	h := &DeviceMQTTHandler{
		config:          cfg,
		connectorWriter: newConnectorWriter(configsDir, gw, specialConnectorCodes),
		specialConnectorWriters: map[string]ConnectorWriter{
			// Same name/file convention as the real figma-api writer, minus
			// its asset drop (irrelevant to presence).
			"figma-api": newMCPConnectorWriter(mcpConnectorConfig{name: "figma-api"}, configsDir, gw),
		},
	}
	return h, configsDir
}

func writeConnector(t *testing.T, h *DeviceMQTTHandler, code string) {
	t.Helper()
	w := h.connectorWriterFor(code)
	if err := w.Write(context.Background(), ConnectorCreds{Connector: code, AuthType: "oauth", AccessToken: "tok"}); err != nil {
		t.Fatalf("write %s: %v", code, err)
	}
}

func removeConnector(t *testing.T, h *DeviceMQTTHandler, code string) {
	t.Helper()
	if _, err := h.connectorWriterFor(code).Remove(context.Background(), code); err != nil {
		t.Fatalf("remove %s: %v", code, err)
	}
}

// The guard must read exactly what connector.set.<code> writes and
// connector.remove.<code> deletes — through the same writer routing — so the
// generic writer's per-connector token file is the source of truth.
func TestConnectorInstalled_FollowsConnectorSetAndRemove(t *testing.T) {
	h, _ := guardHandler(t)

	if h.connectorInstalled("gmail") {
		t.Fatal("gmail reported installed before any connector.set")
	}
	writeConnector(t, h, "gmail")
	if !h.connectorInstalled("gmail") {
		t.Fatal("gmail not reported installed after connector.set")
	}
	if h.connectorInstalled("slack") {
		t.Fatal("slack reported installed although only gmail was set")
	}
	removeConnector(t, h, "gmail") // generic Remove leaves an empty file behind
	if h.connectorInstalled("gmail") {
		t.Fatal("gmail still reported installed after connector.remove")
	}
}

// MCP connectors owned by a special writer (figma-api) keep their own token
// file with their own lifecycle (Remove deletes the whole file); the guard
// must see them through that writer too.
func TestConnectorInstalled_SpecialWriterConnector(t *testing.T) {
	h, _ := guardHandler(t)

	writeConnector(t, h, "figma-api")
	if !h.connectorInstalled("figma-api") {
		t.Fatal("figma-api not reported installed after connector.set")
	}
	removeConnector(t, h, "figma-api")
	if h.connectorInstalled("figma-api") {
		t.Fatal("figma-api still reported installed after connector.remove")
	}
}

// Firmware before the per-connector files stored some connectors in the
// shared connectors.json. Nothing writes it any more, but the connectors skill
// still reads it, so an entry there means the agent can use the connector.
func TestConnectorInstalled_LegacyConnectorsJSONCounts(t *testing.T) {
	h, configsDir := guardHandler(t)
	legacy := domain.ConnectorsFile{Version: 1, Connectors: map[string]domain.ConnectorEntry{"slack": {AccessToken: "tok"}}}
	if err := writeConnectorsFile(filepath.Join(configsDir, "connectors.json"), legacy); err != nil {
		t.Fatalf("seed connectors.json: %v", err)
	}
	if !h.connectorInstalled("slack") {
		t.Fatal("a connector present only in the legacy connectors.json was reported missing")
	}
	if h.connectorInstalled("gmail") {
		t.Fatal("gmail reported installed although neither store has it")
	}
}

// connector.set holds a writer's mutex across the openclaw gateway restart
// (30-60s on a Pi). The presence check runs on the runner tick and on the
// schedule.run MQTT handler, so it must NOT wait on that mutex: token files
// are replaced atomically (tmp+rename), which makes a lock-free read safe.
func TestConnectorInstalled_DoesNotWaitOnAWriterMidConnectorSet(t *testing.T) {
	h, _ := guardHandler(t)
	writeConnector(t, h, "gmail")
	writeConnector(t, h, "figma-api")

	h.connectorWriter.mu.Lock() // a connector.set mid gateway-restart
	defer h.connectorWriter.mu.Unlock()
	figma := h.specialConnectorWriters["figma-api"].(*mcpConnectorWriter)
	figma.mu.Lock()
	defer figma.mu.Unlock()

	done := make(chan [2]bool, 1)
	go func() { done <- [2]bool{h.connectorInstalled("gmail"), h.connectorInstalled("figma-api")} }()
	select {
	case got := <-done:
		if !got[0] || !got[1] {
			t.Fatalf("installed(gmail, figma-api) = %v, want both true", got)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("connectorInstalled blocked on a connector writer's mutex")
	}
}

// A code no connector.set could ever install (outside the charset every
// writer enforces) is simply not installed.
func TestConnectorInstalled_InvalidCodeIsNeverInstalled(t *testing.T) {
	h, _ := guardHandler(t)
	for _, code := range []string{"../gmail", "Gmail", "", "gmail/x"} {
		if h.connectorInstalled(code) {
			t.Errorf("connectorInstalled(%q) = true, want false", code)
		}
	}
}

// A code that fails validation came from the backend's requires list, and it
// will make the task skip every time. The skip summary alone doesn't say the
// code was malformed rather than merely not connected, so the rejection is
// logged at Warn with the code.
func TestConnectorInstalled_InvalidCodeLogsWarn(t *testing.T) {
	h, _ := guardHandler(t)
	var buf bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})))
	t.Cleanup(func() { slog.SetDefault(previous) })

	if h.connectorInstalled("Gmail/../x") {
		t.Fatal("an invalid code was reported installed")
	}
	out := buf.String()
	if !strings.Contains(out, "level=WARN") || !strings.Contains(out, `connector=Gmail/../x`) {
		t.Fatalf("want a WARN line naming the rejected code, got:\n%s", out)
	}
}

// An unreadable token file is not evidence of absence: the guard fails OPEN
// (run the task, as before the guard existed) rather than tell the user to
// reconnect a connector that may well be connected.
func TestConnectorInstalled_UnreadableTokenFileFailsOpen(t *testing.T) {
	h, configsDir := guardHandler(t)
	if err := os.MkdirAll(configsDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(configsDir, "gmail_access_tokens.json"), []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if !h.connectorInstalled("gmail") {
		t.Fatal("a corrupt token file was treated as proof the connector is missing")
	}
}

// A skip is not a failure: the ack's top-level "error" stays empty (the
// backend must not record or alert on it as one); the reason travels in
// data.summary. Success and failure keep today's mapping.
func TestScheduleRunAckError(t *testing.T) {
	cases := []struct {
		status, summary, want string
	}{
		{"success", "Daily briefing", ""},
		{"failure", "ws disconnected", "ws disconnected"},
		{schedule.RunStatusSkipped, "missing connector: gmail", ""},
	}
	for _, tc := range cases {
		got := scheduleRunAckError(schedule.RunReport{Status: tc.status, Summary: tc.summary})
		if got != tc.want {
			t.Errorf("scheduleRunAckError(%s) = %q, want %q", tc.status, got, tc.want)
		}
	}
}

func TestBuildScheduleRunReportData_SkippedForwardsSummaryAndNextRunAt(t *testing.T) {
	next := time.Date(2026, 9, 19, 8, 0, 0, 0, time.UTC)
	data := buildScheduleRunReportData(schedule.RunReport{
		ScheduleID: "s1",
		StartedAt:  time.Date(2026, 9, 18, 8, 0, 0, 0, time.UTC),
		Status:     schedule.RunStatusSkipped,
		Summary:    "missing connector: gmail, slack",
		NextRunAt:  next,
	})
	if data["summary"] != "missing connector: gmail, slack" {
		t.Errorf("summary = %v, want the skip reason unchanged", data["summary"])
	}
	if data["next_run_at"] != next.Format(time.RFC3339) {
		t.Errorf("next_run_at = %v, want %s", data["next_run_at"], next.Format(time.RFC3339))
	}
}

// End to end through the production wiring: ProvideDeviceMQTTHandler must hand
// the Runner a checker backed by the SAME connector files connector.set
// writes. A template task requiring gmail is skipped (no agent turn, a
// "skipped" schedule.run ack with the reason and no error) until gmail is
// connected, and runs normally afterwards.
func TestProvideDeviceMQTTHandler_RunnerGuardsOnInstalledConnectors(t *testing.T) {
	t.Chdir(t.TempDir()) // schedules.json lands next to config.json (config.Dir)
	factory, messages := statusBroker(t)
	cfg := &config.Config{DeviceID: "sched-guard", FDChannel: "test/fd", OpenclawConfigDir: t.TempDir()}
	gw := &guardGateway{}
	h := ProvideDeviceMQTTHandler(cfg, factory, nil, nil, gw, nil, nil)

	if err := h.scheduleStore.Replace([]schedule.Schedule{{
		ID: "s1", Name: "Inbox digest", Instructions: "Summarize my unread email", Enabled: true,
		Cadence:  schedule.Spec{Repeat: schedule.RepeatDaily, Time: "08:00"},
		Requires: []string{"gmail"},
	}}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	sch, _ := h.scheduleStore.Get("s1")

	readAck := func() map[string]json.RawMessage {
		t.Helper()
		select {
		case raw := <-messages:
			var msg map[string]json.RawMessage
			if err := json.Unmarshal(raw, &msg); err != nil {
				t.Fatalf("ack is not JSON: %v", err)
			}
			return msg
		case <-time.After(5 * time.Second):
			t.Fatal("no schedule.run ack published")
			return nil
		}
	}

	if _, ran := h.scheduleRunner.RunNow(sch); !ran {
		t.Fatal("RunNow deferred a skip")
	}
	if len(gw.sent) != 0 {
		t.Fatalf("agent received %v for a task whose connector is missing", gw.sent)
	}
	ack := readAck()
	if string(ack["kind"]) != `"schedule.run"` || string(ack["status"]) != `"skipped"` {
		t.Fatalf("ack kind/status = %s/%s, want schedule.run/skipped", ack["kind"], ack["status"])
	}
	if _, hasErr := ack["error"]; hasErr {
		t.Errorf("skipped ack carries error %s, want none (a skip is not a failure)", ack["error"])
	}
	var data map[string]any
	if err := json.Unmarshal(ack["data"], &data); err != nil {
		t.Fatalf("ack data: %v", err)
	}
	if data["summary"] != "missing connector: gmail" {
		t.Errorf("ack summary = %v, want %q", data["summary"], "missing connector: gmail")
	}

	writeConnector(t, &h, "gmail")
	if _, ran := h.scheduleRunner.RunNow(sch); !ran {
		t.Fatal("RunNow deferred against a free gateway")
	}
	if len(gw.sent) != 1 {
		t.Fatalf("sent = %v, want the task to run once gmail is connected", gw.sent)
	}
	if ack := readAck(); string(ack["status"]) != `"success"` {
		t.Fatalf("ack status after connecting gmail = %s, want success", ack["status"])
	}
}

// The device web UI renders a skipped run's reason from last_run_summary, so
// the list endpoint must echo it.
func TestToScheduleListItem_EchoesLastRunSummary(t *testing.T) {
	item := toScheduleListItem(schedule.Schedule{
		ID: "s1", LastRunStatus: schedule.RunStatusSkipped, LastRunSummary: "missing connector: gmail",
	})
	if item.LastRunStatus != schedule.RunStatusSkipped || item.LastRunSummary != "missing connector: gmail" {
		t.Fatalf("item = %+v, want status skipped and the summary echoed", item)
	}
	raw, err := json.Marshal(item)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), `"last_run_summary":"missing connector: gmail"`) {
		t.Fatalf("JSON = %s, want a last_run_summary key", raw)
	}
	if raw, _ := json.Marshal(toScheduleListItem(schedule.Schedule{ID: "s2"})); strings.Contains(string(raw), "last_run_summary") {
		t.Fatalf("never-run row JSON = %s, want no last_run_summary key", raw)
	}
}
