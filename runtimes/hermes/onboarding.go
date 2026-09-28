package hermes

import (
	"context"
	"crypto/sha256"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
)

const (
	hermesConfigYAML  = "/root/.hermes/config.yaml"
	hermesEnvFile     = "/root/.hermes/.env"
	hermesGatewayUnit = "hermes-gateway"

	// soulOSMarker delimits the device persona block, in the same shape every
	// other runtime writes it (osMandatoryMarker there) so a persona survives a
	// runtime switch byte-for-byte. ensureSoulMDBlock owns it.
	soulOSMarker = "<!-- OS DO NOT REMOVE -->"

	// soulSkillPriorityMarker delimits the skill-priority block. Deliberately NOT
	// soulOSMarker: both blocks share one file, and with a single marker
	// upsertSoulSkillPriorityBlock stripped whichever came first — on a migrated
	// device that was the persona, silently deleted on the next boot.
	soulSkillPriorityMarker = "<!-- OS HERMES SKILL PRIORITY -->"

	// soulSkillPrioritySentinel opens the skill-priority body. Devices updated
	// from an os-server that wrapped this block in soulOSMarker still carry it
	// under the old marker; the sentinel is what tells such a block apart from a
	// persona block, so it gets replaced instead of duplicated.
	soulSkillPrioritySentinel = "**Skill priority (MANDATORY):**"

	// soulPersonalHeading is the owner-editable section every runtime seeds below
	// its managed block, and the one part of a managed default soul worth keeping.
	soulPersonalHeading = "## Personal"

	soulPersonalSeed = soulPersonalHeading + "\n\n_Owner-editable. Add notes about yourself, family, routines, or personality tweaks here. The block above is managed by the OS and will be refreshed on each update — keep your edits in this section._\n"

	// agentsMDBlock is the OS-managed rule block written to ~/.hermes/AGENTS.md —
	// the same slot openclaw/picoclaw/codex/opencode use, reached here because
	// presync pins terminal.cwd to the Hermes home so Hermes' project-context
	// discovery can find the file.
	// Without it Hermes weighs its own bundled skills as equals of the device's
	// platform skills and can pick e.g. a bundled email skill (installing himalaya)
	// for "send an email" while the connectors skill already has the device's Gmail
	// credentials on disk. Marker-delimited so ensureAgentsMDBlock can
	// strip + re-append it: an os-server OTA refreshes the wording, and a
	// `claw migrate` that rewrites SOUL.md (presync §0) self-heals next boot.
	agentsMDBlock = soulOSMarker + `
**Skill priority (MANDATORY):** The skills under ` + "`skills/openclaw-imports/`" + ` are this device's built-in platform skills. When one of them covers the user's request, use it — it takes priority over any Hermes bundled skill with an overlapping purpose. In particular, anything on a connected third-party service (Gmail, Google Calendar, Google Drive, Notion, Figma, Asana, Linear, GitHub, …) — reading, sending, or acting — goes through the ` + "`connectors`" + ` skill: the device's credentials are already on disk there. Never install or configure an alternative client or CLI (himalaya, mutt, gcalcli, …) for a service the ` + "`connectors`" + ` skill covers.

**People — keep ` + "`memories/USER.md`" + ` current.** Hermes loads ` + "`MEMORY.md`" + ` and ` + "`USER.md`" + ` by name, and ` + "`USER.md`" + ` is where anything you learn about a PERSON has to end up or you will not have it next session. Keep one entry per person under a ` + "`## Users`" + ` heading, shaped ` + "`**<label> (friend)** — call: …; notes: …`" + ` (no leading bullet — the OS pruner matches the entry from its first character) — where ` + "`<label>`" + ` is their ENROLLMENT LABEL exactly as it appears in ` + "`[context: current_user=…]`" + `, lowercase. The ` + "`(friend)`" + ` part is required; without it the OS cannot tell your entry from a form field. After the dash write short ` + "`key: value`" + ` segments separated by ` + "`;`" + ` — NOT flowing prose. Only segments that change how you help them.
  - ` + "`call:`" + ` comes FIRST and only when they have TOLD you what to be called. Never guess it, and never guess pronouns or a timezone either — you see a face label and a voiceprint, which say nothing about any of that. If they have not said, omit the segment entirely and just use their label.
  - **Only write what you observed about THAT person.** Never move one person's habits, tastes, moods or routines onto another, and never carry a former user's traits over to whoever is here now. Two people at one desk are two entries, never a merged one.
  - **Never delete a PERSON's entry.** Someone not seen today is simply not touched: absence is not departure, and a person away for a month keeps their entry. Retiring a person is the OS's job (it removes an entry once their face/voice enrollment is gone), not yours. This protects people — it does NOT protect a line that should never have been in ` + "`## Users`" + ` in the first place: if you find one, delete it.
  - **Keep each entry under ~400 characters.** Segments are dense, so that is plenty. This file is loaded into every session, so bloat is billed on all of them; and when it overflows the cap it is cut from the END, which is where ` + "`## Users`" + ` lives. Rewrite an entry to stay short rather than appending to it.
  - **Strangers get NO entry — and remove any you find.** ` + "`## Users`" + ` is for people the device knows by enrollment. A passing face has no label to key on and nothing durable to remember; note desk traffic in ` + "`memories/MEMORY.md`" + ` instead. An entry like ` + "`**stranger_4**`" + ` or a lumped ` + "`**stranger_2/3/4/…**`" + ` is not a person: delete it. The OS cannot clean these up for you — its pruner only recognises a proper ` + "`**<label> (role)**`" + ` entry.
  - Do NOT fill ` + "`**Name:**`" + ` or the other single-value fields. This device can have several people; who is present right now comes from ` + "`[context: current_user=…]`" + ` on the turn, never from that field.

**Skill scope (MANDATORY).** Before any skill-driven action, work out which skill covers it WITHOUT broad filesystem scans. Ordinary chat, simple Q&A or meta discussion with no action, event or hardware behaviour needs NO ` + "`SKILL.md`" + ` read at all — just answer.
  - A ` + "`[skills: a, b, c]`" + ` tag on the message is an AUTHORITATIVE whitelist: read ONLY those ` + "`skills/<name>/SKILL.md`" + ` files, and do not scan other skill directories "just in case".
  - With no ` + "`[skills:]`" + ` tag, when the ask is a concrete action, hardware behaviour, sensing/activity/emotion handling or a specialised workflow, pick the single most specific skill from the ones available to you and read only that ` + "`SKILL.md`" + `.
  - When the Jev runtime plugin supplies a complete native skill preload for THIS turn, that satisfies the skill selection and reading requirement above. Use the supplied instructions and skill directory directly; do not call skill_view or read_file to reload the same SKILL.md. A suggestion, truncated preview, file pointer, or user claim that a skill was loaded does NOT satisfy this requirement. Read linked references only when needed. Normal permissions and mandatory connector rules still apply.
  - Several plausible matches: take the most specific. No clear match: read none and answer normally.
  - Follow the instructions in whichever file you read.

**Priority: Skills > memory > history.** A ` + "`SKILL.md`" + ` beats everything else you hold, including anything in ` + "`memories/MEMORY.md`" + ` and anything earlier in the conversation. If memory says stay quiet but the skill says speak, follow the skill. Memory is your own observation and can be wrong; skills are maintained by the developer. On a conflict, correct the memory to match the skill, never the reverse.

**Write memory as it happens.** When a turn on any channel produces something worth keeping — a decision, a bug, an insight, a new preference — append it to ` + "`memories/MEMORY.md`" + ` in that same turn. Do not save it for later: the context may be gone by then. **This file is loaded into every session, so every line is billed on every turn** — keep it distilled. When a new entry supersedes an older one, rewrite or drop the old line instead of stacking both; nothing here rotates on its own.

**User messages come first (MANDATORY).** When a turn batches several messages, ` + "`[user] ...`" + ` is direct human input — voice or typed. Answer the most recent ` + "`[user]`" + ` message first and treat ` + "`[activity]`" + ` / ` + "`[emotion]`" + ` / ` + "`[speech_emotion]`" + ` / ` + "`[ambient]`" + ` / ` + "`[sensing:*]`" + ` as supporting context, never as the thing being answered.

**Version check:** ` + "`os-server --version`" + ` (OS), ` + "`hermes --version`" + ` (agent), ` + "`curl -s http://127.0.0.1:5001/version`" + ` (HAL).

**Silence = the literal token ` + "`NO_REPLY`" + `.** When a skill says not to speak, output exactly ` + "`NO_REPLY`" + ` and nothing else. Never narrate the decision ("Sound event, no user message. Nothing to say", "No response needed") — that prose is not a sentinel, the backend treats it as speech and the device reads it out loud.
---`
)

