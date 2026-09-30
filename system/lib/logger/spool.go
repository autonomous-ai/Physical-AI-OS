package logger

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
)

// GELF spool: records that could not be shipped are kept on disk and replayed
// once the relay can deliver them.
//
// Why it exists: a device's first setup runs with no internet and no device
// key — the relay cannot be armed until setup saves the key, and it cannot
// deliver until WiFi joins. Without a spool every log line of a failed setup
// (WiFi join, internet check, first ping) is dropped, which is exactly the
// evidence needed to explain why a customer never got the device online. The
// spool also covers ordinary send failures (collector or uplink down).
//
// Bounded: an active file plus one backup, each at most maxBytes/2. When the
// active file fills it becomes the backup and the previous backup is discarded,
// so the oldest records go first and the spool never grows past maxBytes.

const (
	gelfSpoolMaxBytes = 1 << 20 // 1 MiB per service

	// gelfSpooledField marks a record that was held on the device before it
	// shipped, so Graylog can tell a replayed record from a live one.
	gelfSpooledField = "_spooled"
)

type gelfSpool struct {
	mu       sync.Mutex
	active   string
	backup   string
	replay   string
	maxBytes int64
	size     int64 // bytes in the active file
}

// newGELFSpool opens (creating if needed) the spool for one service under dir.
func newGELFSpool(dir, name string, maxBytes int64) (*gelfSpool, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create gelf spool dir: %w", err)
	}
	s := &gelfSpool{
		active:   filepath.Join(dir, name+".jsonl"),
		backup:   filepath.Join(dir, name+".1.jsonl"),
		replay:   filepath.Join(dir, name+".replay.jsonl"),
		maxBytes: maxBytes,
	}
	if fi, err := os.Stat(s.active); err == nil {
		s.size = fi.Size()
	}
	// Fail now rather than on the first record: an unwritable directory must
	// disable the spool, not turn every log call into an error.
	f, err := os.OpenFile(s.active, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("open gelf spool: %w", err)
	}
	f.Close()
	return s, nil
}

// append stores one record. Records larger than half the budget are dropped:
// they could never fit next to anything else.
func (s *gelfSpool) append(body []byte) {
	half := s.maxBytes / 2
	n := int64(len(body)) + 1
	if n > half {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.size+n > half {
		// Active file is full: it becomes the backup, the old backup goes.
		if err := os.Rename(s.active, s.backup); err != nil && !errors.Is(err, os.ErrNotExist) {
			fmt.Fprintf(os.Stderr, "[gelf] spool rotate: %v\n", err)
			return
		}
		s.size = 0
	}
	f, err := os.OpenFile(s.active, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		fmt.Fprintf(os.Stderr, "[gelf] spool open: %v\n", err)
		return
	}
	defer f.Close()
	line := append(append(make([]byte, 0, n), body...), '\n')
	if _, err := f.Write(line); err != nil {
		fmt.Fprintf(os.Stderr, "[gelf] spool write: %v\n", err)
		return
	}
	s.size += n
}

// pending reports whether anything is waiting to be replayed.
func (s *gelfSpool) pending() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.size > 0 || fileHasData(s.backup) || fileHasData(s.replay)
}

// take moves everything spooled into the replay file, oldest first: an
// unfinished earlier replay, then the backup, then the active file. New
// records keep appending to a fresh active file while the backlog drains.
// The result is trimmed from the front to the spool budget, so repeated failed
// drains cannot grow it without bound.
func (s *gelfSpool) take() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	var buf bytes.Buffer
	for _, p := range []string{s.replay, s.backup, s.active} {
		data, err := os.ReadFile(p)
		if err != nil {
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			return fmt.Errorf("read gelf spool %s: %w", p, err)
		}
		buf.Write(data)
	}
	data := trimToBudget(buf.Bytes(), s.maxBytes)
	tmp := s.replay + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return fmt.Errorf("write gelf replay: %w", err)
	}
	if err := os.Rename(tmp, s.replay); err != nil {
		return fmt.Errorf("commit gelf replay: %w", err)
	}
	_ = os.Remove(s.backup)
	if err := os.Truncate(s.active, 0); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("reset gelf spool: %w", err)
	}
	s.size = 0
	return nil
}

// replayLines returns the records waiting in the replay file.
func (s *gelfSpool) replayLines() ([][]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	f, err := os.Open(s.replay)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, fmt.Errorf("open gelf replay: %w", err)
	}
	defer f.Close()
	var lines [][]byte
	r := bufio.NewReader(f)
	for {
		line, err := r.ReadBytes('\n')
		if trimmed := bytes.TrimSpace(line); len(trimmed) > 0 {
			lines = append(lines, trimmed)
		}
		if err != nil {
			if err == io.EOF {
				return lines, nil
			}
			return lines, fmt.Errorf("read gelf replay: %w", err)
		}
	}
}

// keepReplay rewrites the replay file with the records not delivered yet, or
// removes it when everything shipped.
func (s *gelfSpool) keepReplay(rest [][]byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(rest) == 0 {
		if err := os.Remove(s.replay); err != nil && !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("remove gelf replay: %w", err)
		}
		return nil
	}
	var buf bytes.Buffer
	for _, l := range rest {
		buf.Write(l)
		buf.WriteByte('\n')
	}
	tmp := s.replay + ".tmp"
	if err := os.WriteFile(tmp, buf.Bytes(), 0o644); err != nil {
		return fmt.Errorf("write gelf replay: %w", err)
	}
	return os.Rename(tmp, s.replay)
}

// prepareReplay tags a spooled record and, when it was logged before the
// device knew its id, moves it onto the current host so it is found under the
// device like every other record of that device. A record logged under another
// device id belongs to a previous setup (possibly a previous owner) and returns
// nil: it must never ship with the current device's key.
func prepareReplay(body []byte, preConfigHost, currentHost string) []byte {
	var m map[string]any
	if err := json.Unmarshal(body, &m); err != nil {
		return nil // corrupt line: skip it rather than stall the replay
	}
	h, _ := m["host"].(string)
	if h != preConfigHost && h != currentHost {
		return nil
	}
	m[gelfSpooledField] = "true"
	if h == preConfigHost && currentHost != "" && currentHost != preConfigHost {
		m["host"] = currentHost
	}
	out, err := json.Marshal(m)
	if err != nil {
		return nil
	}
	return out
}

// trimToBudget drops whole records from the front until data fits max.
func trimToBudget(data []byte, max int64) []byte {
	for int64(len(data)) > max {
		i := bytes.IndexByte(data, '\n')
		if i < 0 {
			return nil
		}
		data = data[i+1:]
	}
	return data
}

func fileHasData(p string) bool {
	fi, err := os.Stat(p)
	return err == nil && fi.Size() > 0
}
