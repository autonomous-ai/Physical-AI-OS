package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// picoclawAdapter reads/writes the PicoClaw workspace: OpenClaw layout except MEMORY.md lives
// at memory/MEMORY.md and must be excluded from the daily-memory glob.
type picoclawAdapter struct{}

func (picoclawAdapter) runtime() Runtime { return RuntimePicoclaw }

func (picoclawAdapter) read(opts Options) (*PersonaBundle, error) {
	ws := opts.PicoclawWorkspace
	soul, _ := os.ReadFile(filepath.Join(ws, "SOUL.md"))

	b := &PersonaBundle{
		Soul:      string(soul),
		Identity:  readIdentityFields(filepath.Join(ws, "IDENTITY.md")),
		Memory:    parseEntries(filepath.Join(ws, "memory", "MEMORY.md")),
		Knowledge: parseEntries(filepath.Join(ws, "KNOWLEDGE.md")),
		User:      parseEntries(filepath.Join(ws, "USER.md")),
	}
	if opts.IncludeDailyMemory {
		for _, f := range picoclawDailyMemoryFiles(filepath.Join(ws, "memory")) {
			b.Daily = append(b.Daily, parseEntries(f)...)
		}
	}
	return b, nil
}

func (picoclawAdapter) write(m *baseMigrator, b *PersonaBundle, opts Options) error {
	ws := opts.PicoclawWorkspace

	m.writePersona("soul", rebrandToPicoclaw(stripIdentityCard(b.Soul)), filepath.Join(ws, "SOUL.md"))

	m.writeIdentityFields("identity", b.Identity, filepath.Join(ws, "IDENTITY.md"), rebrandToPicoclaw)

	// Daily entries fold into MEMORY.md; date-stamped daily files cannot be rebuilt from entries.
	mem := append(append([]string{}, b.Memory...), b.Daily...)
	m.writeMemoryEntries("memory", rebrandEntries(mem, rebrandToPicoclaw),
		filepath.Join(ws, "memory", "MEMORY.md"), opts.MemoryCharLimit, openclawFormat)

	if len(b.Knowledge) > 0 {
		m.writeMemoryEntries("knowledge", rebrandEntries(b.Knowledge, rebrandToPicoclaw),
			filepath.Join(ws, "KNOWLEDGE.md"), opts.MemoryCharLimit, openclawFormat)
	}

	m.writeUserProfile("user-profile", rebrandEntries(b.User, rebrandToPicoclaw),
		filepath.Join(ws, "USER.md"), opts.UserCharLimit, openclawFormat)
	return nil
}

// picoclawDailyMemoryFiles lists memory/*.md sorted, excluding MEMORY.md (would double-count).
func picoclawDailyMemoryFiles(dir string) []string {
	ents, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var files []string
	for _, e := range ents {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".md") || e.Name() == "MEMORY.md" {
			continue
		}
		files = append(files, filepath.Join(dir, e.Name()))
	}
	sort.Strings(files)
	return files
}

// rePicoClaw matches PicoClaw spacing/casing variants.
var rePicoClaw = regexp.MustCompile(`(?i)\bPico[\s-]?Claw\b`)

// rebrandToPicoclaw case-preservingly rebrands other runtimes' names onto PicoClaw.
func rebrandToPicoclaw(text string) string {
	repl := casePreserving("PicoClaw")
	text = reOpenClaw.ReplaceAllStringFunc(text, repl)
	text = reHermes.ReplaceAllStringFunc(text, repl)
	text = reClawdBot.ReplaceAllStringFunc(text, repl)
	text = reMoltBot.ReplaceAllStringFunc(text, repl)
	text = reCodex.ReplaceAllStringFunc(text, repl)
	text = reClaudeCode.ReplaceAllStringFunc(text, repl)
	text = reOpenCode.ReplaceAllStringFunc(text, repl)
	return text
}

// personaPaths implements runtimeAdapter; memory/ covers MEMORY.md.
func (picoclawAdapter) personaPaths(opts Options) []string {
	ws := opts.PicoclawWorkspace
	if ws == "" {
		return nil
	}
	return []string{
		filepath.Join(ws, "SOUL.md"),
		filepath.Join(ws, "IDENTITY.md"),
		filepath.Join(ws, "USER.md"),
		filepath.Join(ws, "KNOWLEDGE.md"),
		filepath.Join(ws, "memory"),
	}
}

// userProfilePath implements runtimeAdapter.
func (picoclawAdapter) userProfilePath(opts Options) string {
	if opts.PicoclawWorkspace == "" {
		return ""
	}
	return filepath.Join(opts.PicoclawWorkspace, "USER.md")
}

// memoryFilePath implements runtimeAdapter (memory/MEMORY.md).
func (picoclawAdapter) memoryFilePath(opts Options) string {
	if opts.PicoclawWorkspace == "" {
		return ""
	}
	return filepath.Join(opts.PicoclawWorkspace, "memory", "MEMORY.md")
}

// workspaceRoot implements runtimeAdapter.
func (picoclawAdapter) workspaceRoot(opts Options) string { return opts.PicoclawWorkspace }
