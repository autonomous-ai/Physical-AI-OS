// safety.js — Per-channel throttle + dedup for HTTP robot commands.

export class SafetyThrottle {
  constructor() {
      // Minimum intervals between commands (ms), tuned for HTTP overhead.
    this.limits = {
      led:     80,   // ~12/s max (LED updates are lightweight)
      servo:   250,  // ~4/s max (servo needs time to reach position)
      emotion: 3000, // 1 per 3s (emotions are multi-second animations)
    };

    this._lastSend = {
      led: 0,
      servo: 0,
      emotion: 0,
    };

    this._lastValue = {
      led: null,
      servo: null,
      emotion: null,
    };

    this.stats = {
      sent: { led: 0, servo: 0, emotion: 0 },
      dropped: { led: 0, servo: 0, emotion: 0 },
    };
  }

  setRate(channel, hz) {
    this.limits[channel] = Math.floor(1000 / Math.max(hz, 1));
  }

  canSend(channel, timestamp) {
    const elapsed = timestamp - (this._lastSend[channel] || 0);
    return elapsed >= (this.limits[channel] || 0);
  }

  markSent(channel, timestamp, value = null) {
    this._lastSend[channel] = timestamp;
    this._lastValue[channel] = value;
    this.stats.sent[channel]++;
  }

  markDropped(channel) {
    this.stats.dropped[channel]++;
  }

  isDuplicate(channel, value) {
    const last = this._lastValue[channel];
    if (!last || !value) return false;
    if (typeof value === 'string') return value === last;
    if (Array.isArray(value) && Array.isArray(last)) {
        // LED colors: skip tiny deltas to avoid flicker.
      if (value.length === 3 && last.length === 3) {
        const delta = Math.abs(value[0] - last[0])
          + Math.abs(value[1] - last[1])
          + Math.abs(value[2] - last[2]);
        return delta < 15; // Skip if RGB difference < 15 total
      }
    }
    return JSON.stringify(value) === JSON.stringify(last);
  }

    // Check throttle + dedup; returns whether to send.
  shouldSend(channel, timestamp, value = null) {
    if (!this.canSend(channel, timestamp)) {
      this.markDropped(channel);
      return false;
    }
    if (value && this.isDuplicate(channel, value)) {
      this.markDropped(channel);
      return false;
    }
    return true;
  }

  resetStats() {
    for (const ch of Object.keys(this.stats.sent)) {
      this.stats.sent[ch] = 0;
      this.stats.dropped[ch] = 0;
    }
  }
}
