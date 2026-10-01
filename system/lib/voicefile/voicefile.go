// Package voicefile lists, reads and deletes enrolled voice samples under
// users/<name>/voice/ — the file operations behind the web Voice settings,
// shared by the HTTP API and MQTT.
package voicefile

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// UsersDir is HAL's per-person data root.
const UsersDir = "/root/local/users"

// sharedBucket is HAL's log bucket for unidentified people; never an enrolled person.
const sharedBucket = "unknown"

// speakerRemoveURL drops the whole voice profile once its last WAV is gone.
const speakerRemoveURL = "http://127.0.0.1:5001/speaker/remove"

// Error is a refusal the caller reports as-is; Status is the matching HTTP code.
type Error struct {
	Status int
	Msg    string
}

func (e *Error) Error() string { return e.Msg }

func fail(status int, msg string) error { return &Error{Status: status, Msg: msg} }

// Owner is one person with files in their voice folder.
type Owner struct {
	Label        string   `json:"label"`
	VoiceSamples []string `json:"voice_samples"`
}

// RemoveResult reports one sample deletion; ProfileRemoved means it was the last WAV.
type RemoveResult struct {
	Deleted        string `json:"deleted"`
	Remaining      int    `json:"remaining"`
	ProfileRemoved bool   `json:"profile_removed"`
}

// PathComponent reports whether name is a single, non-special path component.
func PathComponent(name string) bool {
	return name != "" && name != "." && name != ".." && !strings.ContainsAny(name, "/\\\x00")
}

// NormalizeName is how the web addresses a profile: trimmed and lowercased.
func NormalizeName(name string) string { return strings.ToLower(strings.TrimSpace(name)) }

// OpenDir opens users/<name>/voice as a root. Retained directory handles keep a
// concurrent rename or symlink swap from redirecting access outside the profile.
func OpenDir(base, name string) (*os.Root, error) {
	if !PathComponent(name) {
		return nil, fmt.Errorf("invalid voice profile name")
	}
	users, err := os.OpenRoot(base)
	if err != nil {
		return nil, err
	}
	defer users.Close()
	profile, err := users.OpenRoot(name)
	if err != nil {
		return nil, err
	}
	defer profile.Close()
	return profile.OpenRoot("voice")
}

// List returns everyone whose voice folder holds at least one file, with the
// file names sorted — the "Voice Files" list of the web Voice settings.
func List(base string) ([]Owner, error) {
	entries, err := os.ReadDir(base)
	if err != nil {
		if os.IsNotExist(err) {
			return []Owner{}, nil
		}
		return nil, err
	}
	owners := []Owner{}
	for _, e := range entries {
		name := e.Name()
		if !e.IsDir() || strings.HasPrefix(name, ".") || name == sharedBucket {
			continue
		}
		dir, err := OpenDir(base, name)
		if err != nil {
			continue
		}
		files, err := regularFiles(dir)
		dir.Close()
		if err != nil || len(files) == 0 {
			continue
		}
		owners = append(owners, Owner{Label: name, VoiceSamples: files})
	}
	return owners, nil
}

// Read returns one file from users/<name>/voice/, refusing anything over maxBytes.
func Read(base, name, file string, maxBytes int64) ([]byte, error) {
	name = NormalizeName(name)
	file = strings.TrimSpace(file)
	if !PathComponent(name) || !PathComponent(file) {
		return nil, fail(http.StatusBadRequest, "invalid name or file")
	}
	dir, err := OpenDir(base, name)
	if err != nil {
		return nil, openError(err)
	}
	defer dir.Close()
	info, err := dir.Lstat(file)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, fail(http.StatusNotFound, "file not found")
		}
		return nil, fail(http.StatusBadRequest, "invalid sample path")
	}
	if !info.Mode().IsRegular() {
		return nil, fail(http.StatusBadRequest, "sample must be a regular file")
	}
	if info.Size() > maxBytes {
		return nil, fail(http.StatusRequestEntityTooLarge, fmt.Sprintf("file exceeds %d bytes", maxBytes))
	}
	f, err := dir.Open(file)
	if err != nil {
		return nil, fail(http.StatusBadRequest, "invalid sample path")
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, maxBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", file, err)
	}
	if int64(len(data)) > maxBytes {
		return nil, fail(http.StatusRequestEntityTooLarge, fmt.Sprintf("file exceeds %d bytes", maxBytes))
	}
	return data, nil
}

