package http

import (
	"strings"
	"testing"
)

func TestExtractHWCallsCanonical(t *testing.T) {
	calls, rest := extractHWCalls(`[HW:/led/off:{}] LEDs off.`)
	if len(calls) != 1 || calls[0].path != "/led/off" || calls[0].body != "{}" {
		t.Fatalf("canonical marker not parsed: %+v", calls)
	}
	if rest != "LEDs off." {
		t.Fatalf("stripped text = %q", rest)
	}
}

func TestExtractHWCallsMarkdownLinkForm(t *testing.T) {
	// Markdown-link form marker with a non-ASCII label (UTF-8 multibyte coverage).
	calls, rest := extractHWCalls(`[Tắt đèn liền đây!](HW:/led/off:{})`)
	if len(calls) != 1 || calls[0].path != "/led/off" || calls[0].body != "{}" {
		t.Fatalf("link-form marker not parsed: %+v", calls)
	}
	if rest != "Tắt đèn liền đây!" {
		t.Fatalf("label lost, stripped text = %q", rest)
	}
}

func TestExtractHWCallsLinkFormWithBody(t *testing.T) {
	calls, rest := extractHWCalls(`[Red light](HW:/led/solid:{"color":[255,0,0]}) done.`)
	if len(calls) != 1 || calls[0].path != "/led/solid" || calls[0].body != `{"color":[255,0,0]}` {
		t.Fatalf("link-form body not parsed: %+v", calls)
	}
	if rest != "Red light done." {
		t.Fatalf("stripped text = %q", rest)
	}
}

func TestExtractHWCallsLinkFormBodyless(t *testing.T) {
	calls, _ := extractHWCalls(`[Stop the effect](HW:/led/effect/stop)`)
	if len(calls) != 1 || calls[0].path != "/led/effect/stop" || calls[0].body != "{}" {
		t.Fatalf("bodyless link-form not parsed: %+v", calls)
	}
}

func TestExtractHWCallsPlainLinkUntouched(t *testing.T) {
	calls, rest := extractHWCalls(`See the [docs](https://example.com) here.`)
	if len(calls) != 0 {
		t.Fatalf("plain markdown link misparsed as HW call: %+v", calls)
	}
	if rest != `See the [docs](https://example.com) here.` {
		t.Fatalf("plain link mangled: %q", rest)
	}
}

func TestExtractLeadingHWCallsLinkForm(t *testing.T) {
	// A reply opening with a link-form marker must still fire at stream time.
	calls := extractLeadingHWCalls(`[Lights off right away!](HW:/led/off:{})`)
	if len(calls) != 1 || calls[0].path != "/led/off" || calls[0].body != "{}" {
		t.Fatalf("leading link-form not detected: %+v", calls)
	}
}

func TestExtractHWCallsLinkLabelIsCanonicalMarker(t *testing.T) {
	// Only the second marker of a back-to-back pair is link-wrapped; both must fire in emitted order.
	calls, rest := extractHWCalls(`[HW:/led/effect/stop:{}](HW:/led/solid:{"color":[255,0,0]})`)
	if len(calls) != 2 {
		t.Fatalf("expected 2 calls, got %+v", calls)
	}
	if calls[0].path != "/led/effect/stop" || calls[0].body != "{}" {
		t.Fatalf("first (stop) call wrong: %+v", calls[0])
	}
	if calls[1].path != "/led/solid" || calls[1].body != `{"color":[255,0,0]}` {
		t.Fatalf("second (solid) call wrong: %+v", calls[1])
	}
	if rest != "" {
		t.Fatalf("stripped text = %q", rest)
	}
}

func TestExtractHWCallsLinkFormTolerances(t *testing.T) {
	// Variants the strip regexes must not be looser than: lowercase scheme,
	// space after HW:, dangling colon with no body.
	for _, tc := range []struct{ in, path string }{
		{`[lights off](hw:/led/off:{})`, "/led/off"},
		{`[Lights off](HW: /led/off:{})`, "/led/off"},
		{`[Lights off](HW:/led/off:)`, "/led/off"},
	} {
		calls, rest := extractHWCalls(tc.in)
		if len(calls) != 1 || calls[0].path != tc.path || calls[0].body != "{}" {
			t.Fatalf("%q not parsed: %+v", tc.in, calls)
		}
		if rest == "" || strings.Contains(rest, "HW") {
			t.Fatalf("%q label not preserved cleanly: %q", tc.in, rest)
		}
	}
}

func TestExtractHWCallsLinkBodyWithParen(t *testing.T) {
	// A `)` inside a JSON string must not truncate the match.
	calls, rest := extractHWCalls(`[Speaking](HW:/voice/speak:{"text":"OK (done) now"})`)
	if len(calls) != 1 || calls[0].path != "/voice/speak" || calls[0].body != `{"text":"OK (done) now"}` {
		t.Fatalf("paren body not parsed: %+v", calls)
	}
	if rest != "Speaking" {
		t.Fatalf("stripped text = %q", rest)
	}
}

func TestHasPartialHWLinkMarker(t *testing.T) {
	for _, tc := range []struct {
		in   string
		want bool
	}{
		{`[Lights off. Hold on](HW:/led/of`, true},    // mid-stream, unclosed
		{`[Lights off](hw:`, true},                    // lowercase, unclosed
		{`All done [off](`, true},                     // ends inside signature
		{`[Lights off](HW:/led/off:{})`, false},       // complete link marker
		{`[HW:/led/off:{}]`, false},                   // canonical, not link form
		{`See the [docs](https://x.vn) here.`, false}, // plain link
	} {
		if got := hasPartialHWLinkMarker(tc.in); got != tc.want {
			t.Fatalf("hasPartialHWLinkMarker(%q) = %v, want %v", tc.in, got, tc.want)
		}
	}
}
