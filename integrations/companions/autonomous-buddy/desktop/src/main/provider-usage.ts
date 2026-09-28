import { spawn, execFile } from 'node:child_process'
import { open } from 'node:fs/promises'
import { join } from 'node:path'
import { createHash } from 'node:crypto'
import { promisify } from 'node:util'
import type { ProviderUsage } from '../shared/provider-usage'
export type { ProviderUsage } from '../shared/provider-usage'
import { tmpdir, homedir, userInfo } from 'node:os'

type UsageWindow = ProviderUsage['windows'][number]
type Probe = () => Promise<ProviderUsage>
const object = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const unavailable = (provider: ProviderUsage['provider'], message: string): ProviderUsage =>
  ({ provider, state: 'unavailable', message, windows: [], updatedAt: Date.now() })

function windowValue(label: string, used: unknown, reset: unknown): UsageWindow | undefined {
  if (typeof used !== 'number' || !Number.isFinite(used) || used < 0 || used > 100) return undefined
  const result: UsageWindow = { label, usedPercent: used }
  // Both official provider contracts report Unix seconds; the renderer uses ms.
  if (typeof reset === 'number' && Number.isFinite(reset) && reset > 0 && reset < 1e12)
    result.resetsAt = Math.round(reset * 1000)
  return result
}

// Official contract: https://learn.chatgpt.com/docs/app-server#rate-limits-chatgpt
export function normalizeCodexUsage(payload: unknown): ProviderUsage {
  const result = object(payload)
  const buckets = object(result.rateLimitsByLimitId)
  const snapshot = object(buckets.codex ?? result.rateLimits)
  const windows: UsageWindow[] = []
  for (const key of ['primary', 'secondary']) {
    const value = object(snapshot[key])
    const minutes = value.windowDurationMins
    const label = typeof minutes === 'number' && Number.isFinite(minutes) && minutes > 0
      ? minutes % 1440 === 0 ? `${minutes / 1440}d` : minutes % 60 === 0 ? `${minutes / 60}h` : `${minutes}m`
      : key === 'primary' ? 'Primary' : 'Secondary'
    const window = windowValue(label, value.usedPercent, value.resetsAt)
    if (window) windows.push(window)
  }
  return windows.length ? { provider: 'codex', state: 'ready', windows, updatedAt: Date.now() }
    : unavailable('codex', 'Codex did not return subscription limits for this account.')
}

// Official status-line input, not API token accounting or an inferred quota:
// https://code.claude.com/docs/en/statusline#rate-limit-usage
export function normalizeClaudeUsage(payload: unknown): ProviderUsage {
  const limits = object(object(payload).rate_limits)
  const windows: UsageWindow[] = []
  for (const [key, label] of [['five_hour', '5h'], ['seven_day', '7d']]) {
    const value = object(limits[key])
    const window = windowValue(label, value.used_percentage, value.resets_at)
    if (window) windows.push(window)
  }
  return windows.length ? { provider: 'claude', state: 'ready', windows, updatedAt: Date.now() }
    : unavailable('claude', 'Claude quota is unavailable here. Check /usage in Claude Code.')
}

