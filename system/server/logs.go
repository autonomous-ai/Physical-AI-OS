package server

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/syspath"
	"go.autonomous.ai/os/system/server/serializers"
)

// allowedLogs maps source names to their log file paths (supports glob
// patterns). Entries prefixed with "journal:" use journalctl instead of file
// reading.
var allowedLogs = map[string]string{
	"bootstrap":        "journal:bootstrap.service",
	"openclaw":         "/var/log/openclaw/agent.log",
	"openclaw-service": "journal:openclaw.service",
	"buddy":            "/var/log/claude-desktop-buddy.log",
}

// hermesAgentLog is Hermes's own rich per-turn agent log under
// $HERMES_DIR/logs (HERMES_DIR=/root/.hermes, see runtimes/hermes/install.sh)
// — the analogue of openclaw's agent.log file.
const hermesAgentLog = "/root/.hermes/logs/agent.log"

// picoclawAgentLog is PicoClaw's own gateway log under /root/.picoclaw/logs
const picoclawAgentLog = "/root/.picoclaw/logs/gateway.log"

// resolveLogSource maps a web log-source id to its file/journal pattern, with
// one runtime-aware twist: the generic "Agent"/"Agent Service" tabs follow
// whichever agentic backend is ACTIVE.
func (s *Server) resolveLogSource(source string) (string, bool) {
	runtime := device.CurrentAgentRuntimeFromConfig(s.config)

	bridgeLog := syspath.AgentBridgeLog()
	journalOrBridge := func(unit string) string {
		if bridgeLog != "" {
			return bridgeLog
		}
		return unit
	}

	switch source {
	case "hal":
		return syspath.HALLogFile(), true
	case "os-server":
		return syspath.LogFile(), true
	case "hermes":
		return hermesAgentLog, true
	case "picoclaw":
		return picoclawAgentLog, true
	case "codex":
		return journalOrBridge("journal:codex.service"), true
	case "claudecode":
		return journalOrBridge("journal:claudecode.service"), true
	case "opencode":
		return journalOrBridge("journal:opencode.service"), true
	case "openclaw":
		switch runtime {
		case domain.AgentRuntimeHermes:
			return hermesAgentLog, true
		case domain.AgentRuntimePicoclaw:
			return picoclawAgentLog, true
		case domain.AgentRuntimeCodex:
			return journalOrBridge("journal:codex.service"), true
		case domain.AgentRuntimeClaudeCode:
			return journalOrBridge("journal:claudecode.service"), true
		case domain.AgentRuntimeOpenCode:
			return journalOrBridge("journal:opencode.service"), true
		}
		return resolveOpenclawLog(), true
	case "openclaw-service":
		switch runtime {
		case domain.AgentRuntimeHermes:
			return "journal:hermes-gateway.service", true
		case domain.AgentRuntimePicoclaw:
			return "journal:picoclaw.service", true
		case domain.AgentRuntimeCodex:
			return journalOrBridge("journal:codex.service"), true
		case domain.AgentRuntimeClaudeCode:
			return journalOrBridge("journal:claudecode.service"), true
		case domain.AgentRuntimeOpenCode:
			return journalOrBridge("journal:opencode.service"), true
		}
	}
	p, ok := allowedLogs[source]
	return p, ok
}

// resolveOpenclawLog returns the openclaw log path, falling back to the newest
// file in /tmp/openclaw/ when the configured path does not exist.
func resolveOpenclawLog() string {
	primary := allowedLogs["openclaw"]
	if info, err := os.Stat(primary); err == nil && info.Size() > 0 {
		return primary
	}
	matches, _ := filepath.Glob("/tmp/openclaw/openclaw-*.log")
	if len(matches) == 0 {
		return primary
	}
	sort.Strings(matches)
	return matches[len(matches)-1] // newest date-stamped file
}

// resolveLogPaths expands a pattern (plain path or glob) to matching files.
func resolveLogPaths(pattern string) ([]string, error) {
	if !strings.ContainsAny(pattern, "*?[") {
		return []string{pattern}, nil
	}
	matches, err := filepath.Glob(pattern)
	if err != nil {
		return nil, fmt.Errorf("glob: %w", err)
	}
	sort.Strings(matches)
	return matches, nil
}

// logTail returns the last N lines of a whitelisted log file (or merged glob).
// GET /api/logs/tail?source=hal|os-server|bootstrap|openclaw|openclaw-service|buddy&lines=200
func (s *Server) logTail(c *gin.Context) {
	source := c.DefaultQuery("source", "os-server")
	pattern, ok := s.resolveLogSource(source)
	if !ok {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("unknown log source"))
		return
	}

	n, _ := strconv.Atoi(c.DefaultQuery("lines", "200"))
	if n <= 0 || n > 5000 {
		n = 200
	}

	if strings.HasPrefix(pattern, "journal:") {
		unit := strings.TrimPrefix(pattern, "journal:")
		lines, err := journalTail(unit, n)
		errMsg := ""
		if err != nil {
			errMsg = err.Error()
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
			"source": source,
			"path":   "journalctl -u " + unit,
			"lines":  redactLogLines(lines),
			"error":  errMsg,
		}))
		return
	}

	paths, err := resolveLogPaths(pattern)
	if err != nil || len(paths) == 0 {
		errMsg := "no log files found"
		if err != nil {
			errMsg = err.Error()
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
			"source": source,
			"path":   pattern,
			"lines":  []string{},
			"error":  errMsg,
		}))
		return
	}

	var allLines []string
	for _, p := range paths {
		lines, _ := tailFile(p, n)
		allLines = append(allLines, lines...)
	}
	if len(allLines) > n {
		allLines = allLines[len(allLines)-n:]
	}

	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"source": source,
		"path":   pattern,
		"lines":  redactLogLines(allLines),
	}))
}

