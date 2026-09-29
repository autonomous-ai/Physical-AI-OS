package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
)

// codexAdapter reads/writes the Codex workspace (presync seeds it as a copy of OpenClaw's).
type codexAdapter struct{}

func (codexAdapter) runtime() Runtime { return RuntimeCodex }

func (codexAdapter) read(opts Options) (*PersonaBundle, error) {
	ws := opts.CodexWorkspace
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

func (codexAdapter) write(m *baseMigrator, b *PersonaBundle, opts Options) error {
	ws := opts.CodexWorkspace

	m.writePersona("soul", rebrandToCodex(stripIdentityCard(b.Soul)), filepath.Join(ws, "SOUL.md"))

	m.writeIdentityFields("identity", b.Identity, filepath.Join(ws, "IDENTITY.md"), rebrandToCodex)

	// Daily entries fold into MEMORY.md; date-stamped daily files cannot be rebuilt from entries.
	mem := append(append([]string{}, b.Memory...), b.Daily...)
	m.writeMemoryEntries("memory", rebrandEntries(mem, rebrandToCodex),
		filepath.Join(ws, "MEMORY.md"), opts.MemoryCharLimit, openclawFormat)

	if len(b.Knowledge) > 0 {
		m.writeMemoryEntries("knowledge", rebrandEntries(b.Knowledge, rebrandToCodex),
			filepath.Join(ws, "KNOWLEDGE.md"), opts.MemoryCharLimit, openclawFormat)
	}

	m.writeUserProfile("user-profile", rebrandEntries(b.User, rebrandToCodex),
		filepath.Join(ws, "USER.md"), opts.UserCharLimit, openclawFormat)
	return nil
}

// reCodex matches Codex, for rebranding a persona arriving from codex.
var reCodex = regexp.MustCompile(`(?i)\bCodex\b`)

// rebrandToCodex case-preservingly rebrands other runtimes' names onto Codex.
func rebrandToCodex(text string) string {
	repl := casePreserving("Codex")
	text = reOpenClaw.ReplaceAllStringFunc(text, repl)
	text = reHermes.ReplaceAllStringFunc(text, repl)
	text = rePicoClaw.ReplaceAllStringFunc(text, repl)
	text = reClawdBot.ReplaceAllStringFunc(text, repl)
	text = reMoltBot.ReplaceAllStringFunc(text, repl)
	return text
}

// personaPaths implements runtimeAdapter (OpenClaw layout).
func (codexAdapter) personaPaths(opts Options) []string {
	return openclawLayoutPersonaPaths(opts.CodexWorkspace)
}

// userProfilePath implements runtimeAdapter.
func (codexAdapter) userProfilePath(opts Options) string {
	if opts.CodexWorkspace == "" {
		return ""
	}
	return filepath.Join(opts.CodexWorkspace, "USER.md")
}

// memoryFilePath implements runtimeAdapter.
func (codexAdapter) memoryFilePath(opts Options) string {
	if opts.CodexWorkspace == "" {
		return ""
	}
	return filepath.Join(opts.CodexWorkspace, "MEMORY.md")
}

// workspaceRoot implements runtimeAdapter.
func (codexAdapter) workspaceRoot(opts Options) string { return opts.CodexWorkspace }
