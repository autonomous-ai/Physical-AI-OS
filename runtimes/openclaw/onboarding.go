package openclaw

import (
	"context"
	"embed"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

//go:embed resources/KNOWLEDGE.md
var knowledgeFS embed.FS

const (
	osMandatoryMarker = "<!-- OS DO NOT REMOVE -->"

	agentsMDBlock = `<!-- OS DO NOT REMOVE -->
**Hooks** under ` + "`hooks/`" + ` are runtime triggers (handler.ts) that fire automatically on ` + "`message:preprocessed`" + ` before your turn begins. Their HOOK.md files are docstrings describing already-executed handlers — do NOT read them. Skipping HOOK.md reads removes one round-trip per turn with zero behavior change (turn-gate sets busy state, emotion-acknowledge fires the thinking emotion — both server-side, both unconditional).

**MANDATORY (skills):** Before any skill-driven action, determine the skill scope without doing broad filesystem scans. For ordinary chat, simple Q&A, or meta discussion with no action/event/hardware behavior, do NOT read a SKILL.md — answer normally.
  - If the message contains ` + "`[skills: a, b, c]`" + `, treat it as an authoritative whitelist — read ONLY those ` + "`skills/<name>/SKILL.md`" + ` files. Do NOT scan other skill directories "just in case".
  - If no ` + "`[skills:]`" + ` hint is present and the user asks for a concrete action, hardware behavior, sensing/activity/emotion handling, or a specialized workflow, use the injected ` + "`<available_skills>`" + ` descriptions to choose the single most specific matching skill, then read only that SKILL.md.
  - If multiple skills plausibly match, choose the most specific one. If none clearly match, do not read any SKILL.md and answer normally.
  - Never fall back to reading every skill directory. Broad scans are slow and usually reduce quality.

**Connectors (MANDATORY):** The user links third-party services (Gmail, Google Calendar, Google Drive, Notion, Figma, Asana, Linear, GitHub, Ahrefs, …) in the app, and the OS writes their credentials to this device at ` + "`/root/.openclaw/workspace/configs/<code>_access_tokens.json`" + `. ALWAYS use the ` + "`connectors`" + ` skill (` + "`skills/connectors/SKILL.md`" + `) to answer or act on ANY of them — "is my gmail connected?", "what's on my calendar", "list my events", "check my email", "send an email to …", "reply to that email". A question ABOUT a linked service is NOT ordinary chat: it needs this skill even when phrased as a simple question, so read ` + "`skills/connectors/SKILL.md`" + ` and follow it — the credentials are already on disk.
  - Gmail/Calendar/Drive are token-based and have NO MCP server, so NEVER conclude a service is unconnected because it has no MCP tool — check the credential files via the ` + "`connectors`" + ` skill.
  - Never write your own script (` + "`send_email.py`" + `, gcalcli, …) or install another client (mutt, msmtp, himalaya, sendmail, …) to reach a service a connector already covers, and never tell the user to set up an MCP server / OAuth app for these services.

Follow the instructions in whichever file you read.

**Version check:** ` + "`os-server --version`" + ` (OS), ` + "`openclaw --version`" + ` (OpenClaw), ` + "`curl -s http://127.0.0.1:5001/version`" + ` (HAL).

**Session Startup — also read:** ` + "`KNOWLEDGE.md`" + ` (accumulated learnings) in addition to the steps listed below.

**Priority: Skills > Knowledge > memory/*.md > History.** SKILL.md beats EVERYTHING (KNOWLEDGE.md, memory/*.md decisions, history). If memory says NO_REPLY but SKILL says nudge, follow SKILL. KNOWLEDGE.md is your personal observations — it can be wrong. Skills are the source of truth maintained by the developer. If you notice a conflict, update KNOWLEDGE.md to match the skill, not the other way around.

**Memory:** After each turn on any channel (voice, Telegram, or others) that contains something worth remembering (decisions, bugs, insights, new preferences), write it immediately to ` + "`memory/YYYY-MM-DD.md`" + `. Do not wait for heartbeat — context may be dropped before then.

**Silence = the literal token NO_REPLY.** When a skill says not to speak, output exactly NO_REPLY and nothing else. Never narrate the decision ("Sound event, no user message. Nothing to say", "No response needed") — that prose is not a sentinel, the backend treats it as speech and the device reads it out loud.

**Memory writes — DESCRIBE, never PRESCRIBE.** Before writing any "decision/rule" to memory/*.md or KNOWLEDGE.md, re-read the relevant SKILL.md. Blanket forms like "X → always Y" / "X → NO_REPLY for all" are frequency disguised as rule — write what happened with conditions, not a blanket ban.

**Don't duplicate JSONL.** Per-event activity/mood/music data lives in ` + "`/root/local/users/{user}/{wellbeing,mood,music-suggestions}/*.jsonl`" + ` and ` + "`/root/local/flow_events_*.jsonl`" + `. If ` + "`cat`" + ` of a JSONL can answer it, DO NOT write to memory. Memory is for cross-day insights only.

**Mood awareness (MANDATORY): Follow Mood skill.**

**User priority (MANDATORY):** When the turn batches multiple messages, ` + "`[user] ...`" + ` messages are direct human input (voice command or typed chat). Always answer the most recent ` + "`[user]`" + ` message first; treat ` + "`[activity]`" + ` / ` + "`[emotion]`" + ` / ` + "`[speech_emotion]`" + ` / ` + "`[ambient]`" + ` / ` + "`[sensing:*]`" + ` as supporting context, never as the primary prompt. A user who asked a question must get their answer even when sensing events queued alongside look more interesting.

---`

	// heartbeatMDBlock is the OS-managed block in workspace/HEARTBEAT.md, run on the
	// gateway's periodic heartbeat poll (~every 30 min while the device is on).
	//
	// It is deliberately CATCH-UP driven, not clock driven. The synthesis used to be
	// gated on "current time >= 21:00", which silently never fired on a device that
	// is switched off at the end of the working day — the common case for a desk
	// lamp. Device-observed 2026-09-03 on lamp-ac82: three days of flow logs ended
	// 18:39 / 17:57 / 17:34, and memory/2026-08-24.md was never distilled into
	// KNOWLEDGE.md because 21:00 never arrived. Comparing "days with memory" against
	// "days already distilled" instead means the first heartbeat after the device is
	// switched on clears whatever backlog accumulated, on any schedule.
	//
	// Keep this block byte-identical across openclaw/codex/opencode/picoclaw: it is
	// matched verbatim by ensureHeartbeatMDBlock, and a runtime switch must not
	// silently drop the people sync.
	heartbeatMDBlock = `<!-- OS DO NOT REMOVE -->
**Knowledge synthesis (catch-up — do NOT wait for a fixed hour):** Compare the days that have a ` + "`memory/YYYY-MM-DD.md`" + ` against the ` + "`## YYYY-MM-DD`" + ` headers already in ` + "`KNOWLEDGE.md`" + `. For every day BEFORE today that has a memory file but no header, distil that day now — oldest first, each under its own ` + "`## YYYY-MM-DD`" + ` header. Also do today, but only once it is >= 21:00. Only write new learnings — never repeat what is already there. Nothing missing → skip silently. This device is often switched off in the evening, so a fixed hour may simply never arrive; clearing the backlog on whatever heartbeat comes next is what keeps a day from being lost.

**Keep ` + "`KNOWLEDGE.md`" + ` from growing without bound (same pass).** A dated ` + "`## YYYY-MM-DD`" + ` block is raw material, not the archive — the distilled sections at the top are. Keep at most the **14 most recent** dated blocks. For anything older: fold what is still true into the matching top section (Hardware / Users / Skills & APIs / Mistakes Made), then DELETE the dated block. Nothing of value is lost — it was already distilled, and the raw day survives in ` + "`memory/YYYY-MM-DD.md`" + `. Without this the file grows by a section every active day and eventually costs more to read than it is worth.

**People sync (same pass, right after the above):** ` + "`KNOWLEDGE.md`" + ` is yours alone — the OS never loads it. ` + "`USER.md`" + ` IS loaded, into your system prompt, on every single turn. So anything you learned about a PERSON has to reach ` + "`USER.md`" + ` or you will not have it tomorrow. Carry it across:

- Write ONE bullet per person under a ` + "`## Users`" + ` heading in ` + "`USER.md`" + `, shaped ` + "`- **<label> (friend)** — call: …; notes: …`" + ` — where ` + "`<label>`" + ` is their ENROLLMENT LABEL exactly as it appears in ` + "`[context: current_user=…]`" + `, lowercase. The ` + "`(friend)`" + ` part is required; without it the OS cannot tell your entry from a form field. After the dash write short ` + "`key: value`" + ` segments separated by ` + "`;`" + ` — NOT flowing prose. Only segments that change how you help them.
- ` + "`call:`" + ` comes FIRST and only when they have TOLD you what to be called. Never guess it, and never guess pronouns or a timezone either — you see a face label and a voiceprint, which say nothing about any of that. If they have not said, omit the segment entirely and just use their label.
- **Only write what you observed about THAT person.** Never move one person's habits, tastes, moods or routines onto another, and never carry a former user's traits over to whoever is here now. Two people at one desk are two entries, never a merged one. If you cannot tell whose a behaviour was, leave it out.
- **Never delete a PERSON's entry.** Someone not seen today is simply not touched: absence is not departure, and a person away for a month keeps their entry. Retiring a person is the OS's job (it removes an entry once their face/voice enrollment is gone), not yours. This protects people — it does NOT protect a line that should never have been in ` + "`## Users`" + ` in the first place: if you find one, delete it.
- **Keep each entry under ~400 characters.** Segments are dense, so that is plenty. This file is loaded into your prompt on EVERY turn, so bloat is billed on all of them; and when it overflows the cap it is cut from the END, which is where ` + "`## Users`" + ` lives. Rewrite an entry to stay short rather than appending to it.
- **Strangers get NO entry — and remove any you find.** ` + "`## Users`" + ` is for people the device knows by enrollment. A passing face has no label to key on and nothing durable to remember; note desk traffic in ` + "`KNOWLEDGE.md`" + ` instead. An entry like ` + "`**stranger_4**`" + ` or a lumped ` + "`**stranger_2/3/4/…**`" + ` is not a person: delete it. The OS cannot clean these up for you — its pruner only recognises a proper ` + "`**<label> (role)**`" + ` entry.
- Do NOT fill ` + "`**Name:**`" + ` or the other single-value fields at the top. This device can have several people; who is present right now always comes from ` + "`[context: current_user=…]`" + ` on the turn, never from that field.

---`
)

// supportedSkills resolves this device's capabilities from ROBOT.md and filters
// the platform skill catalog (skills.Catalog) to what this device can run. The
// catalog and skill→capability map are runtime-agnostic platform metadata in
// system/skills — OpenClaw is only one consumer (Hermes or any other runtime
// would gate the same way).
func (s *OpenclawService) supportedSkills() []string {
	return skills.Supported(device.Capabilities(s.config.DeviceTypeOrDefault()))
}

// EnsureOnboarding seeds SOUL.md, downloads skills, and injects the mandatory
// block into workspace/AGENTS.md so OpenClaw scans the skills directory.
// IDENTITY.md is managed by OpenClaw itself (created during openclaw onboard).
// otaBaseURL derives the CDN base for OTA-published assets from the device's OTA
// metadata URL (config.json): the metadata URL minus "/ota/metadata.json".
// Skills and hooks live alongside it at <base>/skills and <base>/hooks. Returns
// "" when no metadata URL is configured (device not provisioned) so callers skip
// rather than fall back to a hardcoded URL.
func (s *OpenclawService) otaBaseURL() string {
	u := strings.TrimSpace(s.config.OTAMetadataURL)
	if u == "" {
		return ""
	}
	return strings.TrimSuffix(u, "/ota/metadata.json")
}

func (s *OpenclawService) skillsBaseURL() string {
	if base := s.otaBaseURL(); base != "" {
		return base + "/skills"
	}
	return ""
}

func (s *OpenclawService) hooksBaseURL() string {
	if base := s.otaBaseURL(); base != "" {
		return base + "/hooks"
	}
	return ""
}

func (s *OpenclawService) EnsureOnboarding() error {
	workspace := filepath.Join(s.config.OpenclawConfigDir, "workspace")
	if err := os.MkdirAll(workspace, 0755); err != nil {
		return fmt.Errorf("create workspace dir: %w", err)
	}

	needRestart := false
	if changed, err := s.ensureJevPlugin(); err != nil {
		slog.Warn("ensure Jev plugin failed", "component", "onboarding", "error", err)
	} else if changed {
		needRestart = true
	}

	// Inject SOUL.md core block (owner-editable content stays below the block)
	if modified, err := s.ensureSoulMDBlock(); err != nil {
		slog.Error("ensure SOUL.md block failed", "component", "onboarding", "error", err)
	} else if modified {
		needRestart = true
	}

	// Download skills from CDN
	skillsDir := filepath.Join(workspace, "skills")
	if err := os.MkdirAll(skillsDir, 0755); err != nil {
		return fmt.Errorf("create skills dir: %w", err)
	}
	// Capability gate: this device runs only the skills its ROBOT.md capabilities
	// support (plus platform skills). Create dirs for supported skills; remove any
	// unsupported skill dir so a re-provisioned device (or one whose ROBOT.md
	// changed) self-heals and a provision-time over-seed is cleaned up. Fail-open
	// for a device that declares no capabilities → full catalog (see supportedSkills).
	deviceCaps := device.Capabilities(s.config.DeviceTypeOrDefault())
	wanted := map[string]bool{}
	for _, name := range skills.Supported(deviceCaps) {
		wanted[name] = true
	}
	for _, name := range skills.Catalog {
		dir := filepath.Join(skillsDir, name)
		if wanted[name] {
			if err := os.MkdirAll(dir, 0755); err != nil {
				slog.Error("mkdir failed", "component", "onboarding", "dir", name, "error", err)
			}
		} else if err := os.RemoveAll(dir); err != nil {
			slog.Warn("prune unsupported skill failed", "component", "onboarding", "skill", name, "error", err)
		} else {
			slog.Info("skill not supported by device, pruned", "component", "onboarding", "skill", name, "capability", skills.Capability[name])
		}
	}
	changedSkills := s.downloadSkills()

	// Download hooks from CDN (alongside the device's OTA metadata URL).
	if hooksBase := s.hooksBaseURL(); hooksBase == "" {
		slog.Info("hooks download skipped: no ota_metadata_url configured", "component", "onboarding")
	} else {
		hooksDir := filepath.Join(workspace, "hooks")
		if err := os.MkdirAll(hooksDir, 0755); err != nil {
			return fmt.Errorf("create hooks dir: %w", err)
		}
		hookFiles := []string{"HOOK.md", "handler.ts"}
		// Capability gate (same as skills): a hook that needs an absent capability
		// (e.g. emotion-acknowledge → presence on a no-expression device) is not
		// seeded, and any stale copy is removed.
		wantedHooks := map[string]bool{}
		for _, name := range skills.SupportedHooks(deviceCaps) {
			wantedHooks[name] = true
		}
		for _, name := range skills.Hooks {
			dir := filepath.Join(hooksDir, name)
			if !wantedHooks[name] {
				if err := os.RemoveAll(dir); err != nil {
					slog.Warn("prune unsupported hook failed", "component", "onboarding", "hook", name, "error", err)
				} else {
					slog.Info("hook not supported by device, pruned", "component", "onboarding", "hook", name, "capability", skills.HookCapability[name])
				}
				continue
			}
			if err := os.MkdirAll(dir, 0755); err != nil {
				slog.Error("mkdir failed", "component", "onboarding", "dir", dir, "error", err)
				continue
			}
			for _, file := range hookFiles {
				dst := filepath.Join(dir, file)
				url := fmt.Sprintf("%s/%s/%s", hooksBase, name, file)
				changed, err := downloadFile(url, dst)
				if err != nil {
					slog.Error("download hook file failed", "component", "onboarding", "hook", name, "file", file, "error", err)
					continue
				}
				if changed {
					needRestart = true
				}
			}
			slog.Info("seeded hook", "component", "onboarding", "hook", name)
		}
	}

	// Seed KNOWLEDGE.md template only if the file does not already exist (living doc)
	seedFileIfAbsent(knowledgeFS, "resources/KNOWLEDGE.md", filepath.Join(workspace, "KNOWLEDGE.md"))

	// Ensure AGENTS.md has mandatory block
	if modified, err := s.ensureAgentsMDBlock(); err != nil {
		slog.Error("ensure AGENTS.md block failed", "component", "onboarding", "error", err)
	} else if modified {
		needRestart = true
	}

	// Ensure HEARTBEAT.md has knowledge-synthesis block
	if modified, err := s.ensureHeartbeatMDBlock(); err != nil {
		slog.Error("ensure HEARTBEAT.md block failed", "component", "onboarding", "error", err)
	} else if modified {
		needRestart = true
	}

	// Ensure supported hooks are registered (and unsupported ones removed) in
	// openclaw.json hooks.internal.entries.
	if hooksAdded, err := s.ensureHooksRegistered(skills.SupportedHooks(deviceCaps)); err != nil {
		slog.Error("ensure hooks registered failed", "component", "onboarding", "error", err)
	} else if hooksAdded {
		needRestart = true
	}

	// Ensure logging config is present in openclaw.json
	if loggingAdded, err := s.ensureLoggingConfig(); err != nil {
		slog.Error("ensure logging config failed", "component", "onboarding", "error", err)
	} else if loggingAdded {
		needRestart = true
	}

	// Ensure gateway auth token — generated only in SetupAgent but must also exist
	// when switching TO openclaw from another runtime (e.g. hermes). EnsureOnboarding
	// runs on every boot/switch; SetupAgent never runs in that path.
	if tokenSeeded, err := s.ensureGatewayToken(); err != nil {
		slog.Error("ensure gateway token failed", "component", "onboarding", "error", err)
	} else if tokenSeeded {
		needRestart = true
	}

	// Ensure models.providers.autonomous has the current apiKey + baseUrl from
	// config.json. SetupAgent writes this during the wizard; EnsureOnboarding does
	// not — so switching TO openclaw from hermes leaves the provider entry without
	// an API key and every agent turn fails with "No API key found for provider".
	if providerSynced, err := s.ensureProviderConfig(); err != nil {
		slog.Error("ensure provider config failed", "component", "onboarding", "error", err)
	} else if providerSynced {
		needRestart = true
	}

	// Ensure agent defaults (compaction, bootstrap limits, caching)
	if defaultsPatched, err := s.ensureAgentDefaults(); err != nil {
		slog.Error("ensure agent defaults failed", "component", "onboarding", "error", err)
	} else if defaultsPatched {
		needRestart = true
	}

	// Ensure gateway controlUi allows external origins (nginx proxy)
	if controlUIAdded, err := s.ensureControlUIConfig(); err != nil {
		slog.Error("ensure controlUi config failed", "component", "onboarding", "error", err)
	} else if controlUIAdded {
		needRestart = true
	}

	// Pin messages.queue.mode=steer so the os server's concurrent producers (sensing
	// drains, voice, Telegram, web chat) batch into the active turn at the
	// next model boundary instead of fanning out as serialized followup turns.
	if queueAdded, err := s.ensureMessagesQueueConfig(); err != nil {
		slog.Error("ensure messages.queue config failed", "component", "onboarding", "error", err)
	} else if queueAdded {
		needRestart = true
	}

	// Restart OpenClaw if non-skill files changed (SOUL.md, AGENTS.md, hooks, config)
	if needRestart {
		slog.Info("restarting OpenClaw to pick up changes", "component", "onboarding")
		if err := restartOpenclawGateway(); err != nil {
			return fmt.Errorf("restart openclaw after onboarding: %w", err)
		}
		slog.Info("OpenClaw restarted successfully", "component", "onboarding")
	}

	// For changed skills, tell the agent to re-read them (no restart needed).
	// This runs after restart (if any) so WS is connected.
	s.notifySkillChanges(changedSkills)

	return nil
}

// ensureHooksRegistered reconciles openclaw.json hooks.internal.entries with the
// hooks this device supports: it registers any missing supported hook and removes
// any published hook (skills.Hooks) the device does not support. Returns true if
// the file was modified.
func (s *OpenclawService) ensureHooksRegistered(hookNames []string) (bool, error) {
	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	configBytes, err := os.ReadFile(configPath)
	if err != nil {
		return false, fmt.Errorf("read openclaw.json: %w", err)
	}
	var configData map[string]interface{}
	if err := json.Unmarshal(configBytes, &configData); err != nil {
		return false, fmt.Errorf("parse openclaw.json: %w", err)
	}

	hooksMap := ensureMap(configData, "hooks")
	internalMap := ensureMap(hooksMap, "internal")
	if _, ok := internalMap["enabled"]; !ok {
		internalMap["enabled"] = true
	}
	entriesMap := ensureMap(internalMap, "entries")

	changed := false
	supported := map[string]bool{}
	for _, name := range hookNames {
		supported[name] = true
		if _, exists := entriesMap[name]; !exists {
			entriesMap[name] = map[string]interface{}{"enabled": true}
			changed = true
			slog.Info("registered hook in openclaw.json", "component", "onboarding", "hook", name)
		}
	}
	// Remove any published hook this device no longer supports (capability lost
	// or never had — e.g. emotion-acknowledge on a no-presence device), so it
	// stops firing into a route the device never mounts.
	for _, name := range skills.Hooks {
		if !supported[name] {
			if _, exists := entriesMap[name]; exists {
				delete(entriesMap, name)
				changed = true
				slog.Info("unregistered unsupported hook", "component", "onboarding", "hook", name)
			}
		}
	}
	if !changed {
		return false, nil
	}

	outBytes, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return false, fmt.Errorf("marshal openclaw.json: %w", err)
	}
	if err := os.WriteFile(configPath, outBytes, 0600); err != nil {
		return false, fmt.Errorf("write openclaw.json: %w", err)
	}
	return true, nil
}