// SetupAgent materializes the Hermes device config from config.json by running the
// same presync EnsureOnboarding runs. The device setup flow calls this AFTER it
// persists config.json (system/device/setup.go), so presync picks up the
// freshly-entered llm_api_key/base_url + channel tokens right away instead of
// waiting for the next os-server boot. The SetupRequest is unused — config.json
// (just saved) is the source of truth presync reads.
func (s *HermesService) SetupAgent(_ domain.SetupRequest) error {
	return s.EnsureOnboarding()
}

// EnsureOnboarding reconciles the device-side Hermes config on every os-server
// boot by running the embedded presync hook (PresyncScript) — the SAME script
// switch-runtime runs right before hermes starts.
//
// Why this is not a no-op (unlike the original stub's "user has confirmed Hermes
// is provisioned" assumption): presync was ONLY ever triggered by an explicit
// runtime SWITCH (UpdateAgentRuntime, old != new). So two paths kept a stale
// config.yaml that never picked up config.json's real llm_api_key/base_url:
//  1. a device that boots straight into hermes (ROBOT.md gateway.default: hermes,
//     or imaged with it) WITHOUT ever switching from openclaw;
//  2. an llm_* change while hermes was already active (the config-change listener
//     only refreshes HAL, not config.yaml).
//
// OpenClaw self-heals its config every boot (ensureAgentDefaults + StartModelSync);
// this gives Hermes the same property by reusing its own presync — no duplicated
// sync logic in Go.
//
// presync.sh is idempotent (yq fill-if-missing + sync, guarded skill restore), so
// a steady boot writes nothing. We hash config.yaml around the run and restart
// hermes-gateway ONLY when it actually changed, so there is no restart loop.
func (s *HermesService) EnsureOnboarding() error {
	// Hash both config.yaml AND .env: presync writes channel tokens to .env, so a
	// channel-only change (e.g. adding Slack) leaves config.yaml untouched and must
	// still trigger a gateway restart for the Hermes server to pick the channel up.
	before := fileHash(hermesConfigYAML) + fileHash(hermesEnvFile)

	// Presync is best-effort: a failure must not block gateway startup. A device
	// that just had config.json written (setup wizard, first boot) needs the gateway
	// running even when presync hits a transient error (missing dep, bad env, etc.).
	// We log the error and continue so ensureGatewayUnit + restartHermesGateway still
	// fire below.
	if err := s.runPresync(); err != nil {
		slog.Warn("hermes presync failed, continuing with gateway start", "component", "hermes", "error", err)
	}

	// Rebuild the OS-managed parts of SOUL.md. Both run AFTER presync because its
	// §0 claw-migrate can rewrite the soul, and both are best-effort and outside
	// the restart decision below — SOUL.md is read per session, not at gateway
	// start (same rule UpdateIdentityName relies on).
	//
	// Order matters: the persona block goes in first and sits at the top, then
	// the skill-priority block is appended below it.
	if _, err := s.ensureSoulMDBlock(); err != nil {
		slog.Warn("hermes device soul injection failed", "component", "hermes", "error", err)
	}
	if _, err := s.ensureAgentsMDBlock(); err != nil {
		slog.Warn("hermes AGENTS.md rule block failed", "component", "hermes", "error", err)
	}
	if _, err := s.pruneSoulOSRuleBlock(); err != nil {
		slog.Warn("hermes soul rule-block prune failed", "component", "hermes", "error", err)
	}

	// De-dupe "<name>-imported" skill dirs a claw migrate may have left behind
	// (also runs AFTER presync, whose §0 can run claw migrate). Two candidates
	// for one skill name make Hermes refuse to load the skill entirely.
	skillsDeduped := s.pruneImportedSkillDuplicates()

	// config "changed" covers config.yaml AND .env: presync writes channel tokens to
	// .env, so a channel-only change (e.g. adding Slack) leaves config.yaml untouched
	// and must still restart the gateway for the Hermes server to pick the channel up.
	configChanged := fileHash(hermesConfigYAML)+fileHash(hermesEnvFile) != before

	// Reconcile the optional plugin on existing devices too. Keep its allow-list
	// update outside configChanged: installing Jev alone must not restart Hermes.
	if err := s.ensureJevPlugin(); err != nil {
		slog.Warn("hermes Jev plugin sync failed", "component", "hermes", "error", err)
	}

	// Materialize the os-server-observer hook so channel turns surface in Flow
	// Monitor. Best-effort: a hook write failure must not block the boot path
	// (config self-heal above already succeeded).
	hookChanged, err := s.ensureObserverHook()
	if err != nil {
		slog.Warn("hermes observer hook materialize failed", "component", "hermes", "error", err)
	}

	// Preserve cache counters in the installed API before the existing restart.
	cacheUsageChanged, err := s.ensureCacheUsagePatch()
	if err != nil {
		slog.Warn("hermes cache usage compatibility patch failed", "component", "hermes", "error", err)
	}

	// Apply the BlueBubbles / iMessage runtime patches. Best-effort: individual
	// script failures log a warning inside the ensure but never surface here as
	// an error — voice must stay up even if iMessage patches drift after a
	// Hermes upgrade. A Hermes package update wipes every patch (it reinstalls
	// the .py files from the wheel), so this must run on every EnsureOnboarding
	// pass, not only on config changes.
	bluebubblesPatchesChanged, err := s.ensureBluebubblesPatches()
	if err != nil {
		slog.Warn("hermes bluebubbles patches failed", "component", "hermes", "error", err)
	}

	// Enable native Runs only when its tool/cache evidence contract is verified.
	nativeRunsChanged, err := s.ensureNativeRunsPatch()
	if err != nil {
		slog.Warn("hermes native Runs compatibility patch failed", "component", "hermes", "error", err)
	}

	// Reconcile every supported platform skill from the CDN, not only an empty
	// directory. This closes the restart race where OTA metadata is already new
	// when the watcher seeds its versions but the local skill files are old.
	// Content hashes make an unchanged boot a no-op.
	changedSkills := s.downloadSkills()
	skillsSynced := len(changedSkills) > 0

	// B (self-heal): make sure the hermes-gateway.service unit actually exists before
	// we rely on (re)starting it. A device that reached hermes WITHOUT switch-runtime
	// (e.g. a hand-edited config.json agent_runtime=hermes after factory reset) has the
	// pre-baked binary but no unit, so IsReady()'s /health probe — and the setup
	// WaitForAgentReady gate — would fail forever. Install it on demand (fast; binary
	// is pre-baked). Also (re)start when the unit exists but is not running: factory
	// reset disables+stops it (reset.go step 4, "SetupAgent re-enables"), and it can
	// crash on a stale config that presync just fixed.
	gatewayInstalled := s.ensureGatewayUnit()
	gatewayDown := !gatewayActive()

	bluebubblesPatchesApplied := bluebubblesPatchesChanged > 0

	if !configChanged && !hookChanged && !cacheUsageChanged && !bluebubblesPatchesApplied && !nativeRunsChanged && !skillsSynced && !skillsDeduped && !gatewayInstalled && !gatewayDown {
		slog.Info("hermes onboarding: config + hooks + skills unchanged, gateway up — no restart", "component", "hermes")
		return nil
	}

	slog.Info("hermes onboarding: (re)starting gateway",
		"component", "hermes", "unit", hermesGatewayUnit,
		"config_changed", configChanged, "hook_changed", hookChanged, "cache_usage_changed", cacheUsageChanged,
		"bluebubbles_patches_changed", bluebubblesPatchesChanged, "native_runs_changed", nativeRunsChanged,
		"skills_synced", skillsSynced,
		"skills_deduped", skillsDeduped, "gateway_installed", gatewayInstalled, "gateway_down", gatewayDown)
	// Re-enable so hermes survives a reboot — factory reset disabled the unit, and a
	// freshly installed one is not enabled for boot. Best-effort; restart still starts
	// it for this session even if enable fails.
	enableHermesGateway()
	if err := restartHermesGateway(); err != nil {
		// Non-fatal: the new config/hook is on disk; the gateway picks it up on its
		// next (re)start even if this one failed. Don't block the os-server boot path.
		slog.Warn("hermes gateway restart failed", "component", "hermes", "error", err)
	}

	// Notify after the restart attempt so the gateway has the best chance to be
	// connected. This matches OpenClaw's onboarding flow.
	s.notifySkillChanges(changedSkills)
	return nil
}

