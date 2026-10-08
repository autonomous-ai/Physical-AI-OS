package i18n

import (
	"sort"
	"strings"
)

// Dead-air fillers: short TTS cues spoken while the agent is busy, with per-tool overrides.

// Opening uses the same short thinking sounds as continuation.
var fillerOpening = map[string][]string{
	LangJA:   {"うーん…"},
	LangEN:   {"Hmm..."},
	LangVI:   {"Ừm..."},
	LangZhCN: {"嗯..."},
	LangZhTW: {"嗯..."},
}

// fillerRealtime holds the short bridge a person says when an answer is slow:
// real words, not thinking noises, and never an acknowledgement or a promise.
var fillerRealtime = map[string][]string{
	LangJA:   {"ちょっと待ってね。", "考え中。"},
	LangEN:   {"One sec.", "Still thinking."},
	LangVI:   {"Chờ mình chút.", "Mình đang nghĩ."},
	LangZhCN: {"稍等一下。", "我在想。"},
	LangZhTW: {"稍等一下。", "我在想。"},
}

// Continuation uses a short thinking sound without claiming a specific action.
var fillerContinuation = map[string][]string{
	LangJA:   {"うーん…"},
	LangEN:   {"Hmm..."},
	LangVI:   {"Ừm..."},
	LangZhCN: {"嗯..."},
	LangZhTW: {"嗯..."},
}

