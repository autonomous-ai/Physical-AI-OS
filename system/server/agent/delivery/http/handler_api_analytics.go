package http

import (
	"bufio"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/server/serializers"
)

func (h *AgentHandler) Analytics(c *gin.Context) {
	toDate := c.DefaultQuery("to", time.Now().Format("2006-01-02"))
	fromDate := c.DefaultQuery("from", time.Now().AddDate(0, 0, -7).Format("2006-01-02"))

	from, err := time.Parse("2006-01-02", fromDate)
	if err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid from date"))
		return
	}
	to, err := time.Parse("2006-01-02", toDate)
	if err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid to date"))
		return
	}

	type dvKey struct{ date, version string }
	type dvMetrics struct {
		TurnCount    int     `json:"turnCount"`
		DurationAvg  float64 `json:"durationAvg"`
		DurationP50  float64 `json:"durationP50"`
		DurationP95  float64 `json:"durationP95"`
		TokensTotal  int     `json:"tokensTotal"`
		TokensInput  int     `json:"tokensInput"`
		TokensOutput int     `json:"tokensOutput"`
		TokensBilled int     `json:"tokensBilled"`
		TokensAvg    float64 `json:"tokensAvg"`
		InnerAvg     float64 `json:"innerAvg"`
		InnerMax     int     `json:"innerMax"`
	}

	type flowEvent struct {
		Kind       string         `json:"kind"`
		Node       string         `json:"node"`
		TS         float64        `json:"ts"`
		TraceID    string         `json:"trace_id"`
		DurationMs int64          `json:"duration_ms"`
		Data       map[string]any `json:"data"`
		Version    string         `json:"version"`
	}

	type turnData struct {
		version    string
		durationMs int64
		tokens     int
		tokensIn   int
		tokensOut  int
		cacheRead  int
		cacheWrite int
		toolCalls  int
	}

	allDates := []string{}
	versionSet := make(map[string]bool)
	dayTurns := make(map[string]map[string]*turnData) // date -> traceID -> turnData

	for d := from; !d.After(to); d = d.AddDate(0, 0, 1) {
		dateStr := d.Format("2006-01-02")
		path := filepath.Join("local", fmt.Sprintf("flow_events_%s.jsonl", dateStr))
		f, err := os.Open(path)
		if err != nil {
			continue
		}

		turns := make(map[string]*turnData)
		scanner := bufio.NewScanner(f)
		scanner.Buffer(make([]byte, 256*1024), 256*1024)
		for scanner.Scan() {
			var ev flowEvent
			if json.Unmarshal(scanner.Bytes(), &ev) != nil {
				continue
			}
			tid := ev.TraceID
			if tid == "" {
				continue
			}
			if turns[tid] == nil {
				turns[tid] = &turnData{}
			}
			td := turns[tid]

			if ev.Version != "" && td.version == "" {
				td.version = ev.Version
				versionSet[ev.Version] = true
			}

			if ev.Node == "lifecycle_end" && ev.DurationMs > 0 {
				td.durationMs = ev.DurationMs
			}
			if ev.Node == "token_usage" && ev.Data != nil {
				if v, ok := ev.Data["total_tokens"]; ok {
					td.tokens += toInt(v)
				}
				if v, ok := ev.Data["input_tokens"]; ok {
					td.tokensIn += toInt(v)
				}
				if v, ok := ev.Data["output_tokens"]; ok {
					td.tokensOut += toInt(v)
				}
				if v, ok := ev.Data["cache_read_tokens"]; ok {
					td.cacheRead += toInt(v)
				}
				if v, ok := ev.Data["cache_write_tokens"]; ok {
					td.cacheWrite += toInt(v)
				}
			}
			if ev.Node == "tool_call" {
				td.toolCalls++
			}
		}
		f.Close()

		if len(turns) > 0 {
			allDates = append(allDates, dateStr)
			dayTurns[dateStr] = turns
		}
	}

	type resultRow struct {
		Date    string    `json:"date"`
		Version string    `json:"version"`
		Metrics dvMetrics `json:"metrics"`
	}

	var rows []resultRow
	versions := make([]string, 0, len(versionSet))
	for v := range versionSet {
		versions = append(versions, v)
	}
	sort.Strings(versions)
	if len(versions) == 0 {
		versions = []string{"unknown"}
	}

	for _, dateStr := range allDates {
		turns := dayTurns[dateStr]

		grouped := make(map[string][]*turnData)
		for _, td := range turns {
			ver := td.version
			if ver == "" {
				ver = "unknown"
			}
			grouped[ver] = append(grouped[ver], td)
		}

		for ver, tds := range grouped {
			m := dvMetrics{TurnCount: len(tds)}
			var durations []float64
			for _, td := range tds {
				if td.durationMs > 0 {
					durations = append(durations, float64(td.durationMs))
				}
				m.TokensTotal += td.tokens
				m.TokensInput += td.tokensIn
				m.TokensOutput += td.tokensOut
				// Cached reads are billed at full price (no 0.1x discount) to match backend billing.
				m.TokensBilled += td.tokensIn + td.cacheWrite + td.cacheRead + td.tokensOut
				if td.toolCalls > m.InnerMax {
					m.InnerMax = td.toolCalls
				}
				m.InnerAvg += float64(td.toolCalls)
			}
			if m.TurnCount > 0 {
				m.TokensAvg = float64(m.TokensTotal) / float64(m.TurnCount)
				m.InnerAvg = m.InnerAvg / float64(m.TurnCount)
			}
			if len(durations) > 0 {
				sort.Float64s(durations)
				m.DurationAvg = avg(durations)
				m.DurationP50 = percentile(durations, 50)
				m.DurationP95 = percentile(durations, 95)
			}
			rows = append(rows, resultRow{Date: dateStr, Version: ver, Metrics: m})
		}
	}

	if rows == nil {
		rows = []resultRow{}
	}
	sort.Slice(rows, func(i, j int) bool {
		if rows[i].Date != rows[j].Date {
			return rows[i].Date < rows[j].Date
		}
		return rows[i].Version < rows[j].Version
	})

	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"rows":     rows,
		"dates":    allDates,
		"versions": versions,
	}))
}

func toInt(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case json.Number:
		i, _ := n.Int64()
		return int(i)
	}
	return 0
}

func avg(vals []float64) float64 {
	sum := 0.0
	for _, v := range vals {
		sum += v
	}
	return sum / float64(len(vals))
}

func percentile(sorted []float64, p float64) float64 {
	if len(sorted) == 0 {
		return 0
	}
	rank := p / 100.0 * float64(len(sorted)-1)
	lower := int(math.Floor(rank))
	upper := int(math.Ceil(rank))
	if lower == upper || upper >= len(sorted) {
		return sorted[lower]
	}
	frac := rank - float64(lower)
	return sorted[lower]*(1-frac) + sorted[upper]*frac
}
