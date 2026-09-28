package mqtthandler

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/schedule"
	"go.autonomous.ai/os/system/server/config"
)

type recordedAlert struct{ title, detail string }

// alertRecorder stands in for the ops-alert transport via the handler's
// scheduleAlert hook, recording synchronously so a test can assert exactly
// which alerts one event produced.
type alertRecorder struct {
	mu  sync.Mutex
	got []recordedAlert
}

func (a *alertRecorder) record(title, detail string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.got = append(a.got, recordedAlert{title, detail})
}

func (a *alertRecorder) all() []recordedAlert {
	a.mu.Lock()
	defer a.mu.Unlock()
	return append([]recordedAlert(nil), a.got...)
}

// onlyAlert asserts exactly one alert was recorded and returns it.
func (a *alertRecorder) onlyAlert(t *testing.T) recordedAlert {
	t.Helper()
	got := a.all()
	if len(got) != 1 {
		t.Fatalf("alerts = %+v, want exactly one", got)
	}
	return got[0]
}

// alertTestHandler is a handler with a fake broker (acks and schedule.mutate
// publishes really go out), temp schedule stores, and the alert recorder.
func alertTestHandler(t *testing.T) (*DeviceMQTTHandler, *alertRecorder, <-chan []byte) {
	t.Helper()
	factory, messages := statusBroker(t)
	dir := t.TempDir()
	rec := &alertRecorder{}
	h := &DeviceMQTTHandler{
		config:          &config.Config{DeviceID: "alert-test", FDChannel: "test/fd"},
		mqttFactory:     factory,
		scheduleStore:   schedule.NewStore(filepath.Join(dir, "schedules.json")),
		scheduleIntents: schedule.NewIntentStore(filepath.Join(dir, "schedule-intents.json")),
		scheduleAlert:   rec.record,
	}
	return h, rec, messages
}

func nextPublish(t *testing.T, messages <-chan []byte) map[string]json.RawMessage {
	t.Helper()
	select {
	case raw := <-messages:
		var msg map[string]json.RawMessage
		if err := json.Unmarshal(raw, &msg); err != nil {
			t.Fatalf("publish is not JSON: %v", err)
		}
		return msg
	case <-time.After(5 * time.Second):
		t.Fatal("nothing was published")
		return nil
	}
}

func daily(id, name string) schedule.Schedule {
	return schedule.Schedule{ID: id, Name: name, Instructions: "x", Enabled: true,
		Cadence: schedule.Spec{Repeat: schedule.RepeatDaily, Time: "08:00"}}
}

func syncEnv(t *testing.T, schedules ...schedule.Schedule) domain.MQTTDataCommand {
	t.Helper()
	data, err := json.Marshal(scheduleSyncPayload{Timezone: "UTC", Schedules: schedules})
	if err != nil {
		t.Fatal(err)
	}
	return domain.MQTTDataCommand{Kind: domain.KindScheduleSync, Data: data}
}

// ── schedule.sync ───────────────────────────────────────────────────────────

func TestScheduleSyncAlertTitle(t *testing.T) {
	prior := []schedule.Schedule{daily("a", "Morning check-in"), daily("b", "Old digest")}
	cases := []struct {
		name  string
		prior []schedule.Schedule
		next  []schedule.Schedule
		want  string
	}{
		{"created and deleted", prior,
			[]schedule.Schedule{daily("a", "Morning check-in"), daily("c", "Inbox digest"), daily("d", "Standup")},
			"✅ schedule.sync — applied 3 (created: Inbox digest, Standup; deleted: Old digest)"},
		{"nothing changed", prior, prior, "✅ schedule.sync — applied 2"},
		{"only created", nil, []schedule.Schedule{daily("c", "Inbox digest")}, "✅ schedule.sync — applied 1 (created: Inbox digest)"},
		{"only deleted (empty list)", prior, nil, "✅ schedule.sync — applied 0 (deleted: Morning check-in, Old digest)"},
		{"a nameless row is named by its id", nil, []schedule.Schedule{daily("c", " ")}, "✅ schedule.sync — applied 1 (created: c)"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := scheduleSyncAlertTitle(len(tc.next), tc.prior, tc.next); got != tc.want {
				t.Fatalf("title = %q, want %q", got, tc.want)
			}
		})
	}
}

