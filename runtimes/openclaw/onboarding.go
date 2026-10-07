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

	// bootstrapMaxChars caps each workspace file (SOUL.md, AGENTS.md, …) in the
	// prompt. OpenClaw keeps the first 75% and last 25% of a longer file and
	// silently drops the middle, so a device SOUL must stay well under it.
	// 24k fits the 18.6k lamp soul with room for OS markers and owner edits.
	bootstrapMaxChars = 24000
	// bootstrapTotalMaxChars caps all workspace files together. The lamp soul
	// plus managed AGENTS/HEARTBEAT blocks total about 28k; 48k leaves about
	// 20k for other bootstrap files and owner content, not a latency guarantee.
	bootstrapTotalMaxChars = 48000

	// heartbeatMDBlock is the OS-managed block in workspace/HEARTBEAT.md, run on the gateway's periodic heartbeat poll (~every 30 min while the device is on).
	heartbeatMDBlock = `<!-- OS DO NOT REMOVE -->
**Knowledge synthesis (catch-up — do NOT wait for a fixed hour):** Compare the days that have a ` + "`memory/YYYY-MM-DD.md`" + ` against the ` + "`## YYYY-MM-DD`" + ` headers already in ` + "`KNOWLEDGE.md`" + `. For every day BEFORE today that has a memory file but no header, distil that day now — oldest first, each under its own ` + "`## YYYY-MM-DD`" + ` header. Also do today, but only once it is >= 21:00. Only write new learnings — never repeat what is already there. Nothing missing → skip this step. This device is often switched off in the evening, so a fixed hour may simply never arrive; clearing the backlog on whatever heartbeat comes next is what keeps a day from being lost.

**Keep ` + "`KNOWLEDGE.md`" + ` from growing without bound (same pass).** A dated ` + "`## YYYY-MM-DD`" + ` block is raw material, not the archive — the distilled sections at the top are. Keep at most the **14 most recent** dated blocks. For anything older: fold what is still true into the matching top section (Hardware / Users / Skills & APIs / Mistakes Made), then DELETE the dated block. Nothing of value is lost — it was already distilled, and the raw day survives in ` + "`memory/YYYY-MM-DD.md`" + `. Without this the file grows by a section every active day and eventually costs more to read than it is worth.

**People sync (same pass, right after the above):** ` + "`KNOWLEDGE.md`" + ` is yours alone — the OS never loads it. ` + "`USER.md`" + ` IS loaded, into your system prompt, on every single turn. So anything you learned about a PERSON has to reach ` + "`USER.md`" + ` or you will not have it tomorrow. Carry it across:

- Write ONE bullet per person under a ` + "`## Users`" + ` heading in ` + "`USER.md`" + `, shaped ` + "`- **<label> (friend)** — call: …; notes: …`" + ` — where ` + "`<label>`" + ` is their ENROLLMENT LABEL exactly as it appears in ` + "`[context: current_user=…]`" + `, lowercase. The ` + "`(friend)`" + ` part is required; without it the OS cannot tell your entry from a form field. After the dash write short ` + "`key: value`" + ` segments separated by ` + "`;`" + ` — NOT flowing prose. Only segments that change how you help them.
- ` + "`call:`" + ` comes FIRST and only when they have TOLD you what to be called. Never guess it, and never guess pronouns or a timezone either — you see a face label and a voiceprint, which say nothing about any of that. If they have not said, omit the segment entirely and just use their label.
- **Only write what you observed about THAT person.** Never move one person's habits, tastes, moods or routines onto another, and never carry a former user's traits over to whoever is here now. Two people at one desk are two entries, never a merged one. If you cannot tell whose a behaviour was, leave it out.
- **Never delete a PERSON's entry.** Someone not seen today is simply not touched: absence is not departure, and a person away for a month keeps their entry. Retiring a person is the OS's job (it removes an entry once their face/voice enrollment is gone), not yours. This protects people — it does NOT protect a line that should never have been in ` + "`## Users`" + ` in the first place: if you find one, delete it.
- **Keep each entry under ~400 characters.** Segments are dense, so that is plenty. This file is loaded into your prompt on EVERY turn, so bloat is billed on all of them; and when it overflows the cap it is cut from the END, which is where ` + "`## Users`" + ` lives. Rewrite an entry to stay short rather than appending to it.
- **Strangers get NO entry — and remove any you find.** ` + "`## Users`" + ` is for people the device knows by enrollment. A passing face has no label to key on and nothing durable to remember; note desk traffic in ` + "`KNOWLEDGE.md`" + ` instead. An entry like ` + "`**stranger_4**`" + ` or a lumped ` + "`**stranger_2/3/4/…**`" + ` is not a person: delete it. The OS cannot clean these up for you — its pruner only recognises a proper ` + "`**<label> (role)**`" + ` entry.
- Do NOT fill ` + "`**Name:**`" + ` or the other single-value fields at the top. This device can have several people; who is present right now always comes from ` + "`[context: current_user=…]`" + ` on the turn, never from that field.

**Ending the heartbeat:** this pass is housekeeping, not a conversation. When it is done — including when there was nothing to do — reply with exactly ` + "`NO_REPLY`" + ` and nothing else. Never end with an empty reply: OpenClaw treats an empty heartbeat as a failure and posts "Agent couldn't generate a response" to the owner's chat.

---`
)

