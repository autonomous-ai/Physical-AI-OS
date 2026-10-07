package openclaw

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"go.autonomous.ai/os/system/domain"
)

// defaultModels is the hardcoded list of supported models.
var defaultModels = []domain.LLMModel{
	{
		Key:       "claude-opus-4-6",
		Name:      "claude-opus-4-6",
		Reasoning: true,
		Input:     []string{"text", "image"},
		Privacy:   "private",
		Capabilities: &domain.LLMModelCapabilities{
			SupportsReasoning:       true,
			SupportsVision:          true,
			SupportsFunctionCalling: true,
		},
	},
	{
		Key:       "claude-haiku-4-5",
		Name:      "claude-haiku-4-5",
		Reasoning: true,
		Input:     []string{"text", "image"},
		Privacy:   "private",
		Capabilities: &domain.LLMModelCapabilities{
			SupportsReasoning:       true,
			SupportsVision:          true,
			SupportsFunctionCalling: true,
		},
	},
}

// SetupAgent writes openclaw.json from the setup request and restarts the gateway.
func (s *OpenclawService) SetupAgent(data domain.SetupRequest) error {
	slog.Debug("checking openclaw in PATH", "component", "openclaw")
	if _, err := exec.LookPath("openclaw"); err != nil {
		return fmt.Errorf("openclaw not found in PATH: %w", err)
	}
	slog.Debug("openclaw found", "component", "openclaw")

	llmAPIKey := data.LLMAPIKey
	llmBaseURL := data.LLMBaseURL
	channel := data.EffectiveChannel()

	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	if _, err := os.Stat(configPath); os.IsNotExist(err) {
		slog.Debug("config does not exist, running onboardOpenclaw", "component", "openclaw")
		if err := s.onboardOpenclaw(); err != nil {
			return fmt.Errorf("onboard openclaw: %w", err)
		}
	}
	slog.Debug("loading config", "component", "openclaw", "path", configPath)
	var configData map[string]interface{}
	if data, err := os.ReadFile(configPath); err == nil {
		if err := json.Unmarshal(data, &configData); err != nil {
			return fmt.Errorf("parse openclaw config: %w", err)
		}
		slog.Debug("config loaded and parsed", "component", "openclaw")
	} else {
		configData = make(map[string]interface{})
		slog.Debug("no existing config, starting fresh", "component", "openclaw")
	}

	runtimeManaged := s.config.LLMRuntimeManaged()
	var modelsResp *domain.LLMModelsListResponse
	var defaultModel domain.LLMModel
	usedFallback := false
	if !runtimeManaged {
		slog.Debug("fetching models", "component", "openclaw")
		var byo bool
		var err error
		modelsResp, byo, err = resolveModels(context.Background(), llmBaseURL, llmAPIKey)
		if err != nil {
			slog.Warn("setup: model fetch failed, using hardcoded fallback",
				"component", "openclaw", "byo", byo, "err", err)
			modelsResp = &domain.LLMModelsListResponse{Count: len(defaultModels), Models: defaultModels}
			usedFallback = true
		}
		if len(modelsResp.Models) == 0 {
			return fmt.Errorf("no llm models found")
		}
		slog.Debug("got models", "component", "openclaw", "count", len(modelsResp.Models), "fallback", usedFallback)

		// Precedence: upstream default_model, else persisted config.LLMModel (API down), else first in catalog.
		wantKey := strings.TrimSpace(modelsResp.DefaultModel)
		if usedFallback {
			wantKey = strings.TrimSpace(s.config.LLMModel)
		}
		if wantKey == "" {
			wantKey = modelsResp.Models[0].Key
		}
		defaultModel, err = findModelByLLMModel(modelsResp.Models, wantKey)
		if err != nil {
			// Requested key not in the catalog — fall back to the first model so setup never hard-fails on a stale/unknown selection.
			slog.Warn("setup: requested model not in catalog, using first", "component", "openclaw", "want", wantKey)
			defaultModel = modelsResp.Models[0]
		}
		slog.Debug("selected default model", "component", "openclaw", "key", defaultModel.Key)

		slog.Debug("building models.providers.autonomous", "component", "openclaw")
		modelsMap := ensureMap(configData, "models")
		modelsMap["mode"] = "merge"
		providersMap := ensureMap(modelsMap, "providers")
		modelsEntries := make([]any, 0, len(modelsResp.Models))
		for _, m := range modelsResp.Models {
			if s.config.LLMThinkingDisabled() {
				m.Reasoning = false
			}
			modelsEntries = append(modelsEntries, openclawModelToProviderEntry(m))
		}
		providersMap[customProviderName] = map[string]any{
			"baseUrl": llmBaseURL,
			"api":     resolveAutonomousAPI(modelsResp.API),
			"apiKey":  llmAPIKey,
			"models":  modelsEntries,
		}
		configData["models"] = modelsMap
	}

	slog.Debug("building agents.defaults", "component", "openclaw")
	agentsMap := ensureMap(configData, "agents")
	defaultsMap := ensureMap(agentsMap, "defaults")
	workspace := filepath.Join(s.config.OpenclawConfigDir, "workspace")
	defaultsMap["workspace"] = workspace
	defaultsMap["elevatedDefault"] = "full"
	sandboxMap := ensureMap(defaultsMap, "sandbox")
	sandboxMap["mode"] = "off"
	defaultsMap["sandbox"] = sandboxMap
	compactionMap := ensureMap(defaultsMap, "compaction")
	compactionMap["mode"] = "safeguard"
	compactionMap["reserveTokensFloor"] = 80000
	defaultsMap["compaction"] = compactionMap
	defaultsMap["bootstrapMaxChars"] = bootstrapMaxChars
	defaultsMap["bootstrapTotalMaxChars"] = bootstrapTotalMaxChars
	if !runtimeManaged {
		agentModelsMap := ensureMap(defaultsMap, "models")
		for _, m := range modelsResp.Models {
			agentModelsMap[agentModelKey(m)] = map[string]any{
				"params": map[string]any{
					"cacheRetention": "short",
				},
			}
		}
		defaultsMap["model"] = map[string]any{
			"primary": fmt.Sprintf("%s/%s", customProviderName, defaultModel.Key),
		}
		defaultsMap["models"] = agentModelsMap
		// Seed the default image/vision model from upstream when published.
		if img := strings.TrimSpace(modelsResp.DefaultImageModel); img != "" {
			defaultsMap["imageModel"] = map[string]any{
				"primary": fmt.Sprintf("%s/%s", customProviderName, img),
			}
		}
	}
	agentsMap["defaults"] = defaultsMap
	configData["agents"] = agentsMap

	channelsMap := ensureMap(configData, "channels")
	pluginsMap := ensureMap(configData, "plugins")
	entriesMap := ensureMap(pluginsMap, "entries")

	switch channel {
	case "slack":
		slog.Debug("setting channels.slack", "component", "openclaw")
		slackPluginCtx, slackCancel := context.WithTimeout(context.Background(), channelPluginInstallTimeout)
		slackPluginErr := ensureChannelPlugin(slackPluginCtx, domain.ChannelSlack, slackPluginPackage)
		slackCancel()
		if slackPluginErr != nil {
			return fmt.Errorf("ensure slack plugin: %w", slackPluginErr)
		}
		slackMap := ensureMap(channelsMap, "slack")
		applySlackChannelConfig(slackMap, slackChannelConfig{
			BotToken: data.SlackBotToken,
			AppToken: data.SlackAppToken,
			UserID:   data.SlackUserID,
			Runtime:  currentOpenclawRuntime(),
		})
		channelsMap["slack"] = slackMap
		if telegramMap, ok := channelsMap["telegram"].(map[string]any); ok {
			telegramMap["enabled"] = false
		}
		slackEntryMap := ensureMap(entriesMap, "slack")
		slackEntryMap["enabled"] = true
	case "discord":
		slog.Debug("setting channels.discord", "component", "openclaw")
		pluginCtx, cancel := context.WithTimeout(context.Background(), channelPluginInstallTimeout)
		err := ensureChannelPlugin(pluginCtx, domain.ChannelDiscord, discordPluginPackage)
		cancel()
		if err != nil {
			return fmt.Errorf("ensure discord plugin: %w", err)
		}
		discordMap := ensureMap(channelsMap, "discord")
		applyDiscordChannelConfig(discordMap, data.DiscordBotToken, data.DiscordUserID, data.DiscordGuildID)
		channelsMap["discord"] = discordMap
		discordEntryMap := ensureMap(entriesMap, "discord")
		discordEntryMap["enabled"] = true
	default:
		slog.Debug("setting channels.telegram", "component", "openclaw")
		telegramMap := ensureMap(channelsMap, "telegram")
		telegramMap["enabled"] = true
		telegramMap["botToken"] = data.TelegramBotToken
		if data.TelegramUserID != "" {
			telegramMap["dmPolicy"] = "allowlist"
			telegramMap["allowFrom"] = mergeStringList(telegramMap["allowFrom"], data.TelegramUserID)
		} else {
			telegramMap["dmPolicy"] = "open"
			telegramMap["allowFrom"] = mergeStringList(telegramMap["allowFrom"], "*")
		}
		channelsMap["telegram"] = telegramMap
		telegramEntryMap := ensureMap(entriesMap, "telegram")
		telegramEntryMap["enabled"] = true
	}
	configData["channels"] = channelsMap

	slog.Debug("ensuring gateway defaults", "component", "openclaw")
	gatewayMap := ensureMap(configData, "gateway")
	setDefaultValue(gatewayMap, "mode", defaultGatewayMode)
	setDefaultValue(gatewayMap, "bind", defaultGatewayBind)
	setDefaultValue(gatewayMap, "port", defaultGatewayPort)
	gatewayAuthMap := ensureMap(gatewayMap, "auth")
	setDefaultValue(gatewayAuthMap, "mode", "token")
	if existingToken := strings.TrimSpace(getStringValue(gatewayAuthMap, "token")); existingToken == "" {
		token, err := generateGatewayToken()
		if err != nil {
			return fmt.Errorf("generate gateway token: %w", err)
		}
		gatewayAuthMap["token"] = token
	}
	gatewayMap["auth"] = gatewayAuthMap
	configData["gateway"] = gatewayMap

	slog.Debug("ensuring full-access tools defaults", "component", "openclaw")
	toolsMap := ensureMap(configData, "tools")
	toolsMap["profile"] = "full"
	execMap := ensureMap(toolsMap, "exec")
	execMap["host"] = "gateway"
	execMap["security"] = "full"
	execMap["ask"] = "off"
	toolsMap["exec"] = execMap
	elevatedMap := ensureMap(toolsMap, "elevated")
	elevatedMap["enabled"] = true
	elevatedAllowFrom := ensureMap(elevatedMap, "allowFrom")
	elevatedAllowFrom[channel] = []any{"*"}
	elevatedMap["allowFrom"] = elevatedAllowFrom
	toolsMap["elevated"] = elevatedMap
	configData["tools"] = toolsMap

	slog.Debug("ensuring messages defaults", "component", "openclaw")
	messagesMap := ensureMap(configData, "messages")
	// No reply prefix: "auto" prints the agent id ("[main]") before every reply.
	if v, _ := messagesMap["responsePrefix"].(string); v == "auto" {
		delete(messagesMap, "responsePrefix")
	}
	messagesMap["ackReactionScope"] = "all"
	messagesMap["removeAckAfterReply"] = true
	configData["messages"] = messagesMap

	slog.Debug("ensuring logging defaults", "component", "openclaw")
	loggingMap := ensureMap(configData, "logging")
	loggingMap["consoleStyle"] = "pretty"
	loggingMap["file"] = "/var/log/openclaw/agent.log"
	loggingMap["level"] = "debug"
	loggingMap["consoleLevel"] = "debug"
	configData["logging"] = loggingMap

	slog.Debug("ensuring commands defaults", "component", "openclaw")
	commandsMap := ensureMap(configData, "commands")
	commandsMap["native"] = true
	commandsMap["nativeSkills"] = true
	commandsMap["text"] = true
	commandsMap["bash"] = true
	commandsMap["bashForegroundMs"] = 2000
	commandsMap["config"] = true
	commandsMap["debug"] = true
	commandsMap["restart"] = true
	commandsMap["useAccessGroups"] = false
	commandsMap["ownerAllowFrom"] = []any{"*"}
	configData["commands"] = commandsMap

	slog.Debug("ensuring skills defaults", "component", "openclaw")
	skillsMap := ensureMap(configData, "skills")
	loadMap := ensureMap(skillsMap, "load")
	skillsDir := filepath.Join(workspace, "skills")
	loadMap["extraDirs"] = []any{skillsDir}
	loadMap["watch"] = true
	skillsMap["load"] = loadMap
	configData["skills"] = skillsMap

	slog.Debug("marshalling and writing openclaw.json", "component", "openclaw")
	written, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal openclaw config: %w", err)
	}
	if err := os.MkdirAll(s.config.OpenclawConfigDir, 0755); err != nil {
		return fmt.Errorf("create openclaw config dir: %w", err)
	}
	expectedPrimary := customProviderName + "/" + defaultModel.Key
	s.primarySyncMu.Lock()
	if !runtimeManaged {
		setOSWriteFlag(s.config.OpenclawConfigDir, expectedPrimary)
	}
	writeErr := os.WriteFile(configPath, written, 0600)
	s.primarySyncMu.Unlock()
	if writeErr != nil {
		return fmt.Errorf("write openclaw config: %w", writeErr)
	}
	if err := chownRuntimeUserIfRoot(configPath, openclawRuntimeUser); err != nil {
		return fmt.Errorf("set openclaw config ownership: %w", err)
	}
	slog.Info("wrote openclaw config", "component", "openclaw", "path", configPath)

	// On a successful fetch, update config.LLMModel to the resolved upstream default_model and record the catalog version.
	if !runtimeManaged && !usedFallback {
		s.config.LLMModel = defaultModel.Key
		if modelsResp.Version > 0 {
			s.config.DefaultModelVersion = modelsResp.Version
		}
	}

	slog.Debug("restarting openclaw gateway", "component", "openclaw")
	if err := restartOpenclawGateway(); err != nil {
		return err
	}
	slog.Info("gateway restart completed", "component", "openclaw")
	return nil
}

