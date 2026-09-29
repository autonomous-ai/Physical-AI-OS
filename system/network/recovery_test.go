package network

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

type recoveryDevice struct {
	ap        bool
	link      string
	ip        string
	stations  string
	fail      string
	mutations []string
	joinArgs  []string
}

func (d *recoveryDevice) run(_ context.Context, name string, args ...string) ([]byte, error) {
	key := name + " " + strings.Join(args, " ")
	if key == d.fail {
		return nil, errors.New("probe failed")
	}
	switch key {
	case "iw dev wlan0 info":
		if d.ap {
			return []byte("Interface wlan0\n\ttype AP\n"), nil
		}
		return []byte("Interface wlan0\n\ttype managed\n"), nil
	case "iw dev wlan0 station dump":
		return []byte(d.stations), nil
	case "iw dev wlan0 link":
		return []byte(d.link), nil
	case "ip -4 -o addr show dev wlan0 scope global":
		return []byte(d.ip), nil
	}
	d.mutations = append(d.mutations, name)
	switch name {
	case "/usr/local/bin/device-ap-mode":
		d.ap = true
	case "connect-wifi":
		d.ap = false
		d.joinArgs = append([]string(nil), args...)
	}
	return nil, nil
}

func TestWiFiRecoveryRoundTrip(t *testing.T) {
	ctx := context.Background()
	now := time.Now()
	d := &recoveryDevice{}
	r := wifiRecovery{run: d.run}
	tick := func(at time.Time) { r.tick(ctx, at, "home", "saved-password") }
	tick(now)
	tick(now.Add(wifiLossGrace - time.Second))
	if len(d.mutations) != 0 {
		t.Fatal("switched before grace period")
	}
	tick(now.Add(wifiLossGrace))
	if !d.ap {
		t.Fatal("lost WiFi did not enable hotspot")
	}
	tick(r.retryAt.Add(-time.Second))
	if len(d.mutations) != 1 {
		t.Fatal("retried before cooldown")
	}
	tick(r.retryAt)
	if d.ap || r.joinUntil.IsZero() {
		t.Fatal("did not try station mode")
	}
	if !reflect.DeepEqual(d.joinArgs, []string{"home", "saved-password"}) {
		t.Fatal("did not restore saved credentials")
	}
	// A script exit without association/DHCP is not success.
	tick(r.joinUntil)
	if !d.ap {
		t.Fatal("failed join did not restore hotspot")
	}
	tick(r.retryAt)
	d.link = "Connected to aa:bb:cc:dd:ee:ff\n\tSSID: home\n"
	d.ip = "3: wlan0 inet 192.168.1.23/24 scope global wlan0"
	tick(time.Now())
	if d.ap || !r.joinUntil.IsZero() {
		t.Fatal("successful join did not finish recovery")
	}
	if d.mutations[len(d.mutations)-1] != "systemctl" {
		t.Fatal("mDNS not reannounced")
	}
}

func TestWiFiRecoveryKeepsLocalLinkWithoutInternet(t *testing.T) {
	d := &recoveryDevice{link: "Connected to aa:bb\n\tSSID: home", ip: "3: wlan0 inet 10.0.0.5/24 scope global wlan0"}
	r := wifiRecovery{run: d.run}
	now := time.Now()
	r.tick(context.Background(), now, "home", "")
	r.tick(context.Background(), now.Add(10*time.Minute), "home", "")
	if len(d.mutations) != 0 {
		t.Fatalf("disrupted healthy LAN: %v", d.mutations)
	}
}

func TestWiFiRecoveryHotspotGuards(t *testing.T) {
	for _, tc := range []struct{ name, stations, fail string }{
		{"client connected", "Station aa:bb (on wlan0)", ""},
		{"client probe failed", "", "iw dev wlan0 station dump"},
		{"mode probe failed", "", "iw dev wlan0 info"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			d := &recoveryDevice{ap: true, stations: tc.stations, fail: tc.fail}
			now := time.Now()
			r := wifiRecovery{run: d.run, retryAt: now.Add(-time.Second)}
			r.tick(context.Background(), now, "home", "")
			if len(d.mutations) != 0 {
				t.Fatal("interrupted hotspot")
			}
		})
	}
}

