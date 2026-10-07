import { useEffect, useState } from "react";
import { getSetupStatus, type SetupStatus } from "@/lib/api";
import { C } from "@/components/setup/shared";

export function SetupRuntimeMessage({ failed, error }: { failed: boolean; error?: string }) {
  return (
    <div role="status" style={{ padding: 24, textAlign: "center", color: C.text }}>
      <h2>{failed ? "Voice setup needs attention" : "Preparing your robot’s voice…"}</h2>
      <p>{failed ? error || "Your robot could not finish starting. Your Wi-Fi settings are saved."
        : "Your robot is connected. Please wait while it gets ready to listen and speak."}</p>
      {!failed && <div className="lm-indeterminate" />}
      {failed && <button type="button" className="lm-btn lm-btn-primary" onClick={() => {
        window.location.hash = "force";
        window.location.reload();
      }}>Back to setup</button>}
    </div>
  );
}

// Survive the AP-to-LAN redirect or a reload while onboarding is still preparing.
export function SetupRuntimeProgress({ initial }: { initial: SetupStatus }) {
  const [status, setStatus] = useState(initial);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await getSetupStatus();
        if (cancelled) return;
        if (next.runtime_phase === "ready" || (!next.runtime_phase && next.set_up_completed)) {
          window.location.reload();
          return;
        }
        setStatus(next);
      } catch { /* Keep the pending state across a temporary connection failure. */ }
      if (!cancelled) timer = setTimeout(poll, 1500);
    };
    timer = setTimeout(poll, 1500);
    return () => { cancelled = true; clearTimeout(timer); };
  }, []);
  return <SetupRuntimeMessage failed={status.runtime_phase === "failed"} error={status.error} />;
}
