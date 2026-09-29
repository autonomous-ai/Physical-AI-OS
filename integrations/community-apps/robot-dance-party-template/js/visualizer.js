// visualizer.js — 2D canvas frequency bars, waveform and beat flash.

export class Visualizer {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this._resize();

    this._smoothBars = null;
    this._beatFlash = 0;

    this._resizeHandler = () => this._resize();
    window.addEventListener('resize', this._resizeHandler);
  }

  destroy() {
    window.removeEventListener('resize', this._resizeHandler);
  }

  _resize() {
    const rect = this.canvas.parentElement.getBoundingClientRect();
    this.canvas.width = rect.width * window.devicePixelRatio;
    this.canvas.height = rect.height * window.devicePixelRatio;
    this.ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
    this.w = rect.width;
    this.h = rect.height;
  }

  draw(freqData, timeData, analysis) {
    const { w, h, ctx } = this;
    const { bass, mid, high, energy, isBeat } = analysis;

    if (isBeat) this._beatFlash = 1.0;
    else this._beatFlash *= 0.9;

    ctx.fillStyle = `rgba(10, 10, 15, 0.3)`;
    ctx.fillRect(0, 0, w, h);

    if (this._beatFlash > 0.05) {
      ctx.fillStyle = `rgba(108, 92, 231, ${this._beatFlash * 0.12})`;
      ctx.fillRect(0, 0, w, h);
    }

    if (freqData) {
      this._drawBars(freqData);
    }

    if (timeData) {
      this._drawWaveform(timeData);
    }

    this._drawEnergyCircle(energy, isBeat);
  }

  _drawBars(freqData) {
    const { w, h, ctx } = this;
    const barCount = Math.min(freqData.length, 96);

    if (!this._smoothBars || this._smoothBars.length !== barCount) {
      this._smoothBars = new Float32Array(barCount);
    }

    const barW = (w / barCount) * 0.8;
    const gap = (w / barCount) * 0.2;

    for (let i = 0; i < barCount; i++) {
      const raw = freqData[i] / 255;

      if (raw > this._smoothBars[i]) {
        this._smoothBars[i] = raw;
      } else {
        this._smoothBars[i] *= 0.92; // Decay
      }

      const val = this._smoothBars[i];
      const barH = val * h * 0.75;
      const x = i * (barW + gap);

      const ratio = i / barCount;
      const r = ratio < 0.1 ? 160 : ratio < 0.4 ? 60 : Math.floor(180 * ratio);
      const g = ratio < 0.1 ? 50 : ratio < 0.4 ? Math.floor(160 + 95 * ratio) : 230;
      const b = ratio < 0.1 ? 255 : ratio < 0.4 ? 255 : Math.floor(80 * (1 - ratio));

      const alpha = 0.3 + val * 0.7;
      ctx.fillStyle = `rgba(${r},${g},${b},${alpha})`;

      const halfH = barH / 2;
      const centerY = h / 2;
      ctx.fillRect(x, centerY - halfH, barW, halfH);
      ctx.fillRect(x, centerY, barW, halfH * 0.6);
    }
  }

  _drawWaveform(timeData) {
    const { w, h, ctx } = this;
    ctx.beginPath();
    ctx.strokeStyle = `rgba(108, 92, 231, ${0.3 + this._beatFlash * 0.4})`;
    ctx.lineWidth = 1.5;

    const sliceW = w / timeData.length;
    for (let i = 0; i < timeData.length; i++) {
      const v = timeData[i] / 128.0;
      const y = (v * h) / 2;
      if (i === 0) ctx.moveTo(0, y);
      else ctx.lineTo(i * sliceW, y);
    }
    ctx.stroke();
  }

  _drawEnergyCircle(energy, isBeat) {
    const { w, h, ctx } = this;
    const cx = w / 2;
    const cy = h / 2;
    const norm = Math.min(energy / 200, 1);
    const radius = 20 + norm * 40 + (isBeat ? 15 : 0);

    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(108, 92, 231, ${0.05 + norm * 0.1})`;
    ctx.fill();
    ctx.strokeStyle = `rgba(108, 92, 231, ${0.2 + norm * 0.3 + this._beatFlash * 0.3})`;
    ctx.lineWidth = 2;
    ctx.stroke();
  }
}
