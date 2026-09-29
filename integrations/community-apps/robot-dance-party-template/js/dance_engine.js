// dance_engine.js — Maps audio analysis to robot LED/servo/emotion commands.

import { selectSequence, getMoveAtBeat, classifyEnergy, spectrumColor, flashColor, dimColor } from './move_library.js';

export class DanceEngine extends EventTarget {
  constructor(audioEngine, connection, safety) {
    super();
    this.audio = audioEngine;
    this.robot = connection;
    this.safety = safety;

    this._currentSequence = null;
    this._currentEnergy = 'low';
    this._beatCount = 0;        // Global beat counter
    this._blockBeat = 0;        // Beat within current 8-beat block
    this._lastBlockEnergy = '';  // Detect energy level changes

    this.ledEnabled = true;
    this.servoEnabled = true;
    this.emotionEnabled = false;

    this._frameId = null;
    this._running = false;

    this._tick = this._tick.bind(this);
  }

  start() {
    if (this._running) return;
    this._running = true;
    this._beatCount = 0;
    this._blockBeat = 0;
    this._currentSequence = selectSequence('medium');
    this._frameId = requestAnimationFrame(this._tick);
    this._emit('started');
  }

  stop() {
    this._running = false;
    if (this._frameId) {
      cancelAnimationFrame(this._frameId);
      this._frameId = null;
    }
    this.robot.ledEffectStop().catch(() => {});
    this._emit('stopped');
  }

    // Runs at display framerate (~60Hz); robot commands are throttled by safety.

  _tick(timestamp) {
    if (!this._running) return;
    this._frameId = requestAnimationFrame(this._tick);

    const analysis = this.audio.analyze(timestamp);
    const { bass, mid, high, energy, isBeat, bpm } = analysis;

    const energyLevel = classifyEnergy(energy);

    if (energyLevel !== this._lastBlockEnergy) {
      this._currentSequence = selectSequence(energyLevel);
      this._blockBeat = 0;
      this._lastBlockEnergy = energyLevel;
      this._emit('energy-change', { level: energyLevel });
    }

    if (isBeat) {
      this._beatCount++;
      this._blockBeat = this._beatCount % 8;

        // Every 8 beats: rotate to next sequence.
      if (this._blockBeat === 0) {
        this._currentSequence = selectSequence(energyLevel);
      }

      const move = getMoveAtBeat(this._currentSequence, this._blockBeat);
      this._executeMove(move, analysis, timestamp);
      this._emit('beat', { beat: this._beatCount, move, bpm, energy: energyLevel });
    }

    if (this.ledEnabled) {
      this._sendAmbientLed(bass, mid, high, energy, isBeat, timestamp);
    }

    this._emit('tick', { bass, mid, high, energy, isBeat, bpm, timestamp });
  }

  _executeMove(move, analysis, timestamp) {
    if (this.servoEnabled && move.positions) {
      const moveKey = JSON.stringify(move.positions);
      if (this.safety.shouldSend('servo', timestamp, moveKey)) {
        this.robot.servoMove(move.positions, move.duration).catch(() => {});
        this.safety.markSent('servo', timestamp, moveKey);
        const yaw = move.positions['base_yaw.pos'];
        const pitch = move.positions['base_pitch.pos'];
        this._emit('command', { type: 'servo', yaw, pitch, duration: move.duration });
      }
    }

    if (this.emotionEnabled && move.emotion) {
      if (this.safety.shouldSend('emotion', timestamp, move.emotion)) {
        const intensity = Math.min(analysis.energy / 180, 1.0);
        this.robot.emotion(move.emotion, intensity).catch(() => {});
        this.safety.markSent('emotion', timestamp, move.emotion);
        this._emit('command', { type: 'emotion', emotion: move.emotion, intensity });
      }
    }
  }

    // Continuous LED: spectrum color between beats, flash on beats.
  _sendAmbientLed(bass, mid, high, energy, isBeat, timestamp) {
    let color;
    if (isBeat && energy > 80) {
      const base = spectrumColor(bass, mid, high);
      color = flashColor(base, Math.min(energy / 200, 1));
    } else {
      const base = spectrumColor(bass, mid, high);
      color = dimColor(base, 0.3 + (energy / 255) * 0.3);
    }

    if (this.safety.shouldSend('led', timestamp, color)) {
      this.robot.ledSolid(color, true).catch(() => {});
      this.safety.markSent('led', timestamp, color);
    }
  }

  _emit(type, detail = {}) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }
}
