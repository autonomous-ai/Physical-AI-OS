package migratepersona

import (
	"regexp"
	"strings"

	"go.autonomous.ai/os/system/lib/usercanon"
)

// Quarantined is one block the guard removed; its text goes only to the sidecar, never to a
// flow event (may hold personal notes).
type Quarantined struct {
	Text   string
	Reason string
}

// Quarantine reasons; stable strings used in logs, sidecars and flow events.
const (
	ReasonFreeProse    = "free-prose"    // USER.md: filled content outside the `## Users` shape
	ReasonUnknownLabel = "unknown-label" // USER.md: `## Users` entry whose label has no enrollment
	ReasonPrescriptive = "prescriptive"  // names a tool/endpoint and/or says what to DO rather than what happened
)

// toolRefRe matches a tool, shell, CLI, endpoint path or file name the agent could act with.
var toolRefRe = regexp.MustCompile(`(?i)(?:` +
	`\b(?:obsidian|notebook|vault|terminal|shell|bash|zsh|exec|curl|wget|ssh|sudo|systemctl|journalctl|python|pip|npm|node|docker|git|cron|cli|api|endpoint|skills?|tools?|commands?|scripts?|prompt|model|llm|servo|camera|mqtt)\b` +
	`|(?:^|[\s(` + "`" + `"'])/[a-z][a-z0-9_-]*(?:/[a-z0-9_{}.-]+)+` + // an endpoint path like /servo/search
	`|\b[a-z0-9_-]+\.(?:md|py|sh|json|ya?ml|js|ts)\b` + // a file name
	`|/dev/` +
	`)`)

// prescriptiveRe matches directive phrasing (what to DO); use/run/call/try/avoid/skip count only
// in imperative position, so "tried to use X but..." observations survive.
var prescriptiveRe = regexp.MustCompile(`(?i)(?:\b(?:` +
	`always|never|must|should|do not|don'?t|instead of|rather than` +
	`|prefer(?:s|red)? (?:to|that (?:you|i))` +
	`|match(?:ing)? the|respond(?:ing)? in|repl(?:y|ying) in|answer(?:ing)? in|speak(?:ing)? in` +
	`|hands[- ]on|wants? .{0,40}\bdone|works? best` +
	`|be (?:brief|concise|short|direct)|keep (?:it|replies|answers)` +
	`|when (?:asked|told|the user)` +
	`)\b` +
	`|(?:^|[.;:!]\s+|\b(?:always|never|just|please|should|must|only|then)\s+)(?:use|run|call|try|avoid|skip)\b` +
	`)`)

func namesToolOrEndpoint(s string) bool { return toolRefRe.MatchString(s) }
func prescribesBehaviour(s string) bool { return prescriptiveRe.MatchString(s) }

// segmentToolRefRe is toolRefRe minus words that are often personal facts (python, git, camera...).
// `## Users` segments trip on either rule alone and are re-added by People sync, so a false
// positive becomes a rewrite loop; the segment rules stay narrow.
var segmentToolRefRe = regexp.MustCompile(`(?i)(?:` +
	`\b(?:obsidian|vault|terminal|shell|bash|zsh|exec|curl|wget|ssh|sudo|systemctl|journalctl|pip|npm|docker|cron|cli|api|endpoint|skills?|tools?|commands?|scripts?|llm|servo|mqtt)\b` +
	`|(?:^|[\s(` + "`" + `"'])/[a-z][a-z0-9_-]*(?:/[a-z0-9_{}.-]+)+` + // an endpoint path like /servo/search
	`|\b[a-z0-9_-]+\.(?:md|py|sh|json|ya?ml|js|ts)\b` + // a file name
	`|/dev/` +
	`)`)

// segmentPrescriptiveRe is prescriptiveRe without bare modals: always/never/should/must count
// only when a verb follows.
var segmentPrescriptiveRe = regexp.MustCompile(`(?i)(?:\b(?:` +
	`do not|don'?t|instead of|rather than` +
	`|match(?:ing)? the|respond(?:ing)? in|repl(?:y|ying) in|answer(?:ing)? in|speak(?:ing)? in` +
	`|hands[- ]on|wants? .{0,40}\bdone|works? best` +
	`|be (?:brief|concise|short|direct)|keep (?:it|replies|answers)` +
	`)\b` +
	`|(?:^|[.;:!]\s+|\b(?:always|never|just|please|should|must|only|then)\s+)(?:use|run|call|try|avoid|skip|do|say|reply|respond|answer|speak|match|keep|be)\b` +
	`)`)

func segmentNamesTool(s string) bool           { return segmentToolRefRe.MatchString(s) }
func segmentPrescribesBehaviour(s string) bool { return segmentPrescriptiveRe.MatchString(s) }