// One summary alert per applied sync, diffed against what was on disk before.
func TestHandleScheduleSync_AlertsTheDiff(t *testing.T) {
	h, rec, messages := alertTestHandler(t)
	if err := h.scheduleStore.Replace([]schedule.Schedule{daily("a", "Morning check-in"), daily("b", "Old digest")}); err != nil {
		t.Fatalf("seed: %v", err)
	}

	if err := h.handleScheduleSync(syncEnv(t, daily("a", "Morning check-in"), daily("c", "Inbox digest"), daily("d", "Standup"))); err != nil {
		t.Fatalf("handleScheduleSync: %v", err)
	}

	if ack := nextPublish(t, messages); string(ack["status"]) != `"success"` {
		t.Fatalf("ack status = %s, want success", ack["status"])
	}
	got := rec.onlyAlert(t)
	if want := "✅ schedule.sync — applied 3 (created: Inbox digest, Standup; deleted: Old digest)"; got.title != want {
		t.Fatalf("title = %q, want %q", got.title, want)
	}
}

func TestHandleScheduleSync_AlertsInvalidPayload(t *testing.T) {
	h, rec, messages := alertTestHandler(t)
	_ = h.handleScheduleSync(domain.MQTTDataCommand{Kind: domain.KindScheduleSync, Data: json.RawMessage(`{"schedules": 7}`)})

	if ack := nextPublish(t, messages); string(ack["status"]) != `"failure"` {
		t.Fatalf("ack status = %s, want failure", ack["status"])
	}
	got := rec.onlyAlert(t)
	if got.title != "❌ schedule.sync — FAILED" || !strings.HasPrefix(got.detail, "invalid schedule.sync data: ") {
		t.Fatalf("alert = %+v, want the FAILED title and the parse error as detail", got)
	}
}

