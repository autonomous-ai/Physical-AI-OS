package http

import (
	"context"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/network"
	agenthttp "go.autonomous.ai/os/system/server/agent/delivery/http"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/server/serializers"
)

// serverStartTime records when the os-server process started.
var serverStartTime = time.Now()

// HealthHandler represents the HTTP handler for health and system info.
type HealthHandler struct {
	config         *config.Config
	networkService *network.Service
	agentGateway   domain.AgentGateway
}

func ProvideHealthHandler(cfg *config.Config, ns *network.Service, gw domain.AgentGateway) HealthHandler {
	return HealthHandler{config: cfg, networkService: ns, agentGateway: gw}
}

func (h *HealthHandler) Live(c *gin.Context) {
	c.JSON(http.StatusOK, serializers.ResponseSuccess("OK"))
}

func (h *HealthHandler) Readiness(c *gin.Context) {
	c.JSON(http.StatusOK, serializers.ResponseSuccess("OK"))
}

// SystemInfo returns CPU load, RAM usage, temperature, and uptime.
func (h *HealthHandler) SystemInfo(c *gin.Context) {
	info := map[string]any{
		"cpuLoad":       readCPUPercent(),
		"cpuCount":      runtime.NumCPU(),
		"cpuPerCore":    readCPUPerCore(),
		"memTotal":      0,
		"memUsed":       0,
		"memPercent":    0.0,
		"swapTotal":     0,
		"swapUsed":      0,
		"swapPercent":   0.0,
		"cpuTemp":       readCPUTemp(),
		"uptime":        readUptime(),
		"serviceUptime": int64(time.Since(serverStartTime).Seconds()),
		"halUptime":     readHALUptime(),
		"halVersion":    readHALVersion(),
		"goRoutines":    runtime.NumGoroutine(),
		"version":       config.OSVersion,
		"deviceId":      h.config.DeviceID,
		"capabilities":  h.deviceCapabilities(),
		"agent":         h.agentInfo(),
	}

	if data, err := os.ReadFile("/proc/meminfo"); err == nil {
		memTotal, memAvail, swapTotal, swapFree := parseMeminfo(string(data))
		info["memTotal"] = memTotal
		info["memUsed"] = memTotal - memAvail
		if memTotal > 0 {
			info["memPercent"] = float64(memTotal-memAvail) / float64(memTotal) * 100
		}
		info["swapTotal"] = swapTotal
		info["swapUsed"] = swapTotal - swapFree
		if swapTotal > 0 {
			info["swapPercent"] = float64(swapTotal-swapFree) / float64(swapTotal) * 100
		}
	}

	diskTotal, diskUsed, diskPercent := readDiskUsage("/")
	info["diskTotal"] = diskTotal
	info["diskUsed"] = diskUsed
	info["diskPercent"] = diskPercent

	c.JSON(http.StatusOK, serializers.ResponseSuccess(info))
}

// deviceCapabilities returns this device's DECLARED capabilities (sorted),
// read from robots/<type>/ROBOT.md via device.Capabilities.
func (h *HealthHandler) deviceCapabilities() []string {
	caps := device.Capabilities(h.config.DeviceTypeOrDefault())
	out := make([]string, 0, len(caps))
	for c := range caps {
		out = append(out, c)
	}
	sort.Strings(out)
	return out
}

// agentInfo returns the OpenClaw agent connection snapshot — name,
// connected state, emotion, version, and uptime counters.
func (h *HealthHandler) agentInfo() map[string]any {
	emotion, _ := hal.GetEmotion()
	var uptime int64
	if connectedAt := h.agentGateway.ConnectedAt(); connectedAt > 0 {
		uptime = time.Now().Unix() - connectedAt
		if uptime < 0 {
			uptime = 0
		}
	}
	return map[string]any{
		"name":        h.agentGateway.Name(),
		"connected":   h.agentGateway.IsReady(),
		"sessionKey":  h.agentGateway.GetSessionKey() != "",
		"emotion":     emotion,
		"version":     agenthttp.GetOpenClawVersion(),
		"uptime":      uptime,
		"agentUptime": h.agentGateway.AgentUptime(),
	}
}

// readDiskUsage returns total, used (in MB) and usage percent for the given path.
func readDiskUsage(path string) (totalMB, usedMB int64, percent float64) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return 0, 0, 0
	}
	total := stat.Blocks * uint64(stat.Bsize)
	free := stat.Bavail * uint64(stat.Bsize)
	used := total - free
	totalMB = int64(total / (1024 * 1024))
	usedMB = int64(used / (1024 * 1024))
	if total > 0 {
		percent = float64(used) / float64(total) * 100
	}
	return
}

