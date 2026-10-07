package i18n

import (
	"sort"
	"strings"
)

// Dead-air fillers: short TTS cues spoken while the agent is busy, with per-tool overrides.

// Opening acknowledges receipt with one short sound, without implying a task.
var fillerOpening = map[string][]string{
	LangJA:   {"うん。"},
	LangEN:   {"Uhm."},
	LangVI:   {"Ừm."},
	LangZhCN: {"嗯。"},
	LangZhTW: {"嗯。"},
}

// fillerRealtime holds quiet thinking sounds, not acknowledgements or promises.
var fillerRealtime = map[string][]string{
	LangJA:   {"うーん…", "ええと…"},
	LangEN:   {"Hmm...", "Mm..."},
	LangVI:   {"Ừm...", "Hừm..."},
	LangZhCN: {"嗯...", "呃..."},
	LangZhTW: {"嗯...", "呃..."},
}

var fillerContinuation = map[string][]string{
	LangJA: {"うーん、見てみよう。", "ちょっと待ってね。", "試してみるね。", "確認してみるね。"},
	LangEN: {
		"Hmm, let's see.", "Yeah, one sec.", "Let me try.",
		"Hang on a bit.", "Alright, let's look.",
	},
	LangVI: {
		"Ừm, để coi.", "Ờ, chờ tí.", "Hừm, để thử xem.",
		"À, để mình ngó.", "Ừ, để xem nào.",
	},
	LangZhCN: {
		"嗯，看看。", "等一下。", "让我试试。", "我看看。",
	},
	LangZhTW: {
		"嗯，看看。", "等一下。", "讓我試試。", "我看看。",
	},
}

