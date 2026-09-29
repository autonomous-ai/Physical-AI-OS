// app.js — Main orchestrator: wires modules to the UI.

import { RobotConnection } from './connection.js';
import { AudioEngine } from './audio_engine.js';
import { DanceEngine } from './dance_engine.js';
import { SafetyThrottle } from './safety.js';
import { Visualizer } from './visualizer.js';

const robot = new RobotConnection();
const audio = new AudioEngine();
const safety = new SafetyThrottle();
let dance = null;
let visualizer = null;

const $ = id => document.getElementById(id);

$('btn-connect').addEventListener('click', doConnect);
$('inp-pass').addEventListener('keydown', e => { if (e.key === 'Enter') doConnect(); });

async function doConnect() {
  const host = $('inp-host').value.trim();
  const errEl = $('connect-error');
  const btn = $('btn-connect');

  if (!host) { showError(errEl, 'Enter robot IP or hostname'); return; }

  btn.disabled = true;
  btn.textContent = 'Connecting...';
  errEl.style.display = 'none';

  try {
    const pass = $('inp-pass').value;
    await robot.connect(host, pass);
    $('screen-connect').style.display = 'none';
    $('screen-dance').style.display = 'block';
    $('lbl-host').textContent = host;

    visualizer = new Visualizer($('visualizer'));

    toast('Connected to ' + host);
  } catch (e) {
    showError(errEl, e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Connect';
  }
}

$('btn-disconnect').addEventListener('click', () => {
  if (dance) dance.stop();
  audio.stop();
  robot.disconnect();
  if (visualizer) { visualizer.destroy(); visualizer = null; }
  $('screen-dance').style.display = 'none';
  $('screen-connect').style.display = 'flex';
  $('now-playing').classList.remove('active');
});

robot.addEventListener('connection-lost', () => {
  $('status-dot').classList.add('offline');
  toast('Connection lost', true);
});
robot.addEventListener('reconnected', () => {
  $('status-dot').classList.remove('offline');
  toast('Reconnected');
});

document.querySelectorAll('.source-tab').forEach((tab, i) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.source-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.source-panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.querySelectorAll('.source-panel')[i].classList.add('active');
  });
});

const dropZone = $('drop-zone');
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('dragover');
  if (e.dataTransfer.files.length) loadAudioFile(e.dataTransfer.files[0]);
});

$('file-input').addEventListener('change', e => {
  if (e.target.files.length) loadAudioFile(e.target.files[0]);
});

async function loadAudioFile(file) {
  if (!file || !file.type.startsWith('audio/')) {
    toast('Not an audio file', true);
    return;
  }
  stopDance();
  await audio.loadFile(file);
  $('track-name').textContent = file.name;
  $('now-playing').classList.add('active');
  $('btn-play').innerHTML = '&#10074;&#10074;';
  startDance();
  toast('Playing: ' + file.name);
}

$('btn-yt-play').addEventListener('click', loadYouTube);
$('inp-yt-url').addEventListener('keydown', e => { if (e.key === 'Enter') loadYouTube(); });

function extractYouTubeId(input) {
  input = input.trim();
  try {
    const url = new URL(input);
    const h = url.hostname.replace('www.', '');
    if (h === 'youtube.com' || h === 'music.youtube.com') {
      if (url.pathname === '/watch') return url.searchParams.get('v');
      if (url.pathname.startsWith('/embed/')) return url.pathname.split('/')[2];
      if (url.pathname.startsWith('/shorts/')) return url.pathname.split('/')[2];
    }
    if (h === 'youtu.be') return url.pathname.slice(1).split('/')[0];
  } catch (_) {}
  if (/^[a-zA-Z0-9_-]{11}$/.test(input)) return input;
  return null;
}

async function loadYouTube() {
  const url = $('inp-yt-url').value.trim();
  if (!url) { toast('Paste a YouTube URL', true); return; }

  const btn = $('btn-yt-play');
  btn.disabled = true;
  btn.textContent = 'Downloading...';
  $('yt-hint').textContent = 'Downloading audio via yt-dlp...';

  try {
    const res = await fetch('/api/yt/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Download failed');

    stopDance();
    await audio.loadUrl(data.audio_url, data.title);
    $('track-name').textContent = data.title;
    $('now-playing').classList.add('active');
    $('btn-play').innerHTML = '&#10074;&#10074;';
    startDance();
    $('yt-hint').textContent = 'Playing: ' + data.title;
    toast('Playing: ' + data.title);
  } catch (e) {
    $('yt-hint').textContent = e.message;
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Play';
  }
}

$('btn-mic').addEventListener('click', async () => {
  stopDance();
  try {
    await audio.loadMic();
    $('track-name').textContent = 'Microphone';
    $('now-playing').classList.add('active');
    $('btn-mic').textContent = 'Listening...';
    $('btn-mic').disabled = true;
    startDance();
    toast('Microphone active');
  } catch (e) {
    toast('Microphone access denied', true);
  }
});

