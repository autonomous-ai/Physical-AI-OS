package buddy

import (
	"encoding/base64"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
)

// CharsRoot is where received character folders are written.
const CharsRoot = "/opt/claude-desktop-buddy/chars"

// Transfer handles an in-progress folder push from Claude Desktop.
// Only one transfer is active at a time (single BLE connection).
type Transfer struct {
	charName     string
	charDir      string
	totalBytes   int
	currentPath  string
	currentSize  int
	currentFile  *os.File
	currentBytes int
	totalWritten int
}

// Begin starts a new char transfer. The previous transfer (if any) is aborted.
func (t *Transfer) Begin(name string, total int) error {
	t.Abort()
	safeName, err := sanitizeName(name)
	if err != nil {
		return err
	}
	dir := filepath.Join(CharsRoot, safeName)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("mkdir %s: %w", dir, err)
	}
	t.charName = name
	t.charDir = dir
	t.totalBytes = total
	t.totalWritten = 0
	log.Printf("[xfer] begin char=%q total=%d dir=%s", name, total, dir)
	return nil
}

// StartFile opens a new file for writing under the current char directory.
// Closes any previously-open file without error.
func (t *Transfer) StartFile(relPath string, size int) error {
	t.closeCurrent()
	if t.charDir == "" {
		return fmt.Errorf("file before char_begin")
	}
	clean, err := sanitizeRelPath(relPath)
	if err != nil {
		return err
	}
	full := filepath.Join(t.charDir, clean)
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		return fmt.Errorf("mkdir %s: %w", filepath.Dir(full), err)
	}
	f, err := os.Create(full)
	if err != nil {
		return fmt.Errorf("create %s: %w", full, err)
	}
	t.currentFile = f
	t.currentPath = clean
	t.currentSize = size
	t.currentBytes = 0
	log.Printf("[xfer] file path=%q size=%d", clean, size)
	return nil
}

// WriteChunk decodes base64 data and appends it to the current file.
// Returns the number of bytes written to the current file so far.
func (t *Transfer) WriteChunk(b64 string) (int, error) {
	if t.currentFile == nil {
		return 0, fmt.Errorf("chunk before file")
	}
	raw, err := base64.StdEncoding.DecodeString(b64)
	if err != nil {
		return t.currentBytes, fmt.Errorf("decode chunk: %w", err)
	}
	n, err := t.currentFile.Write(raw)
	if err != nil {
		return t.currentBytes, fmt.Errorf("write chunk: %w", err)
	}
	t.currentBytes += n
	t.totalWritten += n
	return t.currentBytes, nil
}

// EndFile closes the current file and returns its final size.
func (t *Transfer) EndFile() (int, error) {
	n := t.currentBytes
	path := t.currentPath
	if err := t.closeCurrent(); err != nil {
		return n, err
	}
	log.Printf("[xfer] file_end path=%q bytes=%d", path, n)
	return n, nil
}

// End closes the transfer and logs a summary.
func (t *Transfer) End() {
	_ = t.closeCurrent()
	log.Printf("[xfer] end char=%q total_written=%d (expected=%d) dir=%s",
		t.charName, t.totalWritten, t.totalBytes, t.charDir)
	*t = Transfer{}
}

// Abort discards any in-progress state without touching the filesystem.
func (t *Transfer) Abort() {
	_ = t.closeCurrent()
	*t = Transfer{}
}

func (t *Transfer) closeCurrent() error {
	if t.currentFile == nil {
		return nil
	}
	err := t.currentFile.Close()
	t.currentFile = nil
	return err
}

// sanitizeName ensures a char name has no path separators or traversal.
func sanitizeName(name string) (string, error) {
	if name == "" {
		return "", fmt.Errorf("empty char name")
	}
	if strings.ContainsAny(name, "/\\") || name == "." || name == ".." {
		return "", fmt.Errorf("invalid char name: %q", name)
	}
	return name, nil
}

// sanitizeRelPath rejects absolute paths and any `..` segments after cleaning.
func sanitizeRelPath(p string) (string, error) {
	if p == "" {
		return "", fmt.Errorf("empty path")
	}
	if filepath.IsAbs(p) {
		return "", fmt.Errorf("absolute path not allowed: %q", p)
	}
	clean := filepath.Clean(p)
	if clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("path escapes root: %q", p)
	}
	return clean, nil
}
