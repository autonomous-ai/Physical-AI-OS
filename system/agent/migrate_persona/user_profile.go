package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// userProfileFieldNames are USER.md's singular fields (matched case-insensitively); a second
// copy is a contradiction, everything else entry-merges.
var userProfileFieldNames = []string{
	"Name",
	"What to call them",
	"Pronouns",
	"Timezone",
}

// userFieldEntryRe matches a "**Field:** value" bullet, including an empty value (unfilled slot).
var userFieldEntryRe = regexp.MustCompile(`^\*\*(.+?):\*\*\s*(.*)$`)

// hasRealFieldValue reports whether a field value is filled, not empty or a placeholder hint.
func hasRealFieldValue(v string) bool {
	v = strings.TrimSpace(v)
	return v != "" && !strings.HasPrefix(v, "_(") && !strings.HasPrefix(v, "*(")
}

func isUserProfileField(name string) bool {
	for _, f := range userProfileFieldNames {
		if strings.EqualFold(f, name) {
			return true
		}
	}
	return false
}

// userFieldOf returns the singular field name an entry carries, or "".
func userFieldOf(entry string) string {
	mt := userFieldEntryRe.FindStringSubmatch(strings.TrimSpace(entry))
	if mt == nil {
		return ""
	}
	name := strings.TrimSpace(mt[1])
	if !isUserProfileField(name) {
		return ""
	}
	return name
}

// partitionUserFields splits entries into filled singular-field values and the rest; field
// bullets are removed from the rest so entry-merge cannot duplicate them.
func partitionUserFields(entries []string) ([]IdentityField, []string) {
	byName := map[string]string{}
	var rest []string
	for _, e := range entries {
		field := userFieldOf(e)
		if field == "" {
			rest = append(rest, e)
			continue
		}
		mt := userFieldEntryRe.FindStringSubmatch(strings.TrimSpace(e))
		if v := strings.TrimSpace(mt[2]); hasRealFieldValue(v) {
			// Last filled value wins (the appended copy is newer).
			byName[strings.ToLower(field)] = v
		}
	}

	var fields []IdentityField
	for _, canonical := range userProfileFieldNames {
		if v, ok := byName[strings.ToLower(canonical)]; ok {
			fields = append(fields, IdentityField{name: canonical, value: v})
		}
	}
	return fields, rest
}

// mergeUserFields applies incoming field values over existing ones (incoming wins). An absent
// or unfilled incoming field never blanks a filled destination.
func mergeUserFields(existing, incoming []IdentityField) []IdentityField {
	value := map[string]string{}
	for _, f := range existing {
		value[strings.ToLower(f.name)] = f.value
	}
	for _, f := range incoming {
		value[strings.ToLower(f.name)] = f.value
	}
	var out []IdentityField
	for _, canonical := range userProfileFieldNames {
		if v, ok := value[strings.ToLower(canonical)]; ok {
			out = append(out, IdentityField{name: canonical, value: v})
		}
	}
	return out
}

// applyUserFields fills each field's existing bullet in place, drops later duplicates (the
// retirement), and appends fields with no slot; empty slots without a value stay as is.
func applyUserFields(entries []string, fields []IdentityField) []string {
	rendered := map[string]string{}
	for _, f := range fields {
		rendered[strings.ToLower(f.name)] = "**" + f.name + ":** " + f.value
	}

	placed := map[string]bool{}
	out := make([]string, 0, len(entries)+len(fields))
	for _, e := range entries {
		field := userFieldOf(e)
		if field == "" {
			out = append(out, e)
			continue
		}
		key := strings.ToLower(field)
		text, have := rendered[key]
		if !have {
			out = append(out, e)
			continue
		}
		if placed[key] {
			continue
		}
		out = append(out, text)
		placed[key] = true
	}

	for _, f := range fields {
		key := strings.ToLower(f.name)
		if !placed[key] {
			out = append(out, rendered[key])
		}
	}
	return out
}

// writeUserProfile is writeMemoryEntries for USER.md: singular fields fill in place, the rest
// entry-merges; the template is preserved and the char limit applies to the free-form rest only.
func (b *baseMigrator) writeUserProfile(kind string, incoming []string, destination string, limit int, dstFormat entryFormat) {
	existingRaw := parseEntries(destination)
	existingFields, _ := partitionUserFields(existingRaw)
	incomingFields, incomingRest := partitionUserFields(incoming)

	fields := mergeUserFields(existingFields, incomingFields)
	withFields := applyUserFields(existingRaw, fields)
	merged, stats, overflowed := mergeEntries(withFields, incomingRest, limit)

	// A changed field or dropped duplicate is a real edit even without new entries.
	structureChanged := !sameEntries(withFields, existingRaw)

	details := map[string]any{
		"existing_entries":   stats.existing,
		"added_entries":      stats.added,
		"duplicate_entries":  stats.duplicates,
		"overflowed_entries": stats.overflowed,
		"profile_fields":     len(fields),
		"char_limit":         limit,
	}

	if len(incoming) == 0 && !structureChanged {
		b.record(kind, "", destination, StatusSkipped, "no importable entries found", details)
		return
	}
	if !b.opts.Execute {
		b.record(kind, "", destination, StatusMigrated, "would merge user profile", details)
		return
	}
	if stats.added == 0 && len(overflowed) == 0 && !structureChanged {
		b.record(kind, "", destination, StatusSkipped, "no new entries to import", details)
		return
	}

	if bak, err := b.backup(destination); err != nil {
		b.record(kind, "", destination, StatusError, "backup failed: "+err.Error(), nil)
		return
	} else if bak != "" {
		details["backup"] = bak
	}
	if err := os.MkdirAll(filepath.Dir(destination), 0o755); err != nil {
		b.record(kind, "", destination, StatusError, "create dest dir: "+err.Error(), nil)
		return
	}
	if err := os.WriteFile(destination, []byte(dstFormat.serialize(merged)), 0o644); err != nil {
		b.record(kind, "", destination, StatusError, "write: "+err.Error(), nil)
		return
	}
	b.record(kind, "", destination, StatusMigrated, "", details)
}

func sameEntries(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