// Remove deletes one audio sample and its .npy embedding sidecar. When no WAV
// is left it asks HAL to drop the whole voice profile, so list endpoints don't
// show a phantom user with 0 samples.
func Remove(base, name, file string) (*RemoveResult, error) {
	name = NormalizeName(name)
	file = strings.TrimSpace(file)
	// Both profile and sample names must be single path components.
	if !PathComponent(name) || !PathComponent(file) {
		return nil, fail(http.StatusBadRequest, "invalid name or file")
	}
	// Audio samples only: a .npy goes with its WAV, and metadata.json is
	// profile state.
	switch strings.ToLower(filepath.Ext(file)) {
	case ".wav", ".ogg", ".mp3", ".webm", ".m4a":
	default:
		return nil, fail(http.StatusBadRequest, "only audio samples can be deleted")
	}

	target := filepath.Join(base, name, "voice", file)
	dir, err := OpenDir(base, name)
	if err != nil {
		return nil, openError(err)
	}
	defer dir.Close()
	info, err := dir.Stat(file)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, fail(http.StatusNotFound, "file not found")
		}
		return nil, fail(http.StatusBadRequest, "invalid sample path")
	}
	if !info.Mode().IsRegular() {
		return nil, fail(http.StatusBadRequest, "sample must be a regular file")
	}
	if err := dir.Remove(file); err != nil {
		slog.Warn("voice file remove failed", "component", "voice", "path", target, "error", err)
		return nil, fail(http.StatusInternalServerError, "delete failed: "+err.Error())
	}
	// Remove the .npy sidecar too: the UI cannot delete a .npy directly, so an orphan would linger.
	sidecar := strings.TrimSuffix(file, filepath.Ext(file)) + ".npy"
	if err := dir.Remove(sidecar); err != nil && !os.IsNotExist(err) {
		slog.Warn("voice sidecar remove failed", "component", "voice", "path", sidecar, "error", err)
	}
	slog.Info("voice file deleted", "component", "voice", "name", name, "file", file)

	remaining, err := countWAVs(dir)
	if err != nil {
		return nil, fail(http.StatusInternalServerError, "cannot inspect remaining voice samples")
	}
	if remaining == 0 {
		body, _ := json.Marshal(map[string]any{"name": name})
		resp, err := http.Post(speakerRemoveURL, "application/json", bytes.NewReader(body))
		if err != nil {
			slog.Warn("speaker/remove call failed", "component", "voice", "error", err)
		} else {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}
		return &RemoveResult{Deleted: file, ProfileRemoved: true}, nil
	}

	// No re-enroll: the bank is one row per WAV, and enroll would duplicate
	// the remaining samples.
	slog.Info("voice file deleted", "component", "voice", "name", name,
		"file", file, "remaining", remaining)
	return &RemoveResult{Deleted: file, Remaining: remaining}, nil
}

// ErrorStatus maps err to an HTTP status: an *Error keeps its own, anything else is 500.
func ErrorStatus(err error) int {
	var e *Error
	if errors.As(err, &e) {
		return e.Status
	}
	return http.StatusInternalServerError
}

func openError(err error) error {
	if os.IsNotExist(err) {
		return fail(http.StatusNotFound, "file not found")
	}
	return fail(http.StatusBadRequest, "invalid voice directory")
}

func regularFiles(dir *os.Root) ([]string, error) {
	d, err := dir.Open(".")
	if err != nil {
		return nil, err
	}
	entries, err := d.ReadDir(-1)
	d.Close()
	if err != nil {
		return nil, err
	}
	files := []string{}
	for _, e := range entries {
		if e.Type().IsRegular() {
			files = append(files, e.Name())
		}
	}
	sort.Strings(files)
	return files, nil
}

func countWAVs(dir *os.Root) (int, error) {
	d, err := dir.Open(".")
	if err != nil {
		return 0, err
	}
	entries, err := d.ReadDir(-1)
	d.Close()
	if err != nil {
		return 0, err
	}
	n := 0
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(strings.ToLower(e.Name()), ".wav") {
			n++
		}
	}
	return n, nil
}