// ensureAgentsMDBlock injects the mandatory skills block into AGENTS.md.
// Returns true if the file was modified.
func (s *OpenclawService) ensureAgentsMDBlock() (bool, error) {
	agentsFile := filepath.Join(s.config.OpenclawConfigDir, "workspace", "AGENTS.md")

	// If AGENTS.md is missing, run `openclaw setup` to regenerate the base template
	// before injecting the mandatory block. This preserves the full default content
	// (session startup instructions, memory rules, etc.) instead of writing to an empty file.
	if _, err := os.Stat(agentsFile); os.IsNotExist(err) {
		slog.Info("AGENTS.md missing, running openclaw setup to regenerate", "component", "onboarding")
		if out, err := exec.Command("openclaw", "setup").CombinedOutput(); err != nil {
			slog.Warn("openclaw setup failed, will inject into empty file", "component", "onboarding", "error", err, "output", strings.TrimSpace(string(out)))
		}
	}

	content, err := os.ReadFile(agentsFile)
	if err != nil && !os.IsNotExist(err) {
		return false, fmt.Errorf("read AGENTS.md: %w", err)
	}

	text := string(content)

	// Already has the exact current block → skip
	if strings.Contains(text, agentsMDBlock) {
		slog.Debug("AGENTS.md already has current mandatory block, skipping", "component", "onboarding")
		return false, nil
	}

	// Remove old block (with or without marker) before injecting current version
	if strings.Contains(text, osMandatoryMarker) {
		text = stripMarkedBlock(text)
	} else {
		text = stripLegacyMandatoryBlock(text)
	}

	// Find "Your workspace" line and inject block below it
	lines := strings.Split(text, "\n")
	var result []string
	injected := false

	for _, line := range lines {
		result = append(result, line)
		if !injected && strings.Contains(strings.ToLower(line), "your workspace") {
			result = append(result, agentsMDBlock)
			injected = true
		}
	}

	// If "Your workspace" not found, prepend to top of file
	if !injected {
		slog.Debug("'Your workspace' not found in AGENTS.md, prepending block", "component", "onboarding")
		result = append([]string{agentsMDBlock, ""}, result...)
	}

	output := strings.Join(result, "\n")
	if err := os.WriteFile(agentsFile, []byte(output), 0644); err != nil {
		return false, fmt.Errorf("write AGENTS.md: %w", err)
	}

	slog.Info("injected mandatory block into AGENTS.md", "component", "onboarding", "path", agentsFile)
	return true, nil
}

