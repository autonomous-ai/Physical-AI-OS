package agent

import (
	"context"
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

type fakeGateway struct {
	domain.AgentGateway
	supported []string
	addCalls  []domain.AddChannelRequest
}

func (f *fakeGateway) Name() string                { return "Fake" }
func (f *fakeGateway) SupportedChannels() []string { return f.supported }
func (f *fakeGateway) AddChannel(_ context.Context, d domain.AddChannelRequest) error {
	f.addCalls = append(f.addCalls, d)
	return nil
}

func channelsOf(reqs []domain.AddChannelRequest) map[string]bool {
	out := map[string]bool{}
	for _, r := range reqs {
		out[r.Channel] = true
	}
	return out
}

func TestReconcileNoOpWhenRuntimeUnchanged(t *testing.T) {
	gw := &fakeGateway{supported: []string{domain.ChannelTelegram}}
	cfg := &config.Config{AgentRuntime: "hermes", ChannelsAppliedRuntime: "hermes", TelegramBotToken: "t"}
	(&ChannelReconcile{cfg: cfg, gw: gw}).Reconcile()

	if len(gw.addCalls) != 0 {
		t.Errorf("Reconcile re-applied %d channels with no runtime change, want 0", len(gw.addCalls))
	}
}

func TestReconcileFirstObservationRecordsBaselineNoReapply(t *testing.T) {
	t.Chdir(t.TempDir()) // WithLockSave writes config/config.json relative to cwd

	gw := &fakeGateway{supported: []string{domain.ChannelTelegram, domain.ChannelSlack}}
	cfg := &config.Config{
		AgentRuntime:           "hermes",
		ChannelsAppliedRuntime: "",
		TelegramBotToken:       "t",
		SlackBotToken:          "s",
	}
	(&ChannelReconcile{cfg: cfg, gw: gw}).Reconcile()

	if len(gw.addCalls) != 0 {
		t.Errorf("first observation re-applied %d channels, want 0 (baseline only)", len(gw.addCalls))
	}
	if cfg.ChannelsAppliedRuntime != "hermes" {
		t.Errorf("baseline marker = %q, want hermes", cfg.ChannelsAppliedRuntime)
	}
}

func TestReconcileReappliesSupportedAndCollectsUnsupported(t *testing.T) {
	t.Chdir(t.TempDir()) // WithLockSave writes config/config.json relative to cwd

	gw := &fakeGateway{supported: []string{domain.ChannelTelegram, domain.ChannelSlack}}
	cfg := &config.Config{
		AgentRuntime:           "hermes",
		ChannelsAppliedRuntime: "openclaw",
		TelegramBotToken:       "t",
		SlackBotToken:          "s",
		DiscordBotToken:        "d",
	}
	r := &ChannelReconcile{cfg: cfg, gw: gw}
	r.Reconcile()

	applied := channelsOf(gw.addCalls)
	if !applied[domain.ChannelTelegram] || !applied[domain.ChannelSlack] {
		t.Errorf("supported channels not re-applied: %v", applied)
	}
	if applied[domain.ChannelDiscord] {
		t.Errorf("unsupported discord was re-applied, want skipped")
	}
	if len(cfg.ChannelsUnsupported) != 1 || cfg.ChannelsUnsupported[0] != domain.ChannelDiscord {
		t.Errorf("ChannelsUnsupported = %v, want [discord]", cfg.ChannelsUnsupported)
	}
	if cfg.ChannelsAppliedRuntime != "hermes" {
		t.Errorf("marker = %q, want advanced to hermes", cfg.ChannelsAppliedRuntime)
	}

	before := len(gw.addCalls)
	r.Reconcile()
	if len(gw.addCalls) != before {
		t.Errorf("second Reconcile re-applied channels, want no-op")
	}
}
