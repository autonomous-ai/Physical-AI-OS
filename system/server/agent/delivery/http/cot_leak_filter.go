package http

// Go port of hal/drivers/voice/_internal/cot_leak_filter.py (keep in sync):
// drops chain-of-thought leaks from agent reply text before TTS/chat/channels.
// Tiers: TRIGGER markers (always dropped, enter CoT mode), SECONDARY markers
// (dropped in CoT mode), and CoT-mode heuristics. Go adds snake_case
// identifiers to TRIGGER.

import (
	"regexp"
	"strings"
	"unicode"
)

// cotTriggerRe matches unambiguous CoT; "the user" is verb-bound to spare "the user manual".
var cotTriggerRe = regexp.MustCompile(
	`(?i)(?:\bthe (?:user|speaker)s? (?:is|are|was|were|wants?|wanted|asks?` +
		`|asked|insists?|insisted|seems?|seemed|says?|said|repeats?|repeated` +
		`|claims?|claimed|mentions?|mentioned|requests?|requested|greets?|greeted` +
		`|needs?|needed)\b` +
		`|phrasing draft|delivery guidance|spoken delivery` +
		// Anchored to avoid matching ordinary song/device talk.
		`|^\s*they named (?:a|the) song\s*:` +
		`|^\s*speaker(?: identity)? is unknown\b` +
		`|\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b)`,
)

// cotSecondaryRe matches meta terms a legit reply could contain; dropped only in CoT mode.
var cotSecondaryRe = regexp.MustCompile(
	`(?i)(?:\bpersonas?\b|\bsystem prompts?\b|language lock|\baudio tags?\b` +
		`|\bemotion tool\b|via tool call` +
		`|\bmy search results?\b|\bsearch results? (?:show|suggest|indicate)\b` +
		`|\bsearch quer(?:y|ies)\b` +
		`|\b\d+ (?:sentences?|words)\b)`,
)

// cotLabelRe matches ASCII label sentences like "Length Check:" (non-English replies only).
var cotLabelRe = regexp.MustCompile(`^[\x20-\x7e]{1,40}:$`)

// cotOpenerRe matches sentence-initial planning openers (non-English replies only).
var cotOpenerRe = regexp.MustCompile(
	`(?i)^\s*(?:users? (?:want|wants|is|are|asked|insists?)\b` +
		`|i (?:need|should|must|will|can(?:not|'t)?) \b` +
		`|therefore,? i\b|looking at the\b|re-examining\b|plan:\s*$)`,
)

const cotQuotes = "\"'“”‘’«»「」『』【】＂＇"

// Quoted spans are excluded from language detection. RE2 lacks lookarounds, so
// single-quote spans use boundary groups; contractions ("didn't") can't open one.
var cotQuotedSpanRe = regexp.MustCompile(
	`"[^"]*"|“[^”]*”|‘[^’]*’|«[^»]*»|「[^」]*」|『[^』]*』`,
)

var cotSingleQuotedSpanRe = regexp.MustCompile(
	`(^|[^\pL\pN_])'[^']*'($|[^\pL\pN_])`,
)

func cotStripQuotedSpans(s string) string {
	s = cotQuotedSpanRe.ReplaceAllString(s, " ")
	return cotSingleQuotedSpanRe.ReplaceAllString(s, "$1 $2")
}

var cotNonASCIIRe = regexp.MustCompile(`[^\x00-\x7f]`)

// Leading audio/emotion tags like "[caring] " don't decide the language.
var cotLeadingTagsRe = regexp.MustCompile(`^(?:\s*\[[^\]]{1,30}\])+\s*`)

// CJK/Hangul/Kana tokenize per character; other letters per word.
const cotCJKRange = `぀-ヿ㐀-䶿一-鿿豈-﫿가-힯`

var cotTokenRe = regexp.MustCompile(`[` + cotCJKRange + `]|[0-9]+|[^\P{L}` + cotCJKRange + `]+`)

var cotEnWordRe = regexp.MustCompile(`[A-Za-z]+`)

// Latin-script non-English replies need English stopwords to count as English.
var cotEnStopwords = func() map[string]bool {
	m := map[string]bool{}
	for _, w := range strings.Fields(
		"the a an is are was were to of and or that this it its i you we they" +
			" will would should must need in on at with for be as by from since") {
		m[w] = true
	}
	return m
}()

// cotSplitSentences mirrors the Python _SENTENCE_SPLIT regex without lookarounds.
// Splits at enders+space, newlines, and ender glued to uppercase/non-ASCII; "3.5" and "Node.js" stay intact.
func cotSplitSentences(text string) []string {
	var out []string
	var cur strings.Builder
	runes := []rune(text)
	n := len(runes)
	flush := func() {
		if cur.Len() > 0 {
			out = append(out, cur.String())
			cur.Reset()
		}
	}
	for i := 0; i < n; i++ {
		r := runes[i]
		if r == '\n' {
			flush()
			continue
		}
		cur.WriteRune(r)
		if i+1 >= n {
			continue
		}
		next := runes[i+1]
		switch r {
		case '.', '!', '?', '。', '！', '？', '：', '；', ':':
			if unicode.IsSpace(next) {
				flush()
				for i+1 < n && unicode.IsSpace(runes[i+1]) {
					i++
				}
				continue
			}
		}
		switch r {
		case '.', '!', '?':
			if (next >= 'A' && next <= 'Z') || next > 0x7f {
				flush()
			}
		}
	}
	flush()
	return out
}