// publicIPCache caches the public IP to avoid calling ifconfig.me on every request.
var publicIPCache = struct {
	mu        sync.Mutex
	ip        string
	fetchedAt time.Time
}{}

func getPublicIP() string {
	publicIPCache.mu.Lock()
	defer publicIPCache.mu.Unlock()
	if time.Since(publicIPCache.fetchedAt) < 5*time.Minute && publicIPCache.ip != "" {
		return publicIPCache.ip
	}
	client := &http.Client{Timeout: 3 * time.Second}
	req, err := http.NewRequest("GET", "https://ifconfig.me/ip", nil)
	if err != nil {
		return ""
	}
	req.Header.Set("User-Agent", "curl/7.64.1")
	resp, err := client.Do(req)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	buf := make([]byte, 64)
	n, _ := resp.Body.Read(buf)
	ip := strings.TrimSpace(string(buf[:n]))
	publicIPCache.ip = ip
	publicIPCache.fetchedAt = time.Now()
	return ip
}

// getTailscaleIP returns the device's Tailscale IPv4 address, or empty string
// if Tailscale isn't installed/running.
func getTailscaleIP() string {
	ctx, cancel := context.WithTimeout(context.Background(), 1500*time.Millisecond)
	defer cancel()
	out, err := exec.CommandContext(ctx, "tailscale", "ip", "-4").Output()
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(out), "\n") {
		if ip := strings.TrimSpace(line); ip != "" {
			return ip
		}
	}
	return ""
}

// NetworkInfo returns combined network status: SSID, IP, public IP, signal, internet.
func (h *HealthHandler) NetworkInfo(c *gin.Context) {
	info := map[string]any{
		"ssid":        "",
		"ip":          "",
		"publicIp":    "",
		"tailscaleIp": "",
		"signal":      0,
		"linkRate":    0,
		"internet":    false,
		"pingMs":      0,
		"mac":         device.GetDeviceMac(),
	}

	if netw, err := h.networkService.CurrentNetwork(); err == nil && netw != nil {
		info["ssid"] = netw.SSID
		info["signal"] = netw.Signal
		info["linkRate"] = netw.LinkRate
	}

	if ip, err := h.networkService.GetCurrentIP(); err == nil {
		info["ip"] = ip
	}

	info["tailscaleIp"] = getTailscaleIP()

	if ok, rtt := h.networkService.CheckInternetRTT(); ok {
		info["internet"] = true
		info["pingMs"] = int(rtt + 0.5)
		info["publicIp"] = getPublicIP()
	}

	c.JSON(http.StatusOK, serializers.ResponseSuccess(info))
}

// Dashboard returns a combined status snapshot for the monitor page.
func (h *HealthHandler) Dashboard(c *gin.Context) {
	dash := map[string]any{
		"openclaw": map[string]any{
			"connected":  h.agentGateway.IsReady(),
			"sessionKey": h.agentGateway.GetSessionKey() != "",
		},
		"version":  config.OSVersion,
		"deviceId": h.config.DeviceID,
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(dash))
}

// cpuSampler periodically measures actual CPU usage from /proc/stat,
// both aggregate and per-core, computed as delta between two snapshots.
var cpuSampler = struct {
	mu      sync.RWMutex
	pct     float64
	perCore []float64
	once    sync.Once
}{}

func initCPUSampler() {
	cpuSampler.once.Do(func() {
		go func() {
			prevAgg, prevCores := readCPUStatAll()
			for {
				time.Sleep(2 * time.Second)
				curAgg, curCores := readCPUStatAll()

				totalDelta := curAgg.total - prevAgg.total
				idleDelta := curAgg.idle - prevAgg.idle
				var pct float64
				if totalDelta > 0 {
					pct = float64(totalDelta-idleDelta) / float64(totalDelta) * 100
				}

				perCore := make([]float64, 0, len(curCores))
				for i := 0; i < len(curCores) && i < len(prevCores); i++ {
					td := curCores[i].total - prevCores[i].total
					id := curCores[i].idle - prevCores[i].idle
					if td > 0 {
						perCore = append(perCore, float64(td-id)/float64(td)*100)
					} else {
						perCore = append(perCore, 0)
					}
				}

				cpuSampler.mu.Lock()
				cpuSampler.pct = pct
				cpuSampler.perCore = perCore
				cpuSampler.mu.Unlock()

				prevAgg, prevCores = curAgg, curCores
			}
		}()
	})
}

