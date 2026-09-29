package system

import (
	"bufio"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"

	"github.com/creack/pty"
	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
)

// shellUpgrader allows any origin — same-origin enforcement is already handled
// at the network/proxy layer (this endpoint is only reachable on the LAN).
var shellUpgrader = websocket.Upgrader{
	ReadBufferSize:  4096,
	WriteBufferSize: 4096,
	CheckOrigin:     func(_ *http.Request) bool { return true },
}

// ShellHandler upgrades the request to a WebSocket and pipes a /bin/bash PTY
// in both directions.
func ShellHandler(agentEnvFile func() string) gin.HandlerFunc {
	return func(c *gin.Context) {
		shellSession(c, agentEnvFile)
	}
}

func shellSession(c *gin.Context, agentEnvFile func() string) {
	ws, err := shellUpgrader.Upgrade(c.Writer, c.Request, nil)
	if err != nil {
		log.Printf("[shell] upgrade failed: %v", err)
		return
	}
	defer ws.Close()

	cmd := exec.Command("/bin/bash", "-il")
	cmd.Env = append(os.Environ(),
		"TERM=xterm-256color",
		"COLORTERM=truecolor",
	)
	if agentEnvFile != nil {
		if extra := loadAgentEnv(agentEnvFile()); len(extra) > 0 {
			cmd.Env = append(cmd.Env, extra...)
		}
	}

	ptmx, err := pty.Start(cmd)
	if err != nil {
		log.Printf("[shell] pty start failed: %v", err)
		_ = ws.WriteMessage(websocket.TextMessage, []byte("\r\n[shell] failed to start PTY: "+err.Error()+"\r\n"))
		return
	}
	defer func() {
		_ = ptmx.Close()
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
		_, _ = cmd.Process.Wait()
	}()

	_ = pty.Setsize(ptmx, &pty.Winsize{Rows: 24, Cols: 80})

	// One writer mutex: WebSocket connections require all writes to be serialized.
	var writeMu sync.Mutex
	writeBytes := func(t int, b []byte) error {
		writeMu.Lock()
		defer writeMu.Unlock()
		_ = ws.SetWriteDeadline(time.Now().Add(10 * time.Second))
		return ws.WriteMessage(t, b)
	}

	done := make(chan struct{})
	var closeOnce sync.Once
	closeDone := func() { closeOnce.Do(func() { close(done) }) }

	go func() {
		defer closeDone()
		buf := make([]byte, 4096)
		for {
			n, err := ptmx.Read(buf)
			if n > 0 {
				if werr := writeBytes(websocket.BinaryMessage, buf[:n]); werr != nil {
					return
				}
			}
			if err != nil {
				if err != io.EOF {
					log.Printf("[shell] pty read: %v", err)
				}
				return
			}
		}
	}()

	for {
		select {
		case <-done:
			return
		default:
		}
		mt, data, err := ws.ReadMessage()
		if err != nil {
			closeDone()
			return
		}

		if mt == websocket.TextMessage && len(data) > 1 && data[0] == '{' {
			var env struct {
				Type string `json:"type"`
				Rows uint16 `json:"rows"`
				Cols uint16 `json:"cols"`
			}
			if jerr := json.Unmarshal(data, &env); jerr == nil && env.Type == "resize" {
				if env.Rows == 0 {
					env.Rows = 24
				}
				if env.Cols == 0 {
					env.Cols = 80
				}
				_ = pty.Setsize(ptmx, &pty.Winsize{Rows: env.Rows, Cols: env.Cols})
				continue
			}
		}

		if _, werr := ptmx.Write(data); werr != nil {
			log.Printf("[shell] pty write: %v", werr)
			closeDone()
			return
		}
	}
}

// loadAgentEnv parses a KEY=VALUE launch env file (same rules as the gatewayd
// child env loader) into PTY env entries; nil when absent.
func loadAgentEnv(path string) []string {
	if path == "" {
		return nil
	}
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()

	var out []string
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		i := strings.Index(line, "=")
		if i < 0 {
			continue
		}
		key := strings.TrimSpace(line[:i])
		val := strings.Trim(strings.TrimSpace(line[i+1:]), `"`)
		if key == "" {
			continue
		}
		out = append(out, key+"="+val)
	}
	if len(out) == 0 {
		return nil
	}
	return append(out, "IS_SANDBOX=1")
}