func cotWordSet(s string) map[string]struct{} {
	set := map[string]struct{}{}
	for _, t := range cotTokenRe.FindAllString(strings.ToLower(s), -1) {
		set[t] = struct{}{}
	}
	return set
}

func cotJaccard(a, b map[string]struct{}) float64 {
	if len(a) == 0 || len(b) == 0 {
		return 0
	}
	inter := 0
	for t := range a {
		if _, ok := b[t]; ok {
			inter++
		}
	}
	return float64(inter) / float64(len(a)+len(b)-inter)
}

// cotLeakFilter is the per-turn stateful filter; reuse one instance per turn so
// CoT mode and dedup memory carry across streamed chunks.
type cotLeakFilter struct {
	nonEnglish     bool
	nonASCIIScript bool
	cotMode        bool
	seen           []map[string]struct{}
	// dropped collects removed sentences for the caller to log.
	dropped []string
}

// newCoTLeakFilter builds a filter for an stt_language code ("vi", "zh-CN"); "" means English.
func newCoTLeakFilter(langCode string) *cotLeakFilter {
	code := strings.ToLower(strings.TrimSpace(langCode))
	primary := code
	if i := strings.IndexAny(primary, "-_"); i > 0 {
		primary = primary[:i]
	}
	f := &cotLeakFilter{
		nonEnglish: code != "" && primary != "en",
	}
	switch primary {
	case "vi", "zh", "ja", "ko", "th":
		f.nonASCIIScript = true
	}
	return f
}

func (f *cotLeakFilter) looksEnglish(sentence string) bool {
	s := strings.TrimSpace(cotLeadingTagsRe.ReplaceAllString(sentence, ""))
	s = cotStripQuotedSpans(s)
	letters, nonASCII := 0, 0
	for _, r := range s {
		if unicode.IsLetter(r) {
			letters++
			if r > 127 {
				nonASCII++
			}
		}
	}
	if letters == 0 {
		return false
	}
	// Ratio, not any(): "non-cliché" is English; Vietnamese runs ~30%+ non-ASCII.
	if float64(nonASCII)/float64(letters) > 0.05 {
		return false
	}
	words := cotEnWordRe.FindAllString(s, -1)
	if len(words) < 3 {
		return false
	}
	if f.nonASCIIScript {
		return true
	}
	stop := 0
	for _, w := range words {
		if cotEnStopwords[strings.ToLower(w)] {
			stop++
		}
	}
	return stop >= 2
}

func (f *cotLeakFilter) isLeak(sentence string) bool {
	s := strings.TrimSpace(sentence)
	if s == "" {
		return false
	}
	if cotTriggerRe.MatchString(s) {
		f.cotMode = true
		return true
	}
	if f.nonEnglish && cotOpenerRe.MatchString(s) {
		f.cotMode = true
		return true
	}
	if f.nonEnglish && cotLabelRe.MatchString(s) {
		f.cotMode = true
		return true
	}
	if !f.cotMode {
		return false
	}
	if cotSecondaryRe.MatchString(s) {
		return true
	}
	if f.nonEnglish && f.looksEnglish(s) {
		return true
	}
	// Quoted drafts of the answer would otherwise be spoken twice.
	runes := []rune(s)
	if strings.ContainsRune(cotQuotes, runes[0]) || strings.ContainsRune(cotQuotes, runes[len(runes)-1]) {
		return true
	}
	// Bare ASCII plan runts (<=2 tokens); pure audio tags are exempt.
	bare := strings.TrimSpace(cotLeadingTagsRe.ReplaceAllString(s, ""))
	if bare != "" && !cotNonASCIIRe.MatchString(bare) && len(cotWordSet(bare)) <= 2 {
		return true
	}
	// Fuzzy near-duplicate of an already-kept sentence.
	words := cotWordSet(s)
	if len(words) > 0 {
		for _, seen := range f.seen {
			if cotJaccard(words, seen) >= 0.7 {
				return true
			}
		}
	}
	return false
}

// filterText drops CoT sentences from text, appending them to f.dropped.
func (f *cotLeakFilter) filterText(text string) string {
	if text == "" {
		return text
	}
	var kept []string
	for _, sentence := range cotSplitSentences(text) {
		if f.isLeak(sentence) {
			f.dropped = append(f.dropped, strings.TrimSpace(sentence))
			continue
		}
		s := strings.TrimSpace(sentence)
		if s == "" {
			continue
		}
		kept = append(kept, s)
		if ws := cotWordSet(s); len(ws) > 0 {
			f.seen = append(f.seen, ws)
		}
	}
	return strings.Join(kept, " ")
}

// cotDroppedPreview joins dropped sentences into a bounded preview for logs.
func cotDroppedPreview(dropped []string, max int) string {
	joined := strings.Join(dropped, " | ")
	if len(joined) > max {
		return joined[:max] + "…"
	}
	return joined
}