// logStream streams new log lines via SSE from one or more log files.
// GET /api/logs/stream?source=hal|os-server|bootstrap|openclaw|openclaw-service|buddy
func (s *Server) logStream(c *gin.Context) {
	source := c.DefaultQuery("source", "os-server")
	pattern, ok := s.resolveLogSource(source)
	if !ok {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("unknown log source"))
		return
	}

	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	c.Header("X-Accel-Buffering", "no")

	if strings.HasPrefix(pattern, "journal:") {
		unit := strings.TrimPrefix(pattern, "journal:")
		s.streamJournal(c, unit)
		return
	}

	paths, err := resolveLogPaths(pattern)
	if err != nil || len(paths) == 0 {
		errMsg := "no log files found"
		if err != nil {
			errMsg = err.Error()
		}
		c.SSEvent("error", errMsg)
		return
	}

	type fileTail struct {
		f      *os.File
		reader *bufio.Reader
	}
	var tails []fileTail
	for _, p := range paths {
		f, err := os.Open(p)
		if err != nil {
			continue
		}
		_, _ = f.Seek(0, 2)
		tails = append(tails, fileTail{f: f, reader: bufio.NewReader(f)})
	}
	if len(tails) == 0 {
		c.SSEvent("error", "cannot open any log files")
		return
	}
	defer func() {
		for _, t := range tails {
			t.f.Close()
		}
	}()

	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	c.Stream(func(w io.Writer) bool {
		select {
		case <-c.Request.Context().Done():
			return false
		case <-ticker.C:
			for i := range tails {
				for {
					line, err := tails[i].reader.ReadString('\n')
					if len(line) > 0 {
						c.SSEvent("log", redactLogLine(strings.TrimRight(line, "\n")))
					}
					if err != nil {
						break
					}
				}
			}
			return true
		}
	})
}

// logSecretPatterns scrub api keys / tokens / passwords out of log lines
// before they're shipped to the web monitor.
var logSecretPatterns = []struct {
	re  *regexp.Regexp
	rep string
}{
	{regexp.MustCompile(`(?i)((?:api[_-]?key|token|secret|password)\s*["']?\s*[=:]\s*["']?)[A-Za-z0-9\-_./+]{4,}`), "${1}***"},
	// Authorization: Bearer <token> — common log line shape for HTTP request dumps
	{regexp.MustCompile(`(?i)(authorization\s*:\s*bearer\s+)\S+`), "${1}***"},
	{regexp.MustCompile(`sk-(?:proj-|ant-|svcacct-)?[A-Za-z0-9_\-]{20,}`), "sk-***"},
}

func redactLogLine(line string) string {
	out := line
	for _, p := range logSecretPatterns {
		out = p.re.ReplaceAllString(out, p.rep)
	}
	return out
}

func redactLogLines(lines []string) []string {
	for i := range lines {
		lines[i] = redactLogLine(lines[i])
	}
	return lines
}

// journalTail returns the last n lines from a systemd journal unit.
func journalTail(unit string, n int) ([]string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "journalctl", "-u", unit, "--no-pager", "-n", strconv.Itoa(n))
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("journalctl: %w", err)
	}
	var lines []string
	for _, line := range strings.Split(string(out), "\n") {
		if line != "" {
			lines = append(lines, line)
		}
	}
	return lines, nil
}

// streamJournal streams journal lines via SSE using journalctl -f.
func (s *Server) streamJournal(c *gin.Context, unit string) {
	ctx := c.Request.Context()
	cmd := exec.CommandContext(ctx, "journalctl", "-u", unit, "--no-pager", "-f", "-n", "0")
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		c.SSEvent("error", "journalctl pipe: "+err.Error())
		return
	}
	if err := cmd.Start(); err != nil {
		c.SSEvent("error", "journalctl start: "+err.Error())
		return
	}
	defer func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	}()

	scanner := bufio.NewScanner(stdout)
	lineCh := make(chan string, 64)
	go func() {
		defer close(lineCh)
		for scanner.Scan() {
			lineCh <- scanner.Text()
		}
	}()

	c.Stream(func(w io.Writer) bool {
		select {
		case <-ctx.Done():
			return false
		case line, ok := <-lineCh:
			if !ok {
				return false
			}
			c.SSEvent("log", redactLogLine(line))
			for {
				select {
				case l, ok := <-lineCh:
					if !ok {
						return false
					}
					c.SSEvent("log", redactLogLine(l))
				default:
					return true
				}
			}
		}
	})
}

// tailFile reads the last n lines from a single file.
func tailFile(path string, n int) ([]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("open: %w", err)
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 256*1024), 256*1024)

	var ring []string
	for scanner.Scan() {
		ring = append(ring, scanner.Text())
		if len(ring) > n {
			ring = ring[1:]
		}
	}
	if err := scanner.Err(); err != nil {
		return ring, fmt.Errorf("scan: %w", err)
	}
	return ring, nil
}