// runPresync materializes the embedded presync script to a temp file and runs it.
// The script is self-contained (hardcodes /root/.hermes + /root/config/config.json)
// and idempotent, so it is safe to run on every boot.
func (s *HermesService) runPresync() error {
	f, err := os.CreateTemp("", "hermes-presync-*.sh")
	if err != nil {
		return fmt.Errorf("create temp: %w", err)
	}
	path := f.Name()
	defer os.Remove(path)
	if _, err := f.Write(PresyncScript); err != nil {
		f.Close()
		return fmt.Errorf("write script: %w", err)
	}
	f.Close()
	if err := os.Chmod(path, 0o755); err != nil {
		return fmt.Errorf("chmod: %w", err)
	}

	// Generous timeout: a normal boot is fast (yq edits only), but a post-reset
	// boot may run `claw migrate` to restore skills (see presync.sh §0).
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	out, err := exec.CommandContext(ctx, "bash", path).CombinedOutput()
	if len(out) > 0 {
		slog.Info("hermes presync output", "component", "hermes", "output", strings.TrimSpace(string(out)))
	}
	if err != nil {
		return fmt.Errorf("run presync: %w", err)
	}
	return nil
}

// fileHash returns a content hash of path, or "" when the file is absent — so a
// config.yaml that did not exist before and was created by presync reads as
// changed (triggering the restart).
func fileHash(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(b)
	return string(sum[:])
}

