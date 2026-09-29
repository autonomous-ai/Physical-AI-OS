package openclaw

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// Pairing-flow tunables.
const (
	whatsappPairingTimeout = 90 * time.Second
	whatsappPairingMaxQRs  = 5
	whatsappQRTTL          = 20 * time.Second
	// whatsappPostPairSyncDelay is how long we wait after the CLI prints "✅ Linked" before emitting `success`.
	whatsappPostPairSyncDelay = 5 * time.Minute

	// whatsappPluginPackage is the npm package for the externalized WhatsApp plugin (openclaw 2026.5.x+).
	whatsappPluginPackage = "@openclaw/whatsapp"

	// Base npm names for externalized channel plugins; ensureChannelPlugin pins the version to the running gateway.
	discordPluginPackage = "@openclaw/discord"
	slackPluginPackage   = "@openclaw/slack"

	// channelPluginInstallTimeout bounds enable+install of a channel plugin when invoked outside an existing request context (the setup path).
	channelPluginInstallTimeout = 5 * time.Minute
)

// Per-process mutex: only one pairing flow runs at a time.
var (
	whatsappPairingMu     sync.Mutex
	whatsappPairingActive bool
)

// HasWhatsappSession reports whether a Baileys session exists on disk for the given account.
func (s *OpenclawService) HasWhatsappSession(account string) bool {
	if account == "" {
		account = "default"
	}
	credsPath := filepath.Join(s.config.OpenclawConfigDir, "credentials", domain.ChannelWhatsapp, account, "creds.json")
	info, err := os.Stat(credsPath)
	return err == nil && info.Size() > 0
}

// PairWhatsapp runs `openclaw channels login --channel whatsapp` and emits PairingEvents on the returned channel.
func (s *OpenclawService) PairWhatsapp(ctx context.Context) <-chan domain.PairingEvent {
	ch := make(chan domain.PairingEvent, 8)

	whatsappPairingMu.Lock()
	if whatsappPairingActive {
		whatsappPairingMu.Unlock()
		ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: "pairing_already_in_progress"}
		close(ch)
		return ch
	}
	whatsappPairingActive = true
	whatsappPairingMu.Unlock()

	go func() {
		defer func() {
			close(ch)
			whatsappPairingMu.Lock()
			whatsappPairingActive = false
			whatsappPairingMu.Unlock()
		}()
		s.runPairingProcess(ctx, ch)
	}()

	return ch
}

// runPairingProcess spawns the login CLI, scans its stdout for QR blocks and terminal markers, and emits PairingEvents.
func (s *OpenclawService) runPairingProcess(ctx context.Context, ch chan<- domain.PairingEvent) {
	runCtx, cancel := context.WithTimeout(ctx, whatsappPairingTimeout)
	defer cancel()

	cmd := exec.CommandContext(runCtx, "openclaw", "channels", "login", "--channel", domain.ChannelWhatsapp)

	pr, pw := io.Pipe()
	cmd.Stdout = pw
	cmd.Stderr = pw

	if err := cmd.Start(); err != nil {
		_ = pw.Close()
		ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: fmt.Sprintf("start pairing CLI: %v", err)}
		return
	}

	waitErr := make(chan error, 1)
	go func() {
		waitErr <- cmd.Wait()
		_ = pw.Close()
	}()

	ch <- domain.PairingEvent{Status: domain.PairingStatusStarting}

	linked := scanPairingStdout(pr, ch)

	err := <-waitErr
	if linked {
		select {
		case <-time.After(whatsappPostPairSyncDelay):
		case <-ctx.Done():
			ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: fmt.Sprintf("cancelled during post-pair sync: %v", ctx.Err())}
			return
		}
		ch <- domain.PairingEvent{Status: domain.PairingStatusSuccess}
		return
	}
	switch {
	case runCtx.Err() == context.DeadlineExceeded:
		ch <- domain.PairingEvent{Status: domain.PairingStatusTimeout, Error: fmt.Sprintf("no scan within %s", whatsappPairingTimeout)}
	case err != nil:
		ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: fmt.Sprintf("pairing CLI exited: %v", err)}
	default:
		ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: "pairing CLI exited without confirmation"}
	}
}

