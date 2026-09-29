import { useEffect, useState } from "react";

// Declared device capabilities from /api/system/info.
export function useCapabilities() {
  const [caps, setCaps] = useState<Set<string> | null>(null);
  useEffect(() => {
    fetch("/api/system/info")
      .then((r) => r.json())
      .then((r) => {
        if (r.status === 1 && r.data?.capabilities) {
          setCaps(new Set<string>(r.data.capabilities));
        }
      })
      .catch(() => {});
  }, []);
  // null caps (not yet loaded / none declared) → fail-open.
  const hasCap = (c: string): boolean => !caps || caps.has(c);
  return { caps, hasCap };
}
