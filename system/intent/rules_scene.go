package intent

import (
	"fmt"
	"strings"

	"go.autonomous.ai/os/system/device"
)

// sceneOffWords mean "leave the scene" (English; input is already translated).
var sceneOffWords = []string{
	"turn off", "switch off", "disable", "deactivate", "exit", "stop",
	"cancel", "quit", "kill", "out of", "end ", " off",
}

var sceneNames = []string{"focus", "reading", "relax", "movie", "night", "energize"}

func hasSceneOffWord(t string) bool {
	for _, w := range sceneOffWords {
		if strings.Contains(t, w) {
			return true
		}
	}
	return false
}

func hasSceneName(t string) bool {
	for _, s := range sceneNames {
		if strings.Contains(t, s) {
			return true
		}
	}
	return false
}

// sceneOn matches scene activation only when no off-word is present.
func sceneOn(keywords ...string) func(string) bool {
	m := anyOf(keywords...)
	return func(t string) bool {
		return m(t) && !hasSceneOffWord(t)
	}
}

func sceneExec(scene, reply string) func(string) *Result {
	return func(string) *Result {
		body := fmt.Sprintf(`{"scene":"%s"}`, scene)
		executionFailed := post("/scene", body) != nil
		return &Result{ExecutionFailed: executionFailed, TTSText: reply, LEDChanged: true, Actions: []string{"POST /scene " + body}}
	}
}

var sceneRules = []rule{
	// Scene off must run before scene activation.
	{
		name:       "scene_off",
		capability: device.CapLight,
		match: func(t string) bool {
			return hasSceneOffWord(t) &&
				(strings.Contains(t, "mode") || strings.Contains(t, "scene") || hasSceneName(t))
		},
		exec: func(string) *Result {
			executionFailed := post("/scene/off", "") != nil
			return &Result{ExecutionFailed: executionFailed, TTSText: "Back to normal!", LEDOff: true, Actions: []string{"POST /scene/off"}}
		},
	},

	{
		name:       "scene_reading",
		capability: device.CapLight,
		match:      sceneOn("reading mode", "reading light"),
		exec:       sceneExec("reading", "Reading mode!"),
	},
	{
		name:       "scene_focus",
		capability: device.CapLight,
		match:      sceneOn("focus mode", "focus light"),
		exec:       sceneExec("focus", "Focus mode!"),
	},
	{
		name:       "scene_relax",
		capability: device.CapLight,
		match:      sceneOn("relax mode", "relax light"),
		exec:       sceneExec("relax", "Relax mode!"),
	},
	{
		name:       "scene_movie",
		capability: device.CapLight,
		match:      sceneOn("movie mode", "movie light"),
		exec:       sceneExec("movie", "Movie mode!"),
	},
	{
		name:       "scene_night",
		capability: device.CapLight,
		match:      sceneOn("goodnight", "good night", "night mode"),
		exec: func(string) *Result {
			executionFailed := post("/scene", `{"scene":"night"}`) != nil
			executionFailed = postEmotion(`{"emotion":"sleepy","intensity":0.4}`) != nil || executionFailed
			return &Result{ExecutionFailed: executionFailed, TTSText: "Goodnight!", LEDChanged: true, Actions: []string{`POST /scene {"scene":"night"}`, `POST /emotion {"emotion":"sleepy","intensity":0.4}`}}
		},
	},
	{
		name:       "scene_energize",
		capability: device.CapLight,
		match:      sceneOn("energize"),
		exec:       sceneExec("energize", "Energize mode!"),
	},
}
