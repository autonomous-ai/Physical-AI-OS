//go:build ignore

// TEMPLATE — adding a runtime to the persona-migration hub.
//
// COPY this file to runtime_<name>.go, delete the `//go:build ignore` line above,
// then do the 5 wiring steps and fill in read()/write(). The whole point of the
// hub is that this ONE file makes your runtime migrate to/from EVERY existing
// runtime, both directions — no per-pair files, no Direction enum.
// See docs/agentic/adding-agent-runtime.md §4.
//
// ── Wiring checklist (all 5) ──────────────────────────────────────────────────
//  1. migrator.go: add `RuntimeExample Runtime = "example"`. The string MUST equal
//     the config.agent_runtime value the factory resolves (system/agent/factory.go).
//  2. migrator.go: add `RuntimeExample: exampleAdapter{}` to the `adapters` map.
//  3. migrator.go Options: add a root-path field for your layout (e.g. `ExampleRoot
//     string`) and set it in DefaultOptions.
//  4. This file: implement read() + write() below (rename exampleAdapter → your name).
//     personaPaths/userProfilePath/memoryFilePath/workspaceRoot are already filled
//     in against ExampleRoot — the whole adapter surface the interface requires;
//     just swap in your own root field/path names.
//  5. Done. RunMigration(any, RuntimeExample) and the reverse now work; the switch
//     reconciler (persona_migration.go) picks it up via CanMigrate automatically.
//
// ── The one rule that keeps round-trips correct ───────────────────────────────
// Whatever read() pulls OUT of a slot, write() must put BACK. Every field that you
// inline or move on the way in needs a matching restore on the way out, or a
// round-trip (openclaw → you → openclaw) silently drops it. Fields your runtime
// has NO slot for are folded by the OTHER side's write —
// leave them nil in read(); they survive as content, just flattened in structure.

package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
)

// exampleAdapter reads/writes the <Example> on-disk layout. Rename throughout.
type exampleAdapter struct{}

func (exampleAdapter) runtime() Runtime { return RuntimeExample }

// read maps your runtime's on-disk files INTO the canonical bundle. Use parseEntries
// for memory-style files (it round-trips markdown bullets and Hermes `§` blocks).
// Leave Knowledge/Daily nil unless your runtime keeps them as separate slots.
func (exampleAdapter) read(opts Options) (*PersonaBundle, error) {
	root := opts.ExampleRoot // ← the Options field you add in step 3
	rawSoul, _ := os.ReadFile(filepath.Join(root, "SOUL.md"))
	soul := string(rawSoul)

	// IDENTITY — pick the strategy that matches how your runtime stores the name:
	//
	//   (a) SEPARATE FILE (like OpenClaw's IDENTITY.md): keep the raw soul, and
	//       Identity: readIdentityFields(filepath.Join(root, "IDENTITY.md"))
	//
	//   (b) INLINED IN SOUL (like Hermes' "## Your identity card"): strip the card
	//       so Soul is the bare body, and pull the fields out of it:
	//       Soul: stripIdentityCard(soul), Identity: identityCardFields(soul)
	return &PersonaBundle{
		Soul:     soul,
		Identity: readIdentityFields(filepath.Join(root, "IDENTITY.md")), // strategy (a)
		Memory:   parseEntries(filepath.Join(root, "MEMORY.md")),
		User:     parseEntries(filepath.Join(root, "USER.md")),
		// Knowledge: parseEntries(filepath.Join(root, "KNOWLEDGE.md")), // only if you have the slot
		// Daily:     readYourDailyEntries(root),                        // only if you have the slot
	}, nil
}