// devicesDir returns the root that holds per-device profile folders
// (robots/<type>/{DEVICE,SOUL}.md). Override with DEVICES_DIR; defaults to the
// on-device install path. The same tree HAL reads via HAL_DEVICES_DIR.
func devicesDir() string {
	if d := strings.TrimSpace(os.Getenv("DEVICES_DIR")); d != "" {
		return d
	}
	return "/opt/devices"
}

// deviceSoulCore returns the soul text to inject for this device, resolved from
// the `soul_ref` declared in robots/<type>/ROBOT.md (config.device_type):
//   - absent  → hasSoul=false: inject nothing, leaving the agentic runtime
//     (OpenClaw) to use its own default soul. We never override a soulless body.
//   - http(s) URL → download the soul artifact.
//   - any other value → a path read relative to robots/<type>/ (e.g. SOUL.md).
//
// A declared soul_ref that fails to resolve is a deploy fault (named a soul but
// did not ship it), so it returns an error rather than silently going soulless.
//
// This is what makes "device → which soul" real: each device type gets its own
// soul (or none) — from the same binary, no embedded hardcode.
func (s *OpenclawService) deviceSoulCore() (content []byte, hasSoul bool, err error) {
	devType := s.config.DeviceTypeOrDefault()
	ref := device.SoulRef(devType)
	if ref == "" {
		return nil, false, nil // body without a soul_ref: no override
	}
	if strings.HasPrefix(ref, "http://") || strings.HasPrefix(ref, "https://") {
		b, derr := downloadSoul(ref)
		if derr != nil {
			return nil, false, fmt.Errorf("download soul_ref %q: %w", ref, derr)
		}
		return b, true, nil
	}
	if strings.Contains(ref, "://") {
		return nil, false, fmt.Errorf("unsupported soul_ref scheme: %q (use http(s):// or a path)", ref)
	}
	path := filepath.Join(devicesDir(), devType, ref)
	b, rerr := os.ReadFile(path)
	if rerr != nil {
		return nil, false, fmt.Errorf("read soul_ref %q: %w", path, rerr)
	}
	return b, true, nil
}

