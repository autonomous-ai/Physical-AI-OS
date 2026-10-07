package codex

import (
	"embed"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// knowledgeFS holds the KNOWLEDGE.md skeleton, embedded so a fresh codex-only
// device (one that never ran openclaw, so presync.sh §1 had nothing to copy) still
// gets the living-learnings doc the AGENTS.md block tells the agent to read.
//
//go:embed resources/KNOWLEDGE.md
var knowledgeFS embed.FS

// agentsFS holds the base workspace AGENTS.md.
//
//go:embed resources/AGENTS.md
var agentsFS embed.FS

// Onboarding (Codex).
// Those run during the switch-runtime flow (and presync again from EnsureOnboarding below), NOT
// here.

const (
	// osMandatoryMarker delimits the OS-managed block so it can be stripped +
	// re-injected cleanly on update.
	osMandatoryMarker = "<!-- OS DO NOT REMOVE -->"

	// personaInlineStart/personaInlineEnd delimit the persona block inlined at the
	// very top of workspace/AGENTS.md (see ensurePersonaInlineBlock for why the
	// persona must live inside AGENTS.md for codex).
	personaInlineStart = "<!-- OS PERSONA INLINE — DO NOT EDIT (generated from SOUL.md + IDENTITY.md) -->"
	personaInlineEnd   = "<!-- /OS PERSONA INLINE -->"

	// personaInlineSoulCap caps the inlined SOUL.md bytes so AGENTS.md stays
	// under codex's 32KiB project-doc cap (AGENTS.md is the only file codex
	// auto-loads, and it truncates past that limit).
	personaInlineSoulCap = 20_000
)

// Path-derived values: resolved from codexHome (CODEX_HOME) at process start,
// so the block text and the dirs it names can never disagree.
var (
	// codexWorkspaceDir is Codex's workspace ($CODEX_HOME/workspace).
	codexWorkspaceDir = codexHome + "/workspace"

	// codexSkillsDir is the on-device skill store.
	codexSkillsDir = codexHome + "/skills"

	// codexUserAgentsMD is codex's GLOBAL user-instructions file
	// ($CODEX_HOME/AGENTS.md).
	codexUserAgentsMD = codexHome + "/AGENTS.md"

	// agentsMDBlock is the OS-managed block injected into workspace/AGENTS.md.
	agentsMDBlock = `<!-- OS DO NOT REMOVE -->
**MANDATORY (skills):** Your device skills live at ` + "`" + codexSkillsDir + "/<name>/SKILL.md`" + ` (absolute path — reachable from any cwd, including coding sessions in another folder). Before any skill-driven action, determine the skill scope without doing broad filesystem scans. For ordinary chat, simple Q&A, or meta discussion with no action/event/hardware behavior and no connected-service data, do NOT read a SKILL.md — answer normally. A question ABOUT a linked third-party service (see Connectors) is NOT ordinary chat: it needs the skill even when phrased as a simple question.
  - If the message contains ` + "`[skills: a, b, c]`" + `, treat it as an authoritative whitelist — read ONLY those ` + "`" + codexSkillsDir + "/<name>/SKILL.md`" + ` files. Do NOT scan other skill directories "just in case".
  - If no ` + "`[skills:]`" + ` hint is present and the user asks for a concrete action, hardware behavior, sensing/activity/emotion handling, or a specialized workflow, choose the single most specific matching skill, then read only that SKILL.md.
  - If multiple skills plausibly match, choose the most specific one. If none clearly match, do not read any SKILL.md and answer normally.
  - Never fall back to reading every skill directory. Broad scans are slow and usually reduce quality.

Follow the instructions in whichever file you read.

**Version check:** ` + "`os-server --version`" + ` (OS), ` + "`codex --version`" + ` (Codex), ` + "`curl -s http://127.0.0.1:5001/version`" + ` (HAL).

**Session Startup — also read:** ` + "`KNOWLEDGE.md`" + ` (accumulated learnings) in addition to the steps listed below.

**Priority: Skills > Knowledge > memory/*.md > History.** SKILL.md beats EVERYTHING (KNOWLEDGE.md, memory/*.md decisions, history). If memory says NO_REPLY but SKILL says nudge, follow SKILL. KNOWLEDGE.md is your personal observations — it can be wrong. Skills are the source of truth maintained by the developer. If you notice a conflict, update KNOWLEDGE.md to match the skill, not the other way around.

**Memory:** After each turn on any channel (voice, Telegram, or others) that contains something worth remembering (decisions, bugs, insights, new preferences), write it immediately to ` + "`memory/YYYY-MM-DD.md`" + `. Do not wait for heartbeat — context may be dropped before then.

**Silence = the literal token NO_REPLY.** When a skill says not to speak, output exactly NO_REPLY and nothing else. Never narrate the decision ("Sound event, no user message. Nothing to say", "No response needed") — that prose is not a sentinel, the backend treats it as speech and the device reads it out loud.

**Memory writes — DESCRIBE, never PRESCRIBE.** Before writing any "decision/rule" to memory/*.md or KNOWLEDGE.md, re-read the relevant SKILL.md. Blanket forms like "X → always Y" / "X → NO_REPLY for all" are frequency disguised as rule — write what happened with conditions, not a blanket ban.

**Don't duplicate JSONL.** Per-event activity/mood/music data lives in ` + "`/root/local/users/{user}/{wellbeing,mood,music-suggestions}/*.jsonl`" + ` and ` + "`/root/local/flow_events_*.jsonl`" + `. If ` + "`cat`" + ` of a JSONL can answer it, DO NOT write to memory. Memory is for cross-day insights only.

**Mood awareness (MANDATORY): Follow Mood skill.**

**User priority (MANDATORY):** When the turn batches multiple messages, ` + "`[user] ...`" + ` messages are direct human input (voice command or typed chat). Always answer the most recent ` + "`[user]`" + ` message first; treat ` + "`[activity]`" + ` / ` + "`[emotion]`" + ` / ` + "`[speech_emotion]`" + ` / ` + "`[ambient]`" + ` / ` + "`[sensing:*]`" + ` as supporting context, never as the primary prompt. A user who asked a question must get their answer even when sensing events queued alongside look more interesting.

**Connectors (MANDATORY):** The user links third-party services (Gmail, Google Calendar, Google Drive, Notion, Figma, Asana, Linear, GitHub, Ahrefs, …) in the app, and the OS writes their credentials to this device at ` + "`/root/.openclaw/workspace/configs/<code>_access_tokens.json`" + `. ALWAYS use the ` + "`connectors`" + ` skill to answer or act on ANY of them — including "is my gmail connected?", "what's on my calendar", "list my events", "check my email". Read ` + "`" + codexSkillsDir + "/connectors/SKILL.md`" + ` and follow it. This holds in EVERY session — including a coding session started in ` + "`/root`" + `, ` + "`/root/myapp`" + `, or any other folder: the connectors are a property of the DEVICE, not of the folder you happen to be in. Never write your own script (` + "`send_email.py`" + `, gcalcli, …) to reach a service a connector already covers.
  - ` + "`config.toml`" + `/` + "`mcp_servers`" + ` is NOT the connector list. Gmail/Calendar/Drive are token-based and have NO MCP server, so NEVER conclude a service is unconnected because it is missing from the MCP server list — check the credential files via the skill.
  - Never tell the user to set up an MCP server, OAuth app, or another CLI for these services: the credentials are already on disk. Read them only through the ` + "`connectors`" + ` skill, which knows how to keep the secrets safe.

---`

	// heartbeatMDBlock is the OS-managed knowledge-synthesis block injected at the top
	// of workspace/HEARTBEAT.md. Keep byte-identical across runtimes: it is matched verbatim.
	heartbeatMDBlock = `<!-- OS DO NOT REMOVE -->
**Knowledge synthesis (catch-up — do NOT wait for a fixed hour):** Compare the days that have a ` + "`memory/YYYY-MM-DD.md`" + ` against the ` + "`## YYYY-MM-DD`" + ` headers already in ` + "`KNOWLEDGE.md`" + `. For every day BEFORE today that has a memory file but no header, distil that day now — oldest first, each under its own ` + "`## YYYY-MM-DD`" + ` header. Also do today, but only once it is >= 21:00. Only write new learnings — never repeat what is already there. Nothing missing → skip silently. This device is often switched off in the evening, so a fixed hour may simply never arrive; clearing the backlog on whatever heartbeat comes next is what keeps a day from being lost.

**Keep ` + "`KNOWLEDGE.md`" + ` from growing without bound (same pass).** A dated ` + "`## YYYY-MM-DD`" + ` block is raw material, not the archive — the distilled sections at the top are. Keep at most the **14 most recent** dated blocks. For anything older: fold what is still true into the matching top section (Hardware / Users / Skills & APIs / Mistakes Made), then DELETE the dated block. Nothing of value is lost — it was already distilled, and the raw day survives in ` + "`memory/YYYY-MM-DD.md`" + `. Without this the file grows by a section every active day and eventually costs more to read than it is worth.

**People sync (same pass, right after the above):** ` + "`KNOWLEDGE.md`" + ` is yours alone — the OS never loads it. ` + "`USER.md`" + ` IS loaded, into your system prompt, on every single turn. So anything you learned about a PERSON has to reach ` + "`USER.md`" + ` or you will not have it tomorrow. Carry it across:

- Write ONE bullet per person under a ` + "`## Users`" + ` heading in ` + "`USER.md`" + `, shaped ` + "`- **<label> (friend)** — call: …; notes: …`" + ` — where ` + "`<label>`" + ` is their ENROLLMENT LABEL exactly as it appears in ` + "`[context: current_user=…]`" + `, lowercase. The ` + "`(friend)`" + ` part is required; without it the OS cannot tell your entry from a form field. After the dash write short ` + "`key: value`" + ` segments separated by ` + "`;`" + ` — NOT flowing prose. Only segments that change how you help them.
- ` + "`call:`" + ` comes FIRST and only when they have TOLD you what to be called. Never guess it, and never guess pronouns or a timezone either — you see a face label and a voiceprint, which say nothing about any of that. A title or honorific heard in a voice turn (Mr, Ms, Miss, Mrs, anh, chị…) is not them telling you what to be called — speech recognition often invents one ("…is Lee" heard as "Miss Lee"): write the bare name, and never infer gender from a title, a name, a face or a voice. Keep a title only when they explicitly ask for it ("call me Ms Lee"). If they have not said, omit the segment entirely and just use their label.
- **Only write what you observed about THAT person.** Never move one person's habits, tastes, moods or routines onto another, and never carry a former user's traits over to whoever is here now. Two people at one desk are two entries, never a merged one. If you cannot tell whose a behaviour was, leave it out.
- **Never delete a PERSON's entry.** Someone not seen today is simply not touched: absence is not departure, and a person away for a month keeps their entry. Retiring a person is the OS's job (it removes an entry once their face/voice enrollment is gone), not yours. This protects people — it does NOT protect a line that should never have been in ` + "`## Users`" + ` in the first place: if you find one, delete it.
- **Keep each entry under ~400 characters.** Segments are dense, so that is plenty. This file is loaded into your prompt on EVERY turn, so bloat is billed on all of them; and when it overflows the cap it is cut from the END, which is where ` + "`## Users`" + ` lives. Rewrite an entry to stay short rather than appending to it.
- **Strangers get NO entry — and remove any you find.** ` + "`## Users`" + ` is for people the device knows by enrollment. A passing face has no label to key on and nothing durable to remember; note desk traffic in ` + "`KNOWLEDGE.md`" + ` instead. An entry like ` + "`**stranger_4**`" + ` or a lumped ` + "`**stranger_2/3/4/…**`" + ` is not a person: delete it. The OS cannot clean these up for you — its pruner only recognises a proper ` + "`**<label> (role)**`" + ` entry.
- Do NOT fill ` + "`**Name:**`" + ` or the other single-value fields at the top. This device can have several people; who is present right now always comes from ` + "`[context: current_user=…]`" + ` on the turn, never from that field.

---`

	// userAgentsMDBlock is the OS-managed block in codex's GLOBAL user-instructions
	// file (codexUserAgentsMD = $CODEX_HOME/AGENTS.md), which codex loads in EVERY
	// session regardless of cwd (codex-rs CodexHomeUserInstructionsProvider).
	userAgentsMDBlock = `<!-- OS DO NOT REMOVE -->
**This machine is an Autonomous device.** The facts below hold in EVERY folder and session — they describe the DEVICE, not the directory you are working in.

**Connectors (MANDATORY).** The owner links third-party services (Gmail, Google Calendar, Google Drive, Notion, Figma, Asana, Linear, GitHub, Ahrefs, …) in the Autonomous app, and the OS writes their credentials to ` + "`/root/.openclaw/workspace/configs/<code>_access_tokens.json`" + `. To answer or act on ANY of them — "is my gmail connected?", "what's on my calendar", "send an email to …" — read ` + "`" + codexSkillsDir + "/connectors/SKILL.md`" + ` and follow it.
  - NEVER conclude a service is unconnected because no MCP server or CLI is configured: the token connectors (Gmail/Calendar/Drive) have NO MCP server. Check the credential files via the skill before answering.
  - NEVER install or write your own client for a service a connector already covers (no ` + "`send_email.py`" + `, no gcalcli, no OAuth setup) — the credentials are already on disk.

---`
)

// SetupAgent runs onboarding.
func (s *CodexService) SetupAgent(_ domain.SetupRequest) error {
	return s.EnsureOnboarding()
}

// EnsureOnboarding reconciles the device-side Codex workspace on boot/config-change
// (server/config_watch.go, same path openclaw/hermes use): seed KNOWLEDGE.md if
// absent, capability-gate skills, and refresh the OS-managed SOUL/AGENTS/HEARTBEAT
// blocks.
func (s *CodexService) EnsureOnboarding() error {
	// Best-effort: a presync failure must not block gateway startup.
	configBefore := fileHash(codexConfigTOML) + fileHash(codexEnvFile)
	if err := s.runPresync(); err != nil {
		if s.config.LLMMode() != "" {
			return fmt.Errorf("apply LLM configuration: %w", err)
		}
		slog.Warn("codex presync failed, continuing with workspace reconcile", "component", "codex", "error", err)
	}
	presyncChanged := fileHash(codexConfigTOML)+fileHash(codexEnvFile) != configBefore

	// Seed KNOWLEDGE.md from the embedded template only if absent.
	// Never overwrites an existing file.
	seedFileIfAbsent(knowledgeFS, "resources/KNOWLEDGE.md",
		filepath.Join(codexWorkspaceDir, "KNOWLEDGE.md"))
	seedFileIfAbsent(agentsFS, "resources/AGENTS.md",
		filepath.Join(codexWorkspaceDir, "AGENTS.md"))

	migrateSkillsToCodexHome()

	s.pruneUnsupportedSkills()
	changedSkills := s.downloadSkills()

	// OS-managed markdown blocks (incl.
	// the persona inline block below).
	if _, err := s.ensureSoulMDBlock(); err != nil {
		slog.Error("ensure SOUL.md block failed", "component", "codex-onboarding", "error", err)
	}
	if _, err := s.ensureAgentsMDBlock(); err != nil {
		slog.Error("ensure AGENTS.md block failed", "component", "codex-onboarding", "error", err)
	}
	// Persona inline: AFTER ensureSoulMDBlock (so the freshly-reconciled soul is
	// what gets inlined) and AFTER ensureAgentsMDBlock (so the persona block ends
	// up above a just-prepended OS mandatory block).
	if _, err := s.ensurePersonaInlineBlock(); err != nil {
		slog.Error("ensure persona inline block failed", "component", "codex-onboarding", "error", err)
	}
	if _, err := s.ensureHeartbeatMDBlock(); err != nil {
		slog.Error("ensure HEARTBEAT.md block failed", "component", "codex-onboarding", "error", err)
	}
	// Global user AGENTS.md ($CODEX_HOME/AGENTS.md): reaches coding sessions in any
	// cwd, which never load the workspace AGENTS.md.
	ensureUserAgentsMDBlock()

	needRestart := false

	if presyncChanged {
		slog.Info("codex presync changed config.toml/.env", "component", "codex-onboarding")
		needRestart = true
	}

	gatewayInstalled := s.ensureGatewayUnit()
	gatewayDown := !gatewayActive()
	if gatewayInstalled || gatewayDown {
		slog.Info("codex gateway needs (re)start",
			"component", "codex-onboarding",
			"gateway_installed", gatewayInstalled, "gateway_down", gatewayDown)
		needRestart = true
	}

	if needRestart {
		slog.Info("restarting codex gateway (presync config change or unit self-heal)", "component", "codex-onboarding")
		enableCodexGateway()
		if err := restartCodexGateway(); err != nil {
			return fmt.Errorf("restart codex after onboarding: %w", err)
		}
	}

	s.notifySkillChangesWhenReady(changedSkills)

	return nil
}

const skillNotifyReadyTimeout = 60 * time.Second

// notifySkillChangesWhenReady waits for a just-restarted Codex bridge before
// asking it to re-read changed skills.
func (s *CodexService) notifySkillChangesWhenReady(changedSkills []string) {
	if len(changedSkills) == 0 {
		return
	}

	deadline := time.NewTimer(skillNotifyReadyTimeout)
	defer deadline.Stop()
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	for {
		if s.IsReady() {
			s.notifySkillChanges(changedSkills)
			return
		}
		select {
		case <-deadline.C:
			slog.Warn("skill update notification timed out waiting for Codex bridge",
				"component", "skill-watcher", "skills", changedSkills)
			return
		case <-ticker.C:
		}
	}
}

// ensureAgentsMDBlock injects/refreshes the OS-managed block in workspace/AGENTS.md.
func (s *CodexService) ensureAgentsMDBlock() (bool, error) {
	agentsFile := filepath.Join(codexWorkspaceDir, "AGENTS.md")

	content, err := os.ReadFile(agentsFile)
	if err != nil {
		return false, fmt.Errorf("read AGENTS.md: %w", err)
	}
	text := string(content)

	// Already has the exact current block → nothing to do.
	if strings.Contains(text, agentsMDBlock) {
		return false, nil
	}

	// Remove a stale marked block before injecting the current version.
	if strings.Contains(text, osMandatoryMarker) {
		text = stripMarkedBlock(text)
	}

	lines := strings.Split(text, "\n")
	result := make([]string, 0, len(lines)+2)
	injected := false
	for _, line := range lines {
		result = append(result, line)
		if !injected && strings.Contains(strings.ToLower(line), "your workspace") {
			result = append(result, agentsMDBlock)
			injected = true
		}
	}
	if !injected {
		result = append([]string{agentsMDBlock, ""}, result...)
	}

	if err := os.WriteFile(agentsFile, []byte(strings.Join(result, "\n")), 0644); err != nil {
		return false, fmt.Errorf("write AGENTS.md: %w", err)
	}
	slog.Info("injected mandatory block into AGENTS.md", "component", "codex-onboarding", "path", agentsFile)
	return true, nil
}

// ensurePersonaInlineBlock inlines the persona (SOUL.md + the IDENTITY.md name) at
// the very top of workspace/AGENTS.md.
// OpenClaw/Hermes inject the soul into the system prompt at the runtime layer; codex has no such
// layer, so the persona must live inside the one file codex is guaranteed to read.
func (s *CodexService) ensurePersonaInlineBlock() (bool, error) {
	return ensurePersonaInlineBlockIn(codexWorkspaceDir)
}

// ensurePersonaInlineBlockIn is the workspace-parameterized body of
// ensurePersonaInlineBlock (codexWorkspaceDir is resolved from CODEX_HOME at
// start — the parameter exists so tests can point it at a temp dir).
func ensurePersonaInlineBlockIn(workspaceDir string) (bool, error) {
	agentsFile := filepath.Join(workspaceDir, "AGENTS.md")
	agentsRaw, err := os.ReadFile(agentsFile)
	if err != nil {
		if os.IsNotExist(err) {
			slog.Warn("AGENTS.md missing — skipping persona inline",
				"component", "codex-onboarding", "path", agentsFile)
			return false, nil
		}
		return false, fmt.Errorf("read AGENTS.md: %w", err)
	}
	text := string(agentsRaw)

	soulRaw, err := os.ReadFile(filepath.Join(workspaceDir, "SOUL.md"))
	if err != nil {
		if !os.IsNotExist(err) {
			return false, fmt.Errorf("read SOUL.md: %w", err)
		}
		// No soul → nothing to inline; drop a stale block if one is present.
		stripped := stripPersonaInlineBlock(text)
		if stripped == text {
			return false, nil
		}
		if err := atomicWriteFile(agentsFile, []byte(stripped)); err != nil {
			return false, fmt.Errorf("write AGENTS.md: %w", err)
		}
		slog.Info("removed persona inline block (SOUL.md gone)",
			"component", "codex-onboarding", "path", agentsFile)
		return true, nil
	}

	// Agent name from IDENTITY.md (best-effort — the block is still useful
	// without it; parseIdentityName is the same helper WatchIdentity uses).
	name := ""
	if idRaw, rerr := os.ReadFile(filepath.Join(workspaceDir, "IDENTITY.md")); rerr == nil {
		name = parseIdentityName(string(idRaw))
	}

	output := buildPersonaInlineBlock(string(soulRaw), name) + "\n\n" + stripPersonaInlineBlock(text)
	if output == text {
		return false, nil
	}
	if err := atomicWriteFile(agentsFile, []byte(output)); err != nil {
		return false, fmt.Errorf("write AGENTS.md: %w", err)
	}
	slog.Info("inlined persona block into AGENTS.md",
		"component", "codex-onboarding", "path", agentsFile, "name", name)
	return true, nil
}

// buildPersonaInlineBlock composes the marker-delimited persona block from the soul
// text and the agent name.
func buildPersonaInlineBlock(soul, name string) string {
	soul = strings.TrimSpace(soul)
	if len(soul) > personaInlineSoulCap {
		cut := personaInlineSoulCap
		for cut > 0 && !utf8.RuneStart(soul[cut]) {
			cut--
		}
		soul = soul[:cut] + "\n\n_(persona truncated at 20000 bytes — the full text lives in SOUL.md)_"
	}
	var b strings.Builder
	b.WriteString(personaInlineStart)
	b.WriteString("\n# Who you are (MANDATORY)\n\nEmbody this persona in EVERY reply, on every channel. This is your identity — never introduce yourself as \"Codex\".\n")
	if name != "" {
		b.WriteString("\nYour name is **" + name + "**.\n")
	}
	b.WriteString("\n")
	b.WriteString(soul)
	b.WriteString("\n")
	b.WriteString(personaInlineEnd)
	return b.String()
}

// stripPersonaInlineBlock removes the personaInlineStart..personaInlineEnd region
// plus the blank padding around it.
// An unterminated block (hand-deleted end marker) only loses the start-marker line — never
// trailing user content.
func stripPersonaInlineBlock(text string) string {
	start := strings.Index(text, personaInlineStart)
	if start < 0 {
		return text
	}
	rest := text[start+len(personaInlineStart):]
	endRel := strings.Index(rest, personaInlineEnd)
	if endRel < 0 {
		return text[:start] + strings.TrimLeft(rest, "\r\n")
	}
	after := strings.TrimLeft(rest[endRel+len(personaInlineEnd):], "\r\n")
	before := strings.TrimRight(text[:start], "\r\n")
	if before == "" {
		return after
	}
	return before + "\n\n" + after
}

// atomicWriteFile writes data to path via tmp+rename in the same directory (the
// pattern UpdateIdentityName uses) so a mid-write crash cannot leave a truncated
// AGENTS.md — codex re-reads it on every turn.
func atomicWriteFile(path string, data []byte) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".*.tmp")
	if err != nil {
		return fmt.Errorf("create tmp: %w", err)
	}
	tmpPath := tmp.Name()
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return fmt.Errorf("write tmp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("close tmp: %w", err)
	}
	if err := os.Chmod(tmpPath, 0o644); err != nil { // CreateTemp defaults to 0600
		os.Remove(tmpPath)
		return fmt.Errorf("chmod tmp: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("rename: %w", err)
	}
	return nil
}

