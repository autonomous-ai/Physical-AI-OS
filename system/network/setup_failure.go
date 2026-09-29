package network

import (
	"errors"
	"fmt"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Why a WiFi join during setup failed.
//
// The setup loop only asks "internet OK, SSID matched?", so every WiFi failure
// used to surface as one generic "no internet or SSID did not match within
// 60s" — a wrong password, a network out of range and a captive portal were
// indistinguishable in the logs, while wpa_supplicant knew the answer within
// seconds (verified on intern-v2-d94b, 2026-09-28: three "4-Way Handshake
// failed" + CTRL-EVENT-SSID-TEMP-DISABLED reason=WRONG_KEY for a wrong
// password). The reason is logged as the stable field `setup_failure_reason`,
// so failed setups can be counted and grouped by cause in Graylog.

// SetupFailureReason is the stable value of the `setup_failure_reason` log field.
type SetupFailureReason string

const (
	// FailureWrongPassword: the router rejected the key (4-way handshake failed).
	FailureWrongPassword SetupFailureReason = "wrong_password"
	// FailureAssociationRejected: the access point refused the device before
	// any key exchange (MAC filter, client limit, unsupported security mode).
	FailureAssociationRejected SetupFailureReason = "association_rejected"
	// FailureSSIDNotFound: the network was never seen — out of range, hidden,
	// or a band/channel the radio cannot use.
	FailureSSIDNotFound SetupFailureReason = "ssid_not_found"
	// FailureNoDHCP: WiFi connected but the router handed out no address.
	FailureNoDHCP SetupFailureReason = "no_dhcp"
	// FailureNoInternet: connected with an address, yet neither ICMP nor the
	// cloud TLS probe got out (captive portal, firewall, no uplink).
	FailureNoInternet SetupFailureReason = "no_internet"
	// FailureSSIDTooLong: SSID over the 32-byte 802.11 limit.
	FailureSSIDTooLong SetupFailureReason = "ssid_too_long"
	// FailureWiFiConnectError: the connect-wifi helper itself failed.
	FailureWiFiConnectError SetupFailureReason = "wifi_connect_error"
	// FailureUnknown: none of the above could be established.
	FailureUnknown SetupFailureReason = "unknown"
)

// SetupError is the error SetupNetwork returns when the WiFi join fails.
type SetupError struct {
	Reason SetupFailureReason
	// Detail is the human-readable cause shown on the setup screen.
	Detail string
	Err    error
}

func (e *SetupError) Error() string {
	if e.Err != nil {
		return fmt.Sprintf("network setup failed (%s): %s: %v", e.Reason, e.Detail, e.Err)
	}
	return fmt.Sprintf("network setup failed (%s): %s", e.Reason, e.Detail)
}

func (e *SetupError) Unwrap() error { return e.Err }

// FailureReasonOf returns the reason carried by a SetupError anywhere in err's
// chain, or "" when err is nil or not a network setup failure.
func FailureReasonOf(err error) SetupFailureReason {
	var se *SetupError
	if errors.As(err, &se) {
		return se.Reason
	}
	return ""
}

var failureDetail = map[SetupFailureReason]string{
	FailureWrongPassword:       "wrong WiFi password: the router rejected the key",
	FailureAssociationRejected: "the router refused the connection",
	FailureSSIDNotFound:        "WiFi network not found: out of range or on a channel the device cannot use",
	FailureNoDHCP:              "connected to WiFi but the router gave no IP address",
	FailureNoInternet:          "connected to WiFi but there is no internet (captive portal or firewall?)",
	FailureUnknown:             "no internet or SSID did not match within 60s",
}

func newSetupError(reason SetupFailureReason, err error) *SetupError {
	return &SetupError{Reason: reason, Detail: failureDetail[reason], Err: err}
}

// wifiObservation is what the setup loop last saw of the join.
type wifiObservation struct {
	events      []string // wpa_supplicant log lines since this attempt started
	wpaState    string   // wpa_cli status wpa_state (COMPLETED, SCANNING, ...)
	hasIP       bool     // a STA address, not the setup hotspot's
	ssidMatched bool
}

var reAuthFailures = regexp.MustCompile(`auth_failures=(\d+).*reason=WRONG_KEY`)

// wrongKeyFailures is the highest auth_failures count wpa_supplicant reported
// with reason=WRONG_KEY, or the number of failed 4-way handshakes.
func wrongKeyFailures(events []string) int {
	n, handshakes := 0, 0
	for _, e := range events {
		if m := reAuthFailures.FindStringSubmatch(e); m != nil {
			if v, err := strconv.Atoi(m[1]); err == nil && v > n {
				n = v
			}
		}
		if strings.Contains(e, "4-Way Handshake failed") {
			handshakes++
		}
	}
	return max(n, handshakes)
}

// classifyWiFiFailure decides why a WiFi join failed. Authentication evidence
// wins over state: after a wrong key wpa_supplicant keeps rescanning, so the
// final state alone reads like "network not found".
func classifyWiFiFailure(o wifiObservation) SetupFailureReason {
	joined := strings.Join(o.events, "\n")
	switch {
	case wrongKeyFailures(o.events) > 0:
		return FailureWrongPassword
	case strings.Contains(joined, "CTRL-EVENT-ASSOC-REJECT"),
		strings.Contains(joined, "reason=AUTH_FAILED"),
		strings.Contains(joined, "reason=CONN_FAILED"):
		return FailureAssociationRejected
	case o.hasIP && o.ssidMatched:
		return FailureNoInternet
	case o.wpaState == "COMPLETED" && !o.hasIP:
		return FailureNoDHCP
	case strings.Contains(joined, "CTRL-EVENT-NETWORK-NOT-FOUND"),
		len(o.events) > 0 && !strings.Contains(joined, "Trying to associate"):
		return FailureSSIDNotFound
	default:
		return FailureUnknown
	}
}

// wifiFailFastThreshold stops waiting once wpa_supplicant has rejected the key
// this many times: retrying the same password cannot succeed, and the rest of
// the 60s window only delays the customer's retry.
const wifiFailFastThreshold = 2

// Probes, as variables so tests can replace them.
var (
	// wpaEventsSince returns wpa_supplicant's log lines since t (empty when the
	// journal is unavailable — classification then falls back to state).
	wpaEventsSince = func(t time.Time) []string {
		out, err := exec.Command("journalctl", "-t", "wpa_supplicant", "--no-pager", "-o", "cat",
			"--since", "@"+strconv.FormatInt(t.Unix(), 10)).Output()
		if err != nil {
			return nil
		}
		var lines []string
		for _, l := range strings.Split(string(out), "\n") {
			if l = strings.TrimSpace(l); l != "" {
				lines = append(lines, l)
			}
		}
		return lines
	}
	// wpaState returns wpa_cli's wpa_state for wlan0, or "" when unavailable.
	wpaState = func() string {
		out, err := exec.Command("wpa_cli", "-i", wifiInterface, "status").Output()
		if err != nil {
			return ""
		}
		for _, l := range strings.Split(string(out), "\n") {
			if v, ok := strings.CutPrefix(strings.TrimSpace(l), "wpa_state="); ok {
				return v
			}
		}
		return ""
	}
)