// AddChannel adds a messaging channel to openclaw.json (multi-channel) and restarts the gateway.
func (s *OpenclawService) AddChannel(ctx context.Context, data domain.AddChannelRequest) error {
	channel := data.EffectiveChannel()

	s.primarySyncMu.Lock()
	defer s.primarySyncMu.Unlock()

	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	var configData map[string]interface{}
	if raw, err := os.ReadFile(configPath); err == nil {
		if err := json.Unmarshal(raw, &configData); err != nil {
			return fmt.Errorf("parse openclaw config: %w", err)
		}
	} else {
		return fmt.Errorf("read openclaw config: %w (device must be set up first)", err)
	}

	channelsMap := ensureMap(configData, "channels")
	pluginsMap := ensureMap(configData, "plugins")
	entriesMap := ensureMap(pluginsMap, "entries")

	switch channel {
	case domain.ChannelSlack:
		if err := ensureChannelPlugin(ctx, domain.ChannelSlack, slackPluginPackage); err != nil {
			return fmt.Errorf("ensure slack plugin: %w", err)
		}
		slackMap := ensureMap(channelsMap, domain.ChannelSlack)
		// HTTP mode: a public proxy forwards Slack events via MQTT, so the gateway needs the signing secret, not an appToken.
		applySlackChannelConfig(slackMap, slackChannelConfig{
			BotToken:      data.SlackBotToken,
			AppToken:      data.SlackAppToken,
			UserID:        data.SlackUserID,
			Mode:          data.SlackMode,
			SigningSecret: data.SlackSigningSecret,
			WebhookPath:   data.SlackWebhookPath,
			Runtime:       currentOpenclawRuntime(),
		})
		channelsMap[domain.ChannelSlack] = slackMap
		slackEntryMap := ensureMap(entriesMap, domain.ChannelSlack)
		slackEntryMap["enabled"] = true
	case domain.ChannelDiscord:
		if err := ensureChannelPlugin(ctx, domain.ChannelDiscord, discordPluginPackage); err != nil {
			return fmt.Errorf("ensure discord plugin: %w", err)
		}
		discordMap := ensureMap(channelsMap, domain.ChannelDiscord)
		applyDiscordChannelConfig(discordMap, data.DiscordBotToken, data.DiscordUserID, data.DiscordGuildID)
		channelsMap[domain.ChannelDiscord] = discordMap
		discordEntryMap := ensureMap(entriesMap, domain.ChannelDiscord)
		discordEntryMap["enabled"] = true
	case domain.ChannelWhatsapp:
		if err := runOpenclawCLI(ctx, "channels", "add", "--channel", domain.ChannelWhatsapp); err != nil {
			return fmt.Errorf("openclaw channels add whatsapp: %w", err)
		}
		raw, err := os.ReadFile(configPath)
		if err != nil {
			return fmt.Errorf("re-read openclaw config after channels add: %w", err)
		}
		if err := json.Unmarshal(raw, &configData); err != nil {
			return fmt.Errorf("re-parse openclaw config after channels add: %w", err)
		}
		channelsMap = ensureMap(configData, "channels")
		pluginsMap = ensureMap(configData, "plugins")
		entriesMap = ensureMap(pluginsMap, "entries")

		whatsappMap := ensureMap(channelsMap, domain.ChannelWhatsapp)
		applyWhatsappChannelConfig(whatsappMap, data.WhatsappUserID)
		channelsMap[domain.ChannelWhatsapp] = whatsappMap
		if err := ensureChannelPlugin(ctx, domain.ChannelWhatsapp, whatsappPluginPackage); err != nil {
			return err
		}
		whatsappEntryMap := ensureMap(entriesMap, domain.ChannelWhatsapp)
		whatsappEntryMap["enabled"] = true
	default:
		telegramMap := ensureMap(channelsMap, domain.ChannelTelegram)
		telegramMap["enabled"] = true
		telegramMap["botToken"] = data.TelegramBotToken
		if data.TelegramUserID != "" {
			telegramMap["dmPolicy"] = "allowlist"
			telegramMap["allowFrom"] = mergeStringList(telegramMap["allowFrom"], data.TelegramUserID)
		} else {
			telegramMap["dmPolicy"] = "open"
			telegramMap["allowFrom"] = mergeStringList(telegramMap["allowFrom"], "*")
		}
		channelsMap[domain.ChannelTelegram] = telegramMap
		telegramEntryMap := ensureMap(entriesMap, domain.ChannelTelegram)
		telegramEntryMap["enabled"] = true
	}
	configData["channels"] = channelsMap

	if toolsMap, ok := configData["tools"].(map[string]any); ok {
		if elevatedMap, ok := toolsMap["elevated"].(map[string]any); ok {
			elevatedAllowFrom := ensureMap(elevatedMap, "allowFrom")
			elevatedAllowFrom[channel] = []any{"*"}
			elevatedMap["allowFrom"] = elevatedAllowFrom
		}
	}

	written, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal openclaw config: %w", err)
	}
	existingPrimary := extractPrimaryModel(configData)
	if existingPrimary != "" {
		setOSWriteFlag(s.config.OpenclawConfigDir, existingPrimary)
	}
	if err := os.WriteFile(configPath, written, 0600); err != nil {
		return fmt.Errorf("write openclaw config: %w", err)
	}
	if err := chownRuntimeUserIfRoot(configPath, openclawRuntimeUser); err != nil {
		return fmt.Errorf("set openclaw config ownership: %w", err)
	}
	slog.Info("wrote openclaw config", "component", "openclaw", "path", configPath, "channel", channel)

	if err := restartOpenclawGateway(); err != nil {
		return err
	}
	slog.Info("gateway restarted", "component", "openclaw")
	return nil
}

