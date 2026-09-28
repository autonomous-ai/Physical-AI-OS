package harness

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func testResultStore(t *testing.T) (*ResultStore, string) {
	t.Helper()
	dir, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "private", "results.json")
	s, err := OpenResultStore(path)
	if err != nil {
		t.Fatal(err)
	}
	return s, path
}
func reserveResult(t *testing.T, s *ResultStore, key, dest string, bind bool) {
	t.Helper()
	in := ResultInput{Owner: "owner", ServerInstanceID: "instance", AgentID: "agent", IdempotencyKey: key, RunID: "run-" + key, Channel: "voice", Destination: dest, ExpiresAt: time.Now().Add(time.Hour)}
	if err := s.Reserve(in); err != nil {
		t.Fatal(err)
	}
	if bind {
		if err := s.BindReceipt("owner", "instance", "agent", key, "delivery-"+key); err != nil {
			t.Fatal(err)
		}
	}
}
func resultFrame(keys ...string) Frame {
	members := []ResultMember{}
	for _, key := range keys {
		members = append(members, ResultMember{DeliveryID: "delivery-" + key, IdempotencyKey: key})
	}
	scope := "input"
	if len(keys) > 1 {
		scope = "group"
	}
	p := ResultPayload{ServerInstanceID: "instance", ResultID: "result", Correlation: ResultCorrelation{Scope: scope, Inputs: members}, Outcome: "completed", FullText: "Done"}
	b, _ := json.Marshal(p)
	var payload map[string]any
	_ = json.Unmarshal(b, &payload)
	return Frame{"type": "event", "kind": "turn.summary", "agentId": "agent", "payload": payload}
}
func TestResultStoreGroupSubsetReplayAndRestart(t *testing.T) {
	s, path := testResultStore(t)
	for _, key := range []string{"a", "b", "c"} {
		reserveResult(t, s, key, "lamp", true)
	}
	r, fresh, err := s.Apply("owner", resultFrame("b", "a"), time.Now())
	if err != nil || !fresh || r.SpeechState != "pending" {
		t.Fatalf("apply %v %v %+v", err, fresh, r)
	}
	for _, in := range s.Inputs() {
		if (in.IdempotencyKey == "c") != (in.ResultID == "") {
			t.Fatalf("incorrect subset %+v", in)
		}
	}
	r.Payload.Correlation.Inputs[0].IdempotencyKey = "mutated"
	r.Inputs[0].RunID = "mutated"
	if _, fresh, err = s.Apply("owner", resultFrame("a", "b"), time.Now()); err != nil || fresh {
		t.Fatalf("reordered replay %v %v", fresh, err)
	}
	if _, claimed, err := s.Claim("owner", "instance", "result", time.Now()); err != nil || !claimed {
		t.Fatalf("claim %v %v", claimed, err)
	}
	s, err = OpenResultStore(path)
	if err != nil {
		t.Fatal(err)
	}
	r, _ = s.Get("owner", "instance", "result")
	if r.SpeechState != "uncertain" {
		t.Fatal(r.SpeechState)
	}
	if _, claimed, err := s.Claim("owner", "instance", "result", time.Now()); err != nil || claimed {
		t.Fatalf("replay claim %v %v", claimed, err)
	}
	changed := resultFrame("a", "b")
	changed["payload"].(map[string]any)["fullText"] = "Changed"
	if _, _, err = s.Apply("owner", changed, time.Now()); err == nil {
		t.Fatal("changed result accepted")
	}
}
func TestResultStoreAtomicMismatch(t *testing.T) {
	for _, variant := range []string{"key", "delivery", "agent", "owner", "instance"} {
		t.Run(variant, func(t *testing.T) {
			s, _ := testResultStore(t)
			reserveResult(t, s, "a", "lamp", true)
			reserveResult(t, s, "b", "lamp", true)
			f := resultFrame("a", "b")
			p := f["payload"].(map[string]any)
			owner := "owner"
			switch variant {
			case "key":
				p["correlation"].(map[string]any)["inputs"].([]any)[1].(map[string]any)["idempotencyKey"] = "wrong"
			case "delivery":
				p["correlation"].(map[string]any)["inputs"].([]any)[1].(map[string]any)["deliveryId"] = "wrong"
			case "agent":
				f["agentId"] = "wrong"
			case "owner":
				owner = "wrong"
			case "instance":
				p["serverInstanceId"] = "wrong"
			}
			if _, _, err := s.Apply(owner, f, time.Now()); err == nil {
				t.Fatal("mismatch accepted")
			}
			if len(s.Results()) != 0 {
				t.Fatal("partial result")
			}
			for _, in := range s.Inputs() {
				if in.ResultID != "" {
					t.Fatal("partial member")
				}
			}
		})
	}
}
func TestResultStoreReorderedReceiptStage(t *testing.T) {
	s, path := testResultStore(t)
	reserveResult(t, s, "a", "lamp", false)
	f := resultFrame("a")
	if err := s.Stage("owner", f); err != nil {
		t.Fatal(err)
	}
	if _, _, err := s.Apply("owner", f, time.Now()); err == nil {
		t.Fatal("unbound result accepted")
	}
	s, err := OpenResultStore(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(s.Staged()) != 1 {
		t.Fatal("lost inbox")
	}
	if err = s.BindReceipt("owner", "instance", "agent", "a", "delivery-a"); err != nil {
		t.Fatal(err)
	}
	if _, _, err = s.Apply("owner", s.Staged()[0].Frame, time.Now()); err != nil {
		t.Fatal(err)
	}
	if len(s.Staged()) != 0 {
		t.Fatal("inbox not removed")
	}
}
func TestResultStoreSuppressMixedExpiredLong(t *testing.T) {
	for _, variant := range []string{"mixed", "expired", "long"} {
		t.Run(variant, func(t *testing.T) {
			s, _ := testResultStore(t)
			reserveResult(t, s, "a", "lamp", true)
			dest := "lamp"
			if variant == "mixed" {
				dest = "other-lamp"
			}
			reserveResult(t, s, "b", dest, true)
			f := resultFrame("a", "b")
			now := time.Now()
			if variant == "expired" {
				now = now.Add(2 * time.Hour)
			}
			if variant == "long" {
				f["payload"].(map[string]any)["fullText"] = strings.Repeat("a", 2001)
			}
			r, _, err := s.Apply("owner", f, now)
			if err != nil || r.SpeechState != "suppressed" {
				t.Fatalf("%v %+v", err, r)
			}
			if _, claimed, _ := s.Claim("owner", "instance", "result", now); claimed {
				t.Fatal("suppressed playback")
			}
		})
	}
}
func TestResultStoreAdmissionIsNotPlayback(t *testing.T) {
	s, path := testResultStore(t)
	reserveResult(t, s, "a", "lamp", true)
	_, _, _ = s.Apply("owner", resultFrame("a"), time.Now())
	_, _, _ = s.Claim("owner", "instance", "result", time.Now())
	if err := s.Finish("owner", "instance", "result", "accepted"); err != nil {
		t.Fatal(err)
	}
	s, err := OpenResultStore(path)
	if err != nil {
		t.Fatal(err)
	}
	r, _ := s.Get("owner", "instance", "result")
	if r.SpeechState != "accepted" {
		t.Fatal(r.SpeechState)
	}
	if _, claimed, _ := s.Claim("owner", "instance", "result", time.Now()); claimed {
		t.Fatal("replayed admission")
	}
}
func TestResultStoreFileProtection(t *testing.T) {
	s, path := testResultStore(t)
	reserveResult(t, s, "a", "lamp", true)
	fi, err := os.Stat(path)
	if err != nil || fi.Mode().Perm() != 0600 {
		t.Fatalf("file mode %v %v", fi, err)
	}
	fi, err = os.Stat(filepath.Dir(path))
	if err != nil || fi.Mode().Perm() != 0700 {
		t.Fatalf("dir mode %v %v", fi, err)
	}
	link := filepath.Join(filepath.Dir(path), "link")
	if err = os.Symlink(path, link); err != nil {
		t.Fatal(err)
	}
	if _, err = OpenResultStore(link); err == nil {
		t.Fatal("symlink accepted")
	}
	if err = os.WriteFile(path, []byte("broken"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err = OpenResultStore(path); err == nil {
		t.Fatal("corruption accepted")
	}
}
func TestResultParserRejectsInvalidGroup(t *testing.T) {
	for _, variant := range []string{"duplicate", "scope", "singular", "outcome", "missing"} {
		t.Run(variant, func(t *testing.T) {
			f := resultFrame("a", "b")
			p := f["payload"].(map[string]any)
			switch variant {
			case "duplicate":
				f = resultFrame("a", "a")
			case "scope":
				p["correlation"].(map[string]any)["scope"] = "input"
			case "singular":
				p["runId"] = "arbitrary"
			case "outcome":
				p["outcome"] = "delivered"
			case "missing":
				delete(p, "fullText")
			}
			if _, _, err := ParseSummaryResult(f); err == nil {
				t.Fatal("invalid accepted")
			}
		})
	}
}

func TestResultStoreReservationRetryAndConflict(t *testing.T) {
	s, _ := testResultStore(t)
	in := ResultInput{Owner: "owner", ServerInstanceID: "instance", AgentID: "agent", IdempotencyKey: "key", RunID: "run", Channel: "voice", Destination: "lamp", ExpiresAt: time.Now().Add(time.Hour)}
	if err := s.Reserve(in); err != nil {
		t.Fatal(err)
	}
	if err := s.BindReceipt("owner", "instance", "agent", "key", "delivery"); err != nil {
		t.Fatal(err)
	}
	if err := s.Reserve(in); err != nil {
		t.Fatalf("same reservation retry: %v", err)
	}
	in.RunID = "other-run"
	if err := s.Reserve(in); err == nil {
		t.Fatal("changed key params accepted")
	}
	if err := s.BindReceipt("owner", "instance", "agent", "key", "other-delivery"); err == nil {
		t.Fatal("changed receipt accepted")
	}
}
func TestResultStoreCapacityPreservesPending(t *testing.T) {
	s, _ := testResultStore(t)
	reserveResult(t, s, "original", "lamp", true)
	s.mu.Lock()
	for i := len(s.data.Inputs); i < resultRecordLimit; i++ {
		key := string(rune(i + 100))
		s.data.Inputs[key] = ResultInput{IdempotencyKey: key}
	}
	s.mu.Unlock()
	in := ResultInput{Owner: "owner", ServerInstanceID: "instance", AgentID: "agent", IdempotencyKey: "new", RunID: "run", ExpiresAt: time.Now().Add(time.Hour)}
	if err := s.Reserve(in); err == nil {
		t.Fatal("capacity accepted")
	}
	if len(s.Inputs()) != resultRecordLimit {
		t.Fatal("pending entries evicted")
	}
}
func TestResultStorePersistenceFailureDoesNotApply(t *testing.T) {
	s, path := testResultStore(t)
	reserveResult(t, s, "a", "lamp", true)
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal(err)
	}
	if _, _, err := s.Apply("owner", resultFrame("a"), time.Now()); err == nil {
		t.Fatal("write failure accepted")
	}
	if len(s.Results()) != 0 || s.Inputs()[0].ResultID != "" {
		t.Fatal("failed persistence completed input")
	}
}

// This fixture exercises the agreed metadata on the existing summary event.
func TestResultParserSharedContractFixture(t *testing.T) {
	b, err := os.ReadFile("testdata/summary-results/group.json")
	if err != nil {
		t.Fatal(err)
	}
	var frame Frame
	if err = json.Unmarshal(b, &frame); err != nil {
		t.Fatal(err)
	}
	agent, p, err := ParseSummaryResult(frame)
	if err != nil {
		t.Fatal(err)
	}
	if agent != "agent-123" || p.ServerInstanceID != "instance-123" || p.ResultID != "result-456" || p.Correlation.Scope != "group" || len(p.Correlation.Inputs) != 2 || p.Correlation.EngineTurnID != "optional-engine-native-id" || p.Outcome != "completed" || p.FullText != "Created the house with a garden and a red roof." {
		t.Fatalf("unexpected parsed contract: %s %+v", agent, p)
	}
}

func TestResultStoreRejectsRunSharedByDifferentKeys(t *testing.T) {
	s, _ := testResultStore(t)
	in := ResultInput{Owner: "owner", ServerInstanceID: "instance", AgentID: "agent", IdempotencyKey: "a", RunID: "same-run", Channel: "voice", Destination: "lamp", ExpiresAt: time.Now().Add(time.Hour)}
	if err := s.Reserve(in); err != nil {
		t.Fatal(err)
	}
	in.IdempotencyKey = "b"
	in.AgentID = "other-agent"
	if err := s.Reserve(in); err == nil {
		t.Fatal("one run reserved twice")
	}
	if len(s.Inputs()) != 1 {
		t.Fatal("duplicate run persisted")
	}
}

func TestSummaryResultSingularMetadata(t *testing.T) {
	for _, location := range []string{"top", "payload"} {
		for _, scope := range []string{"input", "group"} {
			for _, field := range []string{"idempotencyKey", "turnId", "runId"} {
				t.Run(location+"/"+scope+"/"+field, func(t *testing.T) {
					f := resultFrame("a")
					if scope == "group" {
						f = resultFrame("a", "b")
					}
					fields := map[string]any(f)
					if location == "payload" {
						fields = f["payload"].(map[string]any)
					}
					value := "a"
					if field == "turnId" {
						value = "engine-turn"
					}
					fields[field] = value
					_, _, err := ParseSummaryResult(f)
					valid := scope == "input"
					if valid != (err == nil) {
						t.Fatalf("valid=%v err=%v", valid, err)
					}
					if field == "idempotencyKey" {
						fields[field] = "wrong"
						if _, _, err = ParseSummaryResult(f); err == nil {
							t.Fatal("mismatched singular key accepted")
						}
					}
				})
			}
		}
	}
	f := resultFrame("a")
	f["kind"] = "turn.result"
	if _, _, err := ParseSummaryResult(f); err == nil {
		t.Fatal("new event kind accepted")
	}
}

func TestSummaryResultIgnoresLegacyPreviewFieldsForIdentity(t *testing.T) {
	s, _ := testResultStore(t)
	reserveResult(t, s, "a", "lamp", true)
	f := resultFrame("a")
	p := f["payload"].(map[string]any)
	p["text"] = "old preview"
	p["kind"] = "completion"
	p["recap"] = map[string]any{"text": "legacy"}
	r, fresh, err := s.Apply("owner", f, time.Now())
	if err != nil || !fresh || r.Payload.FullText != "Done" {
		t.Fatalf("summary apply: %v %+v", err, r)
	}
	p["text"] = "changed preview"
	p["recap"] = "changed"
	if _, fresh, err = s.Apply("owner", f, time.Now()); err != nil || fresh {
		t.Fatalf("preview changed immutable identity: %v %v", fresh, err)
	}
}

func TestSummaryResultRunAliasesMustMatchReservationIncludingReplay(t *testing.T) {
	for _, location := range []string{"top", "payload"} {
		for _, alias := range []string{"runId", "run_id"} {
			t.Run(location+"/"+alias, func(t *testing.T) {
				s, _ := testResultStore(t)
				reserveResult(t, s, "a", "lamp", true)
				f := resultFrame("a")
				fields := map[string]any(f)
				if location == "payload" {
					fields = f["payload"].(map[string]any)
				}
				fields[alias] = "wrong-run"
				if err := s.Stage("owner", f); err == nil {
					t.Fatal("wrong run staged")
				}
				if _, _, err := s.Apply("owner", f, time.Now()); err == nil {
					t.Fatal("wrong run applied")
				}
				fields[alias] = "run-a"
				if err := s.Stage("owner", f); err != nil {
					t.Fatal(err)
				}
				if _, fresh, err := s.Apply("owner", f, time.Now()); err != nil || !fresh {
					t.Fatalf("matching run rejected: %v", err)
				}
				fields[alias] = "wrong-run"
				if err := s.Stage("owner", f); err == nil {
					t.Fatal("wrong replay run staged")
				}
				if _, _, err := s.Apply("owner", f, time.Now()); err == nil {
					t.Fatal("wrong replay run applied")
				}
			})
		}
	}
}