// downloadSoul fetches a soul artifact named by an http(s) soul_ref.
func downloadSoul(url string) ([]byte, error) {
	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("status %d", resp.StatusCode)
	}
	return io.ReadAll(resp.Body)
}

// isDefaultSoulHeading reports whether trimmed begins with a MANAGED soul template
// heading that must never be preserved as owner content below the device block:
//   - "# Soul"     — the legacy lamp self-seed (pre-marker onboarding)
//   - "# SOUL.md"  — the OpenClaw gateway's OWN default soul ("# SOUL.md - Who You
//     Are"), which the gateway seeds into workspace/SOUL.md on first boot.
//
// Either, left below the device block, becomes a duplicate second soul (the
// historical SOUL.md duplication bug). HasPrefix is case-sensitive, so "# Soul"
// alone never matched the gateway's "# SOUL.md" default — that gap is exactly what
// let the dup persist.
func isDefaultSoulHeading(trimmed string) bool {
	return strings.HasPrefix(trimmed, "# Soul") || strings.HasPrefix(trimmed, "# SOUL.md")
}

// ensureSoulMDBlock wraps this device's soul as a marker-delimited core block
// at the top of workspace/SOUL.md. The soul is resolved per device_type from
// the ROBOT.md `soul_ref` (path or URL) — see deviceSoulCore. Anything the
// owner writes below the closing `---` is preserved on subsequent onboarding
// runs, mirroring the AGENTS.md / HEARTBEAT.md pattern. Returns true if the file
// was modified. A device that declares no soul injects no block.
func (s *OpenclawService) ensureSoulMDBlock() (bool, error) {
	soulFile := filepath.Join(s.config.OpenclawConfigDir, "workspace", "SOUL.md")

	coreContent, hasSoul, err := s.deviceSoulCore()
	if err != nil {
		return false, fmt.Errorf("resolve device soul: %w", err)
	}
	if !hasSoul {
		slog.Info("no soul_ref for device — leaving the gateway's default soul (no override)",
			"component", "onboarding", "device_type", s.config.DeviceTypeOrDefault())
		return false, nil
	}
	soulMDBlock := osMandatoryMarker + "\n" + strings.TrimSpace(string(coreContent)) + "\n---"

	content, err := os.ReadFile(soulFile)
	if err != nil && !os.IsNotExist(err) {
		return false, fmt.Errorf("read SOUL.md: %w", err)
	}
	text := string(content)

	// Fast path: the block is already present AND nothing but owner content sits
	// below it. Skipping the strip/rejoin here avoids re-introducing an extra
	// blank line after `---` on every run (which would rewrite SOUL.md and
	// restart OpenClaw each boot). But when a default soul template lingers below
	// the block — the historical duplication bug, or an OpenClaw re-seed — fall
	// through to the rebuild path so it gets stripped and the dup self-heals.
	if idx := strings.Index(text, soulMDBlock); idx >= 0 {
		below := strings.TrimLeft(text[idx+len(soulMDBlock):], " \t\r\n")
		if !isDefaultSoulHeading(below) {
			return false, nil
		}
	}

	// Strip any prior marker block first so the legacy-seed heuristic below
	// only sees whatever was below the closing `---`.
	if strings.Contains(text, osMandatoryMarker) {
		text = stripMarkedBlock(text)
	}

	// Discard any managed default soul left in the remaining text so it is not
	// preserved as fake "owner edits" and duplicated below the device block.
	// Two shapes reach here:
	//   - legacy lamp self-seed ("# Soul"): before the marker block existed,
	//     onboarding overwrote SOUL.md with the embedded core verbatim every run,
	//     so unmodified devices carry that core as fake owner content.
	//   - OpenClaw gateway default ("# SOUL.md - Who You Are"): the gateway seeds
	//     its own soul into workspace/SOUL.md, which the device block is meant to
	//     override — keeping it below `---` is the SOUL.md duplication bug.
	// Both also persist on devices that already ran the broken onboarding;
	// stripping the marker first then dropping the default self-heals them.
	// If the owner added their own `## Personal` section below it, keep only that
	// section; otherwise discard entirely.
	trimmed := strings.TrimLeft(text, " \t\r\n")
	if isDefaultSoulHeading(trimmed) {
		if idx := strings.Index(text, "## Personal"); idx >= 0 {
			text = text[idx:]
		} else {
			text = ""
		}
	}

	var output string
	if strings.TrimSpace(text) == "" {
		// First install or clean migration → seed an owner-editable Personal section.
		output = soulMDBlock + "\n\n## Personal\n\n_Owner-editable. Add notes about yourself, family, routines, or personality tweaks here. The block above is managed by the OS and will be refreshed on each update — keep your edits in this section._\n"
	} else {
		output = soulMDBlock + "\n\n" + text
	}

	if output == string(content) {
		slog.Debug("SOUL.md already in canonical shape, skipping", "component", "onboarding")
		return false, nil
	}

	if err := os.WriteFile(soulFile, []byte(output), 0644); err != nil {
		return false, fmt.Errorf("write SOUL.md: %w", err)
	}

	slog.Info("injected core block into SOUL.md", "component", "onboarding", "path", soulFile)
	return true, nil
}

