import { C } from "@/components/setup/shared";
import { useTheme } from "@/lib/useTheme";

// Placeholder shown while the Setup page still can't know which step to open.
export function SetupSkeleton() {
  const [, , themeClass] = useTheme();
  const bar = (w: number | string, h: number, mb = 0) => (
    <div style={{ width: w, height: h, borderRadius: 6, background: C.surface, marginBottom: mb }} />
  );

  return (
    <div
      className={`lm-root lm-setup ${themeClass} lm-fade-in`}
      aria-busy="true"
      aria-label="Loading setup"
      style={{
        display: "flex", height: "100vh",
        background: C.bg, color: C.text,
        fontFamily: "'Inter', 'Segoe UI', sans-serif", fontSize: 14,
      }}
    >
      <aside
        className="lm-sidebar"
        style={{
          width: 192, flexShrink: 0,
          background: C.sidebar, borderRight: `1px solid ${C.border}`,
          display: "flex", flexDirection: "column",
        }}
      >
        <div style={{ padding: "16px 16px 12px" }}>
          {bar(96, 12, 8)}
          {bar(56, 8, 10)}
          <div className="lm-progress-track" />
        </div>
        <nav style={{ padding: "4px 12px 10px", flex: 1, display: "flex", flexDirection: "column", gap: 10 }}>
          {[0, 1, 2].map((i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 4px" }}>
              {bar(15, 15)}
              {bar(i === 0 ? 52 : 72, 9)}
            </div>
          ))}
        </nav>
      </aside>

      <main style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        <div style={{ borderBottom: `1px solid ${C.border}`, flexShrink: 0 }}>
          <div style={{
            padding: "12px 24px 10px",
            display: "flex", alignItems: "center", justifyContent: "space-between",
          }}>
            {bar(88, 13)}
            {bar(52, 9)}
          </div>
          <div className="lm-progress-track" style={{ borderRadius: 0 }} />
        </div>

        <div style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "24px 32px" }}>
          <div style={{ maxWidth: 560, margin: "0 auto" }}>
            <div className="lm-card" style={{ padding: "20px 22px", marginBottom: 16 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 18 }}>
                {bar(34, 34)}
                <div style={{ flex: 1 }}>
                  {bar(132, 11, 8)}
                  {bar("70%", 9)}
                </div>
              </div>
              {bar(64, 9, 10)}
              {bar("100%", 38, 16)}
              {bar(84, 9, 10)}
              {bar("100%", 38)}
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