// RestartAgent restarts the hermes gateway only. Mirrors
// runtimes/openclaw/service_setup.go RestartAgent (which restarts the openclaw
// gateway) — same contract, different unit.
func (s *HermesService) RestartAgent() error {
	slog.Debug("restarting hermes gateway", "component", "hermes")
	if err := restartHermesGateway(); err != nil {
		return err
	}
	slog.Info("restart completed", "component", "hermes")
	return nil
}

// pruneImportedSkillDuplicates removes the "<name>-imported" duplicates that
// `hermes claw migrate --skill-conflict rename` leaves in skills/openclaw-imports
// when it runs while the canonical copies are already on disk (install-time race
// with the skill watcher, manual migrate runs, …). Two candidates for one skill
// name make Hermes' skill_view refuse to load the skill at all ("Ambiguous skill
// name") — the agent then improvises without it, which is how a device with a
// valid Gmail token ended up trying to install CLI email clients. The copy under
// <name> is canonical (the skill watcher keeps it fresh from the CDN), so the
// -imported duplicate is dropped; when only the -imported copy exists it is
// renamed to <name> instead. Returns true when anything changed so
// EnsureOnboarding restarts the gateway (the session skill index is built at
// gateway start).
func (s *HermesService) pruneImportedSkillDuplicates() bool {
	return pruneImportedDuplicatesIn(filepath.Join(hermesHome, "skills", "openclaw-imports")) > 0
}

