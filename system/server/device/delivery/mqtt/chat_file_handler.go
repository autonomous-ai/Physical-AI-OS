package mqtthandler

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"

	"go.autonomous.ai/os/system/agentfile"
	"go.autonomous.ai/os/system/domain"
)

// chatFileMaxInlineBytes caps a file carried inline on fd_channel.
const chatFileMaxInlineBytes = 2 << 20

func (h *DeviceMQTTHandler) handleChatFileGet(env domain.MQTTDataCommand) error {
	var req domain.MQTTChatFileGetData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		return h.publishDataResult(env.Kind, "failure", "invalid data: "+err.Error(), nil)
	}
	if strings.TrimSpace(req.Path) == "" {
		return h.publishDataResult(env.Kind, "failure", "path is required", nil)
	}

	data, err := buildChatFile(req)
	if err != nil {
		slog.Info("chat.file.get refused", "component", "mqtt-chat",
			"path", req.Path, "reason", err)
		return h.publishDataResult(env.Kind, "failure", err.Error(), domain.MQTTChatFileData{
			RunID:     req.RunID,
			SessionID: req.SessionID,
			Path:      req.Path,
		})
	}

	slog.Info("chat.file.get served", "component", "mqtt-chat",
		"path", req.Path, "size", data.Size, "too_large", data.TooLarge)
	return h.publishDataResult(env.Kind, "success", "", data)
}

// buildChatFile validates the requested path (hostile client input, checked
// against the system/agentfile allow-list) and builds the reply payload.
func buildChatFile(req domain.MQTTChatFileGetData) (domain.MQTTChatFileData, error) {
	resolved, mime, err := agentfile.Resolve(req.Path, agentfile.Roots())
	if err != nil {
		// Deliberately vague: the refusal reason would reveal the filesystem.
		return domain.MQTTChatFileData{}, errors.New("file not available")
	}

	info, err := os.Stat(resolved)
	if err != nil {
		return domain.MQTTChatFileData{}, errors.New("file not available")
	}

	out := domain.MQTTChatFileData{
		RunID:     req.RunID,
		SessionID: req.SessionID,
		Name:      filepath.Base(resolved),
		Path:      req.Path,
		MIME:      mime,
		Size:      info.Size(),
	}

	if info.Size() > chatFileMaxInlineBytes {
		out.TooLarge = true
		return out, nil
	}

	body, err := os.ReadFile(resolved)
	if err != nil {
		return domain.MQTTChatFileData{}, errors.New("file not available")
	}
	out.Content = base64.StdEncoding.EncodeToString(body)
	return out, nil
}