func TestWiFiRecoveryRecognizesHotspotAfterRestart(t *testing.T) {
	d := &recoveryDevice{ap: true}
	r := wifiRecovery{run: d.run}
	now := time.Now()
	r.tick(context.Background(), now, "home", "")
	if r.retryAt.IsZero() || len(d.mutations) != 0 {
		t.Fatal("hotspot not given retry grace")
	}
	r.tick(context.Background(), now.Add(wifiAPRetryInterval), "home", "")
	if d.ap {
		t.Fatal("hotspot recovery did not resume")
	}
}

func TestWiFiRecoveryEarlyReconnectResetsGrace(t *testing.T) {
	d := &recoveryDevice{}
	r := wifiRecovery{run: d.run}
	now := time.Now()
	r.tick(context.Background(), now, "home", "")
	d.link = "Connected to aa:bb\n SSID: home"
	d.ip = "inet 10.0.0.5/24"
	r.tick(context.Background(), now.Add(time.Minute), "home", "")
	d.link = "Not connected."
	r.tick(context.Background(), now.Add(2*time.Minute), "home", "")
	if d.ap {
		t.Fatal("reused previous outage grace")
	}
}

func TestWiFiRecoveryFailedScriptRestoresAP(t *testing.T) {
	d := &recoveryDevice{ap: true, fail: "connect-wifi home password"}
	now := time.Now()
	r := wifiRecovery{run: d.run, retryAt: now}
	r.tick(context.Background(), now, "home", "password")
	if !reflect.DeepEqual(d.mutations, []string{"/usr/local/bin/device-ap-mode"}) {
		t.Fatal("failed script did not restore AP")
	}
}

func TestStationAddressAndSSID(t *testing.T) {
	for _, tc := range []struct {
		address string
		valid   bool
	}{
		{"inet 192.168.100.1/24", false}, {"inet 169.254.2.3/16", false},
		{"inet 127.0.0.1/8", false}, {"inet invalid", false}, {"", false},
		{"inet 192.168.1.7/24", true},
	} {
		if got := hasStationIPv4(tc.address); got != tc.valid {
			t.Errorf("address %q: got %v", tc.address, got)
		}
	}
	if stationLinkedTo("Not connected.", "home") || stationLinkedTo("Connected to aa:bb\n SSID: other", "home") {
		t.Fatal("accepted wrong association")
	}
	if !stationLinkedTo("Connected to aa:bb\n SSID: caf\\xc3\\xa9", "café") {
		t.Fatal("escaped SSID not decoded")
	}
}

func TestMonitorSkipsProvisioningAndUnconfiguredDevice(t *testing.T) {
	s := &Service{config: &config.Config{}}
	s.operationMu.Lock()
	done := make(chan struct{})
	go func() { s.runNetworkMonitorTick(context.Background()); close(done) }()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("monitor waited for provisioning lock")
	}
	s.operationMu.Unlock()
	s.recovery.lostSince = time.Now()
	s.runNetworkMonitorTick(context.Background())
	if !s.recovery.lostSince.IsZero() {
		t.Fatal("unconfigured device kept recovery state")
	}
}

func TestRecoveryCommandCancellation(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	r := wifiRecovery{}
	start := time.Now()
	// Shell and child must both stop, or the child keeps the pipe open.
	if _, err := r.command(ctx, "sh", "-c", "sleep 30 & wait"); err == nil {
		t.Fatal("cancelled command succeeded")
	}
	if time.Since(start) > 2*time.Second {
		t.Fatal("script child survived cancellation")
	}
}

func TestWiFiRecoveryCancelledBeforeRetry(t *testing.T) {
	d := &recoveryDevice{ap: true}
	now := time.Now()
	r := wifiRecovery{run: d.run, retryAt: now}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	r.tick(ctx, now, "home", "")
	if len(d.mutations) != 0 {
		t.Fatal("cancelled monitor started a transition")
	}
}

func TestWiFiRecoveryJoinProbeFailureRestoresHotspot(t *testing.T) {
	for _, probe := range []string{"iw dev wlan0 info", "iw dev wlan0 link", "ip -4 -o addr show dev wlan0 scope global"} {
		t.Run(probe, func(t *testing.T) {
			d := &recoveryDevice{fail: probe}
			now := time.Now()
			r := wifiRecovery{run: d.run, joinUntil: now}
			r.tick(context.Background(), now, "home", "")
			if !d.ap {
				t.Fatal("probe failure left hotspot down after join deadline")
			}
		})
	}
}
