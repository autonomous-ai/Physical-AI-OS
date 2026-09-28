package migratepersona

import (
	"os"
	"regexp"
	"strings"
)

// PersonaBundle is the runtime-neutral form of a device's persona + long-term memory: each
// runtime has one read and one write adapter; slots a runtime lacks are nil on read and
// folded by the writer.
type PersonaBundle struct {
	// Soul is the persona body without the inlined identity card; the writer rebrands it.
	Soul     string
	Identity []IdentityField
	// Memory / User are canonical entries; the writer rebrands and entry-merges them.
	Memory []string
	User   []string
	// Knowledge / Daily are set only by runtimes with separate slots (OpenClaw).
	Knowledge []string
	Daily     []string
}

// IdentityField is one "- **Name:** value" line of the owner's identity.
type IdentityField struct{ name, value string }

// identityCardHeading marks the identity block inlined into SOUL.md (Hermes); it is also the
// idempotency guard and strip boundary.
const identityCardHeading = "## Your identity card"

// identityFieldRe matches a filled identity line, e.g. "- **Name:** Ngân".
var identityFieldRe = regexp.MustCompile(`^- \*\*(.+?):\*\*\s*(\S.*)$`)

// readIdentityFields parses filled identity fields from IDENTITY.md; nil when absent.
func readIdentityFields(path string) []IdentityField {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	return parseIdentityLines(string(raw))
}

// identityCardFields extracts filled fields from SOUL.md's identity card only (heading to EOF).
func identityCardFields(soul string) []IdentityField {
	idx := strings.Index(soul, identityCardHeading)
	if idx < 0 {
		return nil
	}
	return parseIdentityLines(soul[idx:])
}

// parseIdentityLines pulls "- **Field:** value" lines (filled only) out of text.
func parseIdentityLines(text string) []IdentityField {
	var out []IdentityField
	for _, line := range strings.Split(text, "\n") {
		mt := identityFieldRe.FindStringSubmatch(strings.TrimSpace(line))
		if mt == nil {
			continue
		}
		val := strings.TrimSpace(mt[2])
		if val == "" || strings.HasPrefix(val, "_(") {
			continue
		}
		out = append(out, IdentityField{name: mt[1], value: val})
	}
	return out
}

// stripIdentityCard removes the trailing identity card block from a SOUL.md.
func stripIdentityCard(text string) string {
	idx := strings.Index(text, identityCardHeading)
	if idx < 0 {
		return text
	}
	return strings.TrimRight(text[:idx], " \t\r\n") + "\n"
}

// setIdentityField replaces the "**field:**" line value (dropping a stale placeholder hint
// below it) or appends "- **field:** value".
func setIdentityField(content, field, value string) string {
	lines := strings.Split(content, "\n")
	needle := "**" + strings.ToLower(field) + ":**"
	for i, line := range lines {
		idx := strings.Index(strings.ToLower(line), needle)
		if idx < 0 {
			continue
		}
		lines[i] = line[:idx] + "**" + field + ":** " + value
		if i+1 < len(lines) && isItalicPlaceholderLine(lines[i+1]) {
			lines = append(lines[:i+1], lines[i+2:]...)
		}
		return strings.Join(lines, "\n")
	}
	prefix := content
	if prefix != "" && !strings.HasSuffix(prefix, "\n") {
		prefix += "\n"
	}
	return prefix + "- **" + field + ":** " + value + "\n"
}

// isItalicPlaceholderLine reports whether line is a `_(...)_` or `*(...)*` template hint.
func isItalicPlaceholderLine(line string) bool {
	t := strings.TrimSpace(line)
	if len(t) < 4 {
		return false
	}
	return (strings.HasPrefix(t, "_(") && strings.HasSuffix(t, ")_")) ||
		(strings.HasPrefix(t, "*(") && strings.HasSuffix(t, ")*"))
}

// rebrandEntries applies brand to each entry.
func rebrandEntries(entries []string, brand func(string) string) []string {
	if len(entries) == 0 {
		return nil
	}
	out := make([]string, len(entries))
	for i, e := range entries {
		out[i] = brand(e)
	}
	return out
}