// ensureHeartbeatMDBlock injects the knowledge-synthesis block into HEARTBEAT.md.
// Returns true if the file was modified.
func (s *OpenclawService) ensureHeartbeatMDBlock() (bool, error) {
	heartbeatFile := filepath.Join(s.config.OpenclawConfigDir, "workspace", "HEARTBEAT.md")

	content, err := os.ReadFile(heartbeatFile)
	if err != nil && !os.IsNotExist(err) {
		return false, fmt.Errorf("read HEARTBEAT.md: %w", err)
	}

	text := string(content)

	// Already has the exact current block → skip
	if strings.Contains(text, heartbeatMDBlock) {
		slog.Debug("HEARTBEAT.md already has current mandatory block, skipping", "component", "onboarding")
		return false, nil
	}

	// Remove old block if marker exists, then inject current version
	if strings.Contains(text, osMandatoryMarker) {
		text = stripMarkedBlock(text)
	}

	// Prepend block at the top of the file
	output := heartbeatMDBlock + "\n\n" + text
	if err := os.WriteFile(heartbeatFile, []byte(output), 0644); err != nil {
		return false, fmt.Errorf("write HEARTBEAT.md: %w", err)
	}

	slog.Info("injected mandatory block into HEARTBEAT.md", "component", "onboarding", "path", heartbeatFile)
	return true, nil
}