// toolFillers maps lang -> FillerToolKey -> override pool.
// Internal-tool cues describe only actions shared by all mapped aliases; broad
// or mixed-action tools use a neutral sound instead of guessing user intent.
var toolFillers = map[string]map[string][]string{
	LangJA: {
		"search_files":         {"探してるよ。"},
		"memory_store":         {"うーん…"},
		"audio_generate":       {"音声を作るね。"},
		"look_searching":       {"探してるよ。", "どこにいるかな？"},
		"look_still_searching": {"まだ探してるよ。", "うーん…"},
		"look_found":           {"そこにいたね。", "見つけた。"},
		"look_lost":            {"見えなくなった。", "見失っちゃった。"},
		"look_capturing":       {"見てみるね。", "うーん…"},
		"look_capturing_main":  {"見てみるね。", "ちょっと見るね。"},
		"look_analyzing":       {"撮れたよ、ちょっと待ってね。", "よし、考えるね。"},
		"demo_intro":           {"こんなことができるよ。", "見ててね。"},
		"demo_left":            {"左の端まで。", "左いっぱいに。"},
		"demo_right":           {"右の端まで。", "右いっぱいに。"},
		"demo_up":              {"上も向けるよ。", "こんなふうに上へ。"},
		"demo_down":            {"下にも。", "こんなふうに下へ。"},
		"demo_done":            {"動ける範囲はここまで。", "これで全部だよ。"},
		"demo_centre":          {"真ん中に戻るね。", "正面に戻るね。"},
		"demo_head":            {"頭だけでも回せるよ。", "今度は頭だけ。"},
		"demo_neck":            {"首も伸ばせるよ。", "首を上げて、下げる。"},
		"demo_lean":            {"体も傾けられるよ。", "ちょっと傾けるね。"},
		"web_search":           {"探してるよ。"},
		"x_search":             {"探してるよ。"},
		"web_fetch":            {"読んでるよ。"},
		"read":                 {"読んでるよ。"},
		"memory_search":        {"確認するね。"},
		"memory_get":           {"確認するね。"},
		"exec":                 {"うーん…"},
		"process":              {"うーん…"},
		"image_generate":       {"画像を作るね。"},
		"video_generate":       {"動画を処理するね。"},
		"music_generate":       {"曲を作るね。"},
		"update_plan":          {"うーん…"},
		"session_status":       {"うーん…"},
		"apply_patch":          {"うーん…"},
		"pdf":                  {"うーん…"},
		"canvas":               {"うーん…"},
		"nodes":                {"うーん…"},
		"subagents":            {"うーん…"},
		"image":                {"確認するね。"},
	},
	LangEN: {
		"search_files":   {"Searching."},
		"memory_store":   {"Hmm..."},
		"audio_generate": {"Preparing audio."},
		// Look-aim states (hal/drivers/tracking/aim.py).
		"look_searching": {"Looking...", "Where are you?"},
		// Said once, at the midpoint of a look-around sweep.
		"look_still_searching": {"Still looking...", "Hmm..."},
		"look_found":           {"There you are.", "Found you."},
		"look_lost":            {"Can't see you.", "Lost you."},
		"look_capturing":       {"Let's see.", "Hmm..."},
		// Main-agent /api/vision/look (system/server/vision.go): before the photo, then while it is described.
		"look_capturing_main": {"Taking a look.", "Let me have a look."},
		"look_analyzing":      {"Got it — give me a sec.", "Okay, thinking about it."},
		// Range-demo narration (hal/drivers/motors/range_demo.py); must fit inside one movement leg.
		"demo_intro":     {"Here's what I can do.", "Watch this."},
		"demo_left":      {"All the way left.", "Left, as far as I go."},
		"demo_right":     {"And all the way right.", "Right, to the end."},
		"demo_up":        {"I can look up.", "Up, like this."},
		"demo_down":      {"And down.", "Down, like this."},
		"demo_done":      {"That's everything.", "That's as far as I go."},
		"demo_centre":    {"And back to the middle.", "Back to centre."},
		"demo_head":      {"My head turns on its own too.", "Just the head this time."},
		"demo_neck":      {"I can stretch my neck.", "Neck up, and down."},
		"demo_lean":      {"And I can lean.", "A little lean."},
		"web_search":     {"Searching."},
		"x_search":       {"Searching."},
		"web_fetch":      {"Reading."},
		"read":           {"Reading."},
		"memory_search":  {"Checking."},
		"memory_get":     {"Checking."},
		"exec":           {"Hmm..."},
		"process":        {"Hmm..."},
		"image_generate": {"Creating an image."},
		"video_generate": {"Processing video."},
		"music_generate": {"Making music."},
		"update_plan":    {"Hmm..."},
		"session_status": {"Hmm..."},
		"apply_patch":    {"Hmm..."},
		"pdf":            {"Hmm..."},
		"canvas":         {"Hmm..."},
		"nodes":          {"Hmm..."},
		"subagents":      {"Hmm..."},
		"image":          {"Checking."},
	},
	LangVI: {
		"search_files":         {"Đang tìm."},
		"memory_store":         {"Ừm..."},
		"audio_generate":       {"Đang tạo âm thanh."},
		"look_searching":       {"Tìm thử...", "Bạn đâu rồi?"},
		"look_found":           {"À, đây rồi.", "Thấy rồi."},
		"look_lost":            {"Không thấy rồi.", "Mất dấu rồi."},
		"look_still_searching": {"Vẫn tìm đây...", "Hừm..."},
		"look_capturing":       {"Để xem.", "Hừm..."},
		"look_capturing_main":  {"Để mình nhìn thử.", "Mình xem nha."},
		"look_analyzing":       {"Chụp xong rồi, đợi mình chút nha.", "Xong rồi, để mình xem kỹ."},
		"demo_intro":           {"Xem nè.", "Để mình khoe chút."},
		"demo_left":            {"Hết cỡ bên trái.", "Sang trái hết mức."},
		"demo_right":           {"Và hết cỡ bên phải.", "Sang phải hết mức."},
		"demo_up":              {"Nhìn lên được nữa.", "Lên như vầy nè."},
		"demo_down":            {"Và xuống.", "Xuống như vầy nè."},
		"demo_done":            {"Đó là hết tầm của mình.", "Xa nhất là tới đó."},
		"demo_centre":          {"Rồi về giữa.", "Quay về giữa nè."},
		"demo_head":            {"Đầu mình cũng tự xoay được.", "Lần này chỉ xoay đầu thôi."},
		"demo_neck":            {"Mình vươn cổ được nữa.", "Cổ lên, rồi xuống."},
		"demo_lean":            {"Và mình nghiêng người được.", "Nghiêng một chút nè."},
		"web_search":           {"Đang tìm."},
		"x_search":             {"Đang tìm."},
		"web_fetch":            {"Đang đọc."},
		"read":                 {"Đang đọc."},
		"memory_search":        {"Đang tra lại."},
		"memory_get":           {"Đang tra lại."},
		"exec":                 {"Ừm..."},
		"process":              {"Ừm..."},
		"image_generate":       {"Đang tạo ảnh."},
		"video_generate":       {"Đang xử lý video."},
		"music_generate":       {"Đang tạo nhạc."},
		"update_plan":          {"Ừm..."},
		"session_status":       {"Ừm..."},
		"apply_patch":          {"Ừm..."},
		"pdf":                  {"Ừm..."},
		"canvas":               {"Ừm..."},
		"nodes":                {"Ừm..."},
		"subagents":            {"Ừm..."},
		"image":                {"Đang xem."},
	},
	LangZhCN: {
		"look_searching":       {"在找...", "你在哪儿？"},
		"look_still_searching": {"还在找...", "嗯..."},
		"look_found":           {"你在这儿。", "找到了。"},
		"look_lost":            {"看不到你。", "跟丢了。"},
		"look_capturing":       {"我看看。", "嗯..."},
		"look_capturing_main":  {"我来看看。", "让我看一下。"},
		"look_analyzing":       {"拍好了，稍等一下。", "好，我想想。"},
		"demo_intro":           {"看这个。", "我给你看看。"},
		"demo_left":            {"左边到底。", "最左边。"},
		"demo_right":           {"右边也到底。", "最右边。"},
		"demo_up":              {"还能往上看。", "像这样往上。"},
		"demo_down":            {"还有往下。", "像这样往下。"},
		"demo_done":            {"这就是我的全部范围。", "最远就到这儿。"},
		"demo_centre":          {"再回到中间。", "回正。"},
		"demo_head":            {"头也能自己转。", "这次只转头。"},
		"demo_neck":            {"我还能伸脖子。", "脖子抬起来，再低下去。"},
		"demo_lean":            {"还能前倾。", "稍微倾一下。"},
		"search_files":         {"查找中。"},
		"memory_store":         {"嗯..."},
		"audio_generate":       {"生成音频中。"},
		"web_search":           {"查找中。"},
		"x_search":             {"查找中。"},
		"web_fetch":            {"阅读中。"},
		"read":                 {"阅读中。"},
		"memory_search":        {"查阅中。"},
		"memory_get":           {"查阅中。"},
		"exec":                 {"嗯..."},
		"process":              {"嗯..."},
		"image_generate":       {"生成图片中。"},
		"video_generate":       {"处理视频中。"},
		"music_generate":       {"制作音乐中。"},
		"update_plan":          {"嗯..."},
		"session_status":       {"嗯..."},
		"apply_patch":          {"嗯..."},
		"pdf":                  {"嗯..."},
		"canvas":               {"嗯..."},
		"nodes":                {"嗯..."},
		"subagents":            {"嗯..."},
		"image":                {"查看中。"},
	},
	LangZhTW: {
		"look_searching":       {"在找...", "你在哪兒？"},
		"look_still_searching": {"還在找...", "嗯..."},
		"look_found":           {"你在這兒。", "找到了。"},
		"look_lost":            {"看不到你。", "跟丟了。"},
		"look_capturing":       {"我看看。", "嗯..."},
		"look_capturing_main":  {"我來看看。", "讓我看一下。"},
		"look_analyzing":       {"拍好了，稍等一下。", "好，我想想。"},
		"demo_intro":           {"看這個。", "我給你看看。"},
		"demo_left":            {"左邊到底。", "最左邊。"},
		"demo_right":           {"右邊也到底。", "最右邊。"},
		"demo_up":              {"還能往上看。", "像這樣往上。"},
		"demo_down":            {"還有往下。", "像這樣往下。"},
		"demo_done":            {"這就是我的全部範圍。", "最遠就到這兒。"},
		"demo_centre":          {"再回到中間。", "回正。"},
		"demo_head":            {"頭也能自己轉。", "這次只轉頭。"},
		"demo_neck":            {"我還能伸脖子。", "脖子抬起來，再低下去。"},
		"demo_lean":            {"還能前傾。", "稍微傾一下。"},
		"search_files":         {"查找中。"},
		"memory_store":         {"嗯..."},
		"audio_generate":       {"產生音訊中。"},
		"web_search":           {"查找中。"},
		"x_search":             {"查找中。"},
		"web_fetch":            {"閱讀中。"},
		"read":                 {"閱讀中。"},
		"memory_search":        {"查閱中。"},
		"memory_get":           {"查閱中。"},
		"exec":                 {"嗯..."},
		"process":              {"嗯..."},
		"image_generate":       {"產生圖片中。"},
		"video_generate":       {"處理影片中。"},
		"music_generate":       {"製作音樂中。"},
		"update_plan":          {"嗯..."},
		"session_status":       {"嗯..."},
		"apply_patch":          {"嗯..."},
		"pdf":                  {"嗯..."},
		"canvas":               {"嗯..."},
		"nodes":                {"嗯..."},
		"subagents":            {"嗯..."},
		"image":                {"查看中。"},
	},
}

