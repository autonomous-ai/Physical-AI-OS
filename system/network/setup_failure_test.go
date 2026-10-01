package network

import (
	"errors"
	"fmt"
	"strings"
	"testing"
)

// Real wpa_supplicant lines from intern-v2-d94b, 2026-09-28: attempt 1 used a
// wrong password, attempt 2 the right one.
var (
	wrongPasswordEvents = []string{
		"Successfully initialized wpa_supplicant",
		"wlan0: Trying to associate with 34:3a:20:95:78:93 (SSID='Glinks' freq=5745 MHz)",
		"wlan0: Associated with 34:3a:20:95:78:93",
		"wlan0: WPA: 4-Way Handshake failed - pre-shared key may be incorrect",
		`wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid="Glinks" auth_failures=1 duration=10 reason=WRONG_KEY`,
		"wlan0: CTRL-EVENT-DISCONNECTED bssid=34:3a:20:95:78:93 reason=3",
		"wlan0: WPA: 4-Way Handshake failed - pre-shared key may be incorrect",
		`wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid="Glinks" auth_failures=2 duration=30 reason=WRONG_KEY`,
	}
	connectedEvents = []string{
		"wlan0: Trying to associate with 34:3a:20:95:78:93 (SSID='Glinks' freq=5745 MHz)",
		"wlan0: Associated with 34:3a:20:95:78:93",
		"wlan0: WPA: Key negotiation completed with 34:3a:20:95:78:93 [PTK=CCMP GTK=CCMP]",
		"wlan0: CTRL-EVENT-CONNECTED - Connection to 34:3a:20:95:78:93 completed [id=0 id_str=]",
	}
)

func TestClassifyWiFiFailure(t *testing.T) {
	cases := []struct {
		name string
		o    wifiObservation
		want SetupFailureReason
	}{
		{name: "wrong password (real capture)", o: wifiObservation{events: wrongPasswordEvents, wpaState: "SCANNING"}, want: FailureWrongPassword},
		{name: "wrong key beats a scanning state that looks like not-found",
			o: wifiObservation{events: wrongPasswordEvents[:5], wpaState: "SCANNING"}, want: FailureWrongPassword},
		{name: "router refused the association",
			o:    wifiObservation{events: []string{"wlan0: Trying to associate with aa (SSID='Home')", "wlan0: CTRL-EVENT-ASSOC-REJECT bssid=aa status_code=17"}},
			want: FailureAssociationRejected},
		{name: "network never seen",
			o:    wifiObservation{events: []string{"Successfully initialized wpa_supplicant", `wlan0: CTRL-EVENT-NETWORK-NOT-FOUND`}, wpaState: "SCANNING"},
			want: FailureSSIDNotFound},
		{name: "never even tried to associate",
			o: wifiObservation{events: []string{"Successfully initialized wpa_supplicant"}, wpaState: "SCANNING"}, want: FailureSSIDNotFound},
		{name: "connected but no DHCP lease", o: wifiObservation{events: connectedEvents, wpaState: "COMPLETED"}, want: FailureNoDHCP},
		{name: "connected with an address but no internet (captive portal)",
			o: wifiObservation{events: connectedEvents, wpaState: "COMPLETED", hasIP: true, ssidMatched: true}, want: FailureNoInternet},
		{name: "journal unavailable and nothing observed", o: wifiObservation{}, want: FailureUnknown},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := classifyWiFiFailure(tc.o); got != tc.want {
				t.Errorf("classifyWiFiFailure() = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestWrongKeyFailuresReachesTheFailFastThreshold(t *testing.T) {
	if got := wrongKeyFailures(wrongPasswordEvents[:5]); got >= wifiFailFastThreshold {
		t.Errorf("after one rejection = %d, must stay below the fail-fast threshold %d", got, wifiFailFastThreshold)
	}
	if got := wrongKeyFailures(wrongPasswordEvents); got < wifiFailFastThreshold {
		t.Errorf("after two rejections = %d, want >= %d so setup stops early", got, wifiFailFastThreshold)
	}
	if got := wrongKeyFailures(connectedEvents); got != 0 {
		t.Errorf("successful join counted %d key failures", got)
	}
}

func TestSetupErrorCarriesItsReasonThroughWrapping(t *testing.T) {
	err := fmt.Errorf("setup network: %w", newSetupError(FailureWrongPassword, nil))
	if got := FailureReasonOf(err); got != FailureWrongPassword {
		t.Fatalf("FailureReasonOf = %q, want wrong_password", got)
	}
	if !strings.Contains(err.Error(), "wrong WiFi password") {
		t.Errorf("error %q should carry the human-readable detail", err)
	}
	if FailureReasonOf(errors.New("other")) != "" || FailureReasonOf(nil) != "" {
		t.Error("non-setup errors must have no network reason")
	}
}