// write restores the bundle INTO your layout. Helpers (in base.go) do the careful
// parts — backup, dry-run, char-limit merge, dedup, report records:
//
//	m.writePersona(kind, content, dest)                 whole-file SOUL (Overwrite honored)
//	m.writeMemoryEntries(kind, entries, dest, lim, fmt) entry-merge MEMORY/USER (fmt: openclawFormat | hermesFormat)
//	m.writeIdentityFields(kind, fields, dest, brand)    restore identity into a separate file
//	m.inlineIdentityCard(soulPath, block)               inline identity into SOUL (no separate slot)
//
// Always rebrand on the way out (rebrandEntries / your rebrand func) so a persona
// arriving from another runtime reads in your brand.
func (exampleAdapter) write(m *baseMigrator, b *PersonaBundle, opts Options) error {
	root := opts.ExampleRoot

	// SOUL. If identity lives in a separate file, strip any inlined card defensively:
	// rebrandToExample(stripIdentityCard(b.Soul)).
	m.writePersona("soul", rebrandToExample(b.Soul), filepath.Join(root, "SOUL.md"))

	// IDENTITY — choose ONE, matching read()'s strategy:
	//   (a) separate slot:
	m.writeIdentityFields("identity", b.Identity, filepath.Join(root, "IDENTITY.md"), rebrandToExample)
	//   (b) inline in soul:
	//   m.inlineIdentityCard(filepath.Join(root, "SOUL.md"), buildIdentityBlockFromFields(b.Identity))

	// MEMORY — fold in any slots YOUR runtime lacks. No Knowledge/Daily slot →
	// fold them here (append). Have the slots → write them to their own files and
	// DON'T fold (see runtime_openclaw.go).
	mem := append(append(append([]string{}, b.Memory...), b.Knowledge...), b.Daily...)
	m.writeMemoryEntries("memory", rebrandEntries(mem, rebrandToExample),
		filepath.Join(root, "MEMORY.md"), opts.MemoryCharLimit, openclawFormat)

	// USER.
	m.writeUserProfile("user-profile", rebrandEntries(b.User, rebrandToExample),
		filepath.Join(root, "USER.md"), opts.UserCharLimit, openclawFormat)
	return nil
}

// rebrandToExample case-preservingly rewrites the OTHER runtimes' brand names onto
// yours (so e.g. an OpenClaw soul reads naturally after switching). Mirror
// rebrandToHermes / rebrandToOpenclaw: one regex per foreign brand token.
var reOtherBrand = regexp.MustCompile(`(?i)\b(OpenClaw|Hermes)\b`)

func rebrandToExample(text string) string {
	return reOtherBrand.ReplaceAllStringFunc(text, casePreserving("Example"))
}

// personaPaths implements runtimeAdapter: every file/dir holding this runtime's
// persona + long-term memory. Factory reset wipes these across ALL runtimes, not
// just the active one — persona files are copies, so a stale profile left in an
// inactive tree migrates back on the next switch.
//
// List FILES plus the memory/ dir. NEVER the workspace or home root: those also
// hold skills/, configs/ and (for a home-rooted runtime) the installation
// itself. Returning nil compiles and silently leaks the profile through a reset.
func (exampleAdapter) personaPaths(opts Options) []string {
	root := opts.ExampleRoot
	if root == "" {
		return nil
	}
	return []string{
		filepath.Join(root, "SOUL.md"),
		filepath.Join(root, "IDENTITY.md"),
		filepath.Join(root, "MEMORY.md"),
		filepath.Join(root, "USER.md"),
		filepath.Join(root, "KNOWLEDGE.md"),
		filepath.Join(root, "memory"),
	}
}

// userProfilePath implements runtimeAdapter: where THIS runtime keeps USER.md.
// The scheduled reconcile uses it to retire a profile whose person no longer has
// a face/voice enrollment, so it must point at the real file (Hermes, for one,
// keeps it under memories/).
func (exampleAdapter) userProfilePath(opts Options) string {
	if opts.ExampleRoot == "" {
		return ""
	}
	return filepath.Join(opts.ExampleRoot, "USER.md")
}

// memoryFilePath implements runtimeAdapter: where THIS runtime keeps MEMORY.md.
// The memory guard sweeps it alongside USER.md, so it must point at the real
// file just like userProfilePath above.
func (exampleAdapter) memoryFilePath(opts Options) string {
	if opts.ExampleRoot == "" {
		return ""
	}
	return filepath.Join(opts.ExampleRoot, "MEMORY.md")
}

// workspaceRoot implements runtimeAdapter.
func (exampleAdapter) workspaceRoot(opts Options) string { return opts.ExampleRoot }
