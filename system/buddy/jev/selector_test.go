package jev

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func fixtureTree() Tree {
	yes := true
	return Tree{SnapshotID: "snapshot", Frontmost: true, ExpiresInMS: 30000, Nodes: []Node{{Ref: "e1", Role: "AXButton", Title: "Continue", Actions: []string{"press"}, Enabled: &yes}}}
}

const decision = `{"answers":{"intent":{"type":"choice","choice":"ui_0","probabilities":{"ui_0":0.99,"none":0.01}},"fit_ui_0":{"type":"noul","noul":0.99}}}`

func TestSuggestObservedOnly(t *testing.T) {
	tree := fixtureTree()
	yes := true
	tree.Nodes = append(tree.Nodes, Node{Ref: "secret", Secure: true, Title: "SECRET", Actions: []string{"press"}, Enabled: &yes}, Node{Ref: "child", ParentRef: "secret", Title: "CHILD_SECRET", Actions: []string{"press"}, Enabled: &yes})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if strings.Contains(string(body), "SECRET") {
			t.Error("secure metadata leaked")
		}
		if r.Header.Get("Authorization") != "Bearer key" {
			t.Error("missing proxy authorization")
		}
		var request struct {
			State struct {
				Candidates []Candidate `json:"candidates"`
			} `json:"state"`
		}
		if err := json.Unmarshal(body, &request); err != nil || len(request.State.Candidates) != 1 {
			t.Error("unexpected candidates", string(body))
		}
		_, _ = io.WriteString(w, decision)
	}))
	defer server.Close()
	got := New(server.Client()).Suggest(context.Background(), "Press Continue", tree, Options{Enabled: true, Endpoint: server.URL, APIKey: "key"})
	if got.Target == nil || got.Target.Title != "Continue" || got.Target.Role != "AXButton" || got.Suggestion == nil || got.Suggestion.Ref != "e1" || got.Suggestion.UIAction != "press" || got.Suggestion.SnapshotID != "snapshot" {
		t.Fatalf("unexpected result: %+v", got)
	}
}

func TestDisabledDoesNotCall(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer server.Close()
	got := New(server.Client()).Suggest(context.Background(), "Press", fixtureTree(), Options{Endpoint: server.URL, APIKey: "key"})
	if got.Reason != "disabled" || calls.Load() != 0 {
		t.Fatal(got, calls.Load())
	}
}

func TestInvalidTrees(t *testing.T) {
	cases := map[string]func(*Tree){
		"duplicate":      func(tr *Tree) { tr.Nodes = append(tr.Nodes, tr.Nodes[0]) },
		"truncated":      func(tr *Tree) { tr.Truncated = true },
		"background":     func(tr *Tree) { tr.Frontmost = false },
		"expired":        func(tr *Tree) { tr.ExpiresInMS = 0 },
		"missing_parent": func(tr *Tree) { tr.Nodes[0].ParentRef = "missing" },
		"cycle":          func(tr *Tree) { tr.Nodes[0].ParentRef = "e1" },
		"oversize":       func(tr *Tree) { tr.Nodes[0].Title = strings.Repeat("a", 3000) },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			tr := fixtureTree()
			mutate(&tr)
			if _, _, ok := treeCandidates(tr); ok {
				t.Fatal("invalid tree accepted")
			}
		})
	}
	for _, mode := range []string{"unknown_enabled", "disabled", "secure", "unsupported"} {
		t.Run(mode, func(t *testing.T) {
			tr := fixtureTree()
			switch mode {
			case "unknown_enabled":
				tr.Nodes[0].Enabled = nil
			case "disabled":
				no := false
				tr.Nodes[0].Enabled = &no
			case "secure":
				tr.Nodes[0].Secure = true
			case "unsupported":
				tr.Nodes[0].Actions = []string{"set_value"}
			}
			cs, _, ok := treeCandidates(tr)
			if !ok || len(cs) != 0 {
				t.Fatal("unsafe candidate retained")
			}
		})
	}
}

func TestBusyTimeoutCooldown(t *testing.T) {
	entered := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		close(entered)
		select {
		case <-r.Context().Done():
		case <-time.After(time.Second):
		}
	}))
	defer server.Close()
	s := New(server.Client())
	opts := Options{Enabled: true, Endpoint: server.URL, APIKey: "key"}
	done := make(chan Result, 1)
	go func() { done <- s.Suggest(context.Background(), "Press Continue", fixtureTree(), opts) }()
	<-entered
	if got := s.Suggest(context.Background(), "Press Continue", fixtureTree(), opts); got.Reason != "busy" {
		t.Fatal(got)
	}
	select {
	case got := <-done:
		if got.Reason != "unavailable" {
			t.Fatal(got)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("deadline not respected")
	}
	if got := s.Suggest(context.Background(), "Press Continue", fixtureTree(), opts); got.Reason != "cooldown" {
		t.Fatal(got)
	}
}

func TestSnapshotExpiresDuringInference(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(40 * time.Millisecond)
		_, _ = io.WriteString(w, decision)
	}))
	defer server.Close()
	tree := fixtureTree()
	tree.ExpiresInMS = 5
	got := New(server.Client()).Suggest(context.Background(), "Press Continue", tree, Options{Enabled: true, Endpoint: server.URL, APIKey: "key"})
	if got.Suggestion != nil || got.Reason != "unavailable" {
		t.Fatal(got)
	}
}

func TestTooManyCandidatesDefersWithoutTruncating(t *testing.T) {
	tree := fixtureTree()
	for i := 1; i < 33; i++ {
		n := tree.Nodes[0]
		n.Ref = fmt.Sprintf("e%d", i+1)
		tree.Nodes = append(tree.Nodes, n)
	}
	if _, _, ok := treeCandidates(tree); ok {
		t.Fatal("oversized catalog accepted")
	}
}
