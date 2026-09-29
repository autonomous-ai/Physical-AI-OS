package network

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

const (
	// wifiInterface is the WiFi NIC, for WiFi-specific operations only;
	// use PrimaryInterface() for address/reachability.
	wifiInterface = "wlan0"

	// Network monitor: forgiving timeouts so brief WiFi hiccups don't flip to no-internet.
	networkMonitorPingTarget    = "8.8.8.8"
	networkMonitorFailsRequired = 5
	networkMonitorInterval      = 5 * time.Second
	networkMonitorPingTimeout   = 3 * time.Second
)

// Service provides WiFi scan, current network, setup and connectivity monitoring.
type Service struct {
	config   *config.Config
	networks []domain.Network

	networkMonitorMu          sync.Mutex
	networkMonitorConsecutive int

	// Set once by StartNetworkMonitor before the goroutine starts.
	onConnectivityLost     func()
	onConnectivityRestored func()

	// Serializes recovery with provisioning and reset operations.
	operationMu sync.Mutex
	recovery    wifiRecovery
}

// ProvideService returns a network service.
func ProvideService(config *config.Config) *Service {
	return &Service{
		config:   config,
		networks: []domain.Network{},
	}
}

// ListNetworks returns visible WiFi networks (STA mode only).
func (s *Service) ListNetworks() ([]domain.Network, error) {
	return s.listNetworksIW()
}

// listNetworksIW runs `iw dev wlan0 scan` and parses BSS/SSID/signal etc.
func (s *Service) listNetworksIW() ([]domain.Network, error) {
	slog.Debug("wifi scan started", "component", "network")
	cmd := exec.Command("iw", "dev", wifiInterface, "scan")
	var outBuf, errBuf bytes.Buffer
	cmd.Stdout = &outBuf
	cmd.Stderr = &errBuf
	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("iw scan: %w", err)
	}
	networks := parseIWScan(outBuf.String())
	s.networks = networks
	slog.Debug("wifi scan done", "component", "network")
	return networks, nil
}

var (
	reBSS = regexp.MustCompile(`BSS ([0-9a-f:]+)`)
	// Anchored so "HESSID:" lines don't match.
	reSSID   = regexp.MustCompile(`^SSID: (.+)`)
	reSignal = regexp.MustCompile(`signal: ([\d.-]+)`)
	reTxRate = regexp.MustCompile(`tx bitrate:\s*([\d.]+)\s*MBit/s`)
	reDS     = regexp.MustCompile(`DS Parameter set: channel (\d+)`)
	reInet   = regexp.MustCompile(`inet (\d+\.\d+\.\d+\.\d+)`)
)

