export interface PoseSide {
  score?: number;
  risk_level?: number;
  body_scores?: {
    upper_arm?: number; upper_arm_angle?: number;
    lower_arm?: number; lower_arm_angle?: number;
    wrist?: number;
    neck?: number;     neck_angle?: number;
    trunk?: number;    trunk_angle?: number;
  };
  skipped_joints?: string[];
}

export interface PoseSample {
  ts: number;
  score: number;
  risk_level: number;
  left?: PoseSide;
  right?: PoseSide;
}

export interface PoseSummary {
  bad_ratio: number;
  samples: number;
  bad_samples: number;
  window_min: number;
  region_frequency: Record<string, number>;
  dominant_region: string;
  dominant_count: number;
  latest_score: number;
  latest_risk_level: number;
}

export interface Perception {
  type: string;
  connected?: boolean;
  last_raw_actions?: string[];
  last_user?: string | null;
  last_sent_emotion?: string | null;
  last_sent_user?: string | null;
  last_detected_emotion?: string | null;
  buffered_snapshots?: number;
  buffered_emotions?: number;
  motion_detected?: boolean;
  emotion_detected?: boolean;
  seconds_since_motion?: number | null;
  seconds_since_detection?: number | null;
  face_present?: boolean;
  faces_count?: number;
  visible?: string[];
  last_person?: string | null;
  last_seen_seconds_ago?: number | null;
  enrolled_count?: number;
  stranger_count?: number;
  level?: number;
  seconds_since_check?: number | null;
  occurrence_count?: number;
  echo_suppression?: boolean;
  ergo_score?: number | null;
  ergo_risk_level?: number | null;
  seconds_since_sample?: number | null;
  samples_in_buffer?: number;
  window_age_s?: number;
  window_duration_s?: number;
  window_min_samples?: number;
  window_complete?: boolean;
  sample_interval_s?: number;
  bad_ratio_threshold?: number;
  summary?: PoseSummary | null;
  running?: PoseSummary | null;
  samples?: PoseSample[];
}

export interface SensingData {
  running: boolean;
  poll_interval: number;
  last_event_seconds_ago: Record<string, number>;
  perceptions: Perception[];
  presence: {
    state: string;
    enabled: boolean;
    seconds_since_motion: number;
    idle_timeout: number;
    away_timeout: number;
  };
}