export function readCodexUsage(options: { executable?: string; args?: string[]; timeoutMs?: number; signal?: AbortSignal } = {}): Promise<ProviderUsage> {
  return new Promise((resolve) => {
    if (options.signal?.aborted) { resolve(unavailable('codex', 'Quota request cancelled.')); return }
    const child = spawn(options.executable ?? 'codex', options.args ?? ['app-server'], {
      cwd: tmpdir(), env: process.env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
    })
    let buffer = ''
    let bytes = 0
    let settled = false
    const finish = (usage: ProviderUsage) => {
      if (settled) return
      settled = true
      clearTimeout(timeout)
      options.signal?.removeEventListener('abort', abort)
      child.stdin.end()
      child.kill('SIGTERM')
      const force = setTimeout(() => child.kill('SIGKILL'), 500)
      force.unref()
      child.once('close', () => clearTimeout(force))
      resolve(usage)
    }
    const abort = () => finish(unavailable('codex', 'Quota request cancelled.'))
    options.signal?.addEventListener('abort', abort, { once: true })
    const timeout = setTimeout(() => finish(unavailable('codex', 'Codex quota request timed out.')), options.timeoutMs ?? 10000)
    child.on('error', () => finish(unavailable('codex', 'Codex CLI could not be started.')))
    child.on('close', () => finish(unavailable('codex', 'Codex quota service exited without a result.')))
    child.stdin.on('error', () => finish(unavailable('codex', 'Codex quota service closed its input.')))
    child.stderr.on('data', () => { /* Never expose account or CLI diagnostics to the renderer. */ })
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (chunk: string) => {
      if (settled) return
      bytes += Buffer.byteLength(chunk)
      if (bytes > 1024 * 1024) { finish(unavailable('codex', 'Codex quota response exceeded the size limit.')); return }
      buffer += chunk
      let newline: number
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline)
        buffer = buffer.slice(newline + 1)
        let message: Record<string, unknown>
        try { message = object(JSON.parse(line)) } catch { continue }
        if (message.id !== 1 && message.id !== 2) continue
        if (message.error) {
          finish(unavailable('codex', 'Codex could not read subscription limits. Check CLI login and account support.'))
          return
        }
        if (message.id === 1) {
          child.stdin.write(JSON.stringify({ method: 'initialized', params: {} }) + '\n')
          child.stdin.write(JSON.stringify({ id: 2, method: 'account/rateLimits/read', params: {} }) + '\n')
        } else {
          finish(normalizeCodexUsage(message.result))
          return
        }
      }
    })
    child.stdin.write(JSON.stringify({ id: 1, method: 'initialize', params: {
      clientInfo: { name: 'autonomous_buddy', title: 'Autonomous Buddy', version: '0.1.0' },
    } }) + '\n')
  })
}

// Compatibility adapter for the same fixed Anthropic OAuth endpoint used by
// Claude Code and Orca. It is not a public stable API; failures stay unavailable.
async function claudeAccessToken(signal?: AbortSignal): Promise<string | undefined> {
  const configDir = process.env.CLAUDE_CONFIG_DIR
  let raw: string | undefined
  if (process.platform === 'darwin') {
    const suffix = configDir ? '-' + createHash('sha256').update(configDir.normalize('NFC')).digest('hex').slice(0, 8) : ''
    try {
      const result = await promisify(execFile)('/usr/bin/security', ['find-generic-password', '-s', 'Claude Code-credentials' + suffix, '-a', userInfo().username, '-w'], { encoding: 'utf8', timeout: 3000, maxBuffer: 65536, signal })
      raw = result.stdout
    } catch {
      // Never approve Keychain prompts, recover tokens or modify credentials.
      return undefined
    }
  } else {
    let file
    try {
      file = await open(join(configDir || join(homedir(), '.claude'), '.credentials.json'), 'r')
      if ((await file.stat()).size > 65536) return undefined
      raw = await file.readFile({ encoding: 'utf8', signal })
    } catch { return undefined }
    finally { await file?.close() }
  }
  try {
    const token = object(object(JSON.parse(raw ?? '')).claudeAiOauth).accessToken
    return typeof token === 'string' && token.length > 0 && token.length < 16384 ? token : undefined
  } catch { return undefined }
}

