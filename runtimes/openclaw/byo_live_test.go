package openclaw

// Live check for #198: resolveModels against a real OpenAI-compatible server.

import (
	"context"
	"os"
	"strings"
	"testing"
)

func liveURL(t *testing.T) string {
	t.Helper()
	u := strings.TrimSpace(os.Getenv("BYO_LIVE_URL"))
	if u == "" {
		t.Skip("BYO_LIVE_URL unset — no live endpoint to test against")
	}
	return u
}

func TestLiveBYOEndpointListsItsOwnModels(t *testing.T) {
	url := liveURL(t)

	if isAutonomousEndpoint(url) {
		t.Fatalf("%s classified as an Autonomous host — it would take the hosted path", url)
	}

	resp, byo, err := resolveModels(context.Background(), url, "")
	if err != nil {
		t.Fatalf("resolveModels(%s): %v", url, err)
	}
	if !byo {
		t.Fatal("byo=false — the caller cannot tell the catalog came from the endpoint")
	}
	if len(resp.Models) == 0 {
		t.Fatal("no models returned; a BYO endpoint serving none must be an error, not an empty catalog")
	}
	// The bug this closes: the brain used to advertise the hosted catalog's model keys at an endpoint that has never heard of them.
	for _, m := range resp.Models {
		if strings.HasPrefix(m.Key, "claude-") {
			t.Errorf("model %q came from the hosted catalog, not from %s", m.Key, url)
		}
	}
	if resp.DefaultModel != resp.Models[0].Key {
		t.Errorf("DefaultModel %q is not the first listed model %q", resp.DefaultModel, resp.Models[0].Key)
	}
	if resp.API != "openai-completions" {
		t.Errorf("API = %q, want openai-completions", resp.API)
	}
	t.Logf("live catalog from %s: %d model(s), default %q, api %q",
		url, len(resp.Models), resp.DefaultModel, resp.API)
	for _, m := range resp.Models {
		t.Logf("  - %s", m.Key)
	}
}

func TestLiveHostedPathIsUnchanged(t *testing.T) {
	liveURL(t)

	// A shipped device must not drift onto the discovery path.
	for _, base := range []string{
		"",
		"   ",
		"https://campaign-api.autonomous.ai/api/v1/ai/v1",
		"https://anything.autonomousdev.xyz/v1",
		"://not-a-url",
	} {
		if !isAutonomousEndpoint(base) {
			t.Errorf("base %q would take the BYO path — a shipped device must stay on the hosted catalog", base)
		}
	}
	// ...and the suffix trap must not read as ours.
	if isAutonomousEndpoint("https://autonomous.ai.evil.example.com/v1") {
		t.Error("autonomous.ai.evil.example.com classified as an Autonomous host")
	}
}
