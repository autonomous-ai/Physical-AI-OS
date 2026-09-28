package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
)

// claudecodeAdapter reads/writes the Claude Code workspace (same layout as OpenClaw).
// CLAUDE.md is the runtime's own loader file and is not carried.
type claudecodeAdapter struct{}

func (claudecodeAdapter) runtime() Runtime { return RuntimeClaudeCode }

func (claudecodeAdapter) read(opts Options) (*PersonaBundle, error) {
	ws := opts.ClaudecodeWorkspace
	soul, _ := os.ReadFile(filepath.Join(ws, "SOUL.md"))

	b := &PersonaBundle{
		Soul:      string(soul),
		Identity:  readIdentityFields(filepath.Join(ws, "IDENTITY.md")),
		Memory:    parseEntries(filepath.Join(ws, "MEMORY.md")),
		Knowledge: parseEntries(filepath.Join(ws, "KNOWLEDGE.md")),
		User:      parseEntries(filepath.Join(ws, "USER.md")),
	}
	if opts.IncludeDailyMemory {
		for _, f := range dailyMemoryFiles(filepath.Join(ws, "memory")) {
			b.Daily = append(b.Daily, parseEntries(f)...)
		}
	}
	return b, nil
}

func (claudecodeAdapter) write(m *baseMigrator, b *PersonaBundle, opts Options) error {
	ws := opts.ClaudecodeWorkspace

	m.writePersona("soul", rebrandToClaudeCode(stripIdentityCard(b.Soul)), filepath.Join(ws, "SOUL.md"))

	m.writeIdentityFields("identity", b.Identity, filepath.Join(ws, "IDENTITY.md"), rebrandToClaudeCode)

	// Daily entries fold into MEMORY.md; date-stamped daily files cannot be rebuilt from entries.
	mem := append(append([]string{}, b.Memory...), b.Daily...)
	m.writeMemoryEntries("memory", rebrandEntries(mem, rebrandToClaudeCode),
		filepath.Join(ws, "MEMORY.md"), opts.MemoryCharLimit, openclawFormat)

	if len(b.Knowledge) > 0 {
		m.writeMemoryEntries("knowledge", rebrandEntries(b.Knowledge, rebrandToClaudeCode),
			filepath.Join(ws, "KNOWLEDGE.md"), opts.MemoryCharLimit, openclawFormat)
	}

	m.writeUserProfile("user-profile", rebrandEntries(b.User, rebrandToClaudeCode),
		filepath.Join(ws, "USER.md"), opts.UserCharLimit, openclawFormat)
	return nil
}

// reClaudeCode matches "Claude Code" variants; requires "Code" so mentions of the Claude
// model are never rebranded.
var reClaudeCode = regexp.MustCompile(`(?i)\bClaude[\s-]?Code\b`)

// rebrandToClaudeCode case-preservingly rebrands other runtimes' names onto Claude Code.
func rebrandToClaudeCode(text string) string {
	repl := casePreserving("Claude Code")
	text = reOpenClaw.ReplaceAllStringFunc(text, repl)
	text = reHermes.ReplaceAllStringFunc(text, repl)
	text = reClawdBot.ReplaceAllStringFunc(text, repl)
	text = reMoltBot.ReplaceAllStringFunc(text, repl)
	text = rePicoClaw.ReplaceAllStringFunc(text, repl)
	return text
}

// personaPaths implements runtimeAdapter (OpenClaw layout).
func (claudecodeAdapter) personaPaths(opts Options) []string {
	return openclawLayoutPersonaPaths(opts.ClaudecodeWorkspace)
}

// userProfilePath implements runtimeAdapter.
func (claudecodeAdapter) userProfilePath(opts Options) string {
	if opts.ClaudecodeWorkspace == "" {
		return ""
	}
	return filepath.Join(opts.ClaudecodeWorkspace, "USER.md")
}

// memoryFilePath implements runtimeAdapter.
func (claudecodeAdapter) memoryFilePath(opts Options) string {
	if opts.ClaudecodeWorkspace == "" {
		return ""
	}
	return filepath.Join(opts.ClaudecodeWorkspace, "MEMORY.md")
}

// workspaceRoot implements runtimeAdapter.
func (claudecodeAdapter) workspaceRoot(opts Options) string { return opts.ClaudecodeWorkspace }