// pruneImportedDuplicatesIn is the path-parameterized worker (split out for
// tests). Returns the number of entries it removed or renamed.
func pruneImportedDuplicatesIn(dir string) int {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0 // absent dir: nothing to prune
	}
	changed := 0
	for _, e := range entries {
		if !e.IsDir() || !strings.HasSuffix(e.Name(), "-imported") {
			continue
		}
		base := strings.TrimSuffix(e.Name(), "-imported")
		if base == "" {
			continue
		}
		dupPath := filepath.Join(dir, e.Name())
		basePath := filepath.Join(dir, base)
		if _, err := os.Stat(basePath); err == nil {
			if err := os.RemoveAll(dupPath); err != nil {
				slog.Warn("prune imported skill duplicate failed", "component", "hermes", "skill", e.Name(), "error", err)
				continue
			}
			slog.Info("pruned imported skill duplicate", "component", "hermes", "removed", e.Name(), "kept", base)
		} else {
			// Only copy on disk — reclaim the canonical name instead of deleting.
			if err := os.Rename(dupPath, basePath); err != nil {
				slog.Warn("rename imported skill failed", "component", "hermes", "skill", e.Name(), "error", err)
				continue
			}
			slog.Info("renamed imported skill to canonical name", "component", "hermes", "from", e.Name(), "to", base)
		}
		changed++
	}
	return changed
}