type cpuStat struct {
	idle  uint64
	total uint64
}

// readCPUStatAll parses /proc/stat once, returning aggregate plus per-core stats.
// Per-core lines look like `cpu0 …`, `cpu1 …` after the leading `cpu ` aggregate.
func readCPUStatAll() (agg cpuStat, perCore []cpuStat) {
	data, err := os.ReadFile("/proc/stat")
	if err != nil {
		return
	}
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) < 5 || !strings.HasPrefix(fields[0], "cpu") {
			continue
		}
		var total, idle uint64
		for i, f := range fields[1:] {
			v, _ := strconv.ParseUint(f, 10, 64)
			total += v
			if i == 3 {
				idle = v
			}
		}
		if fields[0] == "cpu" {
			agg = cpuStat{idle: idle, total: total}
		} else {
			perCore = append(perCore, cpuStat{idle: idle, total: total})
		}
	}
	return
}

// readCPUPercent returns the latest sampled CPU usage percentage.
func readCPUPercent() float64 {
	initCPUSampler()
	cpuSampler.mu.RLock()
	defer cpuSampler.mu.RUnlock()
	return cpuSampler.pct
}

// readCPUPerCore returns latest sampled per-core CPU usage percentages.
func readCPUPerCore() []float64 {
	initCPUSampler()
	cpuSampler.mu.RLock()
	defer cpuSampler.mu.RUnlock()
	out := make([]float64, len(cpuSampler.perCore))
	copy(out, cpuSampler.perCore)
	return out
}

// readCPUTemp reads CPU temperature in celsius from thermal zone.
func readCPUTemp() float64 {
	data, err := os.ReadFile("/sys/class/thermal/thermal_zone0/temp")
	if err != nil {
		return 0
	}
	milliC, _ := strconv.Atoi(strings.TrimSpace(string(data)))
	return float64(milliC) / 1000.0
}

// halVersionCache holds the most recent /version reading.
var halVersionCache = struct {
	mu        sync.Mutex
	value     string
	fetchedAt time.Time
}{}

// readHALVersion returns HAL's runtime version via GET :5001/version,
// memoized for 60s. Empty string when HAL has never responded successfully.
func readHALVersion() string {
	halVersionCache.mu.Lock()
	defer halVersionCache.mu.Unlock()
	if time.Since(halVersionCache.fetchedAt) < 60*time.Second && halVersionCache.value != "" {
		return halVersionCache.value
	}
	v, err := hal.GetVersion()
	if err != nil {
		return halVersionCache.value
	}
	halVersionCache.value = v
	halVersionCache.fetchedAt = time.Now()
	return v
}

// readHALUptime returns uptime in seconds of the hal systemd service.
func readHALUptime() int64 {
	out, err := exec.Command("systemctl", "show", "hal",
		"--property=ActiveEnterTimestamp", "--value").Output()
	if err != nil {
		return 0
	}
	ts := strings.TrimSpace(string(out))
	if ts == "" || ts == "n/a" {
		return 0
	}
	formats := []string{
		"Mon 2006-01-02 15:04:05 -0700",
		"Mon 2006-01-02 15:04:05 MST",
	}
	for _, f := range formats {
		if t, err := time.Parse(f, ts); err == nil {
			d := time.Since(t)
			if d > 0 {
				return int64(d.Seconds())
			}
		}
	}
	return 0
}

// readUptime reads system uptime in seconds from /proc/uptime.
func readUptime() int64 {
	data, err := os.ReadFile("/proc/uptime")
	if err != nil {
		return 0
	}
	parts := strings.Fields(string(data))
	if len(parts) < 1 {
		return 0
	}
	f, _ := strconv.ParseFloat(parts[0], 64)
	return int64(f)
}

// parseMeminfo extracts MemTotal/MemAvailable/SwapTotal/SwapFree (all in KB)
// from /proc/meminfo content.
func parseMeminfo(content string) (memTotal, memAvail, swapTotal, swapFree int64) {
	for _, line := range strings.Split(content, "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		v, _ := strconv.ParseInt(fields[1], 10, 64)
		switch {
		case strings.HasPrefix(line, "MemTotal:"):
			memTotal = v
		case strings.HasPrefix(line, "MemAvailable:"):
			memAvail = v
		case strings.HasPrefix(line, "SwapTotal:"):
			swapTotal = v
		case strings.HasPrefix(line, "SwapFree:"):
			swapFree = v
		}
	}
	return
}
