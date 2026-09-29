package network

import "testing"

// TestParseDefaultRouteIface covers route-table shapes for PrimaryInterface.
func TestParseDefaultRouteIface(t *testing.T) {
	tests := []struct {
		name string
		out  string
		want string
	}{
		{
			name: "wifi only",
			out:  "default via 192.168.1.1 dev wlan0 proto dhcp src 192.168.1.50 metric 303\n",
			want: "wlan0",
		},
		{
			name: "ethernet only",
			out:  "default via 192.168.1.1 dev end0 proto dhcp src 192.168.1.42 metric 202\n",
			want: "end0",
		},
		{
			name: "both up, wired wins on metric",
			out: "default via 192.168.1.1 dev end0 proto dhcp src 192.168.1.42 metric 202\n" +
				"default via 192.168.1.1 dev wlan0 proto dhcp src 192.168.1.50 metric 303\n",
			want: "end0",
		},
		{
			name: "no default route",
			out:  "",
			want: "",
		},
		{
			name: "malformed line without dev",
			out:  "default via 192.168.1.1 proto dhcp metric 202\n",
			want: "",
		},
		{
			name: "dev is last token",
			out:  "default via 192.168.1.1 dev\n",
			want: "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := parseDefaultRouteIface(tt.out); got != tt.want {
				t.Fatalf("parseDefaultRouteIface(%q) = %q, want %q", tt.out, got, tt.want)
			}
		})
	}
}

// TestWifiReconnectSkipReason keeps recovery from disrupting wired devices.
func TestWifiReconnectSkipReason(t *testing.T) {
	tests := []struct {
		name         string
		ssid         string
		primaryIface string
		wantSkip     bool
	}{
		{
			name:         "wifi device with wifi default route",
			ssid:         "home-wifi",
			primaryIface: "wlan0",
			wantSkip:     false,
		},
		{
			name:         "wifi device, link dropped, no default route",
			ssid:         "home-wifi",
			primaryIface: "wlan0",
			wantSkip:     false,
		},
		{
			name:         "wired setup, no credentials on file",
			ssid:         "",
			primaryIface: "end0",
			wantSkip:     true,
		},
		{
			name:         "wired setup, cable pulled, fallback to wlan0",
			ssid:         "",
			primaryIface: "wlan0",
			wantSkip:     true,
		},
		{
			name:         "wifi configured but routing over ethernet",
			ssid:         "home-wifi",
			primaryIface: "end0",
			wantSkip:     true,
		},
		{
			name:         "whitespace-only ssid counts as no credentials",
			ssid:         "   ",
			primaryIface: "wlan0",
			wantSkip:     true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			reason := wifiReconnectSkipReason(tt.ssid, tt.primaryIface)
			if gotSkip := reason != ""; gotSkip != tt.wantSkip {
				t.Fatalf("wifiReconnectSkipReason(%q, %q) = %q (skip=%v), want skip=%v",
					tt.ssid, tt.primaryIface, reason, gotSkip, tt.wantSkip)
			}
		})
	}
}