// stripMarkedBlock removes the block between the marker (<!-- OS DO NOT REMOVE -->)
// and the next --- separator.
func stripMarkedBlock(text string) string {
	lines := strings.Split(text, "\n")
	var cleaned []string
	skip := false
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		if trimmed == osMandatoryMarker {
			skip = true
			continue
		}
		if skip && trimmed == "---" {
			skip = false
			continue
		}
		if skip {
			continue
		}
		cleaned = append(cleaned, line)
	}
	return strings.Join(cleaned, "\n")
}

// stripLegacyMandatoryBlock removes the old MANDATORY block that was injected
// before any marker (<!-- OS DO NOT REMOVE -->) was introduced.
func stripLegacyMandatoryBlock(text string) string {
	lines := strings.Split(text, "\n")
	var cleaned []string
	skip := false
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		// Detect start of legacy block: starts with **MANDATORY:** but no marker above
		if !skip && strings.HasPrefix(trimmed, "**MANDATORY:**") {
			skip = true
			continue
		}
		// End of legacy block: next non-empty line that doesn't look like continuation
		if skip {
			if trimmed == "" || trimmed == "---" {
				skip = false
				// Keep the separator/blank line
				cleaned = append(cleaned, line)
			}
			// Skip continuation lines of the old block
			continue
		}
		cleaned = append(cleaned, line)
	}
	return strings.Join(cleaned, "\n")
}

// ensureLoggingConfig adds the logging block to openclaw.json if it is missing.
// Returns true if the file was modified.
func (s *OpenclawService) ensureLoggingConfig() (bool, error) {
	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	configBytes, err := os.ReadFile(configPath)
	if err != nil {
		return false, fmt.Errorf("read openclaw.json: %w", err)
	}
	var configData map[string]interface{}
	if err := json.Unmarshal(configBytes, &configData); err != nil {
		return false, fmt.Errorf("parse openclaw.json: %w", err)
	}

	if _, ok := configData["logging"]; ok {
		return false, nil
	}

	configData["logging"] = map[string]interface{}{
		"consoleStyle": "pretty",
		"file":         "/var/log/openclaw/agent.log",
		"level":        "debug",
		"consoleLevel": "debug",
	}

	outBytes, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return false, fmt.Errorf("marshal openclaw.json: %w", err)
	}
	if err := os.WriteFile(configPath, outBytes, 0600); err != nil {
		return false, fmt.Errorf("write openclaw.json: %w", err)
	}
	slog.Info("added logging config to openclaw.json", "component", "onboarding")
	return true, nil
}

// ensureControlUIConfig pins gateway.controlUi to local-only defaults so the
// Control UI handshake only accepts loopback origins on plain HTTP. Combined
// with nginx `/gw/` allow 127.0.0.1; deny all; (F6), the gateway is reachable
// only from on-device callers (SSH port-forward, on-device browser).
//
// Defaults:
//   - allowedOrigins = ["http://127.0.0.1", "http://localhost"]
//   - allowInsecureAuth = false
//
// Migration: devices originally provisioned with the loose defaults
// (allowedOrigins=["*"], allowInsecureAuth=true — used before F6 closed LAN
// access at nginx) are upgraded automatically here. Operators who set custom
// origins are left untouched.
func (s *OpenclawService) ensureControlUIConfig() (bool, error) {
	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	configBytes, err := os.ReadFile(configPath)
	if err != nil {
		return false, fmt.Errorf("read openclaw.json: %w", err)
	}
	var configData map[string]interface{}
	if err := json.Unmarshal(configBytes, &configData); err != nil {
		return false, fmt.Errorf("parse openclaw.json: %w", err)
	}

	gw, ok := configData["gateway"].(map[string]interface{})
	if !ok {
		return false, nil
	}

	cu, _ := gw["controlUi"].(map[string]interface{})
	if cu == nil {
		cu = map[string]interface{}{}
		gw["controlUi"] = cu
	}

	strictOrigins := []string{"http://127.0.0.1", "http://localhost"}
	changed := false

	switch v := cu["allowedOrigins"].(type) {
	case nil:
		cu["allowedOrigins"] = strictOrigins
		changed = true
	case []interface{}:
		// Migrate the historical loose default (exactly ["*"]) to strict.
		// Custom operator lists (any other shape) are preserved.
		if len(v) == 1 {
			if s0, ok := v[0].(string); ok && s0 == "*" {
				cu["allowedOrigins"] = strictOrigins
				changed = true
			}
		}
	}

	switch v := cu["allowInsecureAuth"].(type) {
	case nil:
		cu["allowInsecureAuth"] = false
		changed = true
	case bool:
		// Loopback HTTP works without this flag — nginx /gw/ already restricts
		// to loopback peers (F6), so non-loopback HTTP can never reach the
		// handshake. Safe to flip true → false unconditionally.
		if v {
			cu["allowInsecureAuth"] = false
			changed = true
		}
	}

	if !changed {
		return false, nil
	}

	outBytes, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return false, fmt.Errorf("marshal openclaw.json: %w", err)
	}
	if err := os.WriteFile(configPath, outBytes, 0600); err != nil {
		return false, fmt.Errorf("write openclaw.json: %w", err)
	}
	slog.Info("tightened controlUi config in openclaw.json", "component", "onboarding")
	return true, nil
}