// isPoisonForMemory is the MEMORY.md rule: tool/endpoint AND directive; either half alone is kept.
func isPoisonForMemory(s string) bool { return namesToolOrEndpoint(s) && prescribesBehaviour(s) }

type blockKind int

const (
	blockPassthrough blockKind = iota // heading, blank, comment, code, table row, rule
	blockContent                      // a bullet (+ its continuation lines), a paragraph, or a § entry
)

// memBlock is one source region; raw is rejoined verbatim so a clean file round-trips byte for
// byte (no write, no prompt-cache miss).
type memBlock struct {
	raw  string
	text string // bullet marker / indentation stripped, whitespace normalised (blockContent only)
	kind blockKind
}

var reBulletMarker = regexp.MustCompile(`^\s*(?:[-*]|\d+\.)\s+`)

// splitMemoryBlocks splits Hermes "\n§\n" entries or markdown bullets/paragraphs into blocks.
func splitMemoryBlocks(raw string) (blocks []memBlock, delimited bool) {
	if strings.Contains(raw, entryDelimiter) {
		for _, part := range strings.Split(raw, entryDelimiter) {
			blocks = append(blocks, memBlock{raw: part, text: normalizeText(part), kind: blockContent})
		}
		return blocks, true
	}
	lines := strings.SplitAfter(raw, "\n")
	if len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	inCode, inComment := false, false
	for i := 0; i < len(lines); {
		line := lines[i]
		stripped := strings.TrimSpace(line)
		switch {
		case inComment:
			if strings.Contains(stripped, "-->") {
				inComment = false
			}
			blocks = append(blocks, memBlock{raw: line, kind: blockPassthrough})
			i++
		case strings.HasPrefix(stripped, "<!--"):
			inComment = !strings.Contains(stripped, "-->")
			blocks = append(blocks, memBlock{raw: line, kind: blockPassthrough})
			i++
		case strings.HasPrefix(stripped, "```"):
			inCode = !inCode
			blocks = append(blocks, memBlock{raw: line, kind: blockPassthrough})
			i++
		case inCode, stripped == "", stripped == "---", stripped == "***",
			reHeading.MatchString(stripped),
			strings.HasPrefix(stripped, "|") && strings.HasSuffix(stripped, "|"):
			blocks = append(blocks, memBlock{raw: line, kind: blockPassthrough})
			i++
		default:
			isBullet := reBulletMarker.MatchString(line)
			j := i + 1
			for j < len(lines) {
				next := lines[j]
				ns := strings.TrimSpace(next)
				if ns == "" || reHeading.MatchString(ns) || strings.HasPrefix(ns, "```") || strings.HasPrefix(ns, "<!--") {
					break
				}
				if isBullet && !strings.HasPrefix(next, "  ") && !strings.HasPrefix(next, "\t") {
					break
				}
				if !isBullet && reBulletMarker.MatchString(next) {
					break
				}
				j++
			}
			rawBlock := strings.Join(lines[i:j], "")
			text := rawBlock
			if isBullet {
				text = reBulletMarker.ReplaceAllString(lines[i], "")
				for _, c := range lines[i+1 : j] {
					text += " " + strings.TrimSpace(c)
				}
			}
			blocks = append(blocks, memBlock{raw: rawBlock, text: normalizeText(text), kind: blockContent})
			i = j
		}
	}
	return blocks, false
}

func joinMemoryBlocks(blocks []memBlock, delimited bool) string {
	parts := make([]string, 0, len(blocks))
	for _, b := range blocks {
		parts = append(parts, b.raw)
	}
	if delimited {
		return strings.Join(parts, entryDelimiter)
	}
	return strings.Join(parts, "")
}

// rebuildBlock re-serialises a content block whose text changed, keeping the
// original bullet marker (or none, for a § entry / paragraph).
func rebuildBlock(b memBlock, text string, delimited bool) string {
	if delimited {
		return text
	}
	marker := reBulletMarker.FindString(b.raw)
	return marker + text + "\n"
}

var (
	// Single-underscore/star italic hint; bold is not a hint.
	italicHintRe = regexp.MustCompile(`^(?:_[^_]+_|\*[^*]+\*)$`)
	// A bare markdown link, optionally labelled: `Related: [Agent workspace](/concepts/agent-workspace)`.
	linkOnlyRe = regexp.MustCompile(`^(?:[A-Za-z ]+:\s*)?\[[^\]]+\]\([^)]+\)$`)
	// Heading prefix left by an earlier flatten ("Users: ", "A > B: ").
	headingPrefixRe = regexp.MustCompile(`^(?:[A-Za-z][A-Za-z ]{0,30}(?: > [A-Za-z][A-Za-z ]{0,30})*):\s+`)
)