func TestHandleScheduleSync_AlertsStoreFailure(t *testing.T) {
	h, rec, messages := alertTestHandler(t)
	notADir := filepath.Join(t.TempDir(), "file")
	if err := os.WriteFile(notADir, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	h.scheduleStore = schedule.NewStore(filepath.Join(notADir, "schedules.json")) // parent is a file: every write fails

	_ = h.handleScheduleSync(syncEnv(t, daily("a", "Morning check-in")))

	if ack := nextPublish(t, messages); string(ack["status"]) != `"failure"` {
		t.Fatalf("ack status = %s, want failure", ack["status"])
	}
	got := rec.onlyAlert(t)
	if got.title != "❌ schedule.sync — FAILED" || !strings.HasPrefix(got.detail, "store: ") {
		t.Fatalf("alert = %+v, want the FAILED title and the store error as detail", got)
	}
}

// ── device-side create / delete ─────────────────────────────────────────────

func serveCRUD(t *testing.T, handle func(*gin.Context), method, body string, params gin.Params) *httptest.ResponseRecorder {
	t.Helper()
	gin.SetMode(gin.TestMode)
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(method, "/api/schedule", strings.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Params = params
	handle(c)
	return w
}

func TestCreateSchedule_AlertsCreated(t *testing.T) {
	h, rec, _ := alertTestHandler(t)
	w := serveCRUD(t, h.CreateSchedule, http.MethodPost,
		`{"name":"Inbox digest","instructions":"Summarize my unread email","schedule":{"repeat":"daily","time":"08:00"}}`, nil)
	if w.Code != http.StatusAccepted {
		t.Fatalf("status = %d, body %s", w.Code, w.Body)
	}
	got := rec.onlyAlert(t)
	if got.title != "✅ Schedule created: Inbox digest" {
		t.Fatalf("title = %q", got.title)
	}
	if strings.Contains(got.title+got.detail, "Summarize my unread email") {
		t.Fatalf("alert leaks the task instructions: %+v", got)
	}
}

func TestCreateSchedule_AlertsFailure(t *testing.T) {
	t.Run("invalid task", func(t *testing.T) {
		h, rec, _ := alertTestHandler(t)
		w := serveCRUD(t, h.CreateSchedule, http.MethodPost,
			`{"name":"Inbox digest","instructions":"","schedule":{"repeat":"daily","time":"08:00"}}`, nil)
		if w.Code != http.StatusBadRequest {
			t.Fatalf("status = %d", w.Code)
		}
		got := rec.onlyAlert(t)
		if got.title != "❌ Schedule create: Inbox digest — FAILED" || got.detail != "instructions are required" {
			t.Fatalf("alert = %+v", got)
		}
	})
	t.Run("unparseable body has no name", func(t *testing.T) {
		h, rec, _ := alertTestHandler(t)
		w := serveCRUD(t, h.CreateSchedule, http.MethodPost, `{not json`, nil)
		if w.Code != http.StatusBadRequest {
			t.Fatalf("status = %d", w.Code)
		}
		got := rec.onlyAlert(t)
		if got.title != "❌ Schedule create — FAILED" || !strings.HasPrefix(got.detail, "invalid body: ") {
			t.Fatalf("alert = %+v", got)
		}
	})
}

func TestDeleteSchedule_AlertsDeleted(t *testing.T) {
	h, rec, _ := alertTestHandler(t)
	if err := h.scheduleStore.Replace([]schedule.Schedule{daily("a", "Morning check-in")}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	w := serveCRUD(t, h.DeleteSchedule, http.MethodDelete, "", gin.Params{{Key: "id", Value: "a"}})
	if w.Code != http.StatusAccepted {
		t.Fatalf("status = %d, body %s", w.Code, w.Body)
	}
	if got := rec.onlyAlert(t); got.title != "✅ Schedule deleted: Morning check-in" {
		t.Fatalf("title = %q", got.title)
	}
}

func TestDeleteSchedule_AlertsUnknownID(t *testing.T) {
	h, rec, _ := alertTestHandler(t)
	w := serveCRUD(t, h.DeleteSchedule, http.MethodDelete, "", gin.Params{{Key: "id", Value: "nope"}})
	if w.Code != http.StatusNotFound {
		t.Fatalf("status = %d", w.Code)
	}
	got := rec.onlyAlert(t)
	if got.title != "❌ Schedule delete: nope — FAILED" || got.detail != "unknown schedule id: nope" {
		t.Fatalf("alert = %+v", got)
	}
}

// ── runs ────────────────────────────────────────────────────────────────────

func TestScheduleRunAlert(t *testing.T) {
	cases := []struct {
		name       string
		rr         schedule.RunReport
		wantTitle  string
		wantDetail string
	}{
		{"ticker success", schedule.RunReport{Name: "Daily briefing", Status: "success", Summary: "Daily briefing"},
			"✅ Schedule ran: Daily briefing", ""},
		{"run now success", schedule.RunReport{Name: "Daily briefing", Manual: true, Status: "success", Summary: "Daily briefing"},
			"✅ Schedule ran: Daily briefing (run now)", ""},
		{"ticker skip", schedule.RunReport{Name: "Inbox digest", Status: schedule.RunStatusSkipped, Summary: "missing connector: gmail"},
			"⏭️ Schedule skipped: Inbox digest — missing connector: gmail", ""},
		{"run now skip", schedule.RunReport{Name: "Inbox digest", Manual: true, Status: schedule.RunStatusSkipped, Summary: "missing connector: gmail, slack"},
			"⏭️ Schedule skipped: Inbox digest (run now) — missing connector: gmail, slack", ""},
		{"ticker failure", schedule.RunReport{Name: "Daily briefing", Status: "failure", Summary: "ws disconnected"},
			"❌ Schedule run failed: Daily briefing", "ws disconnected"},
		{"run now failure", schedule.RunReport{Name: "Daily briefing", Manual: true, Status: "failure", Summary: "ws disconnected"},
			"❌ Schedule run failed: Daily briefing (run now)", "ws disconnected"},
		{"nameless task falls back to its id", schedule.RunReport{ScheduleID: "s9", Status: "success"},
			"✅ Schedule ran: s9", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			title, detail := scheduleRunAlert(tc.rr)
			if title != tc.wantTitle || detail != tc.wantDetail {
				t.Fatalf("alert = (%q, %q), want (%q, %q)", title, detail, tc.wantTitle, tc.wantDetail)
			}
		})
	}
}

// The alert hangs off the Runner's report callback, so a real Run now through
// the Runner (a skip, here) produces exactly one alert, suffix included.
func TestRunNow_AlertsThroughTheReportCallback(t *testing.T) {
	h, rec, messages := alertTestHandler(t)
	if err := h.scheduleStore.Replace([]schedule.Schedule{{ID: "s1", Name: "Inbox digest", Instructions: "x", Enabled: true,
		Cadence: schedule.Spec{Repeat: schedule.RepeatDaily, Time: "08:00"}, Requires: []string{"gmail"}}}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	h.scheduleRunner = schedule.NewRunner(h.scheduleStore, &guardGateway{}, "alert-test", h.publishScheduleRunReport)
	h.scheduleRunner.SetConnectorChecker(schedule.ConnectorCheckerFunc(func(string) bool { return false }))
	sch, _ := h.scheduleStore.Get("s1")

	if _, ran := h.scheduleRunner.RunNow(sch); !ran {
		t.Fatal("RunNow deferred a skip")
	}
	if ack := nextPublish(t, messages); string(ack["status"]) != `"skipped"` {
		t.Fatalf("ack status = %s", ack["status"])
	}
	if got := rec.onlyAlert(t); got.title != "⏭️ Schedule skipped: Inbox digest (run now) — missing connector: gmail" {
		t.Fatalf("title = %q", got.title)
	}
}

// ── best-effort: the alert can neither change nor delay the ack ─────────────

// Production path, no test hook: the alert endpoint holds every request open
// and then fails it with a 500. The acks must still go out immediately and be
// byte-for-byte what they would have been without alerting — and the alert
// must still have been attempted with the right title.
func TestScheduleAlerts_FailingAlertNeitherChangesNorDelaysTheAck(t *testing.T) {
	release := make(chan struct{})
	alerts := make(chan string, 4)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var p struct {
			Message string `json:"message"`
		}
		_ = json.Unmarshal(body, &p)
		alerts <- p.Message
		<-release
		w.WriteHeader(http.StatusInternalServerError)
	}))
	t.Cleanup(srv.Close)                 // runs second: waits for the held requests to finish
	t.Cleanup(func() { close(release) }) // runs first: lets them fail with the 500

	factory, messages := statusBroker(t)
	dir := t.TempDir()
	h := &DeviceMQTTHandler{
		config: &config.Config{DeviceID: "alert-test", FDChannel: "test/fd",
			LLMBaseURL: srv.URL + "/v1", LLMAPIKey: "lob_test"},
		mqttFactory:   factory,
		scheduleStore: schedule.NewStore(filepath.Join(dir, "schedules.json")),
	}

	rr := schedule.RunReport{
		ScheduleID: "s1", Name: "Inbox digest", StartedAt: time.Date(2026, 9, 18, 8, 0, 0, 0, time.UTC),
		Status: schedule.RunStatusSkipped, Summary: "missing connector: gmail",
		NextRunAt: time.Date(2026, 9, 19, 8, 0, 0, 0, time.UTC),
	}
	within(t, "publishScheduleRunReport", func() { h.publishScheduleRunReport(rr) })
	ack := nextPublish(t, messages)
	if string(ack["status"]) != `"skipped"` {
		t.Errorf("ack status = %s, want skipped", ack["status"])
	}
	if _, hasErr := ack["error"]; hasErr {
		t.Errorf("ack gained an error %s", ack["error"])
	}
	wantData, _ := json.Marshal(buildScheduleRunReportData(rr))
	var gotData, wantDataV map[string]any
	_ = json.Unmarshal(ack["data"], &gotData)
	_ = json.Unmarshal(wantData, &wantDataV)
	if len(gotData) != len(wantDataV) || gotData["summary"] != wantDataV["summary"] || gotData["next_run_at"] != wantDataV["next_run_at"] {
		t.Errorf("ack data = %v, want exactly %v", gotData, wantDataV)
	}

	within(t, "handleScheduleSync", func() { _ = h.handleScheduleSync(syncEnv(t, daily("a", "Morning check-in"))) })
	if ack := nextPublish(t, messages); string(ack["status"]) != `"success"` {
		t.Errorf("sync ack status = %s, want success", ack["status"])
	}

	// Both alerts were attempted on the production transport, each with its
	// exact title as the first line. They are asynchronous, so they may reach
	// the endpoint in either order — match them as a set.
	want := map[string]bool{
		"⏭️ Schedule skipped: Inbox digest — missing connector: gmail": false,
		"✅ schedule.sync — applied 1 (created: Morning check-in)":      false,
	}
	for range want {
		select {
		case msg := <-alerts:
			title, _, _ := strings.Cut(msg, "\n")
			if seen, ok := want[title]; !ok || seen {
				t.Errorf("unexpected or duplicate alert title %q (full message %q)", title, msg)
			}
			want[title] = true
		case <-time.After(10 * time.Second):
			t.Fatalf("not every alert was attempted: %v", want)
		}
	}
}

// within fails the test if fn has not returned within 3s — i.e. if it waited
// on the (deliberately stuck) alert endpoint.
func within(t *testing.T, what string, fn func()) {
	t.Helper()
	done := make(chan struct{})
	go func() { fn(); close(done) }()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatalf("%s blocked on the ops alert", what)
	}
}