$('btn-play').addEventListener('click', () => {
  if (audio.isMicMode) return;
  if (audio.isPlaying) {
    audio.pause();
    $('btn-play').innerHTML = '&#9654;';
  } else {
    audio.resume();
    $('btn-play').innerHTML = '&#10074;&#10074;';
  }
});

$('btn-stop').addEventListener('click', stopDance);

audio.addEventListener('ended', () => {
  stopDance();
  toast('Track ended');
});

audio.addEventListener('duration', (e) => {
});

function startDance() {
  if (dance) dance.stop();
  dance = new DanceEngine(audio, robot, safety);

  dance.ledEnabled = $('sw-led').classList.contains('on');
  dance.servoEnabled = $('sw-servo').classList.contains('on');
  dance.emotionEnabled = $('sw-emotion').classList.contains('on');

  dance.addEventListener('tick', (e) => {
    const { bass, mid, high, energy, isBeat, bpm, timestamp } = e.detail;

    if (visualizer && audio.analyser) {
      visualizer.draw(audio.freqData, audio.timeData, { bass, mid, high, energy, isBeat });
    }

    $('bpm-val').textContent = bpm || '--';

    if (isBeat) {
      $('beat-dot').classList.add('beat');
      setTimeout(() => $('beat-dot').classList.remove('beat'), 150);
    }

    if (!audio.isMicMode && audio.duration) {
      $('track-time').textContent = fmt(audio.currentTime) + ' / ' + fmt(audio.duration);
    } else {
      $('track-time').textContent = '';
    }
  });

  dance.addEventListener('command', (e) => {
    const { type, yaw, pitch, duration, emotion, intensity } = e.detail;
    if (type === 'servo') addLog('servo', `Nudge yaw:${yaw} pitch:${pitch} (${duration}ms)`);
    if (type === 'emotion') addLog('emotion', `Emotion: ${emotion}`);
  });

  dance.addEventListener('beat', (e) => {
    addLog('beat', `Beat #${e.detail.beat} [${e.detail.energy}]`);
  });

  dance.addEventListener('energy-change', (e) => {
    addLog('energy', `Energy: ${e.detail.level}`);
  });

  dance.start();
}

function stopDance() {
  if (dance) { dance.stop(); dance = null; }
  audio.stop();
  $('btn-play').innerHTML = '&#9654;';
  $('now-playing').classList.remove('active');
  $('btn-mic').textContent = 'Start Listening';
  $('btn-mic').disabled = false;
  $('bpm-val').textContent = '--';
  const hint = $('yt-hint');
  if (hint) hint.textContent = 'Downloads audio via yt-dlp, plays locally.';
}

document.querySelectorAll('.switch').forEach(sw => {
  sw.addEventListener('click', () => {
    sw.classList.toggle('on');
    if (dance) {
      dance.ledEnabled = $('sw-led').classList.contains('on');
      dance.servoEnabled = $('sw-servo').classList.contains('on');
      dance.emotionEnabled = $('sw-emotion').classList.contains('on');
    }
  });
});

$('sl-led-rate').addEventListener('input', e => {
  $('val-led-rate').textContent = e.target.value;
  safety.setRate('led', parseInt(e.target.value));
});

$('sl-servo-rate').addEventListener('input', e => {
  $('val-servo-rate').textContent = e.target.value;
  safety.setRate('servo', parseInt(e.target.value));
});

$('sl-sensitivity').addEventListener('input', e => {
  $('val-sensitivity').textContent = e.target.value;
  // Map slider [20..90] → threshold [0.20..0.02] (higher slider = lower threshold = more beats)
  const v = parseInt(e.target.value);
  audio.sensitivity = 0.20 - (v - 20) * (0.18 / 70);
});

const MAX_LOG = 60;
function addLog(type, msg) {
  const log = $('robot-log');
  const line = document.createElement('div');
  line.className = 'log-' + type;
  const ts = new Date().toLocaleTimeString('en', {
    hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
  line.textContent = ts + '  ' + msg;
  log.prepend(line);
  while (log.children.length > MAX_LOG) log.removeChild(log.lastChild);
}

function fmt(s) {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return m + ':' + (sec < 10 ? '0' : '') + sec;
}

function toast(msg, isError = false) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast show' + (isError ? ' error' : '');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.className = 'toast'; }, 2500);
}

function showError(el, msg) {
  el.textContent = msg;
  el.style.display = 'block';
}

(async () => {
  if (await robot.tryReconnect()) {
    const host = robot.osBase.replace('http://', '');
    $('screen-connect').style.display = 'none';
    $('screen-dance').style.display = 'block';
    $('lbl-host').textContent = host;
    $('inp-host').value = host;
    visualizer = new Visualizer($('visualizer'));
    toast('Reconnected to ' + host);
  }
})();