// supportedSkills resolves this device's capabilities from ROBOT.md and filters the platform skill catalog (skills.Catalog) to what this device can run.
func (s *OpenclawService) supportedSkills() []string {
	return skills.Supported(device.Capabilities(s.config.DeviceTypeOrDefault()))
}

// EnsureOnboarding seeds SOUL.md, downloads skills, and injects the mandatory block into workspace/AGENTS.md so OpenClaw scans the skills directory.
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

	if modified, err := s.ensureSoulMDBlock(); err != nil {
		slog.Error("ensure SOUL.md block failed", "component", "onboarding", "error", err)
	} else if modified {
		needRestart = true
	}

	skillsDir := filepath.Join(workspace, "skills")
	if err := os.MkdirAll(skillsDir, 0755); err != nil {
		return fmt.Errorf("create skills dir: %w", err)
	}
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

	if hooksBase := s.hooksBaseURL(); hooksBase == "" {
		slog.Info("hooks download skipped: no ota_metadata_url configured", "component", "onboarding")
	} else {
		hooksDir := filepath.Join(workspace, "hooks")
		if err := os.MkdirAll(hooksDir, 0755); err != nil {
			return fmt.Errorf("create hooks dir: %w", err)
		}
		hookFiles := []string{"HOOK.md", "handler.ts"}
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

	seedFileIfAbsent(knowledgeFS, "resources/KNOWLEDGE.md", filepath.Join(workspace, "KNOWLEDGE.md"))

	if modified, err := s.ensureAgentsMDBlock(); err != nil {
		slog.Error("ensure AGENTS.md block failed", "component", "onboarding", "error", err)
	} else if modified {
		needRestart = true
	}

	if modified, err := s.ensureHeartbeatMDBlock(); err != nil {
		slog.Error("ensure HEARTBEAT.md block failed", "component", "onboarding", "error", err)
	} else if modified {
		needRestart = true
	}

	if hooksAdded, err := s.ensureHooksRegistered(skills.SupportedHooks(deviceCaps)); err != nil {
		slog.Error("ensure hooks registered failed", "component", "onboarding", "error", err)
	} else if hooksAdded {
		needRestart = true
	}

	if loggingAdded, err := s.ensureLoggingConfig(); err != nil {
		slog.Error("ensure logging config failed", "component", "onboarding", "error", err)
	} else if loggingAdded {
		needRestart = true
	}

	// Ensure gateway auth token — generated only in SetupAgent but must also exist when switching TO openclaw from another runtime (e.g. hermes).
	if tokenSeeded, err := s.ensureGatewayToken(); err != nil {
		slog.Error("ensure gateway token failed", "component", "onboarding", "error", err)
	} else if tokenSeeded {
		needRestart = true
	}

	if providerSynced, err := s.ensureProviderConfig(); err != nil {
		slog.Error("ensure provider config failed", "component", "onboarding", "error", err)
	} else if providerSynced {
		needRestart = true
	}

	if defaultsPatched, err := s.ensureAgentDefaults(); err != nil {
		slog.Error("ensure agent defaults failed", "component", "onboarding", "error", err)
	} else if defaultsPatched {
		needRestart = true
	}

	if controlUIAdded, err := s.ensureControlUIConfig(); err != nil {
		slog.Error("ensure controlUi config failed", "component", "onboarding", "error", err)
	} else if controlUIAdded {
		needRestart = true
	}

	// Pin messages.queue.mode=steer so concurrent producers batch into the active turn,
	// and drop the "auto" reply prefix.
	if queueAdded, err := s.ensureMessagesQueueConfig(); err != nil {
		slog.Error("ensure messages.queue config failed", "component", "onboarding", "error", err)
	} else if queueAdded {
		needRestart = true
	}

	if needRestart {
		slog.Info("restarting OpenClaw to pick up changes", "component", "onboarding")
		if err := restartOpenclawGateway(); err != nil {
			return fmt.Errorf("restart openclaw after onboarding: %w", err)
		}
		slog.Info("OpenClaw restarted successfully", "component", "onboarding")
	}

	s.notifySkillChanges(changedSkills)

	return nil
}

