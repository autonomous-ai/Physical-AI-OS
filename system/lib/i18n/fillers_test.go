package i18n

import (
	"reflect"
	"strings"
	"testing"
)

func TestFillerToolKeyNormalisesRuntimeNames(t *testing.T) {
	tests := map[string]string{
		"web_search":                     "web_search",  // OpenClaw / Codex
		"bash":                           "exec",        // OpenCode
		"shell":                          "exec",        // Codex
		"command_execution":              "exec",        // Codex item kind
		"file_changes":                   "apply_patch", // Codex item kind
		"mcp__filesystem__read":          "read",
		"mcp__browser__web_search":       "web_search",
		"mcp__runner__shell":             "exec",
		"memory_search":                  "memory_search",
		"terminal":                       "exec",
		"search_files":                   "search_files",
		"skill_view":                     "read",
		"read_file":                      "read",
		"write_file":                     "apply_patch",
		"web_extract":                    "web_fetch",
		"session_search":                 "memory_search",
		"spotify_search":                 "search_files",
		"memory":                         "memory_store",
		"delegate_task":                  "subagents",
		"text_to_speech":                 "audio_generate",
		"vision_analyze":                 "image",
		"xai_video_edit":                 "video_generate",
		"  SKILL-VIEW  ":                 "read",
		"mcp__hermes__session_search":    "memory_search",
		"mcp__custom__unrecognised":      "mcp__custom__unrecognised",
		"unrecognised_runtime_tool_name": "unrecognised_runtime_tool_name",
	}
	for raw, want := range tests {
		if got := FillerToolKey(raw); got != want {
			t.Errorf("FillerToolKey(%q) = %q, want %q", raw, got, want)
		}
	}
}

// Snapshot of Hermes built-in tool names, kept independent of the alias implementation.
func TestHermesDocumentedToolsHaveLocalisedFillers(t *testing.T) {
	tools := strings.Fields(`
		browser_back browser_click browser_console browser_get_images browser_navigate
		browser_press browser_scroll browser_snapshot browser_type browser_vision browser_cdp browser_dialog
		clarify execute_code cronjob delegate_task
		feishu_doc_read feishu_drive_add_comment feishu_drive_list_comments
		feishu_drive_list_comment_replies feishu_drive_reply_comment
		patch read_file search_files write_file
		ha_call_service ha_get_state ha_list_entities ha_list_services computer_use image_generate
		kanban_show kanban_list kanban_complete kanban_block kanban_request_review kanban_request_changes
		kanban_heartbeat kanban_comment kanban_create kanban_link kanban_unblock kanban_attach
		kanban_attach_url kanban_attachments project_create project_list project_switch
		memory session_search skill_manage skill_view skills_list terminal process
		read_terminal close_terminal open_preview close_preview read_preview drive_preview annotate_preview
		read_window_below focus_pane react_to_message tour tip todo vision_analyze video_analyze
		video_generate xai_video_edit xai_video_extend web_search web_extract x_search text_to_speech
		discord discord_admin spotify_playback spotify_devices spotify_queue spotify_search
		spotify_playlists spotify_albums spotify_library yb_query_group_info yb_query_group_members
		yb_send_dm yb_search_sticker yb_send_sticker
	`)
	for _, tool := range tools {
		t.Run(tool, func(t *testing.T) {
			for _, lang := range []string{LangEN, LangVI, LangZhCN, LangZhTW, LangJA} {
				pool := FillerForTool(lang, tool)
				if len(pool) == 0 {
					t.Errorf("%s: missing filler pool: %v", lang, pool)
				}
				for _, phrase := range pool {
					if strings.TrimSpace(phrase) == "" {
						t.Errorf("%s: empty filler phrase", lang)
					}
				}
				if got := FillerForTool(lang, "mcp__hermes__"+tool); !reflect.DeepEqual(got, pool) {
					t.Errorf("%s: MCP wrapper changed pool: %v != %v", lang, got, pool)
				}
			}
		})
	}
}

func TestFillerUnknownToolAndLanguageFallback(t *testing.T) {
	if got := FillerForTool(LangVI, "future_hermes_tool"); len(got) != 0 {
		t.Fatalf("unknown tool must leave continuation fallback to caller, got %v", got)
	}
	if got, want := FillerForTool("unknown", "terminal"), FillerForTool(LangEN, "exec"); !reflect.DeepEqual(got, want) {
		t.Fatalf("unknown language pool = %v, want English %v", got, want)
	}
}

