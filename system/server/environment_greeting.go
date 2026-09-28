package server

import (
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/environment"
	"go.autonomous.ai/os/system/lib/i18n"
)

// sendWakeGreetingWithEnvironment uses only the worker's cache. Missing or
// warming hardware never delays the existing greeting or adds a HAL request.
func sendWakeGreetingWithEnvironment(prompt string, startup *environment.StartupCoordinator, allowed bool, send func(string) (string, error)) error {
	var snapshot *environment.Event
	if startup != nil && allowed {
		snapshot = startup.ReserveGreeting(time.Now())
	}
	if snapshot != nil {
		prompt += "\n[environment:initial] " + snapshot.Message() + "\n" + environmentGreetingGuidance()
	}
	slog.Info("startup environment context", "component", "environment", "attached", snapshot != nil)
	_, err := send(prompt)
	if startup != nil {
		startup.FinishGreeting(err == nil)
	}
	return err
}

func environmentGreetingGuidance() string {
	switch i18n.Lang() {
	case i18n.LangVI:
		return "Dữ liệu môi trường kèm theo đã đủ điều kiện sử dụng. Theo skill environment, có thể thêm tối đa một câu ngắn về 1–2 số đo hữu ích vào lời chào. Không suy ra xu hướng hay mức an toàn từ một mẫu, không đọc toàn bộ bảng và không gọi thêm công cụ để chờ sensor."
	case i18n.LangZhCN:
		return "附带的环境数据已通过就绪检查。按照 environment 技能，可在问候中增加一句简短的话，提及一两个有用读数。不要从单次读数推断趋势或安全性，不要逐项播报，也不要调用工具等待传感器。"
	case i18n.LangZhTW:
		return "附帶的環境資料已通過就緒檢查。按照 environment 技能，可在問候中增加一句簡短的話，提及一兩個有用讀數。不要從單次讀數推斷趨勢或安全性，不要逐項播報，也不要呼叫工具等待感測器。"
	default:
		return "The attached environment snapshot passed readiness checks. Follow the environment skill and optionally add at most one brief sentence with one or two useful readings to the greeting. Do not infer trends or safety from one snapshot, read every value aloud, or call tools to wait for sensors."
	}
}

// startupGreetingAllowed treats service startup as maintenance, not a wake request.
// Bodies without expression do not expose /emotion/status.
func startupGreetingAllowed(gatewayReady, hasExpression bool, getSleeping func() (bool, error)) bool {
	if !gatewayReady {
		return false
	}
	if !hasExpression {
		return true
	}
	sleeping, err := getSleeping()
	if err != nil {
		slog.Warn("startup sleep state unavailable", "component", "server", "error", err)
		return false
	}
	return !sleeping
}
