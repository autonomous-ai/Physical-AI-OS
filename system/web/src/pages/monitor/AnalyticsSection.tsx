import { useCallback, useEffect, useMemo, useState } from "react";
import { Bar, Line } from "react-chartjs-2";
import { S } from "./styles";
import { API } from "./types";

interface VersionMetrics {
  turnCount: number;
  durationAvg: number;
  durationP50: number;
  durationP95: number;
  tokensTotal: number;
  tokensInput: number;
  tokensOutput: number;
  tokensBilled: number;
  tokensAvg: number;
  innerAvg: number;
  innerMax: number;
}

interface AnalyticsRow {
  date: string;
  version: string;
  metrics: VersionMetrics;
}

interface AnalyticsData {
  rows: AnalyticsRow[];
  dates: string[];
  versions: string[];
}

type Preset = "7d" | "14d" | "30d" | "custom";

function fmtDate(d: Date) {
  return d.toISOString().slice(0, 10);
}

const CHART_COLORS = {
  amber: "rgba(245,158,11,0.85)",
  amberFill: "rgba(245,158,11,0.15)",
  green: "rgba(52,211,153,0.85)",
  greenFill: "rgba(52,211,153,0.15)",
  blue: "rgba(96,165,250,0.85)",
  blueFill: "rgba(96,165,250,0.15)",
  purple: "rgba(168,85,247,0.85)",
  purpleFill: "rgba(168,85,247,0.15)",
  teal: "rgba(45,212,191,0.85)",
  tealFill: "rgba(45,212,191,0.15)",
  red: "rgba(248,113,113,0.85)",
  gridColor: "rgba(255,255,255,0.06)",
  tickColor: "rgba(255,255,255,0.4)",
};

const chartScaleDefaults = {
  grid: { color: CHART_COLORS.gridColor },
  ticks: { color: CHART_COLORS.tickColor, font: { size: 10 } },
};

const VERSION_COLORS = [
  { border: "rgba(56,189,248,0.85)",  bg: "rgba(56,189,248,0.15)"  },
  { border: "rgba(129,140,248,0.85)", bg: "rgba(129,140,248,0.15)" },
  { border: "rgba(167,139,250,0.85)", bg: "rgba(167,139,250,0.15)" },
  { border: "rgba(232,121,249,0.85)", bg: "rgba(232,121,249,0.15)" },
  { border: "rgba(244,114,182,0.85)", bg: "rgba(244,114,182,0.15)" },
  { border: "rgba(251,146,60,0.85)",  bg: "rgba(251,146,60,0.15)"  },
];

function vColor(i: number) {
  return VERSION_COLORS[i % VERSION_COLORS.length];
}

