package hermes

import (
	"context"
	_ "embed"
	"fmt"
	"net"
	"net/url"
	"os"
	"os/exec"
	"strings"
	"time"
)

//go:embed cache_usage_patch.py
var cacheUsagePatch string

// ensureCacheUsagePatch repairs the known local Hermes API serialization gap.
func (s *HermesService) ensureCacheUsagePatch() (bool, error) {
	u, err := url.Parse(BaseURL)
	if err != nil {
		return false, fmt.Errorf("parse Hermes URL: %w", err)
	}
	host := u.Hostname()
	ip := net.ParseIP(host)
	if host != "localhost" && (ip == nil || !ip.IsLoopback()) {
		return false, nil
	}
	const source = "/usr/local/lib/hermes-agent/gateway/platforms/api_server.py"
	if _, err := os.Stat(source); os.IsNotExist(err) {
		return false, nil
	} else if err != nil {
		return false, fmt.Errorf("stat Hermes API source: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "python3", "-c", cacheUsagePatch, source).CombinedOutput()
	if err != nil {
		return false, fmt.Errorf("patch Hermes cache usage: %s: %w", strings.TrimSpace(string(out)), err)
	}
	return strings.TrimSpace(string(out)) == "changed", nil
}
