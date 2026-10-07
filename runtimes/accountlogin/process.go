package accountlogin

import (
	"context"
	"fmt"
	"io"
	"net/url"
	"os/exec"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/creack/pty"
)

type Prompt struct {
	URL           string
	UserCode      string
	InputRequired bool
}

var ansi = regexp.MustCompile(`\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*?(?:\x07|\x1b\\)`)
var urls = regexp.MustCompile(`https://[^\s<>"\x1b]+`)
var deviceCode = regexp.MustCompile(`\b[A-Z0-9]{4}-[A-Z0-9]{4,5}\b`)

// Extract only provider URLs and short device codes, never raw CLI output or tokens.
func (f *Flow) ParsePrompt(output string) Prompt {
	text := ansi.ReplaceAllString(output, "")
	result := Prompt{}
	for _, candidate := range urls.FindAllString(text, -1) {
		candidate = strings.TrimRight(candidate, ".,;)\r")
		u, err := url.Parse(candidate)
		if err != nil || u.Scheme != "https" || u.User != nil || u.Port() != "" {
			continue
		}
		for _, host := range f.Hosts {
			if strings.EqualFold(u.Hostname(), host) && (strings.Contains(u.Path, "oauth") || strings.Contains(u.Path, "authorize") || strings.Contains(u.Path, "device") || strings.Contains(u.Path, "activate")) {
				result.URL = candidate
				break
			}
		}
	}
	if result.URL != "" {
		result.InputRequired = f.InputRequired
		if !f.InputRequired {
			result.UserCode = deviceCode.FindString(text)
		}
	}
	return result
}

// Run uses a PTY for native CLIs that require one. The caller sees structured
// prompts only; output is bounded, never logged, and discarded on completion.
func (f *Flow) Run(ctx context.Context, emit func(Prompt), input <-chan string) error {
	return runInteractive(ctx, f.Command, func(text string) {
		p := f.ParsePrompt(text)
		if p.URL != "" {
			emit(p)
		}
	}, input)
}

func runInteractive(ctx context.Context, cmd *exec.Cmd, output func(string), input <-chan string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	terminal, err := pty.StartWithSize(cmd, &pty.Winsize{Rows: 40, Cols: 2000})
	if err != nil {
		return fmt.Errorf("start native login: %w", err)
	}
	defer terminal.Close()
	// pty starts a new session; kill its process group so cancellation cannot
	// leave a polling login helper alive or a late credential write behind.
	done := make(chan struct{})
	var workers sync.WaitGroup
	workers.Add(1)
	go func() {
		defer workers.Done()
		select {
		case <-ctx.Done():
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		case <-done:
		}
	}()
	if input != nil {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for {
				select {
				case <-done:
					return
				case <-ctx.Done():
					return
				case code, ok := <-input:
					if !ok {
						return
					}
					if _, err := io.WriteString(terminal, code+"\n"); err != nil {
						return
					}
				}
			}
		}()
	}
	var buffer strings.Builder
	chunk := make([]byte, 4096)
	for {
		n, readErr := terminal.Read(chunk)
		if n > 0 {
			buffer.Write(chunk[:n])
			text := buffer.String()
			if len(text) > 65536 {
				text = text[len(text)-65536:]
				buffer.Reset()
				buffer.WriteString(text)
			}
			output(text)
		}
		if readErr != nil {
			break
		}
	}
	err = cmd.Wait()
	close(done)
	_ = terminal.Close()
	workers.Wait()
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if err != nil {
		return fmt.Errorf("native login command failed")
	}
	return nil
}

// runPTY supports post-login native configuration commands without exposing their output.
func runPTY(ctx context.Context, cmd *exec.Cmd) ([]byte, error) {
	var output string
	bounded, cancel := context.WithTimeout(ctx, 45*time.Second)
	defer cancel()
	err := runInteractive(bounded, cmd, func(s string) { output = s }, nil)
	return []byte(output), err
}