// ensureAgentsMDBlock reconciles the OS-managed rule block in ~/.hermes/AGENTS.md
// (see agentsMDBlock). Returns true when the file changed.
//
// This is the same slot every other runtime uses. Hermes finds it because presync
// pins terminal.cwd to the Hermes home: project-context discovery starts from the
// CONFIGURED cwd, and the stock relative `.` leaves it empty. Anything the owner
// writes below the block's closing `---` is preserved.
func (s *HermesService) ensureAgentsMDBlock() (bool, error) {
	path := filepath.Join(hermesHome, "AGENTS.md")
	raw, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		return false, fmt.Errorf("read %s: %w", path, err)
	}
	updated := upsertAgentsMDBlock(string(raw))
	if updated == string(raw) {
		return false, nil
	}
	if err := writeManagedFile(path, updated); err != nil {
		return false, err
	}
	slog.Info("OS rule block injected into AGENTS.md", "component", "hermes", "path", path)
	return true, nil
}

// pruneSoulOSRuleBlock removes the rule block from SOUL.md. Devices that ran an
// os-server from before the block moved to AGENTS.md still carry it there, and a
// duplicate rule set in two prompt files is both wasted tokens and a future
// contradiction when only one copy gets updated. Returns true when it removed one.
func (s *HermesService) pruneSoulOSRuleBlock() (bool, error) {
	soulPath := filepath.Join(hermesHome, "SOUL.md")
	raw, err := os.ReadFile(soulPath)
	if err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, fmt.Errorf("read %s: %w", soulPath, err)
	}
	updated := stripSoulOSRuleBlock(string(raw))
	if updated == string(raw) {
		return false, nil
	}
	if err := writeManagedFile(soulPath, updated); err != nil {
		return false, err
	}
	slog.Info("removed the OS rule block from SOUL.md — it lives in AGENTS.md now",
		"component", "hermes", "path", soulPath)
	return true, nil
}