// ensureSoulMDBlock wraps this device's soul as a marker-delimited core block at the
// top of workspace/SOUL.md; owner content below the closing `---` is preserved.
func (s *CodexService) ensureSoulMDBlock() (bool, error) {
	soulFile := filepath.Join(codexWorkspaceDir, "SOUL.md")

	coreContent, hasSoul, err := s.deviceSoulCore()
	if err != nil {
		return false, fmt.Errorf("resolve device soul: %w", err)
	}
	if !hasSoul {
		slog.Info("no soul_ref for device — leaving the migrated/default soul (no override)",
			"component", "codex-onboarding", "device_type", s.config.DeviceTypeOrDefault())
		return false, nil
	}
	soulMDBlock := osMandatoryMarker + "\n" + strings.TrimSpace(string(coreContent)) + "\n---"

	content, err := os.ReadFile(soulFile)
	if err != nil && !os.IsNotExist(err) {
		return false, fmt.Errorf("read SOUL.md: %w", err)
	}
	text := string(content)

	// Fast path: block already present with only owner content below it.
	if idx := strings.Index(text, soulMDBlock); idx >= 0 {
		below := strings.TrimLeft(text[idx+len(soulMDBlock):], " \t\r\n")
		if !isDefaultSoulHeading(below) {
			return false, nil
		}
	}

	// Strip a prior marker block so the default-seed heuristic only sees what was
	// below the closing `---`.
	if strings.Contains(text, osMandatoryMarker) {
		text = stripMarkedBlock(text)
	}

	// Discard a managed default soul left in the remaining text so it is not preserved
	// as fake owner content and duplicated below the device block.
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
		return false, nil
	}
	if err := os.WriteFile(soulFile, []byte(output), 0644); err != nil {
		return false, fmt.Errorf("write SOUL.md: %w", err)
	}
	slog.Info("injected core block into SOUL.md", "component", "codex-onboarding", "path", soulFile)
	return true, nil
}

