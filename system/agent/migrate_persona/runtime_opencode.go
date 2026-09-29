package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
)

// opencodeAdapter reads/writes the OpenCode workspace (presync seeds it as a copy of OpenClaw's).
type opencodeAdapter struct{}

func (opencodeAdapter) runtime() Runtime { return RuntimeOpenCode }

func (opencodeAdapter) read(opts Options) (*PersonaBundle, error) {
	ws := opts.OpenCodeWorkspace
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

func (opencodeAdapter) write(m *baseMigrator, b *PersonaBundle, opts Options) error {
	ws := opts.OpenCodeWorkspace

	m.writePersona("soul", rebrandToOpenCode(stripIdentityCard(b.Soul)), filepath.Join(ws, "SOUL.md"))

	m.writeIdentityFields("identity", b.Identity, filepath.Join(ws, "IDENTITY.md"), rebrandToOpenCode)

	// Daily entries fold into MEMORY.md; date-stamped daily files cannot be rebuilt from entries.
	mem := append(append([]string{}, b.Memory...), b.Daily...)
	m.writeMemoryEntries("memory", rebrandEntries(mem, rebrandToOpenCode),
		filepath.Join(ws, "MEMORY.md"), opts.MemoryCharLimit, openclawFormat)

	if len(b.Knowledge) > 0 {
		m.writeMemoryEntries("knowledge", rebrandEntries(b.Knowledge, rebrandToOpenCode),
			filepath.Join(ws, "KNOWLEDGE.md"), opts.MemoryCharLimit, openclawFormat)
	}

	m.writeUserProfile("user-profile", rebrandEntries(b.User, rebrandToOpenCode),
		filepath.Join(ws, "USER.md"), opts.UserCharLimit, openclawFormat)
	return nil
}

// reOpenCode matches OpenCode variants; requires "Code" so it never collides with OpenClaw.
var reOpenCode = regexp.MustCompile(`(?i)\bOpen[\s-]?Code\b`)

// rebrandToOpenCode case-preservingly rebrands other runtimes' names onto OpenCode.
func rebrandToOpenCode(text string) string {
	repl := casePreserving("OpenCode")
	text = reOpenClaw.ReplaceAllStringFunc(text, repl)
	text = reHermes.ReplaceAllStringFunc(text, repl)
	text = rePicoClaw.ReplaceAllStringFunc(text, repl)
	text = reClawdBot.ReplaceAllStringFunc(text, repl)
	text = reMoltBot.ReplaceAllStringFunc(text, repl)
	return text
}

// personaPaths implements runtimeAdapter (OpenClaw layout).
func (opencodeAdapter) personaPaths(opts Options) []string {
	return openclawLayoutPersonaPaths(opts.OpenCodeWorkspace)
}

// userProfilePath implements runtimeAdapter.
func (opencodeAdapter) userProfilePath(opts Options) string {
	if opts.OpenCodeWorkspace == "" {
		return ""
	}
	return filepath.Join(opts.OpenCodeWorkspace, "USER.md")
}

// memoryFilePath implements runtimeAdapter.
func (opencodeAdapter) memoryFilePath(opts Options) string {
	if opts.OpenCodeWorkspace == "" {
		return ""
	}
	return filepath.Join(opts.OpenCodeWorkspace, "MEMORY.md")
}

// workspaceRoot implements runtimeAdapter.
func (opencodeAdapter) workspaceRoot(opts Options) string { return opts.OpenCodeWorkspace }