// scanPairingStdout reads lines from the CLI process and emits intermediate PairingEvents (pairing_qr, intermediate timeouts on QR overflow).
func scanPairingStdout(r io.Reader, ch chan<- domain.PairingEvent) bool {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 0, 64*1024), 1<<20)

	var qrBuf bytes.Buffer
	qrSeq := 0
	inQR := false

	flush := func() {
		if qrBuf.Len() == 0 {
			return
		}
		qrSeq++
		ch <- domain.PairingEvent{
			Status:    domain.PairingStatusQR,
			QRText:    strings.TrimRight(qrBuf.String(), "\n"),
			QRSeq:     qrSeq,
			ExpiresAt: time.Now().UTC().Add(whatsappQRTTL),
		}
		qrBuf.Reset()
		if qrSeq >= whatsappPairingMaxQRs {
			ch <- domain.PairingEvent{Status: domain.PairingStatusTimeout, Error: fmt.Sprintf("operator did not scan within %d QR rotations", whatsappPairingMaxQRs)}
		}
	}

	for scanner.Scan() {
		line := scanner.Text()
		slog.Debug("whatsapp-pair stdout", "component", "openclaw", "line", line)

		switch {
		case strings.Contains(line, "Scan this QR in WhatsApp"):
			flush()
			inQR = true
		case strings.HasPrefix(line, "✅ Linked"):
			flush()
			return true
		case isQRLine(line):
			if inQR {
				qrBuf.WriteString(line)
				qrBuf.WriteByte('\n')
			}
		default:
			if inQR && strings.TrimSpace(line) != "" {
				flush()
				inQR = false
			}
		}
	}
	flush()
	return false
}

// isQRLine reports whether a line is a row of the QR ASCII rendering.
func isQRLine(line string) bool {
	if len(line) < 30 {
		return false
	}
	for _, r := range line {
		switch r {
		case '█', '▀', '▄', ' ':
			continue
		default:
			return false
		}
	}
	return true
}

// runOpenclawCLI shells out to the openclaw CLI and surfaces stdout/stderr in the error message.
func runOpenclawCLI(ctx context.Context, args ...string) error {
	cmd := exec.CommandContext(ctx, "openclaw", args...)
	out, err := cmd.CombinedOutput()
	output := strings.TrimSpace(string(out))
	if err != nil {
		return fmt.Errorf("openclaw %s: %w (output: %s)", strings.Join(args, " "), err, output)
	}
	if output != "" {
		slog.Info("openclaw cli", "component", "openclaw", "args", strings.Join(args, " "), "output", output)
	}
	return nil
}

// ensureChannelPlugin enables a channel's openclaw plugin, installing it first when enable fails (the externalized-plugin model on openclaw 2026.5.x+).
func ensureChannelPlugin(ctx context.Context, channel, basePkg string) error {
	if err := runOpenclawCLI(ctx, "plugins", "enable", channel); err == nil {
		return nil
	}
	pkg := basePkg
	if v := GetOpenClawVersion(); v != "" {
		pkg = basePkg + "@" + v
	}
	slog.Warn("plugins enable failed, attempting install", "component", "openclaw", "channel", channel, "package", pkg)
	if err := runOpenclawCLI(ctx, "plugins", "install", pkg); err != nil {
		return fmt.Errorf("plugins install %s: %w", pkg, err)
	}
	if err := runOpenclawCLI(ctx, "plugins", "enable", channel); err != nil {
		return fmt.Errorf("plugins enable %s after install: %w", channel, err)
	}
	return nil
}

// applyWhatsappChannelConfig overlays the canonical channels.whatsapp block onto the map produced by `openclaw channels add` (which seeds defaults like accounts.default, mediaMaxMb).
func applyWhatsappChannelConfig(whatsappMap map[string]any, userID string) {
	whatsappMap["enabled"] = true
	if userID == "" {
		whatsappMap["dmPolicy"] = "pairing"
		return
	}
	whatsappMap["dmPolicy"] = "allowlist"
	whatsappMap["allowFrom"] = mergeStringList(whatsappMap["allowFrom"], userID)
	whatsappMap["groupPolicy"] = "allowlist"
	whatsappMap["groupAllowFrom"] = mergeStringList(whatsappMap["groupAllowFrom"], userID)
	accountsMap := ensureMap(whatsappMap, "accounts")
	defaultAccount := ensureMap(accountsMap, "default")
	defaultAccount["enabled"] = true
	defaultAccount["dmPolicy"] = "allowlist"
	defaultAccount["allowFrom"] = mergeStringList(defaultAccount["allowFrom"], userID)
	accountsMap["default"] = defaultAccount
	whatsappMap["accounts"] = accountsMap
}
