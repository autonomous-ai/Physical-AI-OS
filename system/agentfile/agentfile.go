// Package agentfile decides which device files an agent turn may hand out (web chat
// and MQTT share these rules) and where user-attached files land.
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

// MaxBytes caps a single served file.
const MaxBytes = 32 << 20

// Types maps served extensions to Content-Type; others are refused before touching disk.
// .json and .log are excluded on purpose: runtime config JSON holds gateway tokens.
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

// runtimes whose output dirs are served (all, since old turns may predate a switch).
var runtimes = []string{"openclaw", "hermes", "picoclaw", "codex", "claudecode", "opencode"}

// Roots are the dirs agent output may be served from: each runtime's media/ and
// workspace/ (never its config dir) plus /tmp.
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

// Resolve validates raw against roots and returns the path and Content-Type.
// The extension is checked first so file existence cannot be probed.
func Resolve(raw string, roots []string) (path, contentType string, err error) {
	if raw == "" || !filepath.IsAbs(raw) {
		return "", "", ErrNotFound
	}

	ct, ok := Types[strings.ToLower(filepath.Ext(raw))]
	if !ok {
		return "", "", ErrType
	}

	// Resolve symlinks and `..` before comparing against roots.
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

// underAnyRoot reports whether resolved is inside a (resolved) root; the separator
// suffix stops "/media-evil" from passing as "/media".
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

// pathRE matches an absolute path under a served root with a served extension.
var pathRE = regexp.MustCompile(
	`(?i)(?:/root/\.[a-z0-9_-]+/(?:media|workspace)|/tmp)/[^\s"'` + "`" + `)<>\]\\]+\.` +
		`(?:jpg|jpeg|png|gif|webp|pdf|txt|md|csv|wav|mp3|mp4|webm)\b`)

// Scan returns de-duplicated candidate paths in order of appearance; Resolve still
// rules on each.
func Scan(text string) []string {
	if text == "" {
		return nil
	}
	var out []string
	seen := map[string]bool{}
	for _, m := range pathRE.FindAllString(text, -1) {
		p := strings.TrimRight(m, ".,;:")
		if seen[p] {
			continue
		}
		seen[p] = true
		out = append(out, p)
	}
	return out
}

// InboundMaxBytes caps one decoded attachment (matches the web composer's 10 MB check).
const InboundMaxBytes = 10 << 20

// extRE is the shape a client extension must have to be used verbatim.
var extRE = regexp.MustCompile(`^[a-z0-9]{1,8}$`)

// SaveInbound decodes a base64 attachment into dir and returns the path.
// name only supplies the extension (generated filename); unusable extensions become ".bin".
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

// SafeExt returns a safe leading-dot extension for name, or ".bin".
func SafeExt(name string) string {
	ext := strings.ToLower(strings.TrimPrefix(filepath.Ext(filepath.Base(name)), "."))
	if !extRE.MatchString(ext) {
		return ".bin"
	}
	return "." + ext
}