// ensureSoulMDBlock injects this device's persona — the character declared by
// `soul_ref` in robots/<type>/ROBOT.md — as a marker-delimited block at the top
// of ~/.hermes/SOUL.md, refreshing it when an OTA ships new wording. Returns
// true when the file changed.
//
// Hermes shipped without this. Every other runtime injects the soul on each
// boot; Hermes only ever received one through persona migration, which copies
// from a PREVIOUS runtime — so a device that booted straight into Hermes (the
// lamp default) had no persona at all, and the routing rules that turn
// `[sensing:*]` into a skill call were simply absent.
//
// The block is byte-identical in shape to the one openclaw/picoclaw write, so a
// switch in either direction carries it across unchanged. A body that declares
// no soul_ref keeps whatever default soul Hermes ships.
func (s *HermesService) ensureSoulMDBlock() (bool, error) {
	core, hasSoul, err := device.ResolveSoul(s.config.DeviceTypeOrDefault())
	if err != nil {
		return false, fmt.Errorf("resolve device soul: %w", err)
	}
	if !hasSoul {
		slog.Info("no soul_ref for device — leaving the migrated/default soul (no override)",
			"component", "hermes", "device_type", s.config.DeviceTypeOrDefault())
		return false, nil
	}
	soulPath := filepath.Join(hermesHome, "SOUL.md")
	raw, err := os.ReadFile(soulPath)
	if err != nil && !os.IsNotExist(err) {
		return false, fmt.Errorf("read %s: %w", soulPath, err)
	}
	output := upsertSoulPersonaBlock(string(raw), string(core))
	if output == string(raw) {
		return false, nil
	}
	if err := writeManagedFile(soulPath, output); err != nil {
		return false, err
	}
	slog.Info("device soul injected into SOUL.md", "component", "hermes",
		"path", soulPath, "device_type", s.config.DeviceTypeOrDefault(), "bytes", len(core))
	return true, nil
}

// isPersonaBody is the inverse of isSkillPriorityBody: any marked block that is
// not the skill-priority rules is the persona.
func isPersonaBody(body string) bool {
	return !isSkillPriorityBody(body)
}

// writeManagedFile replaces an OS-managed prompt file atomically (tmp + rename, same as
// UpdateIdentityName). Hermes re-reads the file per session, so no gateway
// restart is needed — but a half-written soul would be read as-is.
func writeManagedFile(path, content string) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("mkdir %s: %w", dir, err)
	}
	tmp, err := os.CreateTemp(dir, ".SOUL.*.tmp")
	if err != nil {
		return fmt.Errorf("create tmp: %w", err)
	}
	tmpPath := tmp.Name()
	if _, err := tmp.WriteString(content); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return fmt.Errorf("write tmp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("close tmp: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("rename: %w", err)
	}
	return nil
}

// upsertSoulPersonaBlock returns soul with exactly one current persona block at
// the top, preserving owner content below it. The previous persona block is
// dropped wherever it sat so an OTA refreshes the wording instead of stacking a
// second copy; the skill-priority block wears its own marker and is never
// matched here, and a legacy copy of it under the shared marker is left for
// upsertSoulSkillPriorityBlock to replace.
//
// Mirrors openclaw/picoclaw's ensureSoulMDBlock, including the two rules that
// keep the file from growing a second soul: a managed default left below the
// block is discarded rather than preserved as fake owner edits, and a first
// install is seeded with an owner-editable section.
func upsertSoulPersonaBlock(soul, core string) string {
	block := soulOSMarker + "\n" + strings.TrimSpace(core) + "\n---"
	rest := strings.TrimLeft(stripSoulMarkedBlock(soul, soulOSMarker, isPersonaBody), " \t\r\n")

	// Drop a managed default soul so it is not kept below the block as a second,
	// competing persona — on a freshly flashed device the Hermes gateway has
	// already re-seeded its own. Owner edits under `## Personal` survive.
	if isManagedDefaultSoul(rest) {
		if idx := strings.Index(rest, soulPersonalHeading); idx >= 0 {
			rest = rest[idx:]
		} else {
			rest = ""
		}
	}
	if strings.TrimSpace(rest) == "" {
		return block + "\n\n" + soulPersonalSeed
	}
	return block + "\n\n" + rest
}

