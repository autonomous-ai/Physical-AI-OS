// Package wellbeing logs per-user activity history from motion.activity events.
package wellbeing

import (
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Event is one wellbeing activity record persisted to JSONL.
type Event struct {
	TS     float64 `json:"ts"`     // Unix seconds
	Seq    int64   `json:"seq"`    // global sequence
	Hour   int     `json:"hour"`   // hour of day (0-23)
	Action string  `json:"action"` // drink, break, celebrate, raw sedentary/eat labels, yawning, enter, leave
	Notes  string  `json:"notes"`  // optional agent observation
}

const (
	usersDir         = "/root/local/users"
	wellbeingSubdir  = "wellbeing"
	fileSuffix       = ".jsonl"
	DefaultUser      = "unknown"
	maxNormalizedLen = 64
	retentionDays    = 30
)

var reNonLabel = regexp.MustCompile(`[^a-z0-9_-]+`)

type logger struct {
	mu   sync.Mutex
	seqN atomic.Int64
	file *os.File
	day  string
	user string
}

var global = &logger{}

// Init creates the users directory and starts the retention cleaner; call once.
func Init() {
	_ = os.MkdirAll(usersDir, 0o755)
	go cleanOldLogs()
}

// cleanOldLogs removes wellbeing files older than retentionDays (startup, then daily).
func cleanOldLogs() {
	for {
		cutoff := time.Now().AddDate(0, 0, -retentionDays).Format("2006-01-02")
		entries, err := os.ReadDir(usersDir)
		if err == nil {
			for _, userDir := range entries {
				if !userDir.IsDir() {
					continue
				}
				dir := filepath.Join(usersDir, userDir.Name(), wellbeingSubdir)
				files, err := os.ReadDir(dir)
				if err != nil {
					continue
				}
				for _, f := range files {
					name := f.Name()
					if !strings.HasSuffix(name, fileSuffix) {
						continue
					}
					day := strings.TrimSuffix(name, fileSuffix)
					if day < cutoff {
						_ = os.Remove(filepath.Join(dir, name))
					}
				}
			}
		}
		time.Sleep(24 * time.Hour)
	}
}

// NormalizeUser mirrors Python normalize_label so Go and Python paths match.
func NormalizeUser(name string) string {
	s := strings.ToLower(strings.TrimSpace(name))
	s = reNonLabel.ReplaceAllString(s, "_")
	s = strings.Trim(s, "_")
	if len(s) > maxNormalizedLen {
		s = s[:maxNormalizedLen]
	}
	if s == "" {
		return DefaultUser
	}
	return s
}

// presenceActions are deduped only on the "unknown" timeline (strangers collapse to
// one user); friend enter/leave comes from HAL session tracking and must not be deduped.
var presenceActions = map[string]bool{
	"enter": true,
	"leave": true,
}

// LogForUser appends an activity entry; unknown-user presence rows are deduped.
func LogForUser(user, action, notes string) {
	user = NormalizeUser(user)
	now := time.Now()

	global.mu.Lock()
	defer global.mu.Unlock()

	if presenceActions[action] && user == DefaultUser {
		day := now.Format("2006-01-02")
		lastPresence := readLastPresenceAction(user, day)
		if action == "enter" && lastPresence == "enter" {
			return
		}
		if action == "leave" && lastPresence != "enter" {
			return
		}
	}

	seq := global.seqN.Add(1)
	evt := Event{
		TS:     float64(now.UnixNano()) / 1e9,
		Seq:    seq,
		Hour:   now.Hour(),
		Action: action,
		Notes:  notes,
	}
	global.writeJSONL(now, user, evt)
}

// readLastPresenceAction returns today's most recent enter/leave for user, or "".
func readLastPresenceAction(user, day string) string {
	path := filePath(user, day)
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	s := strings.TrimRight(string(data), "\n")
	if s == "" {
		return ""
	}
	lines := strings.Split(s, "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		var evt Event
		if err := json.Unmarshal([]byte(lines[i]), &evt); err != nil {
			continue
		}
		if presenceActions[evt.Action] {
			return evt.Action
		}
	}
	return ""
}

// LastActionTS returns the Unix time of the latest action within lookbackDays
// (1 = today only), or 0 if none.
func LastActionTS(user, action string, lookbackDays int) float64 {
	user = NormalizeUser(user)
	if lookbackDays <= 0 {
		lookbackDays = 1
	}
	now := time.Now()
	for i := 0; i < lookbackDays; i++ {
		day := now.AddDate(0, 0, -i).Format("2006-01-02")
		path := filePath(user, day)
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		s := strings.TrimRight(string(data), "\n")
		if s == "" {
			continue
		}
		lines := strings.Split(s, "\n")
		for j := len(lines) - 1; j >= 0; j-- {
			var evt Event
			if err := json.Unmarshal([]byte(lines[j]), &evt); err != nil {
				continue
			}
			if evt.Action == action {
				return evt.TS
			}
		}
	}
	return 0
}

// Query returns up to the last n events for user on day (YYYY-MM-DD); n <= 0 returns all.
func Query(user, day string, n int) []Event {
	user = NormalizeUser(user)
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

func filePath(user, day string) string {
	return filepath.Join(usersDir, user, wellbeingSubdir, day+fileSuffix)
}

// writeJSONL appends evt to the user's daily file. Caller must hold mu.
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
			slog.Error("wellbeing: failed to open log file", "path", path, "error", err)
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