export function AnalyticsSection() {
  const [preset, setPreset] = useState<Preset>("7d");
  const [customFrom, setCustomFrom] = useState(() => fmtDate(new Date(Date.now() - 7 * 86400000)));
  const [customTo, setCustomTo] = useState(() => fmtDate(new Date()));
  const [analytics, setAnalytics] = useState<AnalyticsData | null>(null);
  const [loading, setLoading] = useState(false);

  const dateRange = useMemo(() => {
    if (preset === "custom") return { from: customFrom, to: customTo };
    const days = preset === "7d" ? 7 : preset === "14d" ? 14 : 30;
    // eslint-disable-next-line react-hooks/purity -- a relative preset ("last 7d") must resolve against the clock at the moment the preset changes. Freezing "now" into state would pin the window to whenever the tab was opened, so a dashboard left open overnight would keep querying yesterday's range.
    return { from: fmtDate(new Date(Date.now() - days * 86400000)), to: fmtDate(new Date()) };
  }, [preset, customFrom, customTo]);

  const fetchAnalytics = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch(`${API}/agent/analytics?from=${dateRange.from}&to=${dateRange.to}`);
      const j = await r.json();
      if (j.status === 1) setAnalytics(j.data);
    } catch { /* ignore */ }
    setLoading(false);
  }, [dateRange]);

  useEffect(() => { fetchAnalytics(); }, [fetchAnalytics]);

  const dates = analytics?.dates ?? [];
  const allVersions = useMemo(() => analytics?.versions ?? [], [analytics]);
  const allRows = useMemo(() => analytics?.rows ?? [], [analytics]);
  const labels = dates.map((d) => d.slice(5));

  const VERSION_LIMIT = 5;
  const versions = useMemo(() => {
    if (allVersions.length <= VERSION_LIMIT) return allVersions;
    const lastSeen: Record<string, string> = {};
    for (const r of allRows) {
      if (!lastSeen[r.version] || r.date > lastSeen[r.version]) {
        lastSeen[r.version] = r.date;
      }
    }
    return [...allVersions]
      .sort((a, b) => (lastSeen[b] ?? "").localeCompare(lastSeen[a] ?? ""))
      .slice(0, VERSION_LIMIT);
  }, [allVersions, allRows]);

  const versionSet = useMemo(() => new Set(versions), [versions]);
  const rows = useMemo(() => allRows.filter((r) => versionSet.has(r.version)), [allRows, versionSet]);

  const multiVersion = versions.length > 1;
  const hiddenVersionCount = allVersions.length - versions.length;

  const rowMap = useMemo(() => {
    const m: Record<string, Record<string, VersionMetrics>> = {};
    for (const r of rows) {
      if (!m[r.date]) m[r.date] = {};
      m[r.date][r.version] = r.metrics;
    }
    return m;
  }, [rows]);

  const val = (date: string, ver: string, fn: (m: VersionMetrics) => number) => {
    const m = rowMap[date]?.[ver];
    return m ? fn(m) : 0;
  };

  const totalTurns = rows.reduce((s, r) => s + r.metrics.turnCount, 0);
  const totalTokens = rows.reduce((s, r) => s + r.metrics.tokensTotal, 0);
  const totalBilled = rows.reduce((s, r) => s + r.metrics.tokensBilled, 0);
  const durRows = rows.filter((r) => r.metrics.durationAvg > 0);
  const avgDuration = durRows.length > 0 ? durRows.reduce((s, r) => s + r.metrics.durationAvg, 0) / durRows.length : 0;
  const innerRows = rows.filter((r) => r.metrics.innerAvg > 0);
  const avgInner = innerRows.length > 0 ? innerRows.reduce((s, r) => s + r.metrics.innerAvg, 0) / innerRows.length : 0;

  const commonOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { labels: { color: CHART_COLORS.tickColor, font: { size: 11 } } },
    },
    scales: { x: chartScaleDefaults, y: chartScaleDefaults },
  };

  const pillStyle = (active: boolean): React.CSSProperties => ({
    padding: "5px 14px",
    borderRadius: 6,
    border: `1px solid ${active ? "var(--lm-amber)" : "var(--lm-border)"}`,
    background: active ? "rgba(245,158,11,0.12)" : "transparent",
    color: active ? "var(--lm-amber)" : "var(--lm-text-dim)",
    fontSize: 11.5,
    fontWeight: active ? 600 : 400,
    cursor: "pointer",
  });

  const summaryCardStyle: React.CSSProperties = {
    ...S.card,
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    gap: 4,
    padding: "16px 12px",
  };

  const dateInputStyle: React.CSSProperties = {
    background: "var(--lm-surface)",
    color: "var(--lm-text)",
    border: "1px solid var(--lm-border)",
    borderRadius: 6,
    padding: "5px 10px",
    fontSize: 11,
    fontFamily: "monospace",
    colorScheme: "dark",
  };

  const makeVersionDatasets = (
    fn: (m: VersionMetrics) => number,
    opts?: { type?: "bar" | "line"; fill?: boolean; singleLabel?: string },
  ) => {
    const type = opts?.type ?? "line";
    return versions.map((ver, vi) => ({
      label: multiVersion ? `v${ver}` : (opts?.singleLabel ?? "Value"),
      data: dates.map((d) => val(d, ver, fn)),
      ...(type === "bar"
        ? { backgroundColor: vColor(vi).border, borderRadius: 4, barPercentage: multiVersion ? 0.8 : 0.6 }
        : {
            borderColor: vColor(vi).border,
            backgroundColor: opts?.fill !== false ? vColor(vi).bg : undefined,
            fill: opts?.fill !== false,
            tension: 0.3,
            pointRadius: 3,
          }),
    }));
  };

  const exportCsv = () => {
    const headers = ["date", "version", "turns", "billed_tokens", "raw_tokens", "duration_avg_ms", "duration_p50_ms", "duration_p95_ms", "tokens_avg", "inner_avg", "inner_max"];
    const lines = [headers.join(",")];
    for (const r of rows) {
      lines.push([
        r.date, r.version,
        r.metrics.turnCount, r.metrics.tokensBilled, r.metrics.tokensTotal,
        r.metrics.durationAvg, r.metrics.durationP50, r.metrics.durationP95,
        Math.round(r.metrics.tokensAvg), r.metrics.innerAvg.toFixed(2), r.metrics.innerMax,
      ].join(","));
    }
    const blob = new Blob([lines.join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `analytics-${dateRange.from}_to_${dateRange.to}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={{ ...S.card, display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <span style={{ fontSize: 11, color: "var(--lm-text-muted)", fontWeight: 600 }}>RANGE</span>
          {(["7d", "14d", "30d", "custom"] as Preset[]).map((p) => (
            <button key={p} style={pillStyle(preset === p)} onClick={() => setPreset(p)}>
              {p === "custom" ? "Custom" : p}
            </button>
          ))}
          {preset === "custom" && (
            <>
              <input
                type="date"
                value={customFrom}
                onChange={(e) => setCustomFrom(e.target.value)}
                style={dateInputStyle}
              />
              <span style={{ color: "var(--lm-text-muted)" }}>—</span>
              <input
                type="date"
                value={customTo}
                onChange={(e) => setCustomTo(e.target.value)}
                style={dateInputStyle}
              />
            </>
          )}
          <span style={{ flex: 1 }} />
          {loading && (
            <span style={{
              fontSize: 10, padding: "2px 8px", borderRadius: 4,
              background: "rgba(245,158,11,0.15)", color: "var(--lm-amber)",
              fontWeight: 700, letterSpacing: "0.05em",
            }}>LOADING…</span>
          )}
          <button
            onClick={fetchAnalytics}
            disabled={loading}
            title="Refresh"
            style={{
              padding: "5px 12px", borderRadius: 6, fontSize: 12, fontWeight: 600,
              background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
              color: "var(--lm-text-dim)", cursor: loading ? "wait" : "pointer",
            }}
          >↻</button>
          <button
            onClick={exportCsv}
            disabled={rows.length === 0}
            title="Download as CSV"
            style={{
              padding: "5px 12px", borderRadius: 6, fontSize: 12, fontWeight: 600,
              background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
              color: rows.length === 0 ? "var(--lm-text-muted)" : "var(--lm-amber)",
              cursor: rows.length === 0 ? "not-allowed" : "pointer",
            }}
          >↓ CSV</button>
        </div>

        {versions.length > 0 && (
          <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <span style={{ fontSize: 10, color: "var(--lm-text-muted)", fontWeight: 600, letterSpacing: "0.05em" }}>VERSIONS</span>
            {versions.map((v, i) => (
              <span key={v} style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11, color: "var(--lm-text-dim)" }}>
                <span style={{ display: "inline-block", width: 10, height: 10, borderRadius: 2, background: vColor(i).border }} />
                v{v}
              </span>
            ))}
            {hiddenVersionCount > 0 && (
              <span
                style={{ fontSize: 10, color: "var(--lm-text-muted)", fontStyle: "italic" }}
                title="Older versions are excluded so colors stay distinct. Narrow the range to inspect them."
              >
                +{hiddenVersionCount} older hidden
              </span>
            )}
          </div>
        )}
      </div>

      <div className="lm-grid-4">
        <div style={summaryCardStyle}>
          <span style={{ fontSize: 22, fontWeight: 700, color: "var(--lm-amber)" }}>{totalTurns}</span>
          <span style={{ fontSize: 10, color: "var(--lm-text-muted)", fontWeight: 600 }}>TOTAL TURNS</span>
        </div>
        <div style={summaryCardStyle}>
          <span style={{ fontSize: 22, fontWeight: 700, color: "var(--lm-green)" }}>{totalBilled.toLocaleString()}</span>
          <span style={{ fontSize: 10, color: "var(--lm-text-muted)", fontWeight: 600 }}>BILLED TOKENS</span>
          <span style={{ fontSize: 9, color: "var(--lm-text-muted)" }}>({totalTokens.toLocaleString()} raw)</span>
        </div>
        <div style={summaryCardStyle}>
          <span style={{ fontSize: 22, fontWeight: 700, color: "var(--lm-blue)" }}>{avgDuration ? (avgDuration / 1000).toFixed(1) + "s" : "—"}</span>
          <span style={{ fontSize: 10, color: "var(--lm-text-muted)", fontWeight: 600 }}>AVG DURATION</span>
        </div>
        <div style={summaryCardStyle}>
          <span style={{ fontSize: 22, fontWeight: 700, color: "var(--lm-purple)" }}>{avgInner ? avgInner.toFixed(1) : "—"}</span>
          <span style={{ fontSize: 10, color: "var(--lm-text-muted)", fontWeight: 600 }}>AVG INNER STEPS</span>
        </div>
      </div>

      {rows.length === 0 && !loading && (
        <div style={{ ...S.card, textAlign: "center", padding: 40, color: "var(--lm-text-muted)" }}>
          No analytics data for selected range
        </div>
      )}

      {rows.length > 0 && (
        <>
          <div className="lm-grid-2">
            <div style={{ ...S.card, height: 280 }}>
              <div style={S.cardLabel}>Turn Count per Day {multiVersion && "— by version"}</div>
              <div style={{ height: 230 }}>
                <Bar
                  data={{ labels, datasets: makeVersionDatasets((m) => m.turnCount, { type: "bar", singleLabel: "Turns" }) }}
                  options={commonOptions}
                />
              </div>
            </div>

            <div style={{ ...S.card, height: 280 }}>
              <div style={S.cardLabel}>Avg Duration (seconds) {multiVersion && "— by version"}</div>
              <div style={{ height: 230 }}>
                <Line
                  data={{ labels, datasets: makeVersionDatasets((m) => +(m.durationAvg / 1000).toFixed(2), { singleLabel: "Duration (s)" }) }}
                  options={commonOptions}
                />
              </div>
            </div>
          </div>

          <div className="lm-grid-2">
            <div style={{ ...S.card, height: 280 }}>
              <div style={S.cardLabel}>Billed Tokens {multiVersion && "— by version"}</div>
              <div style={{ height: 230 }}>
                {multiVersion ? (
                  <Bar
                    data={{ labels, datasets: makeVersionDatasets((m) => m.tokensBilled, { type: "bar", singleLabel: "Billed" }) }}
                    options={commonOptions}
                  />
                ) : (
                  <Bar
                    data={{
                      labels,
                      datasets: [
                        { label: "Billed", data: dates.map((d) => val(d, versions[0], (m) => m.tokensBilled)), backgroundColor: CHART_COLORS.green, borderRadius: 2 },
                        { label: "Raw total", data: dates.map((d) => val(d, versions[0], (m) => m.tokensTotal)), backgroundColor: CHART_COLORS.blue, borderRadius: 2, hidden: true },
                      ],
                    }}
                    options={commonOptions}
                  />
                )}
              </div>
            </div>

            <div style={{ ...S.card, height: 280 }}>
              <div style={S.cardLabel}>Tokens per Turn {multiVersion && "— by version"}</div>
              <div style={{ height: 230 }}>
                <Line
                  data={{ labels, datasets: makeVersionDatasets((m) => Math.round(m.tokensAvg), { singleLabel: "Avg tokens" }) }}
                  options={commonOptions}
                />
              </div>
            </div>
          </div>

          <div style={{ ...S.card, height: 280 }}>
            <div
              style={S.cardLabel}
              title="Average number of inner agent loop steps per turn (tool calls + reasoning iterations before final response)"
            >
              Inner Loop Steps {multiVersion && "— by version"}
            </div>
            <div style={{ height: 230 }}>
              <Line
                data={{ labels, datasets: makeVersionDatasets((m) => +m.innerAvg.toFixed(1), { singleLabel: "Inner steps" }) }}
                options={commonOptions}
              />
            </div>
          </div>
        </>
      )}
    </div>
  );
}