// deviceSoulCore resolves the soul text for this device from the `soul_ref` in
// robots/<type>/ROBOT.md.
func (s *CodexService) deviceSoulCore() (content []byte, hasSoul bool, err error) {
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

// devicesDir returns the root holding per-device profile folders
// (robots/<type>/{DEVICE,SOUL}.md).
func devicesDir() string {
	if d := strings.TrimSpace(os.Getenv("DEVICES_DIR")); d != "" {
		return d
	}
	return "/opt/devices"
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

// isDefaultSoulHeading reports whether trimmed begins with a managed soul template
// heading that must not be preserved as owner content below the device block.
func isDefaultSoulHeading(trimmed string) bool {
	return strings.HasPrefix(trimmed, "# Soul") || strings.HasPrefix(trimmed, "# SOUL.md")
}

// ensureHeartbeatMDBlock injects the knowledge-synthesis block at the top of
// workspace/HEARTBEAT.md.
func (s *CodexService) ensureHeartbeatMDBlock() (bool, error) {
	heartbeatFile := filepath.Join(codexWorkspaceDir, "HEARTBEAT.md")

	content, err := os.ReadFile(heartbeatFile)
	if err != nil && !os.IsNotExist(err) {
		return false, fmt.Errorf("read HEARTBEAT.md: %w", err)
	}
	text := string(content)

	if strings.Contains(text, heartbeatMDBlock) {
		return false, nil
	}
	if strings.Contains(text, osMandatoryMarker) {
		text = stripMarkedBlock(text)
	}

	output := heartbeatMDBlock + "\n\n" + text
	if err := os.WriteFile(heartbeatFile, []byte(output), 0644); err != nil {
		return false, fmt.Errorf("write HEARTBEAT.md: %w", err)
	}
	slog.Info("injected mandatory block into HEARTBEAT.md", "component", "codex-onboarding", "path", heartbeatFile)
	return true, nil
}

// ensureUserAgentsMDBlock injects/refreshes the OS-managed block in codex's
// GLOBAL user-instructions file (codexUserAgentsMD = $CODEX_HOME/AGENTS.md),
// which codex loads in every session regardless of cwd.
func ensureUserAgentsMDBlock() bool {
	return ensureUserAgentsMDBlockAt(codexUserAgentsMD)
}

// ensureUserAgentsMDBlockAt is the path-parameterized body of
// ensureUserAgentsMDBlock (codexUserAgentsMD is a hardcoded const — the parameter
// exists so tests can point it at a temp dir).
func ensureUserAgentsMDBlockAt(path string) bool {
	content, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		slog.Warn("ensure user AGENTS.md: read failed", "component", "codex-onboarding", "error", err)
		return false
	}
	text := string(content)

	if strings.Contains(text, userAgentsMDBlock) {
		return false
	}
	if strings.Contains(text, osMandatoryMarker) {
		text = stripMarkedBlock(text)
	}

	output := userAgentsMDBlock + "\n"
	if strings.TrimSpace(text) != "" {
		output += "\n" + strings.TrimLeft(text, " \t\r\n")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		slog.Warn("ensure user AGENTS.md: mkdir failed", "component", "codex-onboarding", "error", err)
		return false
	}
	if err := os.WriteFile(path, []byte(output), 0o644); err != nil {
		slog.Warn("ensure user AGENTS.md: write failed", "component", "codex-onboarding", "error", err)
		return false
	}
	slog.Info("injected mandatory block into user AGENTS.md", "component", "codex-onboarding", "path", path)
	return true
}

// migrateSkillsToCodexHome moves skills installed by an older os-server under
// workspace/skills into codex's NATIVE discovery root (codexSkillsDir =
// $CODEX_HOME/skills), then drops the workspace copy.
func migrateSkillsToCodexHome() bool {
	legacyDir := filepath.Join(codexWorkspaceDir, "skills")
	entries, err := os.ReadDir(legacyDir)
	if err != nil {
		return false
	}

	if err := os.MkdirAll(codexSkillsDir, 0o755); err != nil {
		slog.Warn("migrate skills: mkdir codex-home skills dir failed", "component", "codex-onboarding", "error", err)
		return false
	}

	var moved int
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		dst := filepath.Join(codexSkillsDir, e.Name())
		if _, err := os.Stat(dst); err == nil {
			continue
		}
		if err := os.Rename(filepath.Join(legacyDir, e.Name()), dst); err != nil {
			slog.Warn("migrate skills: move failed", "component", "codex-onboarding", "skill", e.Name(), "error", err)
			continue
		}
		moved++
	}

	if err := os.RemoveAll(legacyDir); err != nil {
		slog.Warn("migrate skills: remove legacy dir failed", "component", "codex-onboarding", "error", err)
	}
	slog.Info("migrated skills to codex-home discovery root", "component", "codex-onboarding",
		"moved", moved, "from", legacyDir, "to", codexSkillsDir)
	return true
}

