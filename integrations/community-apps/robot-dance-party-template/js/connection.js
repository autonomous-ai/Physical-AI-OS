// connection.js — Robot control via the os-server /api/hardware/* proxy to HAL (admin auth).

export class RobotConnection extends EventTarget {
  constructor() {
    super();
    this.base = '';      // http://<host>:5000/api/hardware
    this.osBase = '';    // http://<host>:5000
    this.token = '';     // Bearer token for auth
    this.connected = false;
    this._healthInterval = null;
    this._loadSession();
  }

  async connect(host, password) {
    // No port — nginx fronts os-server on :80
    this.osBase = `http://${host}`;
    this.base = `${this.osBase}/api/hardware`;

      // Session token, since the cookie won't work cross-origin.
    if (password) {
      const loginRes = await fetch(`${this.osBase}/api/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });
      if (!loginRes.ok) throw new Error('Wrong password');
      const loginData = await loginRes.json();
      this.token = loginData?.data?.token || '';
      this._saveSession(host);
    }

    try {
      const res = await fetch(`${this.base}/health`, {
        signal: AbortSignal.timeout(5000),
        headers: this._authHeaders(),
      });
      if (!res.ok) throw new Error(`HAL ${res.status}`);
      const data = await res.json();
      this.connected = true;
      this._startHealthPoll();
      this._emit('connected', { host, health: data });
      return data;
    } catch (e) {
      this.connected = false;
      throw new Error(`Cannot reach robot at ${host}`);
    }
  }

  disconnect() {
    this.connected = false;
    if (this._healthInterval) {
      clearInterval(this._healthInterval);
      this._healthInterval = null;
    }
    this._clearSession();
    this._emit('disconnected');
  }

  async tryReconnect() {
    if (!this.token || !this.osBase) return false;
    this.base = `${this.osBase}/api/hardware`;
    try {
      const res = await fetch(`${this.base}/health`, {
        signal: AbortSignal.timeout(5000),
        headers: this._authHeaders(),
      });
      if (!res.ok) return false;
      const data = await res.json();
      this.connected = true;
      this._startHealthPoll();
      const host = this.osBase.replace('http://', '');
      this._emit('connected', { host, health: data });
      return true;
    } catch {
      this._clearSession();
      return false;
    }
  }

  async ledSolid(color, transient = true) {
    return this._post('/led/solid', { color, transient });
  }

  async ledEffect(effect, color, speed = 1.0) {
    return this._post('/led/effect', { effect, color, speed, transient: true });
  }

  async ledOff() {
    return this._post('/led/off', {});
  }

  async ledEffectStop() {
    return this._post('/led/effect/stop', {});
  }

  async servoAim(direction, durationMs = 400) {
    // HAL expects duration in seconds (0.0–10.0)
    return this._post('/servo/aim', { direction, duration: durationMs / 1000 });
  }

  async servoMove(positions, durationMs = 400) {
    // HAL expects duration in seconds (0.0–10.0)
    return this._post('/servo/move', { positions, duration: durationMs / 1000 });
  }

  async emotion(name, intensity = 1.0) {
    return this._post('/emotion', { emotion: name, intensity });
  }

  async speak(text) {
    return this._post('/voice/speak', { text });
  }

  async setVolume(volume) {
    return this._post('/audio/volume', { volume });
  }

  async getHealth() {
    return this._get('/health');
  }

  async _post(path, body) {
    if (!this.connected) return null;
    try {
      const res = await fetch(this.base + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...this._authHeaders() },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`${res.status}`);
      return res.json();
    } catch (e) {
      this._emit('error', { path, error: e.message });
      return null;
    }
  }

  async _get(path) {
    if (!this.connected) return null;
    try {
      const res = await fetch(this.base + path, {
        headers: this._authHeaders(),
      });
      if (!res.ok) throw new Error(`${res.status}`);
      return res.json();
    } catch (e) {
      this._emit('error', { path, error: e.message });
      return null;
    }
  }

  _startHealthPoll() {
    if (this._healthInterval) clearInterval(this._healthInterval);
    this._healthInterval = setInterval(async () => {
      try {
        await fetch(`${this.base}/health`, {
          signal: AbortSignal.timeout(3000),
          headers: this._authHeaders(),
        });
        if (!this.connected) {
          this.connected = true;
          this._emit('reconnected');
        }
      } catch {
        if (this.connected) {
          this.connected = false;
          this._emit('connection-lost');
        }
      }
    }, 5000);
  }

  _authHeaders() {
    return this.token ? { Authorization: `Bearer ${this.token}` } : {};
  }

  _saveSession(host) {
    try {
      localStorage.setItem('rdp_session', JSON.stringify({ host, token: this.token }));
    } catch (_) {}
  }

  _loadSession() {
    try {
      const s = JSON.parse(localStorage.getItem('rdp_session') || '{}');
      if (s.token) {
        this.token = s.token;
        this.osBase = `http://${s.host}`;
      }
    } catch (_) {}
  }

  _clearSession() {
    this.token = '';
    try { localStorage.removeItem('rdp_session'); } catch (_) {}
  }

  _emit(type, detail = {}) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }
}