// ensureMessagesQueueConfig pins messages.queue.mode to "steer" so concurrent
// messages (sensing drains, voice + Telegram interleave) get batched into the
// active turn at the next model boundary instead of spawning serialized
// followup turns. The os server has multiple producers (sensing handler, voice, web
// chat, Telegram) feeding agent:main:main; legacy "queue" mode runs each as
// its own turn, missing batch opportunities the steer path can collapse.
//
// Trade-offs are tracked in issue #48003 (steer fallback to followup on Pi
// main session via KeyedAsyncQueue) and the ReplyRunAlreadyActive race seen
// on 5.2 — verify on 5.7+ before relying on steer batching savings.
//
// Always overwrites — the os server owns this config knob; an operator who flips it
// to "queue" will see the os server correct on the next boot.
func (s *OpenclawService) ensureMessagesQueueConfig() (bool, error) {
	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	configBytes, err := os.ReadFile(configPath)
	if err != nil {
		return false, fmt.Errorf("read openclaw.json: %w", err)
	}
	var configData map[string]interface{}
	if err := json.Unmarshal(configBytes, &configData); err != nil {
		return false, fmt.Errorf("parse openclaw.json: %w", err)
	}

	messages, _ := configData["messages"].(map[string]interface{})
	if messages == nil {
		messages = map[string]interface{}{}
		configData["messages"] = messages
	}
	queue, _ := messages["queue"].(map[string]interface{})
	if queue == nil {
		queue = map[string]interface{}{}
		messages["queue"] = queue
	}
	if v, _ := queue["mode"].(string); v == "steer" {
		return false, nil
	}
	queue["mode"] = "steer"

	outBytes, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return false, fmt.Errorf("marshal openclaw.json: %w", err)
	}
	if err := os.WriteFile(configPath, outBytes, 0600); err != nil {
		return false, fmt.Errorf("write openclaw.json: %w", err)
	}
	slog.Info("pinned messages.queue.mode=steer in openclaw.json", "component", "onboarding")
	return true, nil
}

// downloadFile fetches url and writes it to dst. Returns true if the file content changed.
func downloadFile(url, dst string) (bool, error) {
	client := &http.Client{Timeout: 30 * time.Second}
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return false, err
	}
	req.Header.Set("Cache-Control", "no-cache")
	resp, err := client.Do(req)
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return false, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	newData, err := io.ReadAll(resp.Body)
	if err != nil {
		return false, err
	}
	existing, err := os.ReadFile(dst)
	if err == nil && string(existing) == string(newData) {
		return false, nil
	}
	if err := os.WriteFile(dst, newData, 0644); err != nil {
		return false, err
	}
	return true, nil
}

// seedFileIfAbsent writes the embedded file to dst only if dst does not already exist.
// Used for living documents (e.g. KNOWLEDGE.md) that accumulate data over time.
func seedFileIfAbsent(efs embed.FS, src, dst string) {
	if _, err := os.Stat(dst); err == nil {
		return // already exists, never overwrite
	}
	data, err := efs.ReadFile(src)
	if err != nil {
		slog.Error("read embedded file failed", "component", "onboarding", "src", src, "error", err)
		return
	}
	if err := os.WriteFile(dst, data, 0644); err != nil {
		slog.Error("write file failed", "component", "onboarding", "dst", dst, "error", err)
		return
	}
	slog.Info("seeded file (initial)", "component", "onboarding", "file", filepath.Base(dst))
}

// seedFile writes the embedded file to dst. Returns true if the file content changed.
func seedFile(efs embed.FS, src, dst string) bool {
	data, err := efs.ReadFile(src)
	if err != nil {
		slog.Error("read embedded file failed", "component", "onboarding", "src", src, "error", err)
		return false
	}
	existing, err := os.ReadFile(dst)
	if err == nil && string(existing) == string(data) {
		return false
	}
	if err := os.WriteFile(dst, data, 0644); err != nil {
		slog.Error("write file failed", "component", "onboarding", "dst", dst, "error", err)
		return false
	}
	slog.Info("seeded file", "component", "onboarding", "file", filepath.Base(dst))
	return true
}

// ensureGatewayToken generates and persists gateway.auth.token in openclaw.json when
// the field is absent. Covers the switch-from-another-runtime path: SetupAgent (the
// only prior source of token generation) is never called when switching TO openclaw
// from hermes; EnsureOnboarding is. Without a token, readGatewayToken() errors on
// every WS connect-challenge and the gateway disconnects in a tight retry loop.
// Returns true if the file was modified (caller triggers a gateway restart).
func (s *OpenclawService) ensureGatewayToken() (bool, error) {
	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	configBytes, err := os.ReadFile(configPath)
	if err != nil {
		return false, fmt.Errorf("read openclaw.json: %w", err)
	}
	var configData map[string]interface{}
	if err := json.Unmarshal(configBytes, &configData); err != nil {
		return false, fmt.Errorf("parse openclaw.json: %w", err)
	}

	gatewayMap := ensureMap(configData, "gateway")
	gatewayAuthMap := ensureMap(gatewayMap, "auth")
	if strings.TrimSpace(getStringValue(gatewayAuthMap, "token")) != "" {
		return false, nil
	}

	token, err := generateGatewayToken()
	if err != nil {
		return false, fmt.Errorf("generate gateway token: %w", err)
	}
	gatewayAuthMap["token"] = token
	if _, ok := gatewayAuthMap["mode"]; !ok {
		gatewayAuthMap["mode"] = "token"
	}
	gatewayMap["auth"] = gatewayAuthMap
	configData["gateway"] = gatewayMap

	outBytes, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return false, fmt.Errorf("marshal openclaw.json: %w", err)
	}
	if err := os.WriteFile(configPath, outBytes, 0600); err != nil {
		return false, fmt.Errorf("write openclaw.json: %w", err)
	}
	slog.Info("seeded gateway auth token in openclaw.json", "component", "onboarding")
	return true, nil
}

// ensureProviderConfig syncs models.providers.autonomous.{apiKey,baseUrl} in
// openclaw.json with the current config.json values. SetupAgent writes this
// during the wizard; EnsureOnboarding does not — so switching TO openclaw from
// another runtime (e.g. hermes) leaves the provider without an API key, causing
// every agent turn to fail with "No API key found for provider".
// Best-effort model fetch: if the API is reachable we refresh the full model
// catalog alongside the key; if not, we update only the auth fields so the
// gateway can at least authenticate on the next turn.
// Returns true if the file was modified (caller triggers a gateway restart).
func (s *OpenclawService) ensureProviderConfig() (bool, error) {
	if s.config.LLMAPIKey == "" {
		return false, nil // device not fully configured yet
	}

	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	configBytes, err := os.ReadFile(configPath)
	if err != nil {
		return false, fmt.Errorf("read openclaw.json: %w", err)
	}
	var configData map[string]interface{}
	if err := json.Unmarshal(configBytes, &configData); err != nil {
		return false, fmt.Errorf("parse openclaw.json: %w", err)
	}

	modelsMap := ensureMap(configData, "models")
	providersMap := ensureMap(modelsMap, "providers")
	autonomousMap, _ := providersMap[customProviderName].(map[string]interface{})

	currentKey, _ := autonomousMap["apiKey"].(string)
	currentBaseURL, _ := autonomousMap["baseUrl"].(string)
	if currentKey == s.config.LLMAPIKey && currentBaseURL == s.config.LLMBaseURL {
		return false, nil
	}

	// Try full catalog refresh; fall back to hardcoded defaultModels (same as
	// SetupAgent) so the provider entry is always complete even when offline.
	// A partial entry (apiKey only, no models) would still fail on the next turn.
	modelsResp, byo, err := resolveModels(context.Background(), s.config.LLMBaseURL, s.config.LLMAPIKey)
	if err != nil {
		slog.Warn("ensureProviderConfig: model fetch failed, using hardcoded fallback",
			"component", "onboarding", "byo", byo, "error", err)
		modelsResp = &domain.LLMModelsListResponse{Models: defaultModels}
	}
	entries := make([]any, 0, len(modelsResp.Models))
	for _, m := range modelsResp.Models {
		if s.config.LLMThinkingDisabled() {
			m.Reasoning = false
		}
		entries = append(entries, openclawModelToProviderEntry(m))
	}
	autonomousMap = map[string]interface{}{
		"baseUrl": s.config.LLMBaseURL,
		"api":     resolveAutonomousAPI(modelsResp.API),
		"apiKey":  s.config.LLMAPIKey,
		"models":  entries,
	}

	modelsMap["mode"] = "merge"
	providersMap[customProviderName] = autonomousMap
	configData["models"] = modelsMap

	outBytes, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return false, fmt.Errorf("marshal openclaw.json: %w", err)
	}
	if err := os.WriteFile(configPath, outBytes, 0600); err != nil {
		return false, fmt.Errorf("write openclaw.json: %w", err)
	}
	slog.Info("synced autonomous provider config in openclaw.json", "component", "onboarding")
	return true, nil
}

