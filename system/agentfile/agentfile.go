// Package agentfile owns the files that cross an agent turn's boundary in both
// directions: which device-local files a turn is allowed to hand OUT (and the
// scanner that finds the ones a turn named), and where a file attached IN by the
// user lands on disk.
//
// A turn can only NAME a file it produced — "take a photo" ends with an absolute
// path like /root/.openclaw/media/hal-snapshots/snap_*.jpg. Two very different
// consumers need to turn that into something a person can see:
//
//	GET /api/agent/file  (system/server/agent/delivery/http) — the web chat, on
//	                     the LAN, fetching the bytes directly
//	kind chat.file.get   (system/server/device/delivery/mqtt) — a phone, which
//	                     cannot reach the device at all, so it asks the backend
//	                     and the bytes come back over MQTT
//
// Both must agree on exactly what may leave the device. That is why the rules
// live here and not in either caller: a second copy is a second chance to widen
// one of them by accident.
package agentfile

import (
	"encoding/base64"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// MaxBytes caps a single file. Generous for a snapshot or a PDF, small enough
// that a stray path to something huge fails fast.
const MaxBytes = 32 << 20

// Types is the whitelist: extension → Content-Type. An extension that isn't here
// is refused before the filesystem is touched.
//
// Deliberately absent: `.json` and `.log`. Both are plausible agent output, but a
// runtime's own config JSON carries gateway tokens — the same reason
// GET /api/agent/config-json is loopback-only — and one mis-scoped root would
// turn a viewer into a credential leak.
var Types = map[string]string{
	".jpg":  "image/jpeg",
	".jpeg": "image/jpeg",
	".png":  "image/png",
	".gif":  "image/gif",
	".webp": "image/webp",
	".pdf":  "application/pdf",
	".txt":  "text/plain; charset=utf-8",
	".md":   "text/plain; charset=utf-8",
	".csv":  "text/csv; charset=utf-8",
	".wav":  "audio/wav",
	".mp3":  "audio/mpeg",
	".mp4":  "video/mp4",
	".webm": "video/webm",
}

// inlineExts is the subset rendered in place rather than downloaded.
var inlineExts = map[string]bool{
	".jpg": true, ".jpeg": true, ".png": true, ".gif": true, ".webp": true, ".pdf": true,
}

// Inline reports whether a file of this path should be shown in place.
func Inline(path string) bool {
	return inlineExts[strings.ToLower(filepath.Ext(path))]
}

// runtimes are the agentic backends whose output directories are served. All of
// them rather than just the active one: a conversation scrolled back far enough
// can reference a file written before a runtime switch, and the file is no more
// sensitive for having been made by the previous backend.
var runtimes = []string{"openclaw", "hermes", "picoclaw", "codex", "claudecode", "opencode"}

// Roots is where an agent's output is allowed to live.
//
// Per runtime this is media/ and workspace/ — NOT the runtime's config dir
// itself, which holds openclaw.json and friends. /tmp is included because the
// CLI-based runtimes genuinely write scratch output there; it is the reason the
// extension whitelist exists, since /tmp is shared with everything else on the
// device.
func Roots() []string {
	roots := make([]string, 0, len(runtimes)*2+1)
	for _, rt := range runtimes {
		home := "/root/." + rt
		roots = append(roots, filepath.Join(home, "media"), filepath.Join(home, "workspace"))
	}
	return append(roots, "/tmp")
}

var (
	// ErrType is an extension that is not served at all.
	ErrType = errors.New("file type not served")
	// ErrOutsideRoots is a path that resolved outside every allow-listed root.
	ErrOutsideRoots = errors.New("path is outside the served roots")
	// ErrNotFound covers absent, unreadable, non-regular and oversized.
	ErrNotFound = errors.New("file not found")
)

// Resolve validates raw against roots and returns the path to serve plus its
// Content-Type.
//
// Order matters: the extension is checked FIRST, so a probe for a file type we
// never serve cannot be used to test whether a path exists.
func Resolve(raw string, roots []string) (path, contentType string, err error) {
	if raw == "" || !filepath.IsAbs(raw) {
		return "", "", ErrNotFound
	}

	ct, ok := Types[strings.ToLower(filepath.Ext(raw))]
	if !ok {
		return "", "", ErrType
	}

	// Resolve BEFORE comparing: `..` segments and symlinks both let a path that
	// reads as being under a root actually land outside it.
	resolved, err := filepath.EvalSymlinks(filepath.Clean(raw))
	if err != nil {
		return "", "", ErrNotFound
	}

	if !underAnyRoot(resolved, roots) {
		return "", "", ErrOutsideRoots
	}

	// An allowed suffix on a symlink must not expose a forbidden target type.
	// Use the resolved target's type so HTTP/MQTT agree with the actual file.
	ct, ok = Types[strings.ToLower(filepath.Ext(resolved))]
	if !ok {
		return "", "", ErrType
	}

	info, err := os.Stat(resolved)
	if err != nil || !info.Mode().IsRegular() {
		return "", "", ErrNotFound
	}
	if info.Size() > MaxBytes {
		return "", "", ErrNotFound
	}
	return resolved, ct, nil
}

// underAnyRoot reports whether resolved sits inside one of roots. Roots are
// resolved too — /tmp is a symlink to /private/tmp on macOS, and a root that only
// matched textually would reject every real file under it.
//
// The separator suffix is what stops "/root/.openclaw/media-evil" from passing as
// being under "/root/.openclaw/media".
func underAnyRoot(resolved string, roots []string) bool {
	for _, root := range roots {
		r, err := filepath.EvalSymlinks(root)
		if err != nil {
			continue // root doesn't exist on this device — nothing can be under it
		}
		if resolved == r || strings.HasPrefix(resolved, r+string(os.PathSeparator)) {
			return true
		}
	}
	return false
}

// pathRE matches one absolute path under a served root ending in a served
// extension. Anchored to the roots so a turn mentioning /usr/bin/something or
// /etc/hosts produces no candidate at all.
//
// The character class stops at whatever normally terminates a path in prose,
// markdown or JSON — whitespace, quotes, brackets, backticks.
var pathRE = regexp.MustCompile(
	`(?i)(?:/root/\.[a-z0-9_-]+/(?:media|workspace)|/tmp)/[^\s"'` + "`" + `)<>\]\\]+\.` +
		`(?:jpg|jpeg|png|gif|webp|pdf|txt|md|csv|wav|mp3|mp4|webm)\b`)

// Scan returns the candidate device paths named in text, de-duplicated, in the
// order they appear. Candidates only — Resolve still rules on every one.
//
// Text is whatever a turn produced: the reply, a tool's arguments, a tool's
// result. All three matter and none is enough alone. Asked to send a photo, an
// agent typically calls its channel tool —
// {"action":"send","media":"/root/.openclaw/media/…jpg"} — and its spoken reply
// names no path; a `curl /camera/snapshot` puts the path in the tool result
// instead; and sometimes the agent just types the path out.
func Scan(text string) []string {
	if text == "" {
		return nil
	}
	var out []string
	seen := map[string]bool{}
	for _, m := range pathRE.FindAllString(text, -1) {
		// Trailing punctuation belongs to the sentence, not the filename.
		p := strings.TrimRight(m, ".,;:")
		if seen[p] {
			continue
		}
		seen[p] = true
		out = append(out, p)
	}
	return out
}

// ─── Inbound ─────────────────────────────────────────────────────────────────
//
// The other direction: a file the USER attached to a turn, which has to land on
// disk so the agent's tools can open it.
//
// This used to only work for images. Everything a client attached — a PDF, a
// CSV — was sent in the sensing event's `image` field and written as
// `/tmp/web-chat-<ms>.jpg`, so a document arrived mislabelled as a photo and
// then failed the describe-first vision gate. Inbound files now carry their own
// name and land with their real extension.

// InboundMaxBytes caps one attached file, decoded. Matches the web composer's
// 10 MB client-side check so the two can't disagree about what is acceptable.
const InboundMaxBytes = 10 << 20

// extRE is the shape an extension must have to be used verbatim: the name comes
// from the client, and it decides a path on disk.
var extRE = regexp.MustCompile(`^[a-z0-9]{1,8}$`)

// SaveInbound decodes a base64 attachment and writes it into dir, returning the
// path. name is used ONLY for its extension — the filename itself is generated,
// so a name like "../../etc/passwd" can never steer the write.
//
// An unusable extension degrades to ".bin" rather than failing: the agent can
// still be told a file is there, which beats dropping the user's attachment.
// Note the file lands in /tmp, which is an outbound root — but the outbound
// whitelist (Types) still governs what may be read back out, so an attachment
// of a type this device won't serve cannot be laundered into one that is.
func SaveInbound(dir, name, contentB64 string, stamp int64) (string, error) {
	if contentB64 == "" {
		return "", errors.New("empty attachment")
	}
	body, err := base64.StdEncoding.DecodeString(contentB64)
	if err != nil {
		return "", fmt.Errorf("decode attachment: %w", err)
	}
	if len(body) > InboundMaxBytes {
		return "", fmt.Errorf("attachment is %d bytes (max %d)", len(body), InboundMaxBytes)
	}

	path := filepath.Join(dir, fmt.Sprintf("chat-attachment-%d%s", stamp, SafeExt(name)))
	if err := os.WriteFile(path, body, 0644); err != nil {
		return "", fmt.Errorf("write attachment: %w", err)
	}
	return path, nil
}

// SafeExt returns the leading-dot extension to write a client-named file with,
// or ".bin" when the name carries nothing usable. Never returns a path
// separator or a multi-part suffix, so the result can be concatenated onto a
// generated filename safely.
func SafeExt(name string) string {
	ext := strings.ToLower(strings.TrimPrefix(filepath.Ext(filepath.Base(name)), "."))
	if !extRE.MatchString(ext) {
		return ".bin"
	}
	return "." + ext
}
