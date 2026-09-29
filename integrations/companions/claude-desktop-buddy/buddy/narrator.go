package buddy

import (
	"log"
	"sync"
)

// Narrator turns Claude Desktop activity into short TTS announcements, each category at most once per turn.
type Narrator struct {
	lang  string
	speak func(text string)

	mu       sync.Mutex
	turnSeen map[NarrationCategory]bool
}

// NewNarrator builds a narrator; speak=nil disables narration and unknown langs fall back to English.
func NewNarrator(lang string, speak func(text string)) *Narrator {
	return &Narrator{
		lang:     supportedLang(lang),
		speak:    speak,
		turnSeen: make(map[NarrationCategory]bool),
	}
}

// StartTurn resets per-turn dedupe; call on each user turn and on idle->busy.
func (n *Narrator) StartTurn() {
	n.mu.Lock()
	n.turnSeen = make(map[NarrationCategory]bool)
	n.mu.Unlock()
}

// Say announces category once per turn; args feed fmt.Sprintf placeholders.
func (n *Narrator) Say(cat NarrationCategory, args ...any) {
	n.mu.Lock()
	if n.turnSeen[cat] {
		n.mu.Unlock()
		return
	}
	n.turnSeen[cat] = true
	n.mu.Unlock()

	text := narrationText(n.lang, cat, args...)
	if text == "" || n.speak == nil {
		return
	}
	log.Printf("[narrator] %s → %q", cat, text)
	n.speak(text)
}

// Warmup passes every narration phrase to prerender so the TTS cache is warm.
func (n *Narrator) Warmup(prerender func(text string)) {
	if prerender == nil {
		return
	}
	for cat := range narrationStrings[n.lang] {
		text := narrationText(n.lang, cat)
		if text == "" {
			continue
		}
		prerender(text)
	}
}

// SayTool narrates a tool invocation via its mapped category.
func (n *Narrator) SayTool(name string) {
	n.Say(toolToCategory(name))
}