// ensureAgentDefaults patches agents.defaults in openclaw.json with performance config.
// Returns true if the file was modified.
func (s *OpenclawService) ensureAgentDefaults() (bool, error) {
	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	configBytes, err := os.ReadFile(configPath)
	if err != nil {
		return false, fmt.Errorf("read openclaw.json: %w", err)
	}
	var configData map[string]interface{}
	if err := json.Unmarshal(configBytes, &configData); err != nil {
		return false, fmt.Errorf("parse openclaw.json: %w", err)
	}

	agentsMap := ensureMap(configData, "agents")
	defaultsMap := ensureMap(agentsMap, "defaults")

	changed := false

	// Compaction
	// reserveTokensFloor=5000: keep safeguard only as a last-resort guard near
	// the model context limit (~195k for 200k models). Previously 80000, which
	// made OpenClaw fire compact at ~120k actual context — same range the os server's
	// /new RPC trigger fires (chat.history TotalTokens > 80k undercounts ~35k),
	// so the two layers raced and produced the 30-60s compact freeze that
	// /new was supposed to avoid.
	compactionMap := ensureMap(defaultsMap, "compaction")
	if v, _ := compactionMap["reserveTokensFloor"].(float64); v != 5000 {
		compactionMap["reserveTokensFloor"] = 5000
		changed = true
	}
	if v, _ := compactionMap["mode"].(string); v != "safeguard" {
		compactionMap["mode"] = "safeguard"
		changed = true
	}

	// Bootstrap limits
	if v, _ := defaultsMap["bootstrapMaxChars"].(float64); v != 12000 {
		defaultsMap["bootstrapMaxChars"] = 12000
		changed = true
	}
	if v, _ := defaultsMap["bootstrapTotalMaxChars"].(float64); v != 30000 {
		defaultsMap["bootstrapTotalMaxChars"] = 30000
		changed = true
	}

	// /think default — favor low latency over deep reasoning for voice turns.
	// Per-message override (`/think medium`) still wins; this only sets the
	// fallback when neither session nor inline directive specify a level.
	if v, _ := defaultsMap["thinkingDefault"].(string); v != "low" {
		defaultsMap["thinkingDefault"] = "low"
		changed = true
	}

	// Cache retention (Claude only) + /fast default = on (priority tier) on all known models.
	// `fastMode=true` maps to provider-specific priority routing — `service_tier=priority`
	// on OpenAI/Codex; no-op on providers that don't expose a priority tier.
	modelsMap := ensureMap(defaultsMap, "models")
	// Autonomous-backed list comes from the live API (single source of truth);
	// non-autonomous entries (e.g. openai-codex) are appended manually because
	// they are not driven by ModelsAPIURL. Fail-soft on API failure: skip the
	// autonomous portion this boot, preserve existing on-disk tuning, retry
	// next boot.
	var knownModels []string
	if resp, _, err := resolveModels(context.Background(), s.config.LLMBaseURL, s.config.LLMAPIKey); err != nil {
		slog.Warn("ensureAgentDefaults: fetch models failed, skipping",
			"component", "onboarding", "err", err)
	} else {
		for _, m := range resp.Models {
			knownModels = append(knownModels, agentModelKey(m))
		}
	}
	knownModels = append(knownModels, "openai-codex/gpt-5.5")
	for _, modelKey := range knownModels {
		m, ok := modelsMap[modelKey].(map[string]interface{})
		if !ok {
			m = map[string]interface{}{}
			modelsMap[modelKey] = m
			changed = true
		}
		params := ensureMap(m, "params")
		// Contains (not HasPrefix) so "{provider}/claude-..." also matches.
		if strings.Contains(modelKey, "claude-") {
			if v, _ := params["cacheRetention"].(string); v != "short" {
				params["cacheRetention"] = "short"
				changed = true
			}
		}
		if v, _ := params["fastMode"].(bool); !v {
			params["fastMode"] = true
			changed = true
		}
		m["params"] = params
		modelsMap[modelKey] = m
	}

	// Sync reasoning field on all provider model entries with current disable_thinking config.
	// Ensures manual edits to config.json take effect on next boot without needing API call.
	disableThinking := s.config.LLMThinkingDisabled()
	wantReasoning := !disableThinking
	if topModels, ok := configData["models"].(map[string]interface{}); ok {
		if providers, ok := topModels["providers"].(map[string]interface{}); ok {
			for _, provider := range providers {
				if p, ok := provider.(map[string]interface{}); ok {
					if modelsList, ok := p["models"].([]interface{}); ok {
						for _, entry := range modelsList {
							if m, ok := entry.(map[string]interface{}); ok {
								if curr, _ := m["reasoning"].(bool); curr != wantReasoning {
									m["reasoning"] = wantReasoning
									changed = true
								}
							}
						}
					}
				}
			}
		}
	}

	if !changed {
		return false, nil
	}

	outBytes, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return false, fmt.Errorf("marshal openclaw.json: %w", err)
	}
	if err := os.WriteFile(configPath, outBytes, 0600); err != nil {
		return false, fmt.Errorf("write openclaw.json: %w", err)
	}
	slog.Info("patched agent defaults in openclaw.json", "component", "onboarding")
	return true, nil
}