export async function readClaudeUsage(options: {
  signal?: AbortSignal
  token?: (signal?: AbortSignal) => Promise<string | undefined>
  fetcher?: typeof fetch
} = {}): Promise<ProviderUsage> {
  const signal = options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(10000)]) : AbortSignal.timeout(10000)
  try {
    const token = await (options.token ?? claudeAccessToken)(signal)
    if (!token) return unavailable('claude', 'Claude login credentials are unavailable or Keychain access was denied.')
    const response = await (options.fetcher ?? fetch)('https://api.anthropic.com/api/oauth/usage', {
      method: 'GET', redirect: 'error', signal,
      headers: { Authorization: `Bearer ${token}`, 'anthropic-beta': 'oauth-2025-04-20' },
    })
    if (!response.ok) {
      await response.body?.cancel()
      return unavailable('claude', response.status === 401 || response.status === 403
        ? 'Claude login cannot read quota. Sign in with Claude Code.'
        : 'Claude quota endpoint is temporarily unavailable.')
    }
    const reader = response.body?.getReader()
    if (!reader) return normalizeClaudeUsage(null)
    const chunks: Uint8Array[] = []
    let size = 0
    try {
      while (true) {
        const next = await reader.read()
        if (next.done) break
        size += next.value.byteLength
        if (size > 65536) { await reader.cancel(); return unavailable('claude', 'Claude quota response exceeded the size limit.') }
        chunks.push(next.value)
      }
    } finally { reader.releaseLock() }
    const payload = object(JSON.parse(Buffer.concat(chunks).toString('utf8')))
    return normalizeClaudeOAuthUsage(payload)
  } catch { return unavailable('claude', 'Claude quota could not be read. Check CLI login or try again later.') }
}

// Optional scoped fields from Orca's compatibility adapter; absent windows are never synthesized.
export function normalizeClaudeOAuthUsage(input: unknown): ProviderUsage {
  const payload = object(input)
  const mapWindow = (label: string, raw: unknown): UsageWindow | undefined => {
    const value = object(raw)
    const reset = typeof value.resets_at === 'string' ? Date.parse(value.resets_at) / 1000 : value.resets_at
    return windowValue(label, value.utilization ?? value.used_percentage, reset)
  }
  const windows: UsageWindow[] = []
  for (const [key, label] of [['five_hour', '5h'], ['seven_day', '7d']]) {
    const window = mapWindow(label, payload[key])
    if (window) windows.push(window)
  }
  const scoped = new Map<string, UsageWindow>()
  // Provider-declared weekly model windows take precedence over legacy aliases.
  for (const raw of Array.isArray(payload.limits) ? payload.limits.slice(0, 32) : []) {
    const value = object(raw)
    const model = object(object(value.scope).model).display_name
    if (value.kind !== 'weekly_scoped' || typeof model !== 'string' || !/^[A-Za-z0-9 ._-]{1,40}$/.test(model.trim())) continue
    const window = mapWindow(`${model.trim()} 7d`, { used_percentage: value.percent, resets_at: value.resets_at })
    if (window && !scoped.has(model.trim().toLowerCase())) scoped.set(model.trim().toLowerCase(), window)
  }
  if (!scoped.has('fable')) {
    for (const key of ['fable_weekly', 'fable_seven_day', 'seven_day_fable']) {
      const window = mapWindow('Fable 7d', payload[key])
      if (window) { scoped.set('fable', window); break }
    }
  }
  windows.push(...scoped.values())
  return windows.length ? { provider: 'claude', state: 'ready', windows, updatedAt: Date.now() }
    : unavailable('claude', 'Claude did not return subscription limits for this account.')
}

export class ProviderUsageService {
  private cached?: ProviderUsage[]
  private cachedAt = 0
  private pending?: Promise<ProviderUsage[]>
  private abort = new AbortController()
  constructor(private probes?: { codex: Probe; claude: Probe }, private now: () => number = Date.now) {}
  dispose() { this.abort.abort() }

  read(force = false): Promise<ProviderUsage[]> {
    if (this.pending) return this.pending
    if (!force && this.cached && this.now() - this.cachedAt < 60000) return Promise.resolve(this.cached)
    this.pending = Promise.all((['claude', 'codex'] as const).map(async (provider) => {
      try { return this.probes ? await this.probes[provider]() : await (provider === 'codex' ? readCodexUsage({ signal: this.abort.signal }) : readClaudeUsage({ signal: this.abort.signal })) }
      catch { return unavailable(provider, 'Provider quota could not be read.') }
    })).then((usage) => {
      this.cached = usage
      this.cachedAt = this.now()
      return usage
    }).finally(() => { this.pending = undefined })
    return this.pending
  }
}
