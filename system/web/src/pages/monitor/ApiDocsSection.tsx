import { useEffect, useRef, useState } from "react";
import { ExternalLink, RefreshCw } from "lucide-react";
import "./api-docs.css";

const DOCS_URL = "/api/hardware/docs";

export function ApiDocsSection() {
  const observerRef = useRef<MutationObserver | null>(null);
  useEffect(() => () => observerRef.current?.disconnect(), []);
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  useEffect(() => {
    if (state !== "loading") return;
    const timeout = window.setTimeout(() => setState("error"), 15000);
    return () => window.clearTimeout(timeout);
  }, [attempt, state]);

  const retry = () => { observerRef.current?.disconnect(); setState("loading"); setAttempt((value) => value + 1); };
  return <section className="lm-api-docs">
    <header>
      <div><h2>Hardware API</h2><p>Endpoint reference and request schemas.</p></div>
      <div className="lm-api-docs-actions">
        <button type="button" onClick={retry}><RefreshCw size={15} aria-hidden="true" />Reload</button>
        <a href={DOCS_URL} target="_blank" rel="noopener noreferrer"><ExternalLink size={15} aria-hidden="true" />Open in new tab</a>
      </div>
    </header>
    <div className="lm-api-docs-frame">
      {state !== "ready" && <div className="lm-api-docs-message" role={state === "error" ? "alert" : "status"}>
        <strong>{state === "loading" ? "Loading API documentation…" : "API documentation could not be loaded"}</strong>
        {state === "error" && <p>Reload to try again, or open the documentation in a new tab.</p>}
      </div>}
      <iframe key={attempt} title="Hardware API documentation" src={DOCS_URL}
        style={{ visibility: state === "ready" ? "visible" : "hidden" }}
        onError={() => setState("error")}
        onLoad={(event) => {
          // The proxied page is same-origin. Reject plain API errors or failed
          // Swagger scripts instead of presenting an empty frame as ready.
          try {
            const doc = event.currentTarget.contentDocument;
            observerRef.current?.disconnect();
            if (doc?.querySelector(".swagger-ui")) {
              setState("ready");
            } else if (doc?.querySelector("#swagger-ui")) {
              // Swagger renders asynchronously after the frame's load event.
              observerRef.current = new MutationObserver(() => {
                if (doc.querySelector(".swagger-ui")) {
                  observerRef.current?.disconnect();
                  setState("ready");
                }
              });
              observerRef.current.observe(doc.body, { childList: true, subtree: true });
            } else {
              setState("error");
            }
          } catch {
            setState("error");
          }
        }} />
    </div>
  </section>;
}
