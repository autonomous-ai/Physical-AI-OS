// Package migratepersona migrates persona + long-term memory (SOUL, IDENTITY, MEMORY, USER,
// KNOWLEDGE/daily) between agent runtimes on a switch, via a runtime-neutral PersonaBundle.
package migratepersona

import (
	"fmt"
	"path/filepath"

	"go.autonomous.ai/os/system/lib/syspath"
)

// Runtime identifies an agent backend with an on-device persona layout; only runtimes in
// adapters migrate.
type Runtime string

const (
	RuntimeOpenclaw   Runtime = "openclaw"
	RuntimeHermes     Runtime = "hermes"
	RuntimePicoclaw   Runtime = "picoclaw"
	RuntimeCodex      Runtime = "codex"
	RuntimeClaudeCode Runtime = "claudecode"
	RuntimeOpenCode   Runtime = "opencode"
)

// runtimeAdapter is the read/write surface every migratable runtime implements.
// personaPaths is part of the interface so a new runtime cannot escape factory-reset wipes.
type runtimeAdapter interface {
	runtime() Runtime
	read(opts Options) (*PersonaBundle, error)
	write(b *baseMigrator, bundle *PersonaBundle, opts Options) error
	personaPaths(opts Options) []string
	userProfilePath(opts Options) string
	// memoryFilePath is the MEMORY.md this runtime loads; swept by the memory guard.
	memoryFilePath(opts Options) string
	// workspaceRoot is the runtime workspace whose realtime/ subdir holds HAL memory.
	workspaceRoot(opts Options) string
}

// adapters is the runtime registry; a new runtime adds its runtime_<name>.go adapter here.
var adapters = map[Runtime]runtimeAdapter{
	RuntimeOpenclaw:   openclawAdapter{},
	RuntimeHermes:     hermesAdapter{},
	RuntimePicoclaw:   picoclawAdapter{},
	RuntimeCodex:      codexAdapter{},
	RuntimeClaudeCode: claudecodeAdapter{},
	RuntimeOpenCode:   opencodeAdapter{},
}

// CanMigrate reports whether r has a registered adapter.
func CanMigrate(r Runtime) bool {
	_, ok := adapters[r]
	return ok
}

// Direction names a migration as "<from>_to_<to>"; new runtimes use RunMigration instead.
type Direction string

const (
	OpenclawToHermes Direction = "openclaw_to_hermes"
	HermesToOpenclaw Direction = "hermes_to_openclaw"
)

// Per-item outcome statuses.
const (
	StatusMigrated = "migrated"
	StatusSkipped  = "skipped"
	StatusConflict = "conflict"
	StatusError    = "error"
)

// Default char limits for memory files.
const (
	DefaultMemoryCharLimit = 2200
	DefaultUserCharLimit   = 1375
)

// Options controls a migration run; start from DefaultOptions.
type Options struct {
	// OpenclawWorkspace holds SOUL/IDENTITY/MEMORY/USER/KNOWLEDGE.md and memory/.
	OpenclawWorkspace string
	// HermesRoot holds SOUL.md; MEMORY.md and USER.md live under memories/.
	HermesRoot string
	// PicoclawWorkspace matches OpenClaw except MEMORY.md lives under memory/.
	PicoclawWorkspace string
	// CodexWorkspace, ClaudecodeWorkspace and OpenCodeWorkspace match the OpenClaw layout.
	CodexWorkspace      string
	ClaudecodeWorkspace string
	OpenCodeWorkspace   string

	// Execute writes changes; false is a dry-run.
	Execute bool
	// Overwrite replaces an existing destination SOUL.md; memory files always merge.
	Overwrite bool
	// IncludeDailyMemory folds OpenClaw daily memory/*.md into the migrated memory.
	IncludeDailyMemory bool

	MemoryCharLimit int
	UserCharLimit   int
}

func DefaultOptions(openclawConfigDir, hermesRoot string) Options {
	if openclawConfigDir == "" {
		openclawConfigDir = "/root/.openclaw"
	}
	if hermesRoot == "" {
		hermesRoot = "/root/.hermes"
	}
	return Options{
		OpenclawWorkspace:   filepath.Join(openclawConfigDir, "workspace"),
		HermesRoot:          hermesRoot,
		PicoclawWorkspace:   "/root/.picoclaw/workspace",
		CodexWorkspace:      filepath.Join(syspath.CodexHome(), "workspace"),
		ClaudecodeWorkspace: "/root/.claudecode/workspace",
		OpenCodeWorkspace:   "/root/.opencode/workspace",
		IncludeDailyMemory:  true,
		MemoryCharLimit:     DefaultMemoryCharLimit,
		UserCharLimit:       DefaultUserCharLimit,
	}
}

// withDefaults fills any unset limits so a zero-value Options still works.
func (o Options) withDefaults() Options {
	if o.MemoryCharLimit <= 0 {
		o.MemoryCharLimit = DefaultMemoryCharLimit
	}
	if o.UserCharLimit <= 0 {
		o.UserCharLimit = DefaultUserCharLimit
	}
	return o
}

// ItemResult is the outcome of migrating one file (SOUL / IDENTITY / MEMORY / …).
type ItemResult struct {
	Kind        string         `json:"kind"`
	Source      string         `json:"source,omitempty"`
	Destination string         `json:"destination,omitempty"`
	Status      string         `json:"status"`
	Reason      string         `json:"reason,omitempty"`
	Details     map[string]any `json:"details,omitempty"`
}

// Report is the structured result of a migration run.
type Report struct {
	Direction string         `json:"direction"`
	Mode      string         `json:"mode"` // "execute" | "dry-run"
	Items     []ItemResult   `json:"items"`
	Summary   map[string]int `json:"summary"`
}

// RunMigration reads from's layout into a bundle and writes it into to's layout.
func RunMigration(from, to Runtime, opts Options) (*Report, error) {
	opts = opts.withDefaults()
	src, ok := adapters[from]
	if !ok {
		return nil, fmt.Errorf("migratepersona: no adapter for source runtime %q", from)
	}
	dst, ok := adapters[to]
	if !ok {
		return nil, fmt.Errorf("migratepersona: no adapter for destination runtime %q", to)
	}

	bundle, err := src.read(opts)
	if err != nil {
		return nil, fmt.Errorf("migratepersona: read %s: %w", from, err)
	}

	base := &baseMigrator{opts: opts}
	if err := dst.write(base, bundle, opts); err != nil {
		return nil, fmt.Errorf("migratepersona: write %s: %w", to, err)
	}
	return base.report(Direction(string(from) + "_to_" + string(to))), nil
}

// Run is the legacy Direction-keyed entry point; prefer RunMigration.
func Run(dir Direction, opts Options) (*Report, error) {
	switch dir {
	case OpenclawToHermes:
		return RunMigration(RuntimeOpenclaw, RuntimeHermes, opts)
	case HermesToOpenclaw:
		return RunMigration(RuntimeHermes, RuntimeOpenclaw, opts)
	default:
		return nil, fmt.Errorf("migratepersona: unknown direction %q", dir)
	}
}
