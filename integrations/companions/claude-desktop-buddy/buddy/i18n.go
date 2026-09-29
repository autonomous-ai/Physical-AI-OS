package buddy

import "fmt"

// NarrationCategory is a narration throttle key and template id; each needs an entry per language in narrationStrings.
type NarrationCategory string

const (
	NarrateConnected     NarrationCategory = "connected"
	NarrateDisconnected  NarrationCategory = "disconnected"
	NarrateBusyStart     NarrationCategory = "busy_start"
	NarrateDone          NarrationCategory = "done"
	NarrateThinking      NarrationCategory = "thinking"
	NarrateToolWebSearch NarrationCategory = "tool_websearch"
	NarrateToolRead      NarrationCategory = "tool_read"
	NarrateToolWrite     NarrationCategory = "tool_write"
	NarrateToolEdit      NarrationCategory = "tool_edit"
	NarrateToolBash      NarrationCategory = "tool_bash"
	NarrateToolSearch    NarrationCategory = "tool_search" // Grep, Glob, ToolSearch
	NarrateToolTask      NarrationCategory = "tool_task"   // Task (delegate to subagent)
	NarrateToolTodo      NarrationCategory = "tool_todo"   // TodoWrite
	NarrateToolNotebook  NarrationCategory = "tool_notebook"
	NarrateToolMCP       NarrationCategory = "tool_mcp"     // mcp__* catch-all
	NarrateToolGeneric   NarrationCategory = "tool_generic" // unknown tool — no name spoken
)

// fallbackLang is used when the configured language lacks a template.
const fallbackLang = "en"

// narrationStrings holds all narration text per language; keep phrases short and prefixed with "Claude".
var narrationStrings = map[string]map[NarrationCategory]string{
	"vi": {
		NarrateConnected:     "Claude đã kết nối",
		NarrateDisconnected:  "Claude đã ngắt kết nối",
		NarrateBusyStart:     "Claude bắt đầu",
		NarrateDone:          "Claude xong rồi",
		NarrateThinking:      "Claude đang suy nghĩ",
		NarrateToolWebSearch: "Claude đang tìm web",
		NarrateToolRead:      "Claude đang đọc file",
		NarrateToolWrite:     "Claude đang viết file",
		NarrateToolEdit:      "Claude đang sửa file",
		NarrateToolBash:      "Claude đang chạy lệnh",
		NarrateToolSearch:    "Claude đang tìm trong mã",
		NarrateToolTask:      "Claude đang giao việc",
		NarrateToolTodo:      "Claude đang cập nhật danh sách",
		NarrateToolNotebook:  "Claude đang sửa notebook",
		NarrateToolMCP:       "Claude đang gọi MCP",
		NarrateToolGeneric:   "Claude đang dùng công cụ",
	},
	"en": {
		NarrateConnected:     "Claude connected",
		NarrateDisconnected:  "Claude disconnected",
		NarrateBusyStart:     "Claude is starting",
		NarrateDone:          "Claude is done",
		NarrateThinking:      "Claude is thinking",
		NarrateToolWebSearch: "Claude is searching the web",
		NarrateToolRead:      "Claude is reading a file",
		NarrateToolWrite:     "Claude is writing a file",
		NarrateToolEdit:      "Claude is editing a file",
		NarrateToolBash:      "Claude is running a shell command",
		NarrateToolSearch:    "Claude is searching the codebase",
		NarrateToolTask:      "Claude is delegating a task",
		NarrateToolTodo:      "Claude is updating the to-do list",
		NarrateToolNotebook:  "Claude is editing a notebook",
		NarrateToolMCP:       "Claude is calling an MCP tool",
		NarrateToolGeneric:   "Claude is running a tool",
	},
	"zh": {
		NarrateConnected:     "Claude 已连接",
		NarrateDisconnected:  "Claude 已断开",
		NarrateBusyStart:     "Claude 开始了",
		NarrateDone:          "Claude 完成了",
		NarrateThinking:      "Claude 正在思考",
		NarrateToolWebSearch: "Claude 正在搜索网页",
		NarrateToolRead:      "Claude 正在读文件",
		NarrateToolWrite:     "Claude 正在写文件",
		NarrateToolEdit:      "Claude 正在编辑文件",
		NarrateToolBash:      "Claude 正在执行命令",
		NarrateToolSearch:    "Claude 正在搜索代码",
		NarrateToolTask:      "Claude 正在分派任务",
		NarrateToolTodo:      "Claude 正在更新待办",
		NarrateToolNotebook:  "Claude 正在编辑笔记本",
		NarrateToolMCP:       "Claude 正在调用 MCP",
		NarrateToolGeneric:   "Claude 正在使用工具",
	},
}

// narrationText resolves category in lang, falling back to English, then the raw category id.
func narrationText(lang string, cat NarrationCategory, args ...any) string {
	if tpl, ok := lookupTemplate(lang, cat); ok {
		return applyArgs(tpl, args...)
	}
	if lang != fallbackLang {
		if tpl, ok := lookupTemplate(fallbackLang, cat); ok {
			return applyArgs(tpl, args...)
		}
	}
	return string(cat)
}

func lookupTemplate(lang string, cat NarrationCategory) (string, bool) {
	if m, ok := narrationStrings[lang]; ok {
		if s, ok := m[cat]; ok && s != "" {
			return s, true
		}
	}
	return "", false
}

func applyArgs(tpl string, args ...any) string {
	if len(args) == 0 {
		return tpl
	}
	return fmt.Sprintf(tpl, args...)
}

// toolToCategory maps a tool_use name to a narration category; unknown tools use NarrateToolGeneric since raw names read badly via TTS.
func toolToCategory(name string) NarrationCategory {
	// Route all mcp__<server>__<method> tools to one announcement.
	if len(name) >= 5 && name[:5] == "mcp__" {
		return NarrateToolMCP
	}
	switch name {
	case "WebSearch", "WebFetch":
		return NarrateToolWebSearch
	case "Read":
		return NarrateToolRead
	case "Write":
		return NarrateToolWrite
	case "Edit", "MultiEdit":
		return NarrateToolEdit
	case "Bash", "BashOutput", "KillShell":
		return NarrateToolBash
	case "Grep", "Glob", "ToolSearch":
		return NarrateToolSearch
	case "Task":
		return NarrateToolTask
	case "TodoWrite":
		return NarrateToolTodo
	case "NotebookEdit":
		return NarrateToolNotebook
	default:
		return NarrateToolGeneric
	}
}

// supportedLang returns lang if narration strings exist for it, otherwise fallbackLang.
func supportedLang(lang string) string {
	if _, ok := narrationStrings[lang]; ok {
		return lang
	}
	return fallbackLang
}