// RefreshModelsConfig patches the models reasoning fields in openclaw.json based on current config and restarts the agent.
func (s *OpenclawService) RefreshModelsConfig() error {
	if s.config.LLMRuntimeManaged() {
		return nil
	}
	s.primarySyncMu.Lock()
	defer s.primarySyncMu.Unlock()
	if s.config.LLMRuntimeManaged() {
		return nil
	}
	if s.config.LLMMode() == "os" {
		if _, err := s.ensureProviderConfig(); err != nil {
			return err
		}
	}

	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	data, err := os.ReadFile(configPath)
	if err != nil {
		return fmt.Errorf("read openclaw config: %w", err)
	}
	var configData map[string]any
	if err := json.Unmarshal(data, &configData); err != nil {
		return fmt.Errorf("parse openclaw config: %w", err)
	}

	disableThinking := s.config.LLMThinkingDisabled()
	// Read LLMModel under config.mu so it cannot race with a concurrent WithLockSave call.
	currentModel := s.config.LLMModelKey()

	currentBaseURL := s.config.LLMBaseURL
	currentAPIKey := s.config.LLMAPIKey
	if modelsMap, ok := configData["models"].(map[string]any); ok {
		if providersMap, ok := modelsMap["providers"].(map[string]any); ok {
			if providerEntry, ok := providersMap[customProviderName].(map[string]any); ok {
				if currentBaseURL != "" {
					providerEntry["baseUrl"] = currentBaseURL
				}
				if currentAPIKey != "" {
					providerEntry["apiKey"] = currentAPIKey
				}
				if modelsList, ok := providerEntry["models"].([]any); ok {
					for _, entry := range modelsList {
						if m, ok := entry.(map[string]any); ok {
							if disableThinking {
								m["reasoning"] = false
							} else {
								m["reasoning"] = true
							}
						}
					}
				}
			}
		}
	}

	// Conditionally sync agents.defaults.model.primary.
	currentPrimary := extractPrimaryModel(configData)
	prov, _, _ := splitProviderModel(currentPrimary)
	var flagPrimary string // value written into the os-server-write flag
	if s.config.LLMMode() == "os" || currentPrimary == "" || prov == customProviderName {
		newPrimary := customProviderName + "/" + currentModel
		agents := ensureMap(configData, "agents")
		defaults := ensureMap(agents, "defaults")
		modelMap := ensureMap(defaults, "model")
		modelMap["primary"] = newPrimary
		defaults["model"] = modelMap
		agents["defaults"] = defaults
		configData["agents"] = agents
		flagPrimary = newPrimary
		slog.Info("refreshed models config in openclaw.json", "component", "openclaw", "disableThinking", disableThinking, "primary", newPrimary)
	} else {
		flagPrimary = currentPrimary
		slog.Warn("[refresh] non-autonomous provider active, skipping primary patch (state drift)",
			"current", currentPrimary, "os_model", s.config.LLMModel)
	}

	written, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal openclaw config: %w", err)
	}
	setOSWriteFlag(s.config.OpenclawConfigDir, flagPrimary)
	if err := os.WriteFile(configPath, written, 0600); err != nil {
		return fmt.Errorf("write openclaw config: %w", err)
	}
	slog.Debug("wrote openclaw.json after models config refresh", "component", "openclaw", "disableThinking", disableThinking)

	if err := restartOpenclawGateway(); err != nil {
		return err
	}
	slog.Info("restart completed after models config refresh", "component", "openclaw")
	return nil
}

