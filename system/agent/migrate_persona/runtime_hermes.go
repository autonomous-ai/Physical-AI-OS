package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// hermesAdapter reads/writes the Hermes home: identity is an inlined card in SOUL.md, and
// Knowledge + Daily fold into MEMORY.md (no separate slots).
type hermesAdapter struct{}

func (hermesAdapter) runtime() Runtime { return RuntimeHermes }

func (hermesAdapter) read(opts Options) (*PersonaBundle, error) {
	root := opts.HermesRoot
	mem := filepath.Join(root, "memories")
	rawSoul, _ := os.ReadFile(filepath.Join(root, "SOUL.md"))
	soul := string(rawSoul)

	return &PersonaBundle{
		Soul:     stripIdentityCard(stripHermesOSBlock(soul)),
		Identity: identityCardFields(soul),
		Memory:   parseEntries(filepath.Join(mem, "MEMORY.md")),
		User:     parseEntries(filepath.Join(mem, "USER.md")),
	}, nil
}

// hermesOSBlockMarker delimits the Hermes-only OS skill-priority block in SOUL.md; it is not
// persona and destinations won't strip it. Literal copy: this package imports no runtime.
const hermesOSBlockMarker = "<!-- OS HERMES SKILL PRIORITY -->"

// stripHermesOSBlock removes that block, from its marker line to the next `---`.
func stripHermesOSBlock(soul string) string {
	if !strings.Contains(soul, hermesOSBlockMarker) {
		return soul
	}
	lines := strings.Split(soul, "\n")
	var cleaned []string
	skip := false
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		if trimmed == hermesOSBlockMarker {
			skip = true
			continue
		}
		if skip {
			if trimmed == "---" {
				skip = false
			}
			continue
		}
		cleaned = append(cleaned, line)
	}
	return strings.TrimRight(strings.Join(cleaned, "\n"), " \t\r\n") + "\n"
}

func (hermesAdapter) write(m *baseMigrator, b *PersonaBundle, opts Options) error {
	root := opts.HermesRoot
	mem := filepath.Join(root, "memories")
	soulDest := filepath.Join(root, "SOUL.md")

	m.writePersona("soul", rebrandToHermes(b.Soul), soulDest)

	m.inlineIdentityCard(soulDest, buildIdentityBlockFromFields(b.Identity))

	// Order Memory, Knowledge, Daily: earlier entries win dedup and the char budget.
	all := append(append(append([]string{}, b.Memory...), b.Knowledge...), b.Daily...)
	m.writeMemoryEntries("memory", rebrandEntries(all, rebrandToHermes),
		filepath.Join(mem, "MEMORY.md"), opts.MemoryCharLimit, hermesFormat)

	m.writeUserProfile("user-profile", rebrandEntries(b.User, rebrandToHermes),
		filepath.Join(mem, "USER.md"), opts.UserCharLimit, hermesFormat)
	return nil
}

// buildIdentityBlockFromFields renders fields as a rebranded identity card; "" when empty.
func buildIdentityBlockFromFields(fields []IdentityField) string {
	if len(fields) == 0 {
		return ""
	}
	lines := make([]string, len(fields))
	for i, f := range fields {
		lines[i] = "- **" + f.name + ":** " + f.value
	}
	body := rebrandToHermes(strings.Join(lines, "\n"))
	return "\n\n" + identityCardHeading + "\n\n" +
		"Your owner set this — it overrides any default name or vibe above.\n\n" +
		body + "\n"
}

// buildIdentityBlock renders an IDENTITY.md file as a Hermes identity card.
func buildIdentityBlock(identityPath string) string {
	return buildIdentityBlockFromFields(readIdentityFields(identityPath))
}

// Brand-name matchers shared by the rebrand functions.
var (
	reOpenClaw = regexp.MustCompile(`(?i)\bOpen[\s-]?Claw\b`)
	reClawdBot = regexp.MustCompile(`(?i)\bClawdBot\b`)
	reMoltBot  = regexp.MustCompile(`(?i)\bMoltBot\b`)
)

func rebrandToHermes(text string) string {
	repl := casePreserving("Hermes")
	text = reOpenClaw.ReplaceAllStringFunc(text, repl)
	text = reClawdBot.ReplaceAllStringFunc(text, repl)
	text = reMoltBot.ReplaceAllStringFunc(text, repl)
	text = rePicoClaw.ReplaceAllStringFunc(text, repl)
	text = reCodex.ReplaceAllStringFunc(text, repl)
	text = reClaudeCode.ReplaceAllStringFunc(text, repl)
	text = reOpenCode.ReplaceAllStringFunc(text, repl)
	return text
}

// personaPaths implements runtimeAdapter; the home dir itself (installation, logs) is not listed.
func (hermesAdapter) personaPaths(opts Options) []string {
	root := opts.HermesRoot
	if root == "" {
		return nil
	}
	return []string{
		filepath.Join(root, "SOUL.md"),
		filepath.Join(root, "memories"),
	}
}

// userProfilePath implements runtimeAdapter (memories/USER.md).
func (hermesAdapter) userProfilePath(opts Options) string {
	if opts.HermesRoot == "" {
		return ""
	}
	return filepath.Join(opts.HermesRoot, "memories", "USER.md")
}

// memoryFilePath implements runtimeAdapter (memories/MEMORY.md).
func (hermesAdapter) memoryFilePath(opts Options) string {
	if opts.HermesRoot == "" {
		return ""
	}
	return filepath.Join(opts.HermesRoot, "memories", "MEMORY.md")
}

// workspaceRoot implements runtimeAdapter.
func (hermesAdapter) workspaceRoot(opts Options) string { return opts.HermesRoot }
