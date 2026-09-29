import { useCallback, useState } from "react";
import { hwUrl } from "@/lib/api";

export interface FaceOwner {
  label: string;
  photo_count: number;
  photos: string[];
  voice_samples?: string[];
}

// Enrolled-owners list for Setup's continue-mode Voice/Face steps.
export function useFaceEnroll() {
  const [faceOwners, setFaceOwners] = useState<FaceOwner[]>([]);

  const loadFaceOwners = useCallback(async () => {
    try {
      const r = await fetch(hwUrl("/face/owners")).then((x) => x.json());
      if (Array.isArray(r?.persons)) setFaceOwners(r.persons);
    } catch { /* hardware unreachable in initial mode; silent */ }
  }, []);

  return { faceOwners, loadFaceOwners };
}
