package network

import (
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"go.autonomous.ai/os/system/lib/urlnorm"
	"go.autonomous.ai/os/system/server/config"
)

func TestCloudProbeAddr(t *testing.T) {
	// Placeholder hosts under the domains the probe accepts.
	domains := urlnorm.AutonomousDomains()
	prod, staging := "api.example."+domains[0], "api.example."+domains[1]
	cases := []struct {
		name, base, want string
	}{
		{name: "fresh device before setup", base: "", want: defaultCloudProbeAddr},
		{name: "device's own cloud API", base: "https://" + prod + "/v1", want: prod + ":443"},
		{name: "staging cloud API", base: "https://" + staging + "/v1", want: staging + ":443"},
		{name: "explicit port kept", base: "https://" + prod + ":8443/v1", want: prod + ":8443"},
		{name: "owner's own provider is never probed", base: "https://api.openai.com/v1", want: defaultCloudProbeAddr},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := &Service{config: &config.Config{LLMBaseURL: tc.base}}
			if got := s.cloudProbeAddr(); got != tc.want {
				t.Errorf("cloudProbeAddr() = %q, want %q", got, tc.want)
			}
		})
	}
}

func stubProbes(t *testing.T, icmpErr, tlsErr error) *int {
	t.Helper()
	oldICMP, oldTLS := icmpPing, tlsReach
	tlsCalls := 0
	icmpPing = func(string) ([]byte, error) { return []byte("time=12.3 ms"), icmpErr }
	tlsReach = func(string, time.Duration) error { tlsCalls++; return tlsErr }
	t.Cleanup(func() { icmpPing, tlsReach = oldICMP, oldTLS })
	return &tlsCalls
}

func TestCheckInternetFallsBackToTLSWhenICMPIsBlocked(t *testing.T) {
	blocked := errors.New("100% packet loss")
	cases := []struct {
		name            string
		icmpErr, tlsErr error
		wantOK          bool
		wantTLSCalls    int
		wantErrMentions []string
	}{
		{name: "ICMP works", wantOK: true, wantTLSCalls: 0},
		{name: "ICMP blocked, cloud reachable", icmpErr: blocked, wantOK: true, wantTLSCalls: 1},
		{name: "no internet", icmpErr: blocked, tlsErr: errors.New("i/o timeout"), wantOK: false, wantTLSCalls: 1,
			wantErrMentions: []string{"8.8.8.8", defaultCloudProbeAddr, "i/o timeout"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			calls := stubProbes(t, tc.icmpErr, tc.tlsErr)
			s := &Service{config: &config.Config{}}
			ok, err := s.CheckInternet()
			if ok != tc.wantOK {
				t.Fatalf("CheckInternet() ok = %v, want %v (err %v)", ok, tc.wantOK, err)
			}
			if *calls != tc.wantTLSCalls {
				t.Errorf("TLS probes = %d, want %d: ICMP success must not pay for a second probe", *calls, tc.wantTLSCalls)
			}
			for _, want := range tc.wantErrMentions {
				if err == nil || !strings.Contains(err.Error(), want) {
					t.Errorf("error %v should mention %q", err, want)
				}
			}
		})
	}
}

func TestTLSReachAcceptsAnyServerCertificate(t *testing.T) {
	// httptest's certificate is self-signed: a verifying probe would fail here
	// exactly as it fails on a device whose clock NTP has not fixed yet.
	server := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer server.Close()
	if err := tlsReach(server.Listener.Addr().String(), time.Second); err != nil {
		t.Fatalf("tlsReach(live TLS server) = %v, want nil", err)
	}

	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	l.Close()
	if err := tlsReach(addr, time.Second); err == nil {
		t.Fatal("tlsReach(closed port) = nil, want an error")
	}
}

func TestCheckInternetRTTFallsBackToTLSWhenICMPIsBlocked(t *testing.T) {
	blocked := errors.New("100% packet loss")
	cases := []struct {
		name            string
		icmpErr, tlsErr error
		wantOK          bool
		wantRTT         float64
	}{
		{name: "ICMP works", wantOK: true, wantRTT: 12.3},
		{name: "ICMP blocked, cloud reachable", icmpErr: blocked, wantOK: true, wantRTT: 0},
		{name: "no internet", icmpErr: blocked, tlsErr: errors.New("i/o timeout"), wantOK: false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			stubProbes(t, tc.icmpErr, tc.tlsErr)
			s := &Service{config: &config.Config{}}
			ok, rtt := s.CheckInternetRTT()
			if ok != tc.wantOK || rtt != tc.wantRTT {
				t.Fatalf("CheckInternetRTT() = (%v, %v), want (%v, %v)", ok, rtt, tc.wantOK, tc.wantRTT)
			}
		})
	}
}
