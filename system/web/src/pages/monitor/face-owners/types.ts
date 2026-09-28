export interface CooldownEntry {
  person_id: string;
  kind: string;
  last_seen_ago: number;
  cooldown_remaining: number;
  cooldown_total: number;
}
export interface CooldownState {
  owners: CooldownEntry[];
  strangers: CooldownEntry[];
  owners_forget_s: number;
  strangers_forget_s: number;
}

export interface StrangerSample {
  filename: string;
  size_bytes: number;
  mtime: number;
}
export interface StrangerCluster {
  hash: string;
  sample_count: number;
  latest_mtime: number;
  samples: StrangerSample[];
}
export interface StrangersData {
  total: number;
  clusters: StrangerCluster[];
}

export interface FaceStrangerStat {
  stranger_id: string;
  count: number;
  first_seen: string;
  last_seen: string;
}

// Mirrors the device's _FAMILIAR_VISIT_THRESHOLD.
export const FAMILIAR_VISIT_THRESHOLD = 2;