// RestartAgent restarts the openclaw gateway only.
func (s *OpenclawService) RestartAgent() error {
	slog.Debug("restarting openclaw gateway", "component", "openclaw")
	if err := restartOpenclawGateway(); err != nil {
		return err
	}
	slog.Info("restart completed", "component", "openclaw")
	return nil
}

func findModelByLLMModel(models []domain.LLMModel, llmModel string) (domain.LLMModel, error) {
	for _, m := range models {
		if m.Key == llmModel || strings.TrimPrefix(m.Key, fmt.Sprintf("%s/", customProviderName)) == llmModel || m.Name == llmModel {
			return m, nil
		}
	}
	return domain.LLMModel{}, fmt.Errorf("no model matching llm_model %q in openclaw models list", llmModel)
}

func openclawModelToProviderEntry(m domain.LLMModel) map[string]interface{} {
	contextWindow := 200000
	if m.ContextWindow != nil {
		contextWindow = *m.ContextWindow
	}
	maxTokens := 8192
	if m.MaxTokens != nil {
		maxTokens = *m.MaxTokens
	}
	return map[string]interface{}{
		"id":        m.Key,
		"name":      m.Name,
		"reasoning": m.Reasoning,
		"input":     m.Input,
		"cost": map[string]interface{}{
			"input":      0,
			"output":     0,
			"cacheRead":  0,
			"cacheWrite": 0,
		},
		"contextWindow": contextWindow,
		"maxTokens":     maxTokens,
	}
}