// FillerOpening returns the first-of-turn filler pool for lang (English fallback).
func FillerOpening(lang string) []string {
	if p, ok := fillerOpening[NormalizeLang(lang)]; ok && len(p) > 0 {
		return applyNameAll(p)
	}
	return applyNameAll(fillerOpening[fallbackLang])
}

// FillerRealtime returns the realtime-wait filler pool for lang (English fallback).
func FillerRealtime(lang string) []string {
	if p, ok := fillerRealtime[NormalizeLang(lang)]; ok && len(p) > 0 {
		return applyNameAll(p)
	}
	return applyNameAll(fillerRealtime[fallbackLang])
}

// FillerContinuation returns the between-tools filler pool for lang (English fallback).
func FillerContinuation(lang string) []string {
	if p, ok := fillerContinuation[NormalizeLang(lang)]; ok && len(p) > 0 {
		return applyNameAll(p)
	}
	return applyNameAll(fillerContinuation[fallbackLang])
}

// FillerToolKey normalises runtime tool names to toolFillers keys; unknown names pass through.
func FillerToolKey(tool string) string {
	key := strings.ToLower(strings.TrimSpace(tool))
	key = strings.ReplaceAll(key, "-", "_")
	key = strings.ReplaceAll(key, ".", "_")

	// Match explicit Hermes names before suffix heuristics (session_search is not a web search).
	name := key
	if strings.HasPrefix(name, "mcp__") {
		if i := strings.LastIndex(name, "__"); i > len("mcp__") {
			name = name[i+2:]
		}
	}
	// Keep in sync with the Hermes tools reference, its coverage test and EN/VI docs.
	switch name {
	case "terminal", "execute_code":
		return "exec"
	case "process", "web_search", "x_search", "image_generate", "video_generate", "search_files":
		return name
	case "read_file", "skill_view", "skills_list", "read_terminal", "read_preview",
		"browser_console", "browser_snapshot", "browser_get_images", "feishu_doc_read",
		"feishu_drive_list_comments", "feishu_drive_list_comment_replies":
		return "read"
	case "write_file", "patch", "skill_manage":
		return "apply_patch"
	case "web_extract", "browser_navigate", "browser_back":
		return "web_fetch"
	case "browser_click", "browser_press", "browser_scroll", "browser_type",
		"browser_cdp", "browser_dialog", "computer_use", "drive_preview",
		"annotate_preview", "open_preview", "close_preview", "close_terminal", "focus_pane":
		return "nodes"
	case "browser_vision", "vision_analyze", "video_analyze":
		return "image"
	case "memory", "honcho_conclude":
		return "memory_store"
	case "session_search", "honcho_search":
		return "memory_search"
	case "honcho_profile", "honcho_context", "honcho_reasoning":
		return "memory_get"
	case "delegate_task":
		return "subagents"
	case "todo", "cronjob", "kanban_complete", "kanban_request_review",
		"kanban_request_changes", "kanban_comment", "kanban_create", "kanban_link",
		"kanban_unblock", "kanban_attach", "kanban_attach_url", "project_create", "project_switch":
		return "update_plan"
	case "xai_video_edit", "xai_video_extend":
		return "video_generate"
	case "text_to_speech":
		return "audio_generate"
	case "ha_call_service":
		return "nodes"
	case "spotify_search", "yb_search_sticker":
		return "search_files" // Neutral lookup phrases also fit a non-web catalog.
	case "clarify", "kanban_block", "kanban_show", "kanban_list", "kanban_heartbeat",
		"kanban_attachments", "project_list", "ha_get_state", "ha_list_entities", "ha_list_services",
		"read_window_below", "react_to_message", "tour", "tip", "discord", "discord_admin",
		"feishu_drive_add_comment", "feishu_drive_reply_comment", "spotify_playback",
		"spotify_devices", "spotify_queue", "spotify_playlists", "spotify_albums", "spotify_library",
		"yb_query_group_info", "yb_query_group_members", "yb_send_dm", "yb_send_sticker":
		// Mixed read/write tools use neutral phrases.
		return "session_status"
	}

	switch {
	case key == "x_search":
		return "x_search"
	case strings.Contains(key, "memory_search"):
		return "memory_search"
	case strings.Contains(key, "memory_get") || strings.Contains(key, "memory_read"):
		return "memory_get"
	case strings.Contains(key, "web_search") || key == "search" || strings.HasSuffix(key, "_search"):
		return "web_search"
	case strings.Contains(key, "web_fetch") || strings.Contains(key, "http_fetch") || strings.HasSuffix(key, "_fetch"):
		return "web_fetch"
	case key == "bash" || key == "shell" || key == "command_execution" || key == "command" || key == "run" ||
		strings.HasSuffix(key, "__exec") || strings.HasSuffix(key, "__shell"):
		return "exec"
	case key == "read" || strings.HasSuffix(key, "__read"):
		return "read"
	case key == "file_changes" || key == "file_change" || key == "edit" || key == "write" || key == "patch":
		return "apply_patch"
	case strings.Contains(key, "image_generate") || strings.Contains(key, "image_create"):
		return "image_generate"
	case strings.Contains(key, "video_generate") || strings.Contains(key, "video_create"):
		return "video_generate"
	case strings.Contains(key, "music_generate") || strings.Contains(key, "music_create"):
		return "music_generate"
	}
	return key
}

// FillerForTool returns the override pool for (lang, tool), falling back to English;
// nil when no language defines it.
func FillerForTool(lang, tool string) []string {
	if tool = FillerToolKey(tool); tool == "" {
		return nil
	}
	pools, ok := toolFillers[NormalizeLang(lang)]
	if !ok {
		pools = toolFillers[fallbackLang]
	}
	if p := pools[tool]; len(p) > 0 {
		return applyNameAll(p)
	}
	return applyNameAll(toolFillers[fallbackLang][tool])
}

// AllPoolKeys returns every pool key defined in any language, sorted.
func AllPoolKeys() []string {
	seen := make(map[string]struct{})
	keys := make([]string, 0, 32)
	for _, pools := range toolFillers {
		for k := range pools {
			if _, dup := seen[k]; dup {
				continue
			}
			seen[k] = struct{}{}
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	return keys
}
