package migratepersona

import (
	"fmt"
	"log/slog"
	"os"
	"regexp"
	"sort"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/usercanon"
)

// userProfileBootstrapCap mirrors OpenClaw bootstrapMaxChars (12000); warn earlier at
// userProfileWarnChars.
const (
	userProfileBootstrapCap = 12000
	userProfileWarnChars    = 9000
)

// ReconcileAction is one change a reconcile pass made or would make.
type ReconcileAction struct {
	Path   string
	Kind   string // "name" | "users-block"
	Detail string
	Reason string
}

func (a ReconcileAction) String() string {
	return fmt.Sprintf("%s: retire %s (%s)", a.Path, a.Detail, a.Reason)
}

// usersBlockRe matches a `## Users` entry like `Users: **long (friend)**: ...` (prefix optional).
// The `(role)` is required: without it a field bullet like `**Notes:**` would be read as a person
// and deleted.
var usersBlockRe = regexp.MustCompile(`(?i)^(?:Users:\s*)?\*\*([^*(]+?)\s*\([^)]*\)\*\*`)

// ReconcileUserProfiles retires people with no face/voice enrollment from every runtime's USER.md.
// A name is stale only when usercanon.Resolve finds no enrollment dir (absence never counts);
// writes only on change (cached prompt). execute=false returns actions without writing.
func ReconcileUserProfiles(opts Options, execute bool) ([]ReconcileAction, error) {
	enrolled, err := enrolledLabels()
	if err != nil {
		// No enrollment store: cannot tell stale from absent, so do nothing.
		return nil, fmt.Errorf("read enrollment store: %w", err)
	}
	if len(enrolled) == 0 {
		// Nobody enrolled yet: retiring would wipe every profile on first boot.
		return nil, nil
	}

	var actions []ReconcileAction
	for _, path := range UserProfilePaths(opts) {
		acts, err := reconcileOneUserProfile(path, enrolled, execute)
		if err != nil {
			return actions, fmt.Errorf("reconcile %s: %w", path, err)
		}
		actions = append(actions, acts...)
	}
	return actions, nil
}

// UserProfilePaths returns every runtime's USER.md, deduped and sorted.
func UserProfilePaths(opts Options) []string {
	seen := map[string]bool{}
	var out []string
	for _, a := range adapters {
		p := a.userProfilePath(opts)
		if p == "" || seen[p] {
			continue
		}
		seen[p] = true
		out = append(out, p)
	}
	sort.Strings(out)
	return out
}

// enrolledLabels lists existing canonical user dirs, excluding "unknown" (not a person).
func enrolledLabels() (map[string]bool, error) {
	ents, err := os.ReadDir(usercanon.UsersDir)
	if err != nil {
		return nil, err
	}
	out := map[string]bool{}
	for _, e := range ents {
		if e.IsDir() && e.Name() != usercanon.DefaultUser {
			out[e.Name()] = true
		}
	}
	return out, nil
}

// isEnrolled reports whether a human-readable name still has an enrollment.
func isEnrolled(name string, enrolled map[string]bool) bool {
	return enrolled[usercanon.Resolve(name)]
}

func reconcileOneUserProfile(path string, enrolled map[string]bool, execute bool) ([]ReconcileAction, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	entries := parseEntriesText(string(raw))

	var actions []ReconcileAction
	out := make([]string, 0, len(entries))
	// Tracks emitted blank slots so retiring a value never leaves a second empty slot.
	blankSlot := map[string]bool{}
	for _, e := range entries {
		field := userFieldOf(e)
		if field != "" {
			mt := userFieldEntryRe.FindStringSubmatch(strings.TrimSpace(e))
			key := strings.ToLower(field)
			if !hasRealFieldValue(mt[2]) {
				if blankSlot[key] {
					continue
				}
				blankSlot[key] = true
				out = append(out, e)
				continue
			}
		}
		// A stale name is cleared to the blank slot (USER.md is a form); dropped if a blank slot exists.
		if strings.EqualFold(field, "Name") {
			mt := userFieldEntryRe.FindStringSubmatch(strings.TrimSpace(e))
			value := strings.TrimSpace(mt[2])
			if hasRealFieldValue(value) && !isEnrolled(value, enrolled) {
				actions = append(actions, ReconcileAction{
					Path: path, Kind: "name",
					Detail: fmt.Sprintf("**Name:** %q", value),
					Reason: fmt.Sprintf("no enrollment for %q", usercanon.Resolve(value)),
				})
				if !blankSlot["name"] {
					blankSlot["name"] = true
					out = append(out, "**Name:**")
				}
				continue
			}
		}
		if mt := usersBlockRe.FindStringSubmatch(strings.TrimSpace(e)); mt != nil {
			label := strings.TrimSpace(mt[1])
			if !isEnrolled(label, enrolled) {
				actions = append(actions, ReconcileAction{
					Path: path, Kind: "users-block",
					Detail: fmt.Sprintf("Users block %q", label),
					Reason: fmt.Sprintf("no enrollment for %q", usercanon.Resolve(label)),
				})
				continue
			}
		}
		out = append(out, e)
	}

	// OpenClaw truncates bootstrap files past bootstrapMaxChars from the tail, where `## Users` lives.
	if n := len(raw); n > userProfileWarnChars {
		slog.Warn("USER.md is approaching the bootstrap cap; `## Users` is at the end of the file and is truncated first",
			"component", "user-reconcile", "path", path, "chars", n,
			"warn_at", userProfileWarnChars, "cap", userProfileBootstrapCap)
	}

	if len(actions) == 0 || !execute {
		return actions, nil
	}
	// Back up before retiring: this pass can fire unattended on any boot.
	if err := backupFile(path); err != nil {
		return actions, fmt.Errorf("backup before retire: %w", err)
	}
	if err := writeEntriesAtomic(path, out, openclawFormat); err != nil {
		return actions, err
	}
	return actions, nil
}

// backupFile copies path aside as "<path>.bak-<unixnano>" before it is rewritten.
func backupFile(path string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return os.WriteFile(fmt.Sprintf("%s.bak-%d", path, time.Now().UnixNano()), data, 0o644)
}

// writeEntriesAtomic serialises entries and writes via writeFileAtomic (gateway may be reading).
func writeEntriesAtomic(path string, entries []string, format entryFormat) error {
	return writeFileAtomic(path, format.serialize(entries))
}
