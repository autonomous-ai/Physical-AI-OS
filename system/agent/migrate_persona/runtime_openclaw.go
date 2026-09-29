package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// openclawAdapter reads/writes the OpenClaw workspace, with separate IDENTITY.md, KNOWLEDGE.md
// and daily memory/*.md slots.
type openclawAdapter struct{}

func (openclawAdapter) runtime() Runtime { return RuntimeOpenclaw }

func (openclawAdapter) read(opts Options) (*PersonaBundle, error) {
	ws := opts.OpenclawWorkspace
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

func (openclawAdapter) write(m *baseMigrator, b *PersonaBundle, opts Options) error {
	ws := opts.OpenclawWorkspace

	m.writePersona("soul", rebrandToOpenclaw(stripIdentityCard(b.Soul)), filepath.Join(ws, "SOUL.md"))

	m.writeIdentityFields("identity", b.Identity, filepath.Join(ws, "IDENTITY.md"), rebrandToOpenclaw)

	// Daily entries fold into MEMORY.md; date-stamped daily files cannot be rebuilt from entries.
	mem := append(append([]string{}, b.Memory...), b.Daily...)
	m.writeMemoryEntries("memory", rebrandEntries(mem, rebrandToOpenclaw),
		filepath.Join(ws, "MEMORY.md"), opts.MemoryCharLimit, openclawFormat)

	if len(b.Knowledge) > 0 {
		m.writeMemoryEntries("knowledge", rebrandEntries(b.Knowledge, rebrandToOpenclaw),
			filepath.Join(ws, "KNOWLEDGE.md"), opts.MemoryCharLimit, openclawFormat)
	}

	m.writeUserProfile("user-profile", rebrandEntries(b.User, rebrandToOpenclaw),
		filepath.Join(ws, "USER.md"), opts.UserCharLimit, openclawFormat)
	return nil
}

// dailyMemoryFiles lists workspace/memory/*.md sorted, for a deterministic merge.
func dailyMemoryFiles(dir string) []string {
	ents, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var files []string
	for _, e := range ents {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".md") {
			continue
		}
		files = append(files, filepath.Join(dir, e.Name()))
	}
	sort.Strings(files)
	return files
}

// reHermes matches Hermes, for rebranding onto OpenClaw.
var reHermes = regexp.MustCompile(`(?i)\bHermes\b`)

func rebrandToOpenclaw(text string) string {
	repl := casePreserving("OpenClaw")
	text = reHermes.ReplaceAllStringFunc(text, repl)
	text = rePicoClaw.ReplaceAllStringFunc(text, repl)
	text = reCodex.ReplaceAllStringFunc(text, repl)
	text = reClaudeCode.ReplaceAllStringFunc(text, repl)
	text = reOpenCode.ReplaceAllStringFunc(text, repl)
	return text
}

// personaPaths implements runtimeAdapter.
func (openclawAdapter) personaPaths(opts Options) []string {
	return openclawLayoutPersonaPaths(opts.OpenclawWorkspace)
}

// userProfilePath implements runtimeAdapter.
func (openclawAdapter) userProfilePath(opts Options) string {
	if opts.OpenclawWorkspace == "" {
		return ""
	}
	return filepath.Join(opts.OpenclawWorkspace, "USER.md")
}

// memoryFilePath implements runtimeAdapter.
func (openclawAdapter) memoryFilePath(opts Options) string {
	if opts.OpenclawWorkspace == "" {
		return ""
	}
	return filepath.Join(opts.OpenclawWorkspace, "MEMORY.md")
}

// workspaceRoot implements runtimeAdapter.
func (openclawAdapter) workspaceRoot(opts Options) string { return opts.OpenclawWorkspace }
