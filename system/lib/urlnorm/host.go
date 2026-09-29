package urlnorm

import (
	"net/url"
	"strings"
)

// autonomousHosts are the registrable domains that serve our own gateway
// (*.autonomous.ai in production, *.autonomousdev.xyz in staging).
var autonomousHosts = []string{"autonomous.ai", "autonomousdev.xyz"}

// IsAutonomousHost reports whether baseURL points at an Autonomous-operated host.
// Strict: empty, unparseable or host-less input is false.
func IsAutonomousHost(baseURL string) bool {
	u, err := url.Parse(strings.TrimSpace(baseURL))
	if err != nil || u.Host == "" {
		return false
	}
	host := strings.ToLower(u.Hostname())
	for _, domain := range autonomousHosts {
		if host == domain || strings.HasSuffix(host, "."+domain) {
			return true
		}
	}
	return false
}
