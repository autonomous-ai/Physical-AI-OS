// move_library.js — 8-beat dance sequences per energy level (joint range [-90, 90]).

// Shorthand: create servo positions object from (yaw, pitch, elbow, wristPitch)
function pose(yaw, pitch, elbow = 0, wristPitch = -20) {
  return {
    'base_yaw.pos': yaw,
    'base_pitch.pos': pitch,
    'elbow_pitch.pos': elbow,
    'wrist_roll.pos': 0,
    'wrist_pitch.pos': wristPitch,
  };
}

const HOME = pose(3, -20, 32, 0);

const SEQUENCES = {
  high: [
    [
      { positions: pose(-60, -10, 40, 10),  duration: 200, led: [255, 0, 100] },
      { positions: pose(60, -30, 50, -30),  duration: 200, led: [100, 0, 255] },
      { positions: pose(-20, 10, 60, 20),   duration: 150, led: [255, 50, 0] },
      { positions: pose(30, -50, 20, -50),  duration: 150, led: [0, 200, 255] },
      { positions: pose(50, 0, 45, 10),     duration: 200, led: [255, 0, 100] },
      { positions: pose(-50, -20, 55, -20), duration: 200, led: [100, 0, 255] },
      { positions: pose(20, 10, 35, 15),    duration: 150, led: [255, 200, 0] },
      { positions: HOME,                    duration: 300, led: [255, 255, 255], emotion: 'happy' },
    ],
    [
      { positions: pose(10, 20, 60, 10),    duration: 150, led: [255, 0, 0] },
      { positions: pose(-10, -40, -20, -60), duration: 150, led: [0, 0, 255] },
      { positions: pose(15, 15, 50, 15),    duration: 150, led: [255, 0, 0] },
      { positions: pose(-15, -35, -10, -50), duration: 150, led: [0, 0, 255] },
      { positions: pose(-55, 0, 45, 10),    duration: 200, led: [255, 100, 0] },
      { positions: pose(55, -10, 50, -10),  duration: 200, led: [0, 255, 100] },
      { positions: pose(-40, 5, 40, 15),    duration: 200, led: [255, 100, 0] },
      { positions: HOME,                    duration: 250, led: [255, 255, 255], emotion: 'surprised' },
    ],
    [
      { positions: pose(50, -5, 55, 5),     duration: 180, led: [200, 0, 255] },
      { positions: pose(-60, -15, 30, -25), duration: 180, led: [0, 255, 200] },
      { positions: pose(15, 20, 65, 20),    duration: 150, led: [255, 255, 0] },
      { positions: pose(45, -20, 40, -15),  duration: 180, led: [200, 0, 255] },
      { positions: pose(-45, 5, 50, 10),    duration: 180, led: [0, 255, 200] },
      { positions: pose(0, -30, 10, -40),   duration: 200, led: [255, 0, 50] },
      { positions: pose(10, 15, 55, 15),    duration: 150, led: [255, 255, 0] },
      { positions: HOME,                    duration: 300, led: [255, 200, 255], emotion: 'happy' },
    ],
  ],

  medium: [
    [
      { positions: pose(-30, -10, 35, -5),  duration: 300, led: [180, 80, 0] },
      { positions: pose(10, -25, 25, -10),  duration: 300, led: [120, 60, 0] },
      { positions: pose(30, -10, 35, -5),   duration: 300, led: [180, 80, 0] },
      { positions: pose(-10, -25, 25, -10), duration: 300, led: [120, 60, 0] },
      { positions: pose(5, 5, 50, 10),      duration: 350, led: [200, 100, 0] },
      { positions: pose(-15, -15, 30, -5),  duration: 300, led: [140, 70, 0] },
      { positions: pose(15, -30, 20, -20),  duration: 350, led: [200, 100, 0] },
      { positions: HOME,                    duration: 400, led: [160, 80, 20] },
    ],
    [
      { positions: pose(5, 5, 50, 15),      duration: 350, led: [0, 150, 200] },
      { positions: pose(-30, -15, 35, -10), duration: 300, led: [0, 120, 180] },
      { positions: pose(10, -25, 20, -20),  duration: 350, led: [0, 150, 200] },
      { positions: pose(30, -10, 40, 0),    duration: 300, led: [0, 120, 180] },
      { positions: pose(-10, 5, 45, 10),    duration: 350, led: [50, 180, 220] },
      { positions: pose(20, -20, 30, -10),  duration: 300, led: [0, 100, 160] },
      { positions: pose(-25, 0, 40, 5),     duration: 300, led: [50, 180, 220] },
      { positions: HOME,                    duration: 400, led: [0, 140, 200], emotion: 'curious' },
    ],
    [
      { positions: pose(35, -10, 40, 0),    duration: 300, led: [150, 0, 200] },
      { positions: pose(-10, 5, 50, 10),    duration: 300, led: [120, 0, 160] },
      { positions: pose(-30, -15, 35, -10), duration: 300, led: [150, 0, 200] },
      { positions: pose(15, -25, 25, -15),  duration: 300, led: [120, 0, 160] },
      { positions: pose(25, 0, 45, 5),      duration: 300, led: [180, 50, 220] },
      { positions: pose(-20, -10, 30, -5),  duration: 350, led: [100, 0, 140] },
      { positions: pose(10, 5, 50, 10),     duration: 300, led: [180, 50, 220] },
      { positions: HOME,                    duration: 400, led: [140, 30, 180] },
    ],
  ],

  low: [
    [
      { positions: pose(-15, -15, 30, -5),  duration: 500, led: [0, 40, 80] },
      { positions: pose(5, -22, 28, -2),    duration: 500, led: [0, 30, 60] },
      { positions: pose(15, -15, 30, -5),   duration: 500, led: [0, 40, 80] },
      { positions: pose(-5, -22, 28, -2),   duration: 500, led: [0, 30, 60] },
      { positions: pose(-12, -12, 32, 0),   duration: 500, led: [0, 50, 90] },
      { positions: pose(8, -22, 28, -3),    duration: 500, led: [0, 30, 60] },
      { positions: pose(12, -12, 32, 0),    duration: 500, led: [0, 50, 90] },
      { positions: HOME,                    duration: 600, led: [0, 40, 80], emotion: 'sleepy' },
    ],
    [
      { positions: pose(5, -5, 45, 10),     duration: 600, led: [30, 0, 60] },
      { positions: pose(-5, -25, 25, -10),  duration: 500, led: [20, 0, 40] },
      { positions: pose(8, -30, 20, -15),   duration: 600, led: [30, 0, 60] },
      { positions: pose(-8, -10, 40, 5),    duration: 500, led: [20, 0, 40] },
      { positions: pose(5, 0, 48, 12),      duration: 600, led: [40, 0, 70] },
      { positions: pose(-5, -22, 28, -5),   duration: 500, led: [20, 0, 40] },
      { positions: pose(8, -28, 22, -12),   duration: 600, led: [40, 0, 70] },
      { positions: HOME,                    duration: 700, led: [30, 0, 50] },
    ],
  ],
};

const _seqIndex = { high: 0, medium: 0, low: 0 };

export function selectSequence(energyLevel) {
  const pool = SEQUENCES[energyLevel] || SEQUENCES.medium;
  const idx = _seqIndex[energyLevel] || 0;
  const seq = pool[idx % pool.length];
  _seqIndex[energyLevel] = (idx + 1) % pool.length;
  return seq;
}

export function getMoveAtBeat(sequence, beatIndex) {
  return sequence[beatIndex % sequence.length];
}

// Classify energy level from raw energy value (0-255 scale).
export function classifyEnergy(energy) {
  if (energy > 140) return 'high';
  if (energy > 80) return 'medium';
  return 'low';
}

// Blend LED color based on frequency bands (bass=red, mid=green, high=blue)
export function spectrumColor(bass, mid, high) {
  return [
    Math.floor(Math.min(bass * 1.6, 255)),
    Math.floor(Math.min(mid * 0.9, 255)),
    Math.floor(Math.min(high * 1.3, 255)),
  ];
}

export function flashColor(color, intensity) {
  const boost = 60 + intensity * 80;
  return color.map(c => Math.min(c + boost, 255));
}

export function dimColor(color, factor = 0.35) {
  return color.map(c => Math.floor(c * factor));
}