// decodeIWSSIDEscape decodes iw/wpa_cli `\xNN` and `\ ` escapes to raw SSID bytes.
func decodeIWSSIDEscape(s string) string {
	if !strings.Contains(s, `\`) {
		return s
	}
	b := make([]byte, 0, len(s))
	for i := 0; i < len(s); {
		if s[i] == '\\' && i+1 < len(s) {
			if s[i+1] == 'x' && i+3 < len(s) {
				if v, err := strconv.ParseUint(s[i+2:i+4], 16, 8); err == nil {
					b = append(b, byte(v))
					i += 4
					continue
				}
			}
			if s[i+1] == ' ' {
				b = append(b, ' ')
				i += 2
				continue
			}
		}
		b = append(b, s[i])
		i++
	}
	return string(b)
}

func parseIWScan(out string) []domain.Network {
	var list []domain.Network
	var current struct {
		bssid   string
		ssid    string
		signal  int
		channel int
	}
	lines := strings.Split(out, "\n")
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if m := reBSS.FindStringSubmatch(line); len(m) > 1 {
			if current.bssid != "" && current.ssid != "" {
				list = append(list, domain.Network{
					BSSID:    current.bssid,
					SSID:     current.ssid,
					Signal:   current.signal,
					Channel:  current.channel,
					Mode:     "STA",
					Rate:     "",
					Security: "",
				})
			}
			current.bssid = m[1]
			current.ssid = ""
			current.signal = 0
			current.channel = 0
			continue
		}
		// First SSID line wins per BSS block.
		if m := reSSID.FindStringSubmatch(line); len(m) > 1 && current.ssid == "" {
			current.ssid = decodeIWSSIDEscape(strings.TrimSpace(m[1]))
			continue
		}
		if m := reSignal.FindStringSubmatch(line); len(m) > 1 {
			f, _ := strconv.ParseFloat(m[1], 64)
			current.signal = int(f)
			continue
		}
		if m := reDS.FindStringSubmatch(line); len(m) > 1 {
			current.channel, _ = strconv.Atoi(m[1])
			continue
		}
	}
	if current.bssid != "" && current.ssid != "" {
		list = append(list, domain.Network{
			BSSID:    current.bssid,
			SSID:     current.ssid,
			Signal:   current.signal,
			Channel:  current.channel,
			Mode:     "STA",
			Rate:     "",
			Security: "",
		})
	}
	return list
}

// PrimaryInterface returns the default-route interface (lowest metric), or wlan0
// when there is none (AP mode).
func PrimaryInterface() string {
	out, err := exec.Command("ip", "route", "show", "default").Output()
	if err != nil {
		return wifiInterface
	}
	if iface := parseDefaultRouteIface(string(out)); iface != "" {
		return iface
	}
	return wifiInterface
}

// parseDefaultRouteIface returns the device of the first `ip route show default` line.
// Example: "default via 192.168.1.1 dev end0 ..." -> "end0"
func parseDefaultRouteIface(out string) string {
	for _, line := range strings.Split(out, "\n") {
		fields := strings.Fields(line)
		for i, f := range fields {
			if f == "dev" && i+1 < len(fields) {
				return fields[i+1]
			}
		}
	}
	return ""
}

// GetCurrentIP returns the IPv4 address of PrimaryInterface, or "".
func (s *Service) GetCurrentIP() (string, error) {
	iface := PrimaryInterface()
	cmd := exec.Command("ip", "-4", "addr", "show", iface)
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("ip addr %s: %w", iface, err)
	}
	if m := reInet.FindStringSubmatch(string(out)); len(m) > 1 {
		return m[1], nil
	}
	slog.Debug("no IP found", "component", "network", "interface", iface, "output", string(out))
	return "", nil
}

// CurrentNetwork returns the currently connected WiFi network.
func (s *Service) CurrentNetwork() (*domain.Network, error) {
	ssid := ReadCurrentSSID()
	if ssid == "" {
		return nil, nil
	}
	signal, linkRate := readCurrentLink()
	return &domain.Network{
		SSID:     ssid,
		Mode:     "",
		BSSID:    "",
		Channel:  0,
		Rate:     "",
		Signal:   signal,
		LinkRate: linkRate,
		Security: "",
	}, nil
}

// readCurrentLink returns signal (dBm) and tx bitrate (Mbps); (0, 0) when unknown.
func readCurrentLink() (signal int, linkRate int) {
	out, err := exec.Command("iw", "dev", wifiInterface, "link").Output()
	if err != nil {
		return 0, 0
	}
	s := string(out)
	if m := reSignal.FindStringSubmatch(s); len(m) > 1 {
		f, _ := strconv.ParseFloat(m[1], 64)
		signal = int(f)
	}
	if m := reTxRate.FindStringSubmatch(s); len(m) > 1 {
		f, _ := strconv.ParseFloat(m[1], 64)
		linkRate = int(f + 0.5)
	}
	return signal, linkRate
}

// ReadCurrentSSID returns the current SSID via iwgetid, then iw, then wpa_cli.
func ReadCurrentSSID() string {
	if out, err := exec.Command("iwgetid", "-r", wifiInterface).Output(); err == nil {
		if s := strings.TrimSpace(string(out)); s != "" {
			return s
		}
	}
	if out, err := exec.Command("iw", "dev", wifiInterface, "link").Output(); err == nil {
		for _, line := range strings.Split(string(out), "\n") {
			line = strings.TrimSpace(line)
			if strings.HasPrefix(line, "SSID:") {
				if s := strings.TrimSpace(strings.TrimPrefix(line, "SSID:")); s != "" {
					return decodeIWSSIDEscape(s)
				}
			}
		}
	}
	if out, err := exec.Command("wpa_cli", "-i", wifiInterface, "status").Output(); err == nil {
		for _, line := range strings.Split(string(out), "\n") {
			line = strings.TrimSpace(line)
			if strings.HasPrefix(line, "ssid=") {
				if s := strings.TrimSpace(strings.TrimPrefix(line, "ssid=")); s != "" {
					return decodeIWSSIDEscape(s)
				}
			}
		}
	}
	return ""
}

// rePingTime extracts the RTT from a ping reply line.
var rePingTime = regexp.MustCompile(`time=([0-9.]+) ms`)

// CheckInternet pings 8.8.8.8.
func (s *Service) CheckInternet() (bool, error) {
	if _, err := s.pingRTT(); err != nil {
		return false, fmt.Errorf("connected but no internet: ping 8.8.8.8 failed: %w", err)
	}
	return true, nil
}

// CheckInternetRTT is CheckInternet plus the RTT in ms (0 if unparsed).
func (s *Service) CheckInternetRTT() (ok bool, rttMs float64) {
	rtt, err := s.pingRTT()
	return err == nil, rtt
}

// pingRTT runs one probe; rtt 0 with nil error when output didn't parse.
func (s *Service) pingRTT() (float64, error) {
	out, err := exec.Command("ping", "-c", "1", "-W", "5", "8.8.8.8").CombinedOutput()
	if err != nil {
		return 0, err
	}
	if m := rePingTime.FindSubmatch(out); len(m) > 1 {
		f, _ := strconv.ParseFloat(string(m[1]), 64)
		return f, nil
	}
	return 0, nil
}

// pingNetworkMonitor runs a short ping for the network monitor.
func (s *Service) pingNetworkMonitor(target string) bool {
	sec := int(networkMonitorPingTimeout.Seconds())
	if sec < 1 {
		sec = 1
	}
	cmd := exec.Command("ping", "-c", "1", "-W", strconv.Itoa(sec), target)
	return cmd.Run() == nil
}

// StartNetworkMonitor runs the monitor loop until ctx ends; call only in STA mode.
// onLost fires after consecutive failures, onRestored after a confirmed outage.
func (s *Service) StartNetworkMonitor(ctx context.Context, onLost, onRestored func()) {
	s.onConnectivityLost = onLost
	s.onConnectivityRestored = onRestored
	go func() {
		ticker := time.NewTicker(networkMonitorInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				s.runNetworkMonitorTick(ctx)
			}
		}
	}()
}

func (s *Service) runNetworkMonitorTick(ctx context.Context) {
	if !s.operationMu.TryLock() {
		return
	}
	defer s.operationMu.Unlock()
	if ctx.Err() != nil {
		return
	}
	// Setup and factory reset own the network until setup completes.
	if !s.config.SetUpCompleted {
		s.networkMonitorMu.Lock()
		s.networkMonitorConsecutive = 0
		s.networkMonitorMu.Unlock()
		s.recovery = wifiRecovery{}
		return
	}
	if wifiReconnectSkipReason(s.config.NetworkSSID, PrimaryInterface()) == "" {
		s.recovery.tick(ctx, time.Now(), s.config.NetworkSSID, s.config.NetworkPassword)
	} else {
		s.recovery = wifiRecovery{}
	}
	if s.pingNetworkMonitor(networkMonitorPingTarget) {
		s.networkMonitorMu.Lock()
		prev := s.networkMonitorConsecutive
		s.networkMonitorConsecutive = 0
		s.networkMonitorMu.Unlock()
		if prev >= networkMonitorFailsRequired {
			slog.Info("internet restored", "component", "network-monitor", "previousFails", prev)
			if s.onConnectivityRestored != nil {
				s.onConnectivityRestored()
			}
		}
		return
	}
	s.networkMonitorMu.Lock()
	s.networkMonitorConsecutive++
	n := s.networkMonitorConsecutive
	s.networkMonitorMu.Unlock()

	slog.Warn("no internet", "component", "network-monitor", "target", networkMonitorPingTarget, "fails", n, "required", networkMonitorFailsRequired)
	if n == networkMonitorFailsRequired && s.onConnectivityLost != nil {
		s.onConnectivityLost()
	}
}

// wifiReconnectSkipReason limits WiFi recovery to devices using WiFi.
// A missing default route falls back to wlan0 so a dropped link can recover.
func wifiReconnectSkipReason(configuredSSID, primaryIface string) string {
	if strings.TrimSpace(configuredSSID) == "" {
		return "device has no WiFi credentials (wired setup)"
	}
	if primaryIface != wifiInterface {
		return "default route is on " + primaryIface + ", not WiFi"
	}
	return ""
}

// ResetNetwork clears credentials, writes a minimal wpa_supplicant config and restarts it.
func (s *Service) ResetNetwork() error {
	s.operationMu.Lock()
	defer s.operationMu.Unlock()
	s.recovery = wifiRecovery{}
	s.config.NetworkSSID = ""
	s.config.NetworkPassword = ""
	wpaSupplicantConf := "/etc/wpa_supplicant/wpa_supplicant-wlan0.conf"
	_ = os.Remove(wpaSupplicantConf)
	minimal := "ctrl_interface=DIR=/run/wpa_supplicant\nupdate_config=1\ncountry=US\nfast_reauth=1\nap_scan=1"
	_ = os.WriteFile(wpaSupplicantConf, []byte(minimal), 0600)
	// Restart fails in AP mode (service masked); ignore.
	_ = exec.Command("systemctl", "restart", "wpa_supplicant@wlan0").Run()
	return s.config.Save()
}

// SetupNetwork submits WiFi credentials via connect-wifi CLI.
func (s *Service) SetupNetwork(ssid string, password string) (bool, error) {
	s.operationMu.Lock()
	defer s.operationMu.Unlock()
	s.recovery = wifiRecovery{}
	ssid = strings.TrimSpace(ssid)
	slog.Debug("starting network setup", "component", "network", "ssid", ssid)
	if ssid == "" {
		return false, fmt.Errorf("ssid is required")
	}
	// 802.11 caps SSID at 32 bytes (not chars).
	if n := len(ssid); n > 32 {
		return false, fmt.Errorf("ssid too long: %d bytes, max 32 (802.11 limit)", n)
	}

	// Fast path: skip reconnecting when ssid+password are unchanged.
	if password == s.config.NetworkPassword {
		if cur, _ := s.CurrentNetwork(); cur != nil && cur.SSID == ssid {
			if ok, _ := s.CheckInternet(); ok {
				slog.Info("network setup: already connected to requested SSID, skipping reconnect", "component", "network", "ssid", ssid)
				s.config.NetworkSSID = ssid
				if err := s.config.Save(); err != nil {
					slog.Error("save config failed", "component", "network", "error", err)
				}
				return true, nil
			}
		}
	}

	args := []string{ssid}
	if password != "" {
		args = append(args, password)
	}
	slog.Debug("running connect-wifi", "component", "network", "args", args)
	cmd := exec.Command("connect-wifi", args...)
	slog.Debug("connect-wifi command", "component", "network", "cmd", cmd)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return false, fmt.Errorf("connect-wifi: %w: %s", err, string(out))
	}
	slog.Debug("connect-wifi output", "component", "network", "output", string(out))
	success := false
	for i := 0; i < 60; i++ {
		slog.Debug("checking internet", "component", "network", "attempt", i)
		if ok, _ := s.CheckInternet(); ok {
			slog.Debug("internet ok", "component", "network", "attempt", i)
			curNet, _ := s.CurrentNetwork()
			slog.Debug("current network", "component", "network", "network", curNet)
			if curNet != nil && curNet.SSID == ssid {
				success = true
				break
			} else {
				current := ""
				if curNet != nil {
					current = curNet.SSID
				}
				slog.Debug("current network does not match", "component", "network", "current", current, "expected", ssid)
			}
		} else {
			slog.Debug("internet not ok", "component", "network", "attempt", i)
		}
		time.Sleep(1 * time.Second)
	}
	if !success {
		return false, fmt.Errorf("network setup failed, no internet or SSID did not match within 60s")
	}
	s.config.NetworkSSID = ssid
	s.config.NetworkPassword = password
	if err := s.config.Save(); err != nil {
		slog.Error("save config failed", "component", "network", "error", err)
	}
	slog.Info("network setup success", "component", "network")
	// Devices without an RTC boot with a stale clock; force NTP so TLS doesn't fail.
	// Images ship chrony or systemd-timesyncd; try both, non-fatal.
	if out, err := exec.Command("chronyc", "makestep").CombinedOutput(); err != nil {
		slog.Warn("chronyc makestep failed, trying systemd-timesyncd", "component", "network", "error", err, "output", strings.TrimSpace(string(out)))
		if out2, err2 := exec.Command("systemctl", "restart", "systemd-timesyncd").CombinedOutput(); err2 != nil {
			slog.Warn("systemd-timesyncd restart failed", "component", "network", "error", err2, "output", strings.TrimSpace(string(out2)))
		}
	}
	for i := range 10 {
		time.Sleep(time.Second)
		out, err := exec.Command("timedatectl", "show", "-p", "NTPSynchronized", "--value").Output()
		if err == nil && strings.TrimSpace(string(out)) == "yes" {
			slog.Info("NTP synchronized after WiFi connect", "component", "network", "attempts", i+1)
			break
		}
		if i == 9 {
			slog.Warn("NTP not yet synchronized after WiFi connect", "component", "network")
		}
	}
	return true, nil
}

// LeaveAPMode tears down the provisioning AP without joining WiFi (wired setup),
// via the same device-sta-mode script connect-wifi uses.
func (s *Service) LeaveAPMode() error {
	s.operationMu.Lock()
	defer s.operationMu.Unlock()
	s.recovery = wifiRecovery{}
	out, err := exec.Command("/usr/local/bin/device-sta-mode").CombinedOutput()
	if err != nil {
		return fmt.Errorf("device-sta-mode: %w: %s", err, string(out))
	}
	slog.Info("left AP mode without WiFi (wired setup)", "component", "network")
	return nil
}

// SwitchToAPMode runs device-ap-mode to return to provisioning mode.
func (s *Service) SwitchToAPMode() error {
	s.operationMu.Lock()
	defer s.operationMu.Unlock()
	s.recovery = wifiRecovery{}
	cmd := exec.Command("/usr/local/bin/device-ap-mode")
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("device-ap-mode: %w: %s", err, string(out))
	}
	slog.Info("switched to AP mode", "component", "network")
	return nil
}
