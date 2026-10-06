import { Wifi, Cable, XCircle, CheckCircle2 } from "lucide-react";
import { C } from "@/components/setup/shared";
import { getInitialSearch } from "@/hooks/setup/useSetupUrlParams";
import { setupBridge } from "@/lib/setupBridge";
import type { SetupPhase } from "@/hooks/setup/useSetupStatusPolling";
import { SetupRuntimeMessage } from "./SetupRuntimeProgress";
import { CopyAddress } from "./CopyAddress";

// Post-submit screen: join progress, then the LAN address to continue setup.
export function SetupProgressScreen({
  setupPhase, setupLanIP, setupErrorMsg, elapsed,
  deviceMdnsHost, deviceTypePrefix, wired = false,
  onRetry,
}: {
  setupPhase: SetupPhase;
  setupLanIP: string;
  setupErrorMsg: string;
  elapsed: number;
  deviceMdnsHost: string;
  deviceTypePrefix: string;
  // Submitted with no SSID (wired uplink): no Wi-Fi join happens.
  wired?: boolean;
  onRetry: () => void;
}) {
  return (
    <div className="lm-card lm-fade-in" style={{
      padding: "32px 24px", textAlign: "center",
    }}>
      {(setupPhase === "preparing" || setupPhase === "runtime_failed") && (
        <SetupRuntimeMessage failed={setupPhase === "runtime_failed"} error={setupErrorMsg} />
      )}
      {setupPhase === "connecting" && (
        <>
          <div style={{ display: "flex", justifyContent: "center", marginBottom: 14 }}>
            <span className="lm-wifi-pulse" aria-hidden>
              <span className="lm-wifi-ring" />
              <span className="lm-wifi-ring lm-r2" />
              <span className="lm-wifi-ring lm-r3" />
              <span className="lm-wifi-icon">
                {wired ? <Cable size={26} strokeWidth={2} /> : <Wifi size={26} strokeWidth={2} />}
              </span>
            </span>
          </div>
          <div style={{ fontSize: 14.5, fontWeight: 600, color: C.amber, marginBottom: 8 }}>
            {wired ? "Finishing setup on your wired connection" : "Your robot is joining Wi-Fi"}
            <span className="lm-blink">.</span><span>.</span><span>.</span>
          </div>
          <div style={{ fontSize: 13, color: C.textDim, marginBottom: 14, lineHeight: 1.5 }}>
            {wired
              ? "Your robot is already online over its cable, so there is no Wi-Fi to join. It's turning off its setup hotspot now."
              : "Please be patient while your robot connects to Wi-Fi. Stay on this network."}
          </div>
          <div className="lm-indeterminate" style={{ marginBottom: 7 }} />
          <div style={{ fontSize: 11, color: C.textMuted }}>
            Elapsed {elapsed}s
          </div>
        </>
      )}

      {setupPhase === "connected" && (
        <>
          <div style={{ display: "flex", justifyContent: "center", marginBottom: 14 }}>
            <CheckCircle2 size={34} color={C.green} strokeWidth={1.75} aria-hidden />
          </div>
          <div style={{ fontSize: 14.5, fontWeight: 600, color: C.amber, marginBottom: 16 }}>
            Network connected
          </div>

          {setupLanIP ? (
            <>
              <div style={{ fontSize: 13, color: C.textDim, marginBottom: 16, lineHeight: 1.5 }}>
                Reconnect your computer to your home Wi-Fi, then click
                Continue to check setup progress. Your robot may still be getting ready.
              </div>
              <a
                // Force reload when already on the device IP (a same-URL click is a no-op).
                href={`http://${setupLanIP}${window.location.pathname}${getInitialSearch()}`}
                onClick={(e) => {
                  setupBridge.continueClicked({ mdns_host: deviceMdnsHost });
                  if (window.location.hostname === setupLanIP) {
                    e.preventDefault();
                    window.location.reload();
                  }
                }}
                className="lm-btn lm-btn-primary"
                style={{
                  display: "inline-block", padding: "10px 22px",
                  textDecoration: "none",
                }}
              >
                Continue setup →
              </a>
              <div style={{
                marginTop: 18, paddingTop: 16,
                borderTop: `1px solid ${C.border}`, textAlign: "left",
              }}>
                <div style={{ fontSize: 13, color: C.textDim, marginBottom: 6, lineHeight: 1.5 }}>
                  Or open this address once you're back on home Wi-Fi:
                </div>
                <CopyAddress url={`http://${setupLanIP}/setup`} />
                <div style={{ fontSize: 12, color: C.textMuted, marginTop: 8, lineHeight: 1.5 }}>
                  Can't reach it? Find your robot's IP in your router's
                  admin page{deviceTypePrefix ? ` (look for "${deviceTypePrefix}")` : ""}.
                </div>
              </div>
            </>
          ) : (
            <div style={{ fontSize: 13, color: C.textDim, lineHeight: 1.5 }}>
              Your robot is connected. Open your router's admin page to find
              the robot's IP address{deviceTypePrefix ? ` (look for "${deviceTypePrefix}")` : ""}.
            </div>
          )}
        </>
      )}

      {setupPhase === "failed" && (
        <>
          <div style={{ display: "flex", justifyContent: "center", marginBottom: 14 }}>
            <XCircle size={34} color={C.red} strokeWidth={1.75} aria-hidden />
          </div>
          <div style={{ fontSize: 14.5, fontWeight: 600, color: C.red, marginBottom: 8 }}>
            {wired ? "Setup failed" : "Wi-Fi setup failed"}
          </div>
          <div style={{ fontSize: 13, color: C.textDim, marginBottom: 16, lineHeight: 1.5 }}>
            {setupErrorMsg || (wired
              ? "The robot couldn't reach the internet over its cable."
              : "Couldn't connect to the network you chose.")}
          </div>

          <div style={{
            textAlign: "left", background: C.surface,
            border: `1px solid ${C.border}`, borderRadius: 8,
            padding: "12px 14px", marginBottom: 18, fontSize: 13,
            color: C.textDim, lineHeight: 1.6,
          }}>
            <div style={{ fontWeight: 600, color: C.text, marginBottom: 6 }}>
              Things to check:
            </div>
            {wired ? (
              <>
                <div>• Make sure the ethernet cable is seated at both ends.</div>
                <div>• Check that the port on your router is live (link light on).</div>
                <div>• Or pick a Wi-Fi network instead and set the robot up that way.</div>
              </>
            ) : (
              <>
                <div>• Double-check the Wi-Fi password (it's case-sensitive).</div>
                <div>• Use a <strong style={{ color: C.text }}>2.4GHz</strong> Wi-Fi network — most devices can't join 5GHz.</div>
                <div>• Keep the robot close to your router during setup.</div>
              </>
            )}
          </div>

          <div style={{
            display: "flex", gap: 10, justifyContent: "center",
            alignItems: "center", flexWrap: "wrap",
          }}>
            <button
              type="button"
              className="lm-btn lm-btn-primary"
              onClick={onRetry}
              style={{ padding: "9px 18px" }}
            >
              {wired ? "Back to setup" : "Back to Wi-Fi"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
