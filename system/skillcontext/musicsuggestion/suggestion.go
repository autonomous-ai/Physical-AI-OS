// Package musicsuggestion logs per-user music suggestions and their outcome.
package musicsuggestion

import (
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type Event struct {
	TS      float64 `json:"ts"`      // Unix seconds
	Seq     int64   `json:"seq"`     // Unix nanoseconds (unique across restarts)
	Hour    int     `json:"hour"`    // hour of day (0-23)
	Trigger string  `json:"trigger"` // what triggered: "mood:tired" (mood is the only supported trigger — activity events route to wellbeing, not here)
	Query   string  `json:"query"`   // suggested song query (empty if text-only suggestion)
	Message string  `json:"message"` // suggestion text sent to user
	Status  string  `json:"status"`  // "pending", "accepted", "rejected", "expired"
	User    string  `json:"user"`    // who was suggested to
}

const (
	usersDir      = "/root/local/users"
	subdir        = "music-suggestions"
	fileSuffix    = ".jsonl"
	retentionDays = 7
)

type logger struct {
	mu   sync.Mutex
	file *os.File
	day  string
	user string
}

var global = &logger{}

func Init() {
	_ = os.MkdirAll(usersDir, 0o755)
	go cleanOldLogs()
}

// Log records a suggestion and returns its sequence number.
func Log(user, trigger, query, message string) int64 {
	now := time.Now()
	seq := now.UnixNano()

	evt := Event{
		TS:      float64(now.UnixNano()) / 1e9,
		Seq:     seq,
		Hour:    now.Hour(),
		Trigger: trigger,
		Query:   query,
		Message: message,
		Status:  "pending",
		User:    user,
	}

	global.mu.Lock()
	global.writeJSONL(now, user, evt)
	global.mu.Unlock()

	slog.Info("music suggestion logged", "component", "music-suggestion", "user", user, "trigger", trigger, "seq", seq)
	return seq
}

// UpdateStatus rewrites the status of event seq in the day's file.
func UpdateStatus(user, day string, seq int64, status string) bool {
	path := filePath(user, day)

	global.mu.Lock()
	defer global.mu.Unlock()

	if global.file != nil && global.day == day && global.user == user {
		_ = global.file.Close()
		global.file = nil
	}

	data, err := os.ReadFile(path)
	if err != nil {
		return false
	}

	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	found := false
	for i, line := range lines {
		if line == "" {
			continue
		}
		var evt Event
		if err := json.Unmarshal([]byte(line), &evt); err != nil {
			continue
		}
		if evt.Seq == seq {
			evt.Status = status
			b, _ := json.Marshal(evt)
			lines[i] = string(b)
			found = true
			break
		}
	}
	if !found {
		return false
	}

	content := strings.Join(lines, "\n") + "\n"
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		slog.Error("music-suggestion: failed to update status", "path", path, "error", err)
		return false
	}
	return true
}

// Query reads suggestion events for a given user and day.
func Query(user string, day string, n int) []Event {
	global.mu.Lock()
	if global.file != nil && global.day == day && global.user == user {
		_ = global.file.Sync()
	}
	global.mu.Unlock()

	path := filePath(user, day)
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}

	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) == 0 || (len(lines) == 1 && lines[0] == "") {
		return nil
	}

	if n > 0 && len(lines) > n {
		lines = lines[len(lines)-n:]
	}

	events := make([]Event, 0, len(lines))
	for _, line := range lines {
		if line == "" {
			continue
		}
		var evt Event
		if err := json.Unmarshal([]byte(line), &evt); err == nil {
			events = append(events, evt)
		}
	}
	return events
}

// LastSuggestion returns the user's most recent suggestion (today or yesterday).
func LastSuggestion(user string) *Event {
	now := time.Now()
	for _, day := range []string{now.Format("2006-01-02"), now.AddDate(0, 0, -1).Format("2006-01-02")} {
		events := Query(user, day, 0)
		if len(events) > 0 {
			last := events[len(events)-1]
			return &last
		}
	}
	return nil
}

// Days returns the list of dates (YYYY-MM-DD) that have suggestion logs for a user.
func Days(user string) []string {
	dir := filepath.Join(usersDir, user, subdir)
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var days []string
	for _, e := range entries {
		name := e.Name()
		if strings.HasSuffix(name, fileSuffix) {
			days = append(days, strings.TrimSuffix(name, fileSuffix))
		}
	}
	return days
}

func filePath(user, day string) string {
	return filepath.Join(usersDir, user, subdir, day+fileSuffix)
}

func (l *logger) writeJSONL(now time.Time, user string, evt Event) {
	day := now.Format("2006-01-02")

	if l.day != day || l.user != user || l.file == nil {
		if l.file != nil {
			_ = l.file.Close()
		}
		path := filePath(user, day)
		_ = os.MkdirAll(filepath.Dir(path), 0o755)
		f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
		if err != nil {
			slog.Error("music-suggestion: failed to open log file", "path", path, "error", err)
			l.file = nil
			return
		}
		l.file = f
		l.day = day
		l.user = user
	}
	b, _ := json.Marshal(evt)
	_, _ = l.file.Write(append(b, '\n'))
}

func cleanOldLogs() {
	cutoff := time.Now().AddDate(0, 0, -retentionDays).Format("2006-01-02")

	userDirs, err := os.ReadDir(usersDir)
	if err != nil {
		return
	}
	for _, ud := range userDirs {
		if !ud.IsDir() || strings.HasPrefix(ud.Name(), ".") {
			continue
		}
		suggDir := filepath.Join(usersDir, ud.Name(), subdir)
		cleanDir(suggDir, cutoff)
	}
}

func cleanDir(dir, cutoff string) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, e := range entries {
		name := e.Name()
		if !strings.HasSuffix(name, fileSuffix) {
			continue
		}
		date := strings.TrimSuffix(name, fileSuffix)
		if date < cutoff {
			path := filepath.Join(dir, name)
			if err := os.Remove(path); err == nil {
				slog.Info("removed old music suggestion log", "component", "music-suggestion", "file", path)
			}
		}
	}
}