// ensureHooksRegistered registers supported hooks and removes published hooks this device does not support.
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
	// Remove published hooks this device does not support.
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
func (s *OpenclawService) ensureAgentsMDBlock() (bool, error) {
	agentsFile := filepath.Join(s.config.OpenclawConfigDir, "workspace", "AGENTS.md")

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

	if strings.Contains(text, agentsMDBlock) {
		slog.Debug("AGENTS.md already has current mandatory block, skipping", "component", "onboarding")
		return false, nil
	}

	if strings.Contains(text, osMandatoryMarker) {
		text = stripMarkedBlock(text)
	} else {
		text = stripLegacyMandatoryBlock(text)
	}

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

// devicesDir returns the root that holds per-device profile folders (robots/<type>/{DEVICE,SOUL}.md).
func devicesDir() string {
	if d := strings.TrimSpace(os.Getenv("DEVICES_DIR")); d != "" {
		return d
	}
	return "/opt/devices"
}

// deviceSoulCore resolves the soul text from robots/<type>/ROBOT.md soul_ref; hasSoul=false means keep the runtime default.
func (s *OpenclawService) deviceSoulCore() (content []byte, hasSoul bool, err error) {
	devType := s.config.DeviceTypeOrDefault()
	ref := device.SoulRef(devType)
	if ref == "" {
		return nil, false, nil
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

// isDefaultSoulHeading reports whether trimmed starts with a managed soul template heading (never kept as owner content).
func isDefaultSoulHeading(trimmed string) bool {
	return strings.HasPrefix(trimmed, "# Soul") || strings.HasPrefix(trimmed, "# SOUL.md")
}

// ensureSoulMDBlock wraps this device's soul as a marker-delimited core block at the top of workspace/SOUL.md.
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

	// Fast path: the block is already present AND nothing but owner content sits below it.
	if idx := strings.Index(text, soulMDBlock); idx >= 0 {
		below := strings.TrimLeft(text[idx+len(soulMDBlock):], " \t\r\n")
		if !isDefaultSoulHeading(below) {
			return false, nil
		}
	}

	if strings.Contains(text, osMandatoryMarker) {
		text = stripMarkedBlock(text)
	}

	// Discard any managed default soul left in the remaining text so it is not preserved as fake "owner edits" and duplicated below the device block.
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
func (s *OpenclawService) ensureHeartbeatMDBlock() (bool, error) {
	heartbeatFile := filepath.Join(s.config.OpenclawConfigDir, "workspace", "HEARTBEAT.md")

	content, err := os.ReadFile(heartbeatFile)
	if err != nil && !os.IsNotExist(err) {
		return false, fmt.Errorf("read HEARTBEAT.md: %w", err)
	}

	text := string(content)

	if strings.Contains(text, heartbeatMDBlock) {
		slog.Debug("HEARTBEAT.md already has current mandatory block, skipping", "component", "onboarding")
		return false, nil
	}

	if strings.Contains(text, osMandatoryMarker) {
		text = stripMarkedBlock(text)
	}

	output := heartbeatMDBlock + "\n\n" + text
	if err := os.WriteFile(heartbeatFile, []byte(output), 0644); err != nil {
		return false, fmt.Errorf("write HEARTBEAT.md: %w", err)
	}

	slog.Info("injected mandatory block into HEARTBEAT.md", "component", "onboarding", "path", heartbeatFile)
	return true, nil
}

// stripMarkedBlock removes the block between the marker (<!-- OS DO NOT REMOVE -->) and the next --- separator.
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

// stripLegacyMandatoryBlock removes the old MANDATORY block that was injected before any marker (<!-- OS DO NOT REMOVE -->) was introduced.
func stripLegacyMandatoryBlock(text string) string {
	lines := strings.Split(text, "\n")
	var cleaned []string
	skip := false
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		if !skip && strings.HasPrefix(trimmed, "**MANDATORY:**") {
			skip = true
			continue
		}
		if skip {
			if trimmed == "" || trimmed == "---" {
				skip = false
				cleaned = append(cleaned, line)
			}
			continue
		}
		cleaned = append(cleaned, line)
	}
	return strings.Join(cleaned, "\n")
}

// ensureLoggingConfig adds the logging block to openclaw.json if it is missing.
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

// ensureControlUIConfig pins gateway.controlUi to local-only defaults so the Control UI handshake only accepts loopback origins on plain HTTP.
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
		// Loopback HTTP works without this flag — nginx /gw/ already restricts to loopback peers (F6), so non-loopback HTTP can never reach the handshake.
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

// ensureMessagesQueueConfig pins messages.queue.mode to "steer" and drops the
// "auto" reply prefix older setups wrote (it prints "[main]" before replies).
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
	changed := dropAutoResponsePrefix(configData)
	if v, _ := queue["mode"].(string); v != "steer" {
		queue["mode"] = "steer"
		changed = true
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
	slog.Info("updated messages config in openclaw.json", "component", "onboarding")
	return true, nil
}

// dropAutoResponsePrefix removes responsePrefix "auto" from messages and from
// every channel and channel account (OpenClaw doctor copies the global value
// down). Custom prefixes are left alone. Reports whether anything changed.
func dropAutoResponsePrefix(configData map[string]interface{}) bool {
	changed := false
	drop := func(m map[string]interface{}) {
		if v, _ := m["responsePrefix"].(string); v == "auto" {
			delete(m, "responsePrefix")
			changed = true
		}
	}
	if messages, ok := configData["messages"].(map[string]interface{}); ok {
		drop(messages)
	}
	channels, _ := configData["channels"].(map[string]interface{})
	for _, ch := range channels {
		chMap, ok := ch.(map[string]interface{})
		if !ok {
			continue
		}
		drop(chMap)
		accounts, _ := chMap["accounts"].(map[string]interface{})
		for _, acc := range accounts {
			if accMap, ok := acc.(map[string]interface{}); ok {
				drop(accMap)
			}
		}
	}
	return changed
}

// downloadFile fetches url and writes it to dst.
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

// seedFile writes the embedded file to dst.
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

// ensureGatewayToken generates and persists gateway.auth.token in openclaw.json when the field is absent.
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

// ensureProviderConfig syncs models.providers.autonomous.{apiKey,baseUrl} in openclaw.json with the current config.json values.
func (s *OpenclawService) ensureProviderConfig() (bool, error) {
	if s.config.LLMRuntimeManaged() || s.config.LLMAPIKey == "" {
		return false, nil
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
// pinSilentHeartbeat turns the recurring heartbeat off and keeps any
// event-driven wake silent. On OpenClaw 2026.9 an empty heartbeat reply is
// retried as a "visible-answer continuation"; the model then read the main chat
// via sessions_history and messaged the owner on Telegram with the `message`
// tool every 30 min, which neither target "none" nor an isolated session stops.
// Reports whether anything changed.
func pinSilentHeartbeat(defaultsMap map[string]any) bool {
	changed := false
	heartbeatMap := ensureMap(defaultsMap, "heartbeat")
	if v, _ := heartbeatMap["every"].(string); v != "0m" {
		heartbeatMap["every"] = "0m"
		changed = true
	}
	if v, _ := heartbeatMap["target"].(string); v != "none" {
		heartbeatMap["target"] = "none"
		changed = true
	}
	if v, _ := heartbeatMap["isolatedSession"].(bool); !v {
		heartbeatMap["isolatedSession"] = true
		changed = true
	}
	return changed
}

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

	compactionMap := ensureMap(defaultsMap, "compaction")
	if v, _ := compactionMap["reserveTokensFloor"].(float64); v != 5000 {
		compactionMap["reserveTokensFloor"] = 5000
		changed = true
	}
	if v, _ := compactionMap["mode"].(string); v != "safeguard" {
		compactionMap["mode"] = "safeguard"
		changed = true
	}

	if v, _ := defaultsMap["bootstrapMaxChars"].(float64); v != bootstrapMaxChars {
		defaultsMap["bootstrapMaxChars"] = bootstrapMaxChars
		changed = true
	}
	if v, _ := defaultsMap["bootstrapTotalMaxChars"].(float64); v != bootstrapTotalMaxChars {
		defaultsMap["bootstrapTotalMaxChars"] = bootstrapTotalMaxChars
		changed = true
	}

	if pinSilentHeartbeat(defaultsMap) {
		changed = true
	}

	if !s.config.LLMRuntimeManaged() {
		if v, _ := defaultsMap["thinkingDefault"].(string); v != "low" {
			defaultsMap["thinkingDefault"] = "low"
			changed = true
		}

		modelsMap := ensureMap(defaultsMap, "models")
		// Autonomous entries come from the live API; non-autonomous ones (e.g. openai-codex) are appended here.
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