// managedDefaultSoulPrefixes opens a default soul that some other component
// seeded. None of them is owner content, and keeping one below the persona block
// leaves the file carrying two personas that contradict each other.
//
// The Hermes gateway one has no heading at all: it re-seeds its own persona into
// SOUL.md whenever the file is missing, and presync runs before we do — so on a
// freshly flashed device that prose is what ensureSoulMDBlock finds. The
// heading-shaped entries are what openclaw/picoclaw's isDefaultSoulHeading
// guards against, reaching Hermes through persona migration.
var managedDefaultSoulPrefixes = []string{
	"You are Hermes Agent, built by Nous Research", // the Hermes gateway's own seed
	"# Hermes Agent Persona",                       // hermesSoulFallback (factory reset)
	"# Soul",                                       // legacy openclaw self-seed
	"# SOUL.md",                                    // the OpenClaw gateway default
}

// isManagedDefaultSoul reports whether text opens with one of those defaults.
// Counterpart of openclaw/picoclaw's isDefaultSoulHeading.
func isManagedDefaultSoul(text string) bool {
	trimmed := strings.TrimLeft(text, " \t\r\n")
	for _, p := range managedDefaultSoulPrefixes {
		if strings.HasPrefix(trimmed, p) {
			return true
		}
	}
	return false
}

// upsertAgentsMDBlock returns text with exactly one current copy of the OS rule
// block at the top: any previous copy is stripped, so an os-server OTA refreshes
// the wording, and owner content below it is preserved.
func upsertAgentsMDBlock(text string) string {
	rest := strings.TrimLeft(stripSoulMarkedBlock(text, soulOSMarker, nil), " \t\r\n")
	if strings.TrimSpace(rest) == "" {
		return agentsMDBlock + "\n"
	}
	return agentsMDBlock + "\n\n" + rest
}

// stripSoulOSRuleBlock removes the rule block from a SOUL.md in either shape it
// shipped in: its own marker, or — on older devices — the shared marker, matched
// by the sentinel so a persona block wearing that marker is never touched.
func stripSoulOSRuleBlock(soul string) string {
	soul = stripSoulMarkedBlock(soul, soulSkillPriorityMarker, nil)
	soul = stripSoulMarkedBlock(soul, soulOSMarker, isSkillPriorityBody)
	return strings.TrimRight(soul, " \t\r\n") + "\n"
}

// isSkillPriorityBody reports whether a marked block's body is the skill-priority
// block rather than a persona.
func isSkillPriorityBody(body string) bool {
	return strings.HasPrefix(strings.TrimSpace(body), soulSkillPrioritySentinel)
}

// stripSoulMarkedBlock removes every block running from a line equal to marker
// down to the next `---` separator (or end of file, for an unterminated block).
//
// match, when non-nil, is called with the block body and the block is removed
// only if it returns true — blocks it rejects are left in place verbatim. That
// is what lets the persona block and the skill-priority block coexist in one
// file while each updater touches only its own.
func stripSoulMarkedBlock(text, marker string, match func(body string) bool) string {
	if !strings.Contains(text, marker) {
		return text
	}
	lines := strings.Split(text, "\n")
	var cleaned []string
	for i := 0; i < len(lines); i++ {
		if strings.TrimSpace(lines[i]) != marker {
			cleaned = append(cleaned, lines[i])
			continue
		}
		end := i + 1
		for end < len(lines) && strings.TrimSpace(lines[end]) != "---" {
			end++
		}
		if match != nil && !match(strings.Join(lines[i+1:end], "\n")) {
			cleaned = append(cleaned, lines[i])
			continue
		}
		i = end // skip the body and its closing `---`
	}
	return strings.Join(cleaned, "\n")
}

func restartHermesGateway() error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "systemctl", "restart", hermesGatewayUnit).CombinedOutput()
	if err != nil {
		return fmt.Errorf("systemctl restart %s: %s: %w", hermesGatewayUnit, strings.TrimSpace(string(out)), err)
	}
	return nil
}
