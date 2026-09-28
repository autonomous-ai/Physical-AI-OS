package mqtthandler

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/i18n"
)

// handleDeviceRename rewrites the agent name in workspace/IDENTITY.md.
func (h *DeviceMQTTHandler) handleDeviceRename(env domain.MQTTDataCommand) error {
	var req domain.MQTTDeviceRenameData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		slog.Error("device.rename: invalid payload", "component", "mqtt", "error", err)
		return h.publishDataResult(domain.KindDeviceRename, "failure", "invalid JSON payload", nil)
	}

	name := strings.TrimSpace(req.Name)
	if name == "" {
		slog.Warn("device.rename: missing name", "component", "mqtt")
		return h.publishDataResult(domain.KindDeviceRename, "failure", "name is required", nil)
	}

	slog.Info("device.rename: received", "component", "mqtt", "name", name)

	if err := h.agentGateway.UpdateIdentityName(name); err != nil {
		slog.Error("device.rename: UpdateIdentityName failed", "component", "mqtt", "error", err)
		return h.publishDataResult(domain.KindDeviceRename, "failure", err.Error(), nil)
	}

	// Tell the agent its new name asynchronously — failures don't fail the
	// ack.
	go func() {
		prompt := renameGreetingPrompt(name)
		if _, err := h.agentGateway.SendSystemChatMessage(prompt); err != nil {
			slog.Warn("device.rename: notify agent failed", "component", "mqtt", "error", err)
		}
	}()

	slog.Info("device.rename: applied", "component", "mqtt", "name", name)
	return h.publishDataResult(domain.KindDeviceRename, "success", "", map[string]interface{}{
		"name": name,
	})
}

// renameGreetingPrompt builds the [system] message that tells the agent it
// was just renamed.
func renameGreetingPrompt(name string) string {
	switch i18n.Lang() {
	case i18n.LangVI:
		return fmt.Sprintf("[system] Chủ nhân vừa đổi tên cho bạn thành \"%s\". Chào lại ngắn gọn, ấm áp.", name)
	case i18n.LangZhCN:
		return fmt.Sprintf("[system] 主人刚刚把你的名字改成了 \"%s\"，请简短温暖地回应。", name)
	case i18n.LangZhTW:
		return fmt.Sprintf("[system] 主人剛剛把你的名字改成了 \"%s\"，請簡短溫暖地回應。", name)
	}
	return fmt.Sprintf("[system] Your owner just renamed you to \"%s\". Acknowledge briefly and warmly.", name)
}
