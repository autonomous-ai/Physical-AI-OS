package network

import (
	"crypto/tls"
	"net"
	"net/url"
	"os/exec"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/urlnorm"
)

// Internet reachability.
//
// ICMP to 8.8.8.8 is the primary probe, but some networks (corporate, hotel,
// some mesh routers) drop ICMP while the web works. With ICMP alone those
// networks failed setup right after the WiFi join — the device tore down its
// hotspot, never reached the cloud, and the customer was dropped back on the
// WiFi form with no explanation. A TLS handshake with the device's own cloud
// API host is the fallback: it is the host setup actually needs next.

const (
	// cloudProbeTimeout bounds the TLS fallback so a dead network still fails
	// fast inside setup's 60s window.
	cloudProbeTimeout = 3 * time.Second
	// defaultCloudProbeAddr is used before config.json carries an Autonomous
	// base URL (a fresh device joins WiFi before setup saves it).
	defaultCloudProbeAddr = "campaign-api.autonomous.ai:443"
)

// Probes, as variables so tests can replace them.
var (
	// icmpPing sends one ICMP echo to 8.8.8.8 and returns ping's output.
	icmpPing = func(timeoutSec string) ([]byte, error) {
		return exec.Command("ping", "-c", "1", "-W", timeoutSec, "8.8.8.8").CombinedOutput()
	}
	// tlsReach completes a TLS handshake with addr and closes it.
	tlsReach = func(addr string, timeout time.Duration) error {
		host, _, err := net.SplitHostPort(addr)
		if err != nil {
			return err
		}
		// Certificate checks are skipped on purpose: this only proves the host
		// is reachable and sends nothing, and it must work before NTP has fixed
		// the clock — devices have no RTC, so right after the WiFi join every
		// certificate still looks "not yet valid".
		conn, err := tls.DialWithDialer(&net.Dialer{Timeout: timeout}, "tcp", addr,
			&tls.Config{ServerName: host, InsecureSkipVerify: true}) //nolint:gosec // reachability probe only, see above
		if err != nil {
			return err
		}
		return conn.Close()
	}
)

// cloudProbeAddr is host:port of the device's Autonomous cloud API, or the
// default before setup has saved one. An owner's own LLM provider is never
// probed: reachability is about our cloud.
func (s *Service) cloudProbeAddr() string {
	base := strings.TrimSpace(s.config.LLMBaseURL)
	if base == "" || !urlnorm.IsAutonomousHost(base) {
		return defaultCloudProbeAddr
	}
	u, err := url.Parse(base)
	if err != nil || u.Hostname() == "" {
		return defaultCloudProbeAddr
	}
	port := u.Port()
	if port == "" {
		port = "443"
	}
	return net.JoinHostPort(u.Hostname(), port)
}

// reachableOverTLS is the fallback probe used when ICMP fails.
func (s *Service) reachableOverTLS() (string, error) {
	addr := s.cloudProbeAddr()
	return addr, tlsReach(addr, cloudProbeTimeout)
}
