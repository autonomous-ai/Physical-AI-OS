package harness

import (
	"encoding/json"
	"os"
	"reflect"
	"sort"
	"testing"
	"time"
)

func TestSummaryProducerFixtures(t *testing.T) {
	for _, name := range []string{"group-result.json", "input-result.json"} {
		t.Run(name, func(t *testing.T) {
			b, err := os.ReadFile("testdata/summary-results/pr336/" + name)
			if err != nil {
				t.Fatal(err)
			}
			var f Frame
			if err := json.Unmarshal(b, &f); err != nil {
				t.Fatal(err)
			}
			if _, _, err := ParseSummaryResult(f); err != nil {
				t.Fatal(err)
			}
			if name == "input-result.json" {
				f["payload"].(map[string]any)["turnId"] = nil
				if _, _, err := ParseSummaryResult(f); err != nil {
					t.Fatalf("nullable single turnId rejected: %v", err)
				}
			} else {
				f["payload"].(map[string]any)["turnId"] = nil
				if _, _, err := ParseSummaryResult(f); err == nil {
					t.Fatal("group allowed arbitrary singular turnId")
				}
			}
		})
	}
}

func TestSummaryProducerReplayCases(t *testing.T) {
	b, err := os.ReadFile("testdata/summary-results/pr336/os-replay-cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Pending  []ResultMember `json:"pending"`
		Instance string         `json:"originatingServerInstanceId"`
		Steps    []struct {
			Event    Frame `json:"event"`
			Expected struct {
				Resolve []string `json:"resolve"`
				Pending []string `json:"pending"`
				TTS     int      `json:"ttsEnqueueCount"`
				Resend  bool     `json:"resendInput"`
			} `json:"expected"`
		} `json:"steps"`
	}
	if err := json.Unmarshal(b, &fixture); err != nil {
		t.Fatal(err)
	}
	store, _ := testResultStore(t)
	for _, m := range fixture.Pending {
		if err := store.Reserve(ResultInput{Owner: "owner", ServerInstanceID: fixture.Instance, AgentID: "agent-fixture",
			IdempotencyKey: m.IdempotencyKey, RunID: "run-" + m.IdempotencyKey, Channel: "voice", Destination: "voice:main:0", ExpiresAt: time.Now().Add(time.Hour)}); err != nil {
			t.Fatal(err)
		}
		if err := store.BindReceipt("owner", fixture.Instance, "agent-fixture", m.IdempotencyKey, m.DeliveryID); err != nil {
			t.Fatal(err)
		}
	}
	for i, step := range fixture.Steps {
		if step.Event["type"] == "resync" {
			if step.Expected.Resend {
				t.Fatal("fixture requested resend")
			}
			continue // Transport restart creates no input or speaker work.
		}
		if err := store.Stage("owner", step.Event); err != nil {
			t.Fatalf("step %d: %v", i, err)
		}
		result, fresh, err := store.Apply("owner", step.Event, time.Now())
		if err != nil {
			t.Fatalf("step %d: %v", i, err)
		}
		resolved := []string{}
		if fresh {
			for _, in := range result.Inputs {
				resolved = append(resolved, in.DeliveryID)
			}
		}
		sort.Strings(resolved)
		sort.Strings(step.Expected.Resolve)
		if !reflect.DeepEqual(resolved, step.Expected.Resolve) {
			t.Fatalf("step %d: resolved=%v expected=%v", i, resolved, step.Expected.Resolve)
		}
		_, claimed, err := store.Claim("owner", result.Payload.ServerInstanceID, result.Payload.ResultID, time.Now())
		if err != nil {
			t.Fatal(err)
		}
		count := 0
		if claimed {
			count = 1
			if err := store.Finish("owner", result.Payload.ServerInstanceID, result.Payload.ResultID, "accepted"); err != nil {
				t.Fatal(err)
			}
		}
		if count != step.Expected.TTS {
			t.Fatalf("step %d: outbox submissions=%d expected=%d", i, count, step.Expected.TTS)
		}
		pending := []string{}
		for _, in := range store.Inputs() {
			if in.ResultID == "" {
				pending = append(pending, in.DeliveryID)
			}
		}
		sort.Strings(pending)
		sort.Strings(step.Expected.Pending)
		if !reflect.DeepEqual(pending, step.Expected.Pending) {
			t.Fatalf("step %d: pending=%v expected=%v", i, pending, step.Expected.Pending)
		}
	}
}