func TestFillerRealtimeUsesDedicatedVietnamesePool(t *testing.T) {
	got := FillerRealtime(LangVI)
	want := map[string]bool{"Ừm...": true, "Hừm...": true}
	if len(got) != len(want) {
		t.Fatalf("FillerRealtime(%q) = %v, want %d phrases", LangVI, got, len(want))
	}
	for _, phrase := range got {
		if !want[phrase] {
			t.Errorf("unexpected realtime filler %q", phrase)
		}
	}
}

func TestFillerContinuationUsesNaturalVietnameseThoughtSounds(t *testing.T) {
	got := FillerContinuation(LangVI)
	want := map[string]bool{"Ừm...": true}

	if len(got) != len(want) {
		t.Fatalf("FillerContinuation(%q) = %v, want %d phrases", LangVI, got, len(want))
	}
	for _, phrase := range got {
		if !want[phrase] {
			t.Errorf("unexpected continuation filler %q", phrase)
		}
	}
}

// Range demo pools must be non-empty; an empty pool plays silently without error.
func TestDemoPoolsExistInEveryLanguage(t *testing.T) {
	for _, lang := range []string{LangEN, LangVI, LangZhCN, LangZhTW, LangJA} {
		for _, pool := range []string{
			"demo_intro", "demo_left", "demo_right", "demo_centre",
			"demo_head", "demo_up", "demo_down", "demo_neck", "demo_lean", "demo_done",
		} {
			if got := FillerForTool(lang, pool); len(got) == 0 {
				t.Errorf("pool %q is empty for lang %q", pool, lang)
			}
		}
	}
}

// A key missing from a known language falls back to English; the key is injected since none is naturally missing.
func TestAMissingKeyFallsBackToEnglishRatherThanSilence(t *testing.T) {
	const injected = "zz_test_only_in_english"
	toolFillers[LangEN][injected] = []string{"English only"}
	t.Cleanup(func() { delete(toolFillers[LangEN], injected) })

	for _, lang := range []string{LangVI, LangZhCN, LangZhTW, LangJA} {
		got := FillerForTool(lang, injected)
		if len(got) == 0 {
			t.Errorf("%s: a known language with a missing key went silent instead of falling back", lang)
			continue
		}
		if got[0] != "English only" {
			t.Errorf("%s: fell back to %q, want the English pool", lang, got[0])
		}
	}
}

// Every pool must exist in every language so the English fallback never hides a gap.
func TestEveryPoolIsTranslatedInEveryLanguage(t *testing.T) {
	for _, lang := range []string{LangEN, LangVI, LangZhCN, LangZhTW, LangJA} {
		for _, k := range AllPoolKeys() {
			if len(toolFillers[lang][k]) == 0 {
				t.Errorf("pool %q has no %s translation — it will speak English there", k, lang)
			}
		}
	}
}

// The fallback must not turn "no such pool" into a pool.
func TestAnUnknownPoolStillResolvesToNothing(t *testing.T) {
	if got := FillerForTool(LangEN, "no_such_pool_anywhere"); len(got) != 0 {
		t.Errorf("unknown pool resolved to %v, want nothing", got)
	}
	if got := FillerForTool(LangVI, ""); got != nil {
		t.Errorf("empty tool name resolved to %v, want nil", got)
	}
}

func TestAllPoolKeysCoversEveryPool(t *testing.T) {
	seen := map[string]bool{}
	for _, k := range AllPoolKeys() {
		seen[k] = true
	}
	for _, want := range []string{
		"look_searching", "look_still_searching", "look_found", "look_lost",
		"look_capturing", "demo_intro", "demo_left", "demo_done", "web_search",
	} {
		if !seen[want] {
			t.Errorf("AllPoolKeys() is missing %q — its phrases would never be prewarmed", want)
		}
	}
}

// Every pool key must be its own normalised form, or FillerForTool can never reach it.
func TestEveryPoolKeyIsItsOwnNormalisedForm(t *testing.T) {
	for _, k := range AllPoolKeys() {
		if got := FillerToolKey(k); got != k {
			t.Errorf("pool key %q normalises to %q — it can never be looked up", k, got)
		}
	}
}
