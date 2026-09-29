// Package posture logs per-user posture alerts, nudges and praises to daily JSONL.
package posture

import (
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/system/lib/usercanon"
)

// Event is one posture history record; only fields relevant to Action are set.
type Event struct {
	TS     float64 `json:"ts"`
	Seq    int64   `json:"seq"`
	Hour   int     `json:"hour"`
	Action string  `json:"action"`

	// Alert-row fields.
	Score      int    `json:"score,omitempty"`
	Risk       string `json:"risk,omitempty"` // medium | high (hal filters lower)
	LeftScore  int    `json:"left_score,omitempty"`
	RightScore int    `json:"right_score,omitempty"`

	// Nudge-row fields.
	NudgeLevel int `json:"nudge_level,omitempty"` // 2..5

	// Notes is the spoken line for nudge/praise rows.
	Notes string `json:"notes,omitempty"`
}

// Action constants; readers rely on this fixed vocabulary.
const (
	ActionAlert  = "posture_alert"  // sensing handler auto-write on bad-window motion.activity
	ActionNudge  = "nudge_posture"  // agent spoke / fired servo / chime
	ActionPraise = "praise_posture" // agent acknowledged a fix
)

const (
	postureSubdir = "posture"
	fileSuffix    = ".jsonl"
	// retentionDays allows weekly/monthly trends (~16 KB/day per user).
	retentionDays = 60
	DefaultUser   = "unknown"
)

type logger struct {
	mu   sync.Mutex
	seqN atomic.Int64
	file *os.File
	day  string
	user string
}

var global = &logger{}

// Init creates the users root and starts the retention cleaner; call once.
func Init() {
	_ = os.MkdirAll(usercanon.UsersDir, 0o755)
	go cleanOldLogs()
}

// AlertExtras carries the HAL posture event facts to persist.
type AlertExtras struct {
	Score      int
	Risk       string
	LeftScore  int
	RightScore int
}

// LogAlert appends a `posture_alert` row capturing the hal event facts.
func LogAlert(user string, e AlertExtras) {
	user = usercanon.Resolve(user)
	now := time.Now()
	global.mu.Lock()
	defer global.mu.Unlock()
	evt := Event{
		TS:         float64(now.UnixNano()) / 1e9,
		Seq:        global.seqN.Add(1),
		Hour:       now.Hour(),
		Action:     ActionAlert,
		Score:      e.Score,
		Risk:       e.Risk,
		LeftScore:  e.LeftScore,
		RightScore: e.RightScore,
	}
	global.writeJSONL(now, user, evt)
}

// LogNudge appends a nudge_posture row; level is 2..5, notes is the spoken line (may be empty).
func LogNudge(user string, level int, notes string) {
	user = usercanon.Resolve(user)
	now := time.Now()
	global.mu.Lock()
	defer global.mu.Unlock()
	evt := Event{
		TS:         float64(now.UnixNano()) / 1e9,
		Seq:        global.seqN.Add(1),
		Hour:       now.Hour(),
		Action:     ActionNudge,
		NudgeLevel: level,
		Notes:      notes,
	}
	global.writeJSONL(now, user, evt)
}

// LogPraise appends a `praise_posture` row.
func LogPraise(user, notes string) {
	logSimple(user, ActionPraise, notes)
}

func logSimple(user, action, notes string) {
	user = usercanon.Resolve(user)
	now := time.Now()
	global.mu.Lock()
	defer global.mu.Unlock()
	evt := Event{
		TS:     float64(now.UnixNano()) / 1e9,
		Seq:    global.seqN.Add(1),
		Hour:   now.Hour(),
		Action: action,
		Notes:  notes,
	}
	global.writeJSONL(now, user, evt)
}

// QueryLastDays returns events from the last days files (1 = today), oldest first.
// perDayCap limits rows per file (0 = no cap).
func QueryLastDays(user string, days, perDayCap int) []Event {
	user = usercanon.Resolve(user)
	if days <= 0 {
		days = 1
	}
	now := time.Now()
	out := make([]Event, 0, days*8)
	for i := days - 1; i >= 0; i-- {
		day := now.AddDate(0, 0, -i).Format("2006-01-02")
		out = append(out, Query(user, day, perDayCap)...)
	}
	return out
}

// Query returns up to the last n rows for user on day; n <= 0 returns all.
func Query(user, day string, n int) []Event {
	user = usercanon.Resolve(user)
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
	out := make([]Event, 0, len(lines))
	for _, line := range lines {
		if line == "" {
			continue
		}
		var evt Event
		if err := json.Unmarshal([]byte(line), &evt); err == nil {
			out = append(out, evt)
		}
	}
	return out
}

// LastActionTS returns the Unix time of the latest action within lookbackDays, or 0.
func LastActionTS(user, action string, lookbackDays int) float64 {
	user = usercanon.Resolve(user)
	if lookbackDays <= 0 {
		lookbackDays = 1
	}
	now := time.Now()
	for i := 0; i < lookbackDays; i++ {
		day := now.AddDate(0, 0, -i).Format("2006-01-02")
		data, err := os.ReadFile(filePath(user, day))
		if err != nil {
			continue
		}
		lines := strings.Split(strings.TrimRight(string(data), "\n"), "\n")
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

// LastNudgeLevel returns today's most recent nudge level, or 0.
func LastNudgeLevel(user string) int {
	events := Query(user, time.Now().Format("2006-01-02"), 0)
	for i := len(events) - 1; i >= 0; i-- {
		if events[i].Action == ActionNudge {
			return events[i].NudgeLevel
		}
	}
	return 0
}

func filePath(user, day string) string {
	return filepath.Join(usercanon.UsersDir, user, postureSubdir, day+fileSuffix)
}

// writeJSONL appends evt to the user's daily file. Caller must hold l.mu.
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
			slog.Error("posture: failed to open log file", "path", path, "error", err)
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

// cleanOldLogs removes files older than retentionDays (startup, then daily).
func cleanOldLogs() {
	for {
		cutoff := time.Now().AddDate(0, 0, -retentionDays).Format("2006-01-02")
		entries, err := os.ReadDir(usercanon.UsersDir)
		if err == nil {
			for _, userDir := range entries {
				if !userDir.IsDir() {
					continue
				}
				dir := filepath.Join(usercanon.UsersDir, userDir.Name(), postureSubdir)
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