// pruneUnsupportedSkills removes platform-catalog skill dirs the device can't use
// from codexSkillsDir ($CODEX_HOME/skills, codex's native discovery root — same
// capability gate openclaw uses).
func (s *CodexService) pruneUnsupportedSkills() {
	skillsDir := codexSkillsDir
	entries, err := os.ReadDir(skillsDir)
	if err != nil {
		if !os.IsNotExist(err) {
			slog.Warn("prune skills: read dir failed", "component", "codex-onboarding", "error", err)
		}
		return
	}
	keep := map[string]bool{}
	for _, n := range s.supportedSkills() {
		keep[n] = true
	}
	catalog := map[string]bool{}
	for _, n := range skills.Catalog {
		catalog[n] = true
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		name := e.Name()
		if !catalog[name] || keep[name] {
			continue
		}
		if err := os.RemoveAll(filepath.Join(skillsDir, name)); err != nil {
			slog.Warn("prune unsupported skill failed", "component", "codex-onboarding", "skill", name, "error", err)
			continue
		}
		slog.Info("pruned unsupported skill (capability gate)", "component", "codex-onboarding", "skill", name)
	}
}

// seedFileIfAbsent writes an embedded file to dst only when dst does not already
// exist (never overwrites — KNOWLEDGE.md is a living doc).
func seedFileIfAbsent(efs embed.FS, src, dst string) {
	if _, err := os.Stat(dst); err == nil {
		return // already exists, never overwrite
	}
	data, err := efs.ReadFile(src)
	if err != nil {
		slog.Error("read embedded file failed", "component", "codex-onboarding", "src", src, "error", err)
		return
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0755); err != nil {
		slog.Error("create dir for seed failed", "component", "codex-onboarding", "dst", dst, "error", err)
		return
	}
	if err := os.WriteFile(dst, data, 0644); err != nil {
		slog.Error("write file failed", "component", "codex-onboarding", "dst", dst, "error", err)
		return
	}
	slog.Info("seeded file (initial)", "component", "codex-onboarding", "file", filepath.Base(dst))
}

// stripMarkedBlock removes the block between the marker and the next --- separator.
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
