package device

import (
	"context"
	"errors"
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// fakeGateway implements only the channel methods; other calls panic.
type fakeGateway struct {
	domain.AgentGateway
	supported    []string
	addCalls     []domain.AddChannelRequest
	refreshCalls []domain.RefreshChannelRequest
}

func (f *fakeGateway) Name() string                   { return "Fake" }
func (f *fakeGateway) SupportedChannels() []string    { return f.supported }
func (f *fakeGateway) HasWhatsappSession(string) bool { return false }

func (f *fakeGateway) AddChannel(_ context.Context, d domain.AddChannelRequest) error {
	f.addCalls = append(f.addCalls, d)
	return nil
}

func (f *fakeGateway) RefreshChannelConfig(_ context.Context, r domain.RefreshChannelRequest) (string, error) {
	f.refreshCalls = append(f.refreshCalls, r)
	return "1.2.3", nil
}

func newService(gw domain.AgentGateway, cfg *config.Config) *Service {
	return &Service{config: cfg, agentGateway: gw}
}

func TestAddChannelRejectsUnsupported(t *testing.T) {
	gw := &fakeGateway{supported: []string{domain.ChannelTelegram}}
	cfg := &config.Config{}
	s := newService(gw, cfg)

	_, err := s.AddChannel(context.Background(), domain.AddChannelRequest{
		Channel: domain.ChannelSlack, SlackBotToken: "xoxb", SlackAppToken: "xapp",
	})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Fatalf("AddChannel(slack) err = %v, want ErrChannelNotSupported", err)
	}
	if cfg.SlackBotToken != "" {
		t.Errorf("unsupported channel persisted SlackBotToken = %q, want empty", cfg.SlackBotToken)
	}
	if len(gw.addCalls) != 0 {
		t.Errorf("gateway.AddChannel called %d times for unsupported channel, want 0", len(gw.addCalls))
	}
}

func TestAddChannelSupportedPersistsAndDelegates(t *testing.T) {
	t.Chdir(t.TempDir()) // config.Save() writes config/config.json relative to cwd

	gw := &fakeGateway{supported: []string{domain.ChannelSlack}}
	cfg := &config.Config{}
	s := newService(gw, cfg)

	if _, err := s.AddChannel(context.Background(), domain.AddChannelRequest{
		Channel: domain.ChannelSlack, SlackBotToken: "xoxb", SlackAppToken: "xapp", SlackUserID: "U1",
	}); err != nil {
		t.Fatalf("AddChannel(slack) err = %v, want nil", err)
	}
	if cfg.SlackBotToken != "xoxb" || cfg.Channel != domain.ChannelSlack {
		t.Errorf("config not persisted: Channel=%q SlackBotToken=%q", cfg.Channel, cfg.SlackBotToken)
	}
	if len(gw.addCalls) != 1 {
		t.Fatalf("gateway.AddChannel called %d times, want 1", len(gw.addCalls))
	}
}

func TestRefreshChannelConfigRejectsUnsupported(t *testing.T) {
	gw := &fakeGateway{supported: []string{domain.ChannelTelegram}}
	s := newService(gw, &config.Config{})

	if _, err := s.RefreshChannelConfig(context.Background(), domain.ChannelSlack); !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Fatalf("RefreshChannelConfig(slack) err = %v, want ErrChannelNotSupported", err)
	}
	if len(gw.refreshCalls) != 0 {
		t.Errorf("gateway.RefreshChannelConfig called for unsupported channel")
	}
}

func TestRefreshChannelConfigDiscordBuildsRequest(t *testing.T) {
	gw := &fakeGateway{supported: []string{domain.ChannelDiscord}}
	cfg := &config.Config{DiscordBotToken: "dtok", DiscordGuildID: "g1", DiscordUserID: "u1"}
	s := newService(gw, cfg)

	if _, err := s.RefreshChannelConfig(context.Background(), domain.ChannelDiscord); err != nil {
		t.Fatalf("RefreshChannelConfig(discord) err = %v, want nil", err)
	}
	if len(gw.refreshCalls) != 1 {
		t.Fatalf("gateway.RefreshChannelConfig called %d times, want 1", len(gw.refreshCalls))
	}
	got := gw.refreshCalls[0]
	if got.Channel != domain.ChannelDiscord || got.DiscordBotToken != "dtok" || got.DiscordGuildID != "g1" {
		t.Errorf("refresh request = %+v, want discord creds from config", got)
	}
}

func TestRefreshChannelConfigMissingCreds(t *testing.T) {
	gw := &fakeGateway{supported: []string{domain.ChannelSlack}}
	s := newService(gw, &config.Config{}) // no slack token

	if _, err := s.RefreshChannelConfig(context.Background(), domain.ChannelSlack); !errors.Is(err, ErrSlackCredentialsMissing) {
		t.Fatalf("RefreshChannelConfig(slack, no creds) err = %v, want ErrSlackCredentialsMissing", err)
	}
}
