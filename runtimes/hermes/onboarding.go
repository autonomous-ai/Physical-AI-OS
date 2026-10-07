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

	// soulOSMarker delimits the device persona block, same shape as other runtimes so it survives a switch.
	soulOSMarker = "<!-- OS DO NOT REMOVE -->"

	// soulSkillPriorityMarker delimits the skill-priority block.
	soulSkillPriorityMarker = "<!-- OS HERMES SKILL PRIORITY -->"

	// soulSkillPrioritySentinel opens the skill-priority body.
	soulSkillPrioritySentinel = "**Skill priority (MANDATORY):**"

	// soulPersonalHeading is the owner-editable section every runtime seeds below its managed block, and the one part of a managed default soul worth keeping.
	soulPersonalHeading = "## Personal"

	soulPersonalSeed = soulPersonalHeading + "\n\n_Owner-editable. Add notes about yourself, family, routines, or personality tweaks here. The block above is managed by the OS and will be refreshed on each update — keep your edits in this section._\n"

	// agentsMDBlock is the OS rule block written to ~/.hermes/AGENTS.md.
	agentsMDBlock = soulOSMarker + `
**Skill priority (MANDATORY):** The skills under ` + "`skills/openclaw-imports/`" + ` are this device's built-in platform skills. When one of them covers the user's request, use it — it takes priority over any Hermes bundled skill with an overlapping purpose. In particular, anything on a connected third-party service (Gmail, Google Calendar, Google Drive, Notion, Figma, Asana, Linear, GitHub, …) — reading, sending, or acting — goes through the ` + "`connectors`" + ` skill: the device's credentials are already on disk there. Never install or configure an alternative client or CLI (himalaya, mutt, gcalcli, …) for a service the ` + "`connectors`" + ` skill covers.

**People — keep ` + "`memories/USER.md`" + ` current.** Hermes loads ` + "`MEMORY.md`" + ` and ` + "`USER.md`" + ` by name, and ` + "`USER.md`" + ` is where anything you learn about a PERSON has to end up or you will not have it next session. Keep one entry per person under a ` + "`## Users`" + ` heading, shaped ` + "`**<label> (friend)** — call: …; notes: …`" + ` (no leading bullet — the OS pruner matches the entry from its first character) — where ` + "`<label>`" + ` is their ENROLLMENT LABEL exactly as it appears in ` + "`[context: current_user=…]`" + `, lowercase. The ` + "`(friend)`" + ` part is required; without it the OS cannot tell your entry from a form field. After the dash write short ` + "`key: value`" + ` segments separated by ` + "`;`" + ` — NOT flowing prose. Only segments that change how you help them.
  - ` + "`call:`" + ` comes FIRST and only when they have TOLD you what to be called. Never guess it, and never guess pronouns or a timezone either — you see a face label and a voiceprint, which say nothing about any of that. A title or honorific heard in a voice turn (Mr, Ms, Miss, Mrs, anh, chị…) is not them telling you what to be called — speech recognition often invents one ("…is Lee" heard as "Miss Lee"): write the bare name, and never infer gender from a title, a name, a face or a voice. Keep a title only when they explicitly ask for it ("call me Ms Lee"). If they have not said, omit the segment entirely and just use their label.
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

// SetupAgent materializes the Hermes device config from config.json by running the same presync EnsureOnboarding runs.
func (s *HermesService) SetupAgent(_ domain.SetupRequest) error {
	return s.EnsureOnboarding()
}

// EnsureOnboarding reconciles device-side Hermes config on boot by running the embedded presync hook.
func (s *HermesService) EnsureOnboarding() error {
	before := fileHash(hermesConfigYAML) + fileHash(hermesEnvFile)

	// Presync is best-effort: a failure must not block gateway startup.
	if err := s.runPresync(); err != nil {
		if s.config.LLMMode() != "" {
			return fmt.Errorf("apply LLM configuration: %w", err)
		}
		slog.Warn("hermes presync failed, continuing with gateway start", "component", "hermes", "error", err)
	}

	// Order matters: persona block first (top), then the skill-priority block below it.
	if _, err := s.ensureSoulMDBlock(); err != nil {
		slog.Warn("hermes device soul injection failed", "component", "hermes", "error", err)
	}
	if _, err := s.ensureAgentsMDBlock(); err != nil {
		slog.Warn("hermes AGENTS.md rule block failed", "component", "hermes", "error", err)
	}
	if _, err := s.pruneSoulOSRuleBlock(); err != nil {
		slog.Warn("hermes soul rule-block prune failed", "component", "hermes", "error", err)
	}

	skillsDeduped := s.pruneImportedSkillDuplicates()

	configChanged := fileHash(hermesConfigYAML)+fileHash(hermesEnvFile) != before

	// Reconcile the optional plugin on existing devices too.
	// Outside configChanged: installing Jev alone must not restart Hermes.
	if err := s.ensureJevPlugin(); err != nil {
		slog.Warn("hermes Jev plugin sync failed", "component", "hermes", "error", err)
	}

	// Materialize the os-server-observer hook so channel turns surface in Flow Monitor.
	hookChanged, err := s.ensureObserverHook()
	if err != nil {
		slog.Warn("hermes observer hook materialize failed", "component", "hermes", "error", err)
	}

	cacheUsageChanged, err := s.ensureCacheUsagePatch()
	if err != nil {
		slog.Warn("hermes cache usage compatibility patch failed", "component", "hermes", "error", err)
	}

	// Apply the BlueBubbles / iMessage runtime patches.
	bluebubblesPatchesChanged, err := s.ensureBluebubblesPatches()
	if err != nil {
		slog.Warn("hermes bluebubbles patches failed", "component", "hermes", "error", err)
	}

	nativeRunsChanged, err := s.ensureNativeRunsPatch()
	if err != nil {
		slog.Warn("hermes native Runs compatibility patch failed", "component", "hermes", "error", err)
	}

	// Reconcile every supported platform skill from the CDN, not only an empty directory.
	changedSkills := s.downloadSkills()
	skillsSynced := len(changedSkills) > 0

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
	enableHermesGateway()
	if err := restartHermesGateway(); err != nil {
		slog.Warn("hermes gateway restart failed", "component", "hermes", "error", err)
	}

	s.notifySkillChanges(changedSkills)
	return nil
}

// runPresync materializes the embedded presync script to a temp file and runs it.
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

// fileHash returns a content hash of path, or "" when absent.
func fileHash(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(b)
	return string(sum[:])
}

// RestartAgent restarts the hermes gateway only.
func (s *HermesService) RestartAgent() error {
	slog.Debug("restarting hermes gateway", "component", "hermes")
	if err := restartHermesGateway(); err != nil {
		return err
	}
	slog.Info("restart completed", "component", "hermes")
	return nil
}

// pruneImportedSkillDuplicates removes "<name>-imported" duplicates left by `hermes claw migrate`.
func (s *HermesService) pruneImportedSkillDuplicates() bool {
	return pruneImportedDuplicatesIn(filepath.Join(hermesHome, "skills", "openclaw-imports")) > 0
}

// pruneImportedDuplicatesIn is the path-parameterized worker (split out for tests).
func pruneImportedDuplicatesIn(dir string) int {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0
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

// ensureAgentsMDBlock reconciles the OS-managed rule block in ~/.hermes/AGENTS.md (see agentsMDBlock).
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

// pruneSoulOSRuleBlock removes the rule block from SOUL.md.
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

// ensureSoulMDBlock injects the device persona (soul_ref) as a marker-delimited block atop ~/.hermes/SOUL.md.
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

// isPersonaBody is the inverse of isSkillPriorityBody: any marked block that is not the skill-priority rules is the persona.
func isPersonaBody(body string) bool {
	return !isSkillPriorityBody(body)
}

// writeManagedFile replaces an OS-managed prompt file atomically (tmp + rename, same as UpdateIdentityName).
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

// upsertSoulPersonaBlock returns soul with exactly one current persona block at the top, preserving owner content below it.
func upsertSoulPersonaBlock(soul, core string) string {
	block := soulOSMarker + "\n" + strings.TrimSpace(core) + "\n---"
	rest := strings.TrimLeft(stripSoulMarkedBlock(soul, soulOSMarker, isPersonaBody), " \t\r\n")

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

// managedDefaultSoulPrefixes opens a default soul that some other component seeded.
var managedDefaultSoulPrefixes = []string{
	"You are Hermes Agent, built by Nous Research",
	"# Hermes Agent Persona",
	"# Soul",
	"# SOUL.md",
}

// isManagedDefaultSoul reports whether text opens with one of those defaults.
func isManagedDefaultSoul(text string) bool {
	trimmed := strings.TrimLeft(text, " \t\r\n")
	for _, p := range managedDefaultSoulPrefixes {
		if strings.HasPrefix(trimmed, p) {
			return true
		}
	}
	return false
}

// upsertAgentsMDBlock returns text with exactly one current OS rule block at the top, owner content preserved.
func upsertAgentsMDBlock(text string) string {
	rest := strings.TrimLeft(stripSoulMarkedBlock(text, soulOSMarker, nil), " \t\r\n")
	if strings.TrimSpace(rest) == "" {
		return agentsMDBlock + "\n"
	}
	return agentsMDBlock + "\n\n" + rest
}

// stripSoulOSRuleBlock removes the legacy rule block from SOUL.md (own marker or sentinel-matched shared marker).
func stripSoulOSRuleBlock(soul string) string {
	soul = stripSoulMarkedBlock(soul, soulSkillPriorityMarker, nil)
	soul = stripSoulMarkedBlock(soul, soulOSMarker, isSkillPriorityBody)
	return strings.TrimRight(soul, " \t\r\n") + "\n"
}

// isSkillPriorityBody reports whether a marked block's body is the skill-priority block rather than a persona.
func isSkillPriorityBody(body string) bool {
	return strings.HasPrefix(strings.TrimSpace(body), soulSkillPrioritySentinel)
}

// stripSoulMarkedBlock removes every block running from a line equal to marker down to the next `---` separator (or end of file, for an unterminated block).
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
		i = end
	}
	return strings.Join(cleaned, "\n")
}

func restartHermesGateway() error {
	return restartHermesGatewayWithRunner(func(ctx context.Context) ([]byte, error) {
		return exec.CommandContext(ctx, "systemctl", "restart", hermesGatewayUnit).CombinedOutput()
	})
}

func restartHermesGatewayWithRunner(run func(context.Context) ([]byte, error)) error {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	out, err := run(ctx)
	if err != nil {
		return fmt.Errorf("systemctl restart %s: %s: %w", hermesGatewayUnit, strings.TrimSpace(string(out)), err)
	}
	return nil
}
