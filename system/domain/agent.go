package domain

import (
	"context"
	"encoding/json"
	"errors"
)

// ErrNotSupportedByRuntime is returned by AgentGateway methods the active backend does not implement.
// Stubs must return it instead of nil so a config change is never silently dropped.
var ErrNotSupportedByRuntime = errors.New("not_supported_by_runtime")

// TelegramTarget represents a Telegram chat the bot is connected to.
type TelegramTarget struct {
	ChatID string // e.g. "158406741" (DM) or "-5179782244" (group)
	Type   string // "private", "group", "supergroup", "channel"
}

// ChannelSender delivers messages to a specific messaging channel (Telegram, Discord, Slack, etc.).
type ChannelSender interface {
	// Name returns the channel name (e.g. "telegram", "discord", "slack").
	Name() string

	// IsConfigured returns true if this channel has valid credentials/config.
	IsConfigured() bool

	// Send delivers a message with an optional image to all targets in this channel.
	Send(msg string, imagePath string) error
}

// AgentEventHandler processes events from an agent gateway connection.
type AgentEventHandler func(ctx context.Context, evt WSEvent) error

// AgentGateway abstracts an agentic runtime (OpenClaw, PicoClaw, etc.).
type AgentGateway interface {
	// Name returns the display name of this agent gateway (e.g. "OpenClaw", "PicoClaw").
	Name() string

	// Version returns the active backend's own version; empty when undetected.
	Version() string

	// IsReady returns true when the agent runtime is connected and ready.
	IsReady() bool

	// ConnectedAt returns when the connection last became ready (Unix seconds), or 0.
	ConnectedAt() int64

	// AgentUptime returns the runtime process uptime in seconds, or 0 when unknown.
	AgentUptime() int64

	// IsBusy returns true when the agent is currently processing a turn.
	IsBusy() bool

	// SetBusy marks the agent as busy (true on lifecycle start, false on lifecycle end).
	SetBusy(busy bool)

	// QueuePendingEvent buffers an event (last-write-wins per type) to replay when idle.
	// fixedRunID preallocates the runID (web_chat); "" allocates one at drain.
	QueuePendingEvent(eventType, msg string, images []string, fixedRunID string)

	// DrainPendingEvents replays buffered events; exposed for device-busy waits with no agent turn ending.
	DrainPendingEvents()

	// SendChatMessage sends a user message to the agent. Returns the run ID.
	SendChatMessage(msg string) (string, error)

	// SendSystemChatMessage sends a system-originated message, shown separately in Flow Monitor.
	SendSystemChatMessage(msg string) (string, error)

	// SendChatMessageWithImages sends a message with base64 JPEG attachments; empty slice = none.
	SendChatMessageWithImages(msg string, imagesBase64 []string) (string, error)

	// NextChatRunID allocates the request id and idempotency key for the next chat.send.
	NextChatRunID() (reqID string, runID string)

	// SendChatMessageWithRun sends using a preallocated pair from NextChatRunID (same idempotency as chat.send).
	SendChatMessageWithRun(msg string, reqID string, runID string) (string, error)

	// SendChatMessageWithImagesAndRun is SendChatMessageWithImages with preallocated ids.
	SendChatMessageWithImagesAndRun(msg string, imagesBase64 []string, reqID string, runID string) (string, error)

	// SendSlashCommandWithRun sends a slash command (e.g. "/status") with deliver:false, replying only to the caller.
	SendSlashCommandWithRun(msg string, reqID string, runID string) (string, error)

	// SendSlashCommandWithImagesAndRun is SendSlashCommandWithRun with image attachments.
	SendSlashCommandWithImagesAndRun(msg string, imagesBase64 []string, reqID string, runID string) (string, error)

	// GetSessionKey returns the current agent session key, or empty string.
	GetSessionKey() string

	// SetSessionKey stores the session key for outgoing messages.
	SetSessionKey(key string)

	// SetupAgent configures and starts the agent runtime from setup data.
	SetupAgent(data SetupRequest) error

	// SupportedChannels returns the messaging channels this runtime can run (e.g. "telegram", "slack").
	SupportedChannels() []string

	// AddChannel adds a messaging channel; ctx bounds the CLI subprocess and restart.
	AddChannel(ctx context.Context, data AddChannelRequest) error

	// RefreshChannelConfig re-applies channels.<channel> config and restarts; returns the runtime version.
	RefreshChannelConfig(ctx context.Context, req RefreshChannelRequest) (runtime string, err error)

	// HasWhatsappSession reports whether a WhatsApp session exists for account ("default" when empty).
	HasWhatsappSession(account string) bool

	// PairWhatsapp runs the WhatsApp login flow; callers must drain. One active flow per device.
	PairWhatsapp(ctx context.Context) <-chan PairingEvent

	// ResetAgent factory-resets the agent runtime configuration.
	ResetAgent() error

	// RestartAgent restarts the agent runtime process.
	RestartAgent() error

	// RefreshModelsConfig patches model reasoning fields from LLMDisableThinking and restarts.
	RefreshModelsConfig() error

	// EnsureOnboarding seeds personality/identity files into the agent workspace.
	EnsureOnboarding() error

	// SaveSkill writes draft as <name>/SKILL.md in the skills dir and returns the path (no restart).
	SaveSkill(draft SkillDraft) (path string, err error)

	// InstallSkillArchive extracts a `.skill` archive into the skills dir; fallbackName is used when unwrapped.
	InstallSkillArchive(archivePath, fallbackName string) (dir string, err error)

	// InstallSkillMarkdown installs a bare SKILL.md named by its front-matter (else ErrInvalidFrontMatter).
	InstallSkillMarkdown(content []byte) (dir string, err error)

	// ListSkills returns the skills in this runtime's skills dir, each with its file tree.
	ListSkills() ([]InstalledSkill, error)

	// ReadSkillFiles returns one installed skill's files as a flat list with text inlined.
	ReadSkillFiles(name string) ([]SkillBundleFile, error)
	ExportSkillArchive(name, destDir string) (string, error)

	// ReadSkillFile returns one skill file by the path ReadSkillFiles emits (e.g. "music/SKILL.md").
	ReadSkillFile(name, filePath string) (SkillBundleFile, error)

	// DeleteSkill removes an installed skill; a missing one returns skills.ErrSkillNotFound.
	DeleteSkill(name string) (path string, err error)

	// FetchChatHistory returns the raw chat.history messages array (best-effort).
	FetchChatHistory(sessionKey string, limit int) (json.RawMessage, error)

	// GetConfigJSON returns the active runtime's raw config file bytes.
	GetConfigJSON() (json.RawMessage, error)

	// WriteMCPEntry upserts mcp.servers.<name> and restarts the gateway.
	WriteMCPEntry(name string, entry map[string]any) error

	// RemoveMCPEntry deletes mcp.servers.<name>; false (no restart) when already absent.
	RemoveMCPEntry(name string) (bool, error)

	// StartWS connects to the agent runtime and runs the event read loop.
	StartWS(ctx context.Context, handler AgentEventHandler)

	// MarkGuardRun marks a runID as a guard-active turn whose reply is broadcast to Telegram.
	MarkGuardRun(runID string, snapshotPath string)

	// ConsumeGuardRun returns the snapshot path for a guard run (one-shot).
	ConsumeGuardRun(runID string) (snapshotPath string, ok bool)

	// MarkBroadcastRun marks a runID whose reply is broadcast to all channels alongside TTS.
	MarkBroadcastRun(runID string)

	// ConsumeBroadcastRun checks if a runID is marked for broadcast. One-shot.
	ConsumeBroadcastRun(runID string) bool

	// MarkPoseBucketRun stashes pose bucket snapshots for a posture-nudge turn.
	// Filenames are relative to <SNAPSHOT_TMP_DIR>/sensing_pose/buckets/<bucketID>/.
	MarkPoseBucketRun(runID string, bucketID string, worstFilenames []string)

	// ConsumePoseBucketRun returns and removes the pose bucket info for a runID (one-shot).
	ConsumePoseBucketRun(runID string) (bucketID string, worstFilenames []string, ok bool)

	// MarkWebChatRun marks a web monitor chat run; TTS is suppressed for it.
	MarkWebChatRun(runID string)

	// IsWebChatRun checks if a runID is a web chat run (non-consuming).
	IsWebChatRun(runID string) bool

	// ConsumeWebChatRun checks and removes a web-chat-marked runID. One-shot.
	ConsumeWebChatRun(runID string) bool

	// MarkSilentRun marks a runID whose spoken reply is suppressed while the turn still runs.
	MarkSilentRun(runID string)

	// IsSilentRun checks if a runID is a silent run (non-consuming).
	IsSilentRun(runID string) bool

	// ConsumeSilentRun checks and removes a silent-marked runID. One-shot.
	ConsumeSilentRun(runID string) bool

	// SetPendingChatTrace records runID and exact sent text for MatchPendingByMessage.
	// message must match the WS text verbatim (the followup queue strips idempotencyKey).
	SetPendingChatTrace(runID string, message string)

	// RemovePendingChatTraceByRunID removes the pending entry whose runID matches target.
	RemovePendingChatTraceByRunID(target string) bool

	// MatchPendingByMessage removes and returns the runID whose message matches needle, or "".
	MatchPendingByMessage(needle string) string

	// GetTelegramBotToken returns the Telegram bot token used by the agent runtime.
	GetTelegramBotToken() string

	// GetTelegramTargets returns all Telegram chats (DMs + groups) the bot is connected to.
	GetTelegramTargets() ([]TelegramTarget, error)

	// Broadcast sends a message to all connected channels; imagePath is optional.
	Broadcast(msg string, imagePath string) error

	// SendToUser sends a Telegram DM; an empty user ID drops the message.
	SendToUser(telegramID string, msg string, imagePath string) error

	// SendToUserWithMedia sends a Telegram DM with multiple images (max 10, Telegram limit).
	SendToUserWithMedia(telegramID string, msg string, imagePaths []string) error

	// SendToHALTTS posts response text to HAL for TTS playback.
	SendToHALTTS(text string) error

	// Speak plays text via /voice/speak with no agent turn and no realtime feedback.
	// nil means HAL accepted the text, not that it was heard; text is 1..2000 chars, empty is a no-op.
	Speak(text string) error

	// SendToHALTTSQueue posts text to /voice/speak-queue (plays now or chains onto current speech).
	SendToHALTTSQueue(text string) error

	// StopTTS interrupts active TTS playback and music on HAL.
	StopTTS() error

	// SetVolume sets speaker volume on HAL (0-100).
	SetVolume(pct int) error

	// StartHALVoice starts the HAL voice pipeline; empty STT/TTS keys or URLs fall back to llmKey / llmBaseURL.
	StartHALVoice(deepgramKey, llmKey, sttKey, ttsKey, llmBaseURL, sttBaseURL, ttsBaseURL, ttsVoice, ttsInstructions, ttsProvider string) error

	// WatchIdentity polls IDENTITY.md and pushes updated wake words to HAL on rename.
	WatchIdentity(ctx context.Context)

	// UpdateIdentityName rewrites the **Name:** line in workspace/IDENTITY.md.
	UpdateIdentityName(name string) error

	// StartSkillWatcher polls OTA metadata for skill version changes and notifies the agent.
	StartSkillWatcher(ctx context.Context)

	// StartModelSync periodically reconciles the upstream model list; restarts only on change.
	StartModelSync(ctx context.Context)

	// UpdatePrimaryModel sets the primary model to "autonomous/{modelKey}" and restarts; no-op if empty.
	UpdatePrimaryModel(modelKey string) error

	// StartPrimaryModelWatch syncs external primary-model edits into config.LLMModel (autonomous provider only).
	StartPrimaryModelWatch(ctx context.Context)

	// GetConfiguredChannel returns the primary channel type (e.g. "telegram"), or "channel" if unknown.
	GetConfiguredChannel() string

	// CompactSession summarizes and reduces the session's history via sessions.compact.
	CompactSession(sessionKey string) error

	// NewSession starts a fresh session for key via sessions.new (device memory is unaffected).
	NewSession(sessionKey string) error

	// ShouldRotateSession reports whether to rotate the session now; each backend sets its own policy.
	ShouldRotateSession(totalTokens, turnsSinceRotation int) bool

	// IsRecentOutboundChat reports whether os-server recently sent this exact text (to skip echoes).
	IsRecentOutboundChat(text string) bool
}

// TurnAwareTTSQueue is implemented by runtimes that attach an ordered turn identity to queued TTS.
type TurnAwareTTSQueue interface {
	SendToHALTTSQueueForTurn(text, turnID string, turnSeq uint64) error
}

// ActiveTurnSteerer is implemented by runtimes that can append user input to a running turn.
type ActiveTurnSteerer interface {
	SupportsActiveTurnSteering() bool
}