// toolFillers maps lang -> FillerToolKey -> override pool.
var toolFillers = map[string]map[string][]string{
	LangJA: {
		"search_files":         {"探してみるね。", "調べてみるね。"},
		"memory_store":         {"覚えておくね。", "メモしておくね。"},
		"audio_generate":       {"音声を準備するね。", "ちょっと待ってね。"},
		"look_searching":       {"探してるよ。", "どこにいるかな？"},
		"look_still_searching": {"まだ探してるよ。", "うーん…"},
		"look_found":           {"そこにいたね。", "見つけた。"},
		"look_lost":            {"見えなくなった。", "見失っちゃった。"},
		"look_capturing":       {"見てみるね。", "うーん…"},
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
		"web_search":           {"調べてみるね。", "うーん…"},
		"x_search":             {"Xで調べるね。", "確認するね。"},
		"web_fetch":            {"見てみるね。", "読んでみるね。"},
		"read":                 {"読んでみるね。", "見てみるね。"},
		"memory_search":        {"思い出してみるね。", "うーん…"},
		"memory_get":           {"思い出してみるね。", "うーん…"},
		"exec":                 {"試してみるね。", "ちょっと待ってね。"},
		"process":              {"進めてるよ。", "ちょっと待ってね。"},
		"image_generate":       {"描いてみるね。", "作ってるよ。"},
		"video_generate":       {"動画を作るね。", "作ってるよ。"},
		"music_generate":       {"曲を作るね。", "作ってるよ。"},
		"update_plan":          {"整理してみるね。", "考えてみるね。"},
		"session_status":       {"確認するね。", "見てみるね。"},
		"apply_patch":          {"直してみるね。", "試してみるね。"},
		"pdf":                  {"読んでみるね。", "見てみるね。"},
		"canvas":               {"描いてみるね。", "試してみるね。"},
		"nodes":                {"試してみるね。", "うーん…"},
		"subagents":            {"手伝ってもらうね。", "確認するね。"},
		"image":                {"見てみるね。", "確認するね。"},
	},
	LangEN: {
		"search_files":   {"Looking it up.", "Let me check."},
		"memory_store":   {"Making a note.", "One sec."},
		"audio_generate": {"Preparing the audio.", "One sec."},
		// Look-aim states (hal/drivers/tracking/aim.py).
		"look_searching": {"Looking...", "Where are you?"},
		// Said once, at the midpoint of a look-around sweep.
		"look_still_searching": {"Still looking...", "Hmm..."},
		"look_found":           {"There you are.", "Found you."},
		"look_lost":            {"Can't see you.", "Lost you."},
		"look_capturing":       {"Let's see.", "Hmm..."},
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
		"web_search":     {"Let's see.", "Hmm..."},
		"x_search":       {"Let's see.", "Checking."},
		"web_fetch":      {"Let's see.", "Reading."},
		"read":           {"Reading.", "Let's see."},
		"memory_search":  {"Let me think.", "Hmm..."},
		"memory_get":     {"Let me think.", "Hmm..."},
		"exec":           {"Trying it.", "One sec."},
		"process":        {"Trying it.", "One sec."},
		"image_generate": {"Let's try.", "Making it."},
		"video_generate": {"Let's try.", "Making it."},
		"music_generate": {"Let's try.", "Making it."},
		"update_plan":    {"Let's see.", "Hmm..."},
		"session_status": {"Let's see.", "Checking."},
		"apply_patch":    {"Fixing it.", "Let's try."},
		"pdf":            {"Reading.", "Let's see."},
		"canvas":         {"Let's try.", "Sketching."},
		"nodes":          {"Let's try.", "Hmm..."},
		"subagents":      {"Let's see.", "Hmm..."},
		"image":          {"Let's see.", "Looking."},
	},
	LangVI: {
		"search_files":         {"Tìm chút.", "Để mình tra."},
		"memory_store":         {"Ghi lại tí.", "Chờ chút."},
		"audio_generate":       {"Chuẩn bị tiếng nhé.", "Chờ chút."},
		"look_searching":       {"Tìm thử...", "Bạn đâu rồi?"},
		"look_found":           {"À, đây rồi.", "Thấy rồi."},
		"look_lost":            {"Không thấy rồi.", "Mất dấu rồi."},
		"look_still_searching": {"Vẫn tìm đây...", "Hừm..."},
		"look_capturing":       {"Để xem.", "Hừm..."},
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
		"web_search":           {"Để coi.", "Hừm..."},
		"x_search":             {"Coi thử.", "Để coi."},
		"web_fetch":            {"Xem thử.", "Đọc chút."},
		"read":                 {"Đọc chút.", "Xem thử."},
		"memory_search":        {"Để nhớ.", "Hừm..."},
		"memory_get":           {"Nhớ xem.", "Hừm..."},
		"exec":                 {"Để thử.", "Làm tí."},
		"process":              {"Làm tí.", "Để thử."},
		"image_generate":       {"Vẽ tí.", "Để thử."},
		"video_generate":       {"Dựng tí.", "Để thử."},
		"music_generate":       {"Soạn tí.", "Để thử."},
		"update_plan":          {"Sắp lại tí.", "Để coi."},
		"session_status":       {"Xem lại tí.", "Để coi."},
		"apply_patch":          {"Sửa tí.", "Để thử."},
		"pdf":                  {"Đọc chút.", "Xem thử."},
		"canvas":               {"Vẽ tí.", "Để thử."},
		"nodes":                {"Để thử.", "Hừm..."},
		"subagents":            {"Nhờ chút.", "Để coi."},
		"image":                {"Xem chút.", "Để coi."},
	},
	LangZhCN: {
		"look_searching":       {"在找...", "你在哪儿？"},
		"look_still_searching": {"还在找...", "嗯..."},
		"look_found":           {"你在这儿。", "找到了。"},
		"look_lost":            {"看不到你。", "跟丢了。"},
		"look_capturing":       {"我看看。", "嗯..."},
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
		"search_files":         {"找一下。", "查查看。"},
		"memory_store":         {"记一下。", "等一下。"},
		"audio_generate":       {"准备音频。", "等一下。"},
		"web_search":           {"我帮你找找", "查一下哦", "我去搜搜", "找一下啊"},
		"x_search":             {"去X看看", "瞅瞅X", "在X瞄一下"},
		"web_fetch":            {"我去看看", "翻开看看", "瞅一眼", "打开瞧瞧"},
		"read":                 {"我看一下", "翻翻看", "瞄一眼", "我读读"},
		"memory_search":        {"我想想", "回忆一下", "翻翻记忆"},
		"memory_get":           {"我想想", "让我回忆下"},
		"exec":                 {"我来弄", "马上做", "在做了", "正在弄"},
		"process":              {"我在弄", "后台跑着"},
		"image_generate":       {"我来画", "画一张哦", "做一张看看", "画着呢"},
		"video_generate":       {"我来弄", "在做呢"},
		"music_generate":       {"在写曲子", "我来作曲"},
		"update_plan":          {"我重新理理", "再想想", "换个思路"},
		"session_status":       {"我看看情况", "瞄一眼"},
		"apply_patch":          {"我来改", "调整一下"},
		"pdf":                  {"我读一下", "扫一遍"},
		"canvas":               {"在画", "随手画一下"},
		"nodes":                {"我来", "马上"},
		"subagents":            {"找帮手", "叫人来帮"},
		"image":                {"我看看", "瞄一眼"},
	},
	LangZhTW: {
		"look_searching":       {"在找...", "你在哪兒？"},
		"look_still_searching": {"還在找...", "嗯..."},
		"look_found":           {"你在這兒。", "找到了。"},
		"look_lost":            {"看不到你。", "跟丟了。"},
		"look_capturing":       {"我看看。", "嗯..."},
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
		"search_files":         {"找一下。", "查查看。"},
		"memory_store":         {"記一下。", "等一下。"},
		"audio_generate":       {"準備音訊。", "等一下。"},
		"web_search":           {"我幫你找找", "查一下喔", "我去搜搜", "找一下啊"},
		"x_search":             {"去X看看", "瞄一下X", "在X瞧瞧"},
		"web_fetch":            {"我去看看", "翻開看看", "瞄一眼", "打開瞧瞧"},
		"read":                 {"我看一下", "翻翻看", "瞄一眼", "我讀讀"},
		"memory_search":        {"我想想", "回憶一下", "翻翻記憶"},
		"memory_get":           {"我想想", "讓我回憶下"},
		"exec":                 {"我來弄", "馬上做", "在做了", "正在弄"},
		"process":              {"我在弄", "背景跑著"},
		"image_generate":       {"我來畫", "畫一張喔", "做一張看看", "畫著呢"},
		"video_generate":       {"我來弄", "在做呢"},
		"music_generate":       {"在寫曲子", "我來作曲"},
		"update_plan":          {"我重新理理", "再想想", "換個思路"},
		"session_status":       {"我看看情況", "瞄一眼"},
		"apply_patch":          {"我來改", "調整一下"},
		"pdf":                  {"我讀一下", "掃一遍"},
		"canvas":               {"在畫", "隨手畫一下"},
		"nodes":                {"我來", "馬上"},
		"subagents":            {"找幫手", "叫人來幫"},
		"image":                {"我看看", "瞄一眼"},
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