// userProfileTemplateSentences are the normalised USER.md template prose lines (the only allowed
// free prose).
var userProfileTemplateSentences = map[string]bool{
	normalizeKey("Learn about the person you're helping. Update this as you go."):                                                                              true,
	normalizeKey("What do they care about? What projects are they working on? What annoys them? What makes them laugh? Build this over time."):                 true,
	normalizeKey("The more you know, the better you can help. But remember — you're learning about a person, not building a dossier. Respect the difference."): true,
	normalizeKey("USER.md - About Your Human"): true,
}

func normalizeKey(s string) string {
	s = strings.ToLower(normalizeText(s))
	s = strings.Trim(s, "_*() ")
	return s
}

// isUserProfileScaffolding reports whether a USER.md block is template (empty slot, hint, rule,
// link, template sentence) or a filled singular field owned by the retire pass.
func isUserProfileScaffolding(text string) bool {
	t := strings.TrimSpace(headingPrefixRe.ReplaceAllString(strings.TrimSpace(text), ""))
	if t == "" || t == "---" || t == "***" {
		return true
	}
	if m := userFieldEntryRe.FindStringSubmatch(t); m != nil {
		if !hasRealFieldValue(m[2]) {
			return true
		}
		return isUserProfileField(strings.TrimSpace(m[1]))
	}
	if italicHintRe.MatchString(t) || linkOnlyRe.MatchString(t) {
		return true
	}
	return userProfileTemplateSentences[normalizeKey(t)]
}

// guardUsersEntry drops `key: value` segments of a `**label (role)**` entry that trip the segment
// rules; returns the input unchanged when nothing was dropped.
func guardUsersEntry(text string) (string, []Quarantined) {
	head := usersBlockRe.FindString(text)
	rest := strings.TrimLeft(strings.TrimSpace(text[len(head):]), "—–-: ")
	if rest == "" {
		return text, nil
	}
	var kept []string
	var dropped []Quarantined
	for _, seg := range strings.Split(rest, ";") {
		seg = strings.TrimSpace(seg)
		if seg == "" {
			continue
		}
		value := seg
		// Judge the trimmed value, not the key ("call" would trip the directive pattern).
		if k, v, ok := strings.Cut(seg, ":"); ok && len(strings.Fields(k)) <= 3 {
			value = strings.TrimSpace(v)
		}
		if segmentNamesTool(value) || segmentPrescribesBehaviour(value) {
			dropped = append(dropped, Quarantined{Text: seg, Reason: ReasonPrescriptive})
			continue
		}
		kept = append(kept, seg)
	}
	if len(dropped) == 0 {
		return text, nil
	}
	if len(kept) == 0 {
		return strings.TrimSpace(head), dropped
	}
	return strings.TrimSpace(head) + " — " + strings.Join(kept, "; "), dropped
}

// GuardUserProfileText applies the strict USER.md allowlist; enrolled nil/empty skips the label
// check. A clean file returns unchanged with nil dropped and must not be written (cached prompt).
func GuardUserProfileText(raw string, enrolled map[string]bool) (string, []Quarantined) {
	blocks, delimited := splitMemoryBlocks(raw)
	var dropped []Quarantined
	out := make([]memBlock, 0, len(blocks))
	for _, b := range blocks {
		if b.kind != blockContent {
			out = append(out, b)
			continue
		}
		if m := usersBlockRe.FindStringSubmatch(b.text); m != nil {
			label := strings.TrimSpace(m[1])
			if len(enrolled) > 0 && !enrolled[usercanon.Resolve(label)] {
				dropped = append(dropped, Quarantined{Text: b.text, Reason: ReasonUnknownLabel})
				continue
			}
			kept, segs := guardUsersEntry(b.text)
			if len(segs) > 0 {
				dropped = append(dropped, segs...)
				b.raw = rebuildBlock(b, kept, delimited)
				b.text = kept
			}
			out = append(out, b)
			continue
		}
		if isUserProfileScaffolding(b.text) {
			out = append(out, b)
			continue
		}
		dropped = append(dropped, Quarantined{Text: b.text, Reason: ReasonFreeProse})
	}
	if len(dropped) == 0 {
		return raw, nil
	}
	return joinMemoryBlocks(out, delimited), dropped
}

// GuardMemoryText applies isPoisonForMemory to every content block; unchanged when clean.
func GuardMemoryText(raw string) (string, []Quarantined) {
	blocks, delimited := splitMemoryBlocks(raw)
	var dropped []Quarantined
	out := make([]memBlock, 0, len(blocks))
	for _, b := range blocks {
		if b.kind == blockContent && isPoisonForMemory(b.text) {
			dropped = append(dropped, Quarantined{Text: b.text, Reason: ReasonPrescriptive})
			continue
		}
		out = append(out, b)
	}
	if len(dropped) == 0 {
		return raw, nil
	}
	return joinMemoryBlocks(out, delimited), dropped
}
