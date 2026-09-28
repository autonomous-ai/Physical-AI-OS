import { _electron, expect } from '@playwright/test'
import electronPath from 'electron'
import { WebSocketServer, WebSocket } from 'ws'
import { once } from 'node:events'
import { createServer } from 'node:http'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { mkdtemp, mkdir, writeFile, readFile, rm, chmod, realpath } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// This runs the compiled production app, real Git and a real PTY. Only the model
// executable is replaced, so no account, credential, or paid model is used.
const exec = promisify(execFile)
const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const temporary = await realpath(await mkdtemp(path.join(tmpdir(), 'buddy-electron-')))
const projectPath = path.join(temporary, 'buddy-demo')
const bin = path.join(temporary, 'bin')
const profile = path.join(temporary, 'profile')
const log = path.join(temporary, 'provider-calls.jsonl')
const artifacts = path.join(desktop, 'artifacts')
let application
let page
let lamp
let voiceApi
let lampSocket
let connectionCount = 0
let commandSequence = 0
const notices = []
const pendingCommands = new Map()
const lampErrors = []
async function startLamp() {
  lamp = new WebSocketServer({ host: '127.0.0.1', port: 0 })
  lamp.on('connection', (socket, request) => {
    if (request.headers.authorization !== 'Bearer test-token') {
      lampErrors.push('Missing test pairing bearer token')
      socket.close()
      return
    }
    lampSocket = socket
    connectionCount++
    socket.on('message', (data) => {
      const message = JSON.parse(data.toString())
      if (message.type === 'agent_event') notices.push(message)
      else {
        const pending = pendingCommands.get(message.id)
        if (pending) {
          pendingCommands.delete(message.id)
          clearTimeout(pending.timer)
          pending.resolve(message)
        }
      }
    })
  })
  await once(lamp, 'listening')
}
async function lampCommand(action, params = {}) {
  await expect.poll(() => lampSocket?.readyState, { timeout: 15000 }).toBe(WebSocket.OPEN)
  const id = `smoke-command-${++commandSequence}`
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pendingCommands.delete(id)
      reject(new Error(`Lamp command timed out: ${action}`))
    }, 15000)
    pendingCommands.set(id, { resolve, timer })
    lampSocket.send(JSON.stringify({ id, action: `agent.${action}`, params, timeout_ms: 10000 }))
  })
}
const pageErrors = []
const processErrors = []
const git = (args) => exec('git', args, { cwd: projectPath })
const fixtureProvider = `#!${process.execPath}
import fs from 'node:fs';
const args = process.argv.slice(2);
let prompt = '';
for await (const chunk of process.stdin) prompt += chunk;
const resumed = args.includes('resume');
const sessionId = resumed ? args[args.indexOf('--json') + 1] : 'buddy-fixture-thread-001';
fs.appendFileSync(process.env.BUDDY_SMOKE_LOG, JSON.stringify({ args, prompt, sessionId }) + '\\n');
const emit = event => process.stdout.write(JSON.stringify(event) + '\\n');
emit({ type: 'thread.started', thread_id: sessionId });
emit({ type: 'item.completed', item: { type: 'agent_message', text: 'Inspecting local project files…' } });
await new Promise(resolve => setTimeout(resolve, 1000));
emit({ type: 'item.completed', item: { type: 'agent_message', text: resumed
  ? 'Continuing the same session.\\n\\nThe session store keeps project, worktree and provider conversation IDs together. Follow-up messages resume this conversation without creating a new thread.\\n\\nValidation\\n  ✓ Session history survives an app restart\\n  ✓ Concurrent sessions keep their output separate\\n  ✓ Terminal output streams through a local PTY\\n\\nNext: connect voice routing to the selected session.'
  : 'I reviewed the workspace and the current changes.\\n\\nImplementation plan\\n  1. Keep projects and worktrees in the left navigation.\\n  2. Give each agent a persistent session with streaming output.\\n  3. Put file changes and Git history beside the conversation.\\n\\nThe native computer-use helper remains independent.' } });
emit({ type: 'turn.completed', usage: { input_tokens: 10, output_tokens: 20 } });
`
async function launchApp() {
  application = await _electron.launch({
    executablePath: process.env.BUDDY_APP_EXECUTABLE || electronPath,
    args: process.env.BUDDY_APP_EXECUTABLE ? [] : ['.'],
    cwd: desktop,
    env: {
      ...process.env,
      PATH: `${bin}${path.delimiter}${process.env.PATH ?? ''}`,
      SHELL: '/bin/sh',
      BUDDY_DATA_DIR: profile,
      BUDDY_SMOKE_LOG: log,
      BUDDY_NATIVE_TEST_MODE: '1',
      BUDDY_TEST_DEVICE_URL: `ws://127.0.0.1:${lamp.address().port}/api/buddy/ws`,
    },
    timeout: 30000,
  })
  application.process().stderr?.on('data', (data) => processErrors.push(String(data)))
  page = await application.firstWindow()
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.waitForFunction(() => !!window.buddy)
  await expect(page.locator('.app-shell')).toBeVisible()
  await expect.poll(async () => (await page.evaluate(() => window.buddy.nativeStatus())).available).toBe(true)
}
async function nativeScreenshot(filename) {
  const data = await application.evaluate(async ({ BrowserWindow }) =>
    (await BrowserWindow.getAllWindows()[0].capturePage()).toPNG().toString('base64'))
  await writeFile(path.join(artifacts, filename), Buffer.from(data, 'base64'))
}
async function helperPids() {
  const { stdout } = await exec('/bin/ps', ['-axo', 'pid=,ppid=,comm='])
  const parent = application.process().pid
  return stdout.split('\n').flatMap((line) => {
    const match = line.trim().match(/^(\d+)\s+(\d+)\s+(.+)$/)
    return match && Number(match[2]) === parent && match[3].endsWith('/AutonomousBuddy')
      ? [Number(match[1])]
      : []
  })
}
function alive(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}
const detail = (id) => page.evaluate((id) => window.buddy.session(id), id)
const snapshot = () => page.evaluate(() => window.buddy.snapshot())
async function createUISession(provider, name) {
  if (provider === 'Codex') {
    const registered = (await snapshot()).projects[0]
    const session = await page.evaluate(({project, name}) => window.buddy.createSession({
      projectId: project.id, worktreePath: project.path, provider: 'codex', mode: 'structured', title: name,
    }), {project: registered, name})
    await page.locator('.session-row').filter({hasText: name}).click()
    return session
  }
  await page.locator('.new-tab').click()
  await page
    .locator('.provider-option')
    .filter({ has: page.locator('strong', { hasText: provider }) })
    .click()
  await page.locator('#session-title').fill(name)
  await page.getByRole('button', { name: 'Create session', exact: true }).click()
  await expect(page.locator('.modal')).toHaveCount(0)
  await expect(page.locator('.session-pane-heading')).toContainText(name)
  return (await snapshot()).sessions.find((session) => session.title === name)
}
async function sendUI(prompt) {
  await page.getByRole('textbox', { name: 'Message agent' }).fill(prompt)
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
}
try {
  await Promise.all([mkdir(projectPath), mkdir(bin), mkdir(profile), mkdir(artifacts, { recursive: true })])
  await mkdir(path.join(projectPath, 'src'))
  await writeFile(
    path.join(projectPath, 'README.md'),
    '# Buddy agent workspace\n\nA local home for your projects and agents.\n',
  )
  await writeFile(
    path.join(projectPath, 'src', 'session-store.ts'),
    'export const sessions = new Map<string, unknown>()\n',
  )
  await writeFile(path.join(projectPath, 'package.json'), '{"name":"buddy-demo","private":true}\n')
  await git(['init', '-b', 'main'])
  await git(['config', 'user.name', 'Buddy Smoke Test'])
  await git(['config', 'user.email', 'buddy-test@example.invalid'])
  await git(['add', 'README.md', 'src/session-store.ts', 'package.json'])
  await git(['commit', '-m', 'Add the agent workspace foundation'])
  await git(['update-ref', 'refs/remotes/origin/main', 'HEAD'])
  await writeFile(
    path.join(projectPath, 'README.md'),
    '# Buddy agent workspace\n\nA local home for your projects and agents.\n\nSessions preserve context across follow-up messages.\n',
  )
  await writeFile(
    path.join(projectPath, 'src', 'session-store.ts'),
    'export interface Session { id: string; projectId: string; providerSessionId?: string }\nexport const sessions = new Map<string, Session>()\n',
  )
  await writeFile(
    path.join(projectPath, 'notes.md'),
    'Connect voice commands to the active project and session.\n',
  )
  // .mjs avoids inheriting package type from a host directory.
  await writeFile(path.join(bin, 'codex.mjs'), fixtureProvider)
  await writeFile(
    path.join(bin, 'codex'),
    `#!/bin/sh\nexec '${process.execPath.replaceAll("'", "'\\''")}' '${path.join(bin, 'codex.mjs').replaceAll("'", "'\\''")}' "$@"\n`,
  )
  await chmod(path.join(bin, 'codex'), 0o755)
  await startLamp()
  await launchApp()
  await application.evaluate(({ dialog }, selected) => {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [selected] })
  }, projectPath)
  await page.getByRole('button', { name: 'Add project', exact: true }).click()
  await expect(page.locator('.project-heading')).toContainText('buddy-demo')
  await expect(page.locator('.tree-select')).toContainText('main')
  const project = (await snapshot()).projects[0]
  expect(project.path).toBe(projectPath)
  await page.getByRole('button', { name: 'New worktree in buddy-demo', exact: true }).click()
  await page.locator('#branch-name').fill('feat/session-routing')
  await page.locator('.agent-picker summary').click()
  await expect(page.locator('.agent-picker-menu .agent-provider-icon')).toHaveCount(3)
  await nativeScreenshot('worktree-agent-picker.png')
  await page.keyboard.press('Escape')
  await expect(page.locator('.agent-picker')).not.toHaveAttribute('open', '')
  await expect(page.getByRole('heading', { name: 'Create a worktree' })).toBeVisible()
  await page.locator('.agent-picker summary').press('Enter')
  await page.getByRole('group', { name: 'Choose agent' }).getByRole('button', { name: 'Terminal', exact: true }).click()
  await expect(page.locator('.agent-picker summary .agent-provider-icon.terminal')).toBeVisible()
  await page.getByRole('button', { name: 'Create worktree', exact: true }).click()
  await expect.poll(async () => (await snapshot()).sessions.some((session) => session.worktreePath !== projectPath && session.provider === 'terminal' && session.status === 'running')).toBe(true)
  await expect(page.locator('.tree-select')).toHaveCount(2)
  await page.locator('.tree-select').filter({ hasText: 'main' }).click()
  const workspaceMenu = () =>
    page.getByRole('button', { name: 'Workspace actions for main', exact: true }).click()
  await workspaceMenu()
  await expect(page.getByRole('menu', { name: 'Workspace actions' })).toBeVisible()
  await expect(page.getByRole('menuitem', { name: 'Delete worktree', exact: true })).toBeDisabled()
  await page.getByRole('menuitem', { name: 'Pin', exact: true }).click()
  await expect
    .poll(async () => (await snapshot()).workspaces.find((item) => item.worktreePath === projectPath)?.pinned)
    .toBe(true)
  await workspaceMenu()
  await page.getByRole('menuitemradio', { name: 'In review', exact: true }).click()
  await expect
    .poll(async () => (await snapshot()).workspaces.find((item) => item.worktreePath === projectPath)?.status)
    .toBe('review')
  await workspaceMenu()
  await page.screenshot({ path: path.join(artifacts, 'workspace-context-menu.png') })
  await page.keyboard.press('Escape')
  const terminal = await createUISession('Terminal', 'Workspace terminal')
  await page.evaluate((id) => window.buddy.terminalWrite(id, "printf 'BUDDY_%s\\n' 'PTY_OK'\r"), terminal.id)
  await expect
    .poll(async () =>
      (await detail(terminal.id)).events
        .filter((event) => event.type === 'terminal')
        .map((event) => event.text)
        .join(''),
    )
    .toContain('BUDDY_PTY_OK')
  expect((await detail(terminal.id)).session.status).toBe('running')
  await expect(page.locator('.xterm')).toBeVisible()
  await page.getByRole('button', { name: 'Close tab Workspace terminal', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Close tab Workspace terminal', exact: true })).toHaveCount(0)
  expect((await detail(terminal.id)).session).toMatchObject({ status: 'stopped', closed: true })
  await expect(page.locator('.session-row').filter({ hasText: 'Workspace terminal' })).toHaveCount(0)
  await page.evaluate((id) => window.buddy.restartInteractive(id), terminal.id)
  await page.locator('.session-row').filter({ hasText: 'Workspace terminal' }).click()
  await expect(page.locator('.xterm')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Close tab Workspace terminal', exact: true })).toBeVisible()
  const agent = await createUISession('Codex', 'Build session orchestration')
  await sendUI('Review the workspace and propose the session manager foundation.')
  await expect(page.locator('.session-status')).toContainText('Working')
  await expect(page.locator('.transcript')).toContainText('Inspecting local project files…')
  await expect.poll(async () => (await detail(agent.id)).session.status).toBe('completed')
  await expect(page.locator('.transcript')).toContainText('Implementation plan')
  await sendUI('Continue with session persistence and explain the validation.')
  await expect
    .poll(async () => (await detail(agent.id)).events.filter((event) => event.type === 'prompt').length)
    .toBe(2)
  await expect.poll(async () => (await detail(agent.id)).session.status).toBe('completed')
  await expect(page.locator('.transcript')).toContainText('Continuing the same session.')
  const calls = (await readFile(log, 'utf8'))
    .trim()
    .split('\n')
    .map((line) => JSON.parse(line))
  expect(calls).toHaveLength(2)
  expect(calls[1].sessionId).toBe(calls[0].sessionId)
  expect(calls[1].args).toContain('resume')
  // A real local WebSocket impersonates only the lamp. Commands cross the
  // packaged Swift connection and private process relay before reaching Manager.
  const listed = await lampCommand('list')
  expect(listed.ok).toBe(true)
  expect(listed.result.projects).toContainEqual(project)
  const createParams = {
    project_id: project.id,
    provider: 'codex',
    mode: 'structured',
    title: 'Lamp voice session',
    request_id: 'smoke-create-voice-001',
  }
  const created = await lampCommand('create', createParams)
  expect(created.ok).toBe(true)
  const voice = created.result
  expect((await lampCommand('create', createParams)).result.id).toBe(voice.id)
  const firstVoice = {
    project_id: project.id,
    session_id: voice.id,
    request_id: 'smoke-send-voice-001',
    prompt: 'Review this project from the lamp voice command.',
  }
  expect(await lampCommand('send', firstVoice)).toMatchObject({
    ok: true,
    result: { accepted: true, session_id: voice.id },
  })
  expect(await lampCommand('send', firstVoice)).toMatchObject({
    ok: true,
    result: { accepted: true, session_id: voice.id },
  })
  await expect
    .poll(() => notices.some((event) => event.session_id === voice.id && event.status === 'completed'))
    .toBe(true)
  const firstNotice = notices.find((event) => event.session_id === voice.id && event.status === 'completed')
  expect(firstNotice.project_id).toBe(project.id)
  expect(firstNotice.seq).toBeGreaterThan(0)
  const firstDetail = await lampCommand('session', { project_id: project.id, session_id: voice.id })
  expect(firstDetail.ok).toBe(true)
  expect(firstDetail.result.events.filter((event) => event.type === 'prompt')).toHaveLength(1)
  expect(
    (
      await lampCommand('session', {
        project_id: project.id,
        session_id: voice.id,
        after_seq: firstDetail.result.next_seq,
      })
    ).result.events,
  ).toEqual([])
  const beforeReconnect = connectionCount
  lampSocket.close(1000, 'smoke reconnect')
  await expect.poll(() => connectionCount, { timeout: 15000 }).toBeGreaterThan(beforeReconnect)
  expect(
    (await lampCommand('session', { project_id: project.id, session_id: voice.id })).result.session
      .providerSessionId,
  ).toBe(firstDetail.result.session.providerSessionId)
  const secondVoice = {
    ...firstVoice,
    request_id: 'smoke-send-voice-002',
    prompt: 'Continue the same lamp session and explain persistence.',
  }
  expect((await lampCommand('send', secondVoice)).ok).toBe(true)
  await expect
    .poll(() =>
      notices.some(
        (event) =>
          event.session_id === voice.id && event.status === 'completed' && event.seq > firstNotice.seq,
      ),
    )
    .toBe(true)
  const secondDetail = await lampCommand('session', {
    project_id: project.id,
    session_id: voice.id,
    after_seq: firstDetail.result.next_seq,
  })
  expect(secondDetail.result.session.providerSessionId).toBe(firstDetail.result.session.providerSessionId)
  expect(secondDetail.result.events.filter((event) => event.type === 'prompt')).toHaveLength(1)
  const voiceCalls = (await readFile(log, 'utf8'))
    .trim()
    .split('\n')
    .map((line) => JSON.parse(line))
  expect(voiceCalls).toHaveLength(4)
  expect(voiceCalls.slice(2).map((call) => call.prompt)).toEqual([firstVoice.prompt, secondVoice.prompt])
  expect(voiceCalls[3].args).toContain('resume')
  expect(voiceCalls[3].sessionId).toBe(voiceCalls[2].sessionId)
  await page.locator('.session-row').filter({ hasText: 'Workspace terminal' }).click()
  await expect(page.locator('.xterm')).toBeVisible()
  expect((await detail(terminal.id)).session.status).toBe('running')
  await page.locator('.session-row').filter({ hasText: 'Build session orchestration' }).click()
  await expect(page.locator('.transcript-block.prompt')).toHaveCount(2)
  await expect(page.locator('.changed-file')).toHaveCount(3)
  await expect(page.locator('.git-tracked-changes .changed-file')).toHaveCount(2)
  await expect(page.locator('.git-untracked-changes .changed-file')).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Untracked files', exact: true })).toBeVisible()
  const pane = (id) => page.locator(`.session-pane[data-session-id="${id}"]`)
  const draft = 'Keep this unsent research prompt while arranging the workspace.'
  await pane(agent.id).getByRole('textbox', { name: 'Message agent' }).fill(draft)
  const beforeSplit = new Set((await snapshot()).sessions.map((session) => session.id))
  await pane(agent.id)
    .getByRole('button', { name: `Split right ${agent.title}`, exact: true })
    .click()
  await expect(page.locator('.session-pane')).toHaveCount(2)
  const splitOne = (await snapshot()).sessions.find((session) => !beforeSplit.has(session.id))
  expect(splitOne).toMatchObject({
    projectId: project.id,
    worktreePath: projectPath,
    provider: 'terminal',
    status: 'running',
  })
  await expect(pane(agent.id).getByRole('textbox', { name: 'Message agent' })).toHaveValue(draft)
  const beforeNested = new Set((await snapshot()).sessions.map((session) => session.id))
  await pane(splitOne.id)
    .getByRole('button', { name: `Split down ${splitOne.title}`, exact: true })
    .click()
  await expect(page.locator('.session-pane')).toHaveCount(3)
  const splitTwo = (await snapshot()).sessions.find((session) => !beforeNested.has(session.id))
  expect(splitTwo).toMatchObject({
    projectId: project.id,
    worktreePath: projectPath,
    provider: 'terminal',
    status: 'running',
  })
  const terminalText = async (id) =>
    (await detail(id)).events
      .filter((event) => event.type === 'terminal')
      .map((event) => event.text)
      .join('')
  const typeInPane = async (id, word) => {
    await pane(id).locator('.xterm-helper-textarea').focus()
    await expect
      .poll(() =>
        page.evaluate(() =>
          document.activeElement?.closest('.session-pane')?.getAttribute('data-session-id'),
        ),
      )
      .toBe(id)
    await page.keyboard.type(`printf 'SPLIT_%s\\n' '${word}'`)
    await page.keyboard.press('Enter')
    await expect.poll(() => terminalText(id)).toContain(`SPLIT_${word}`)
  }
  await typeInPane(splitOne.id, 'ONE_ONLY')
  await typeInPane(splitTwo.id, 'TWO_ONLY')
  await expect.poll(async () => (await lampCommand('list')).result.activeContext?.sessionId).toBe(splitTwo.id)
  expect(await terminalText(splitOne.id)).not.toContain('SPLIT_TWO_ONLY')
  expect(await terminalText(splitTwo.id)).not.toContain('SPLIT_ONE_ONLY')
  expect(await terminalText(terminal.id)).not.toContain('SPLIT_ONE_ONLY')
  await application.evaluate(({ Menu }) => {
    const item = Menu.getApplicationMenu().items.flatMap((entry) => entry.submenu?.items ?? [])
      .find((entry) => entry.label === 'Settings…')
    if (!item || item.accelerator !== 'CmdOrCtrl+,') throw new Error('Missing native Settings menu')
    item.click()
  })
  const settingsDialog = page.getByRole('dialog', { name: 'Settings', exact: true })
  await expect(settingsDialog).toBeVisible()
  const theme = settingsDialog.getByRole('group', { name: 'Theme' })
  await theme.getByRole('button', { name: 'Light', exact: true }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await settingsDialog.getByRole('combobox', { name: 'IDE font', exact: true }).selectOption('mono')
  const fontSize = settingsDialog.getByRole('spinbutton', { name: 'Terminal font size', exact: true })
  await fontSize.fill('16')
  await fontSize.press('Tab')
  await expect.poll(async () => (await page.evaluate(() => window.buddy.settings())).appearance.terminalFontSize).toBe(16)
  await settingsDialog.getByRole('combobox', { name: 'Terminal font family', exact: true }).selectOption('Menlo')
  const increaseZoom = settingsDialog.getByRole('button', { name: 'Increase UI zoom', exact: true })
  await increaseZoom.focus()
  await increaseZoom.press('Space')
  await expect(increaseZoom).toBeEnabled()
  await expect(increaseZoom).toBeFocused()
  await expect.poll(() => application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.getZoomFactor())).toBe(1.05)
  await page.screenshot({ path: path.join(artifacts, 'settings-appearance-light.png') })
  await theme.getByRole('button', { name: 'Dark', exact: true }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await settingsDialog.getByRole('button', { name: 'Advanced terminal settings', exact: true }).click()
  await settingsDialog.getByRole('switch', { name: 'Cursor blink', exact: true }).click()
  await expect(settingsDialog.getByRole('switch', { name: 'Cursor blink', exact: true })).toHaveAttribute('aria-checked', 'false')
  for (const label of ['Show agent usage', 'Show workspace status']) {
    const toggle = settingsDialog.getByRole('switch', { name: label, exact: true })
    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-checked', 'false')
    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-checked', 'true')
  }
  await expect(settingsDialog.getByRole('switch', { name: 'Show workspace status', exact: true })).toBeEnabled()
  await settingsDialog.locator('.settings-content').evaluate((element) => { element.scrollTop = 0 })
  await page.screenshot({ path: path.join(artifacts, 'settings-appearance-dark.png') })
  await settingsDialog.getByRole('textbox', { name: 'Search settings' }).fill('cursor')
  await expect(settingsDialog.getByRole('switch', { name: 'Cursor blink' })).toBeVisible()
  await expect(settingsDialog.getByRole('group', { name: 'Theme' })).toHaveCount(0)
  await settingsDialog.getByRole('button', { name: /Back to app/ }).click()
  await expect(settingsDialog).toHaveCount(0)
  await expect(pane(splitTwo.id).locator('.xterm-helper-textarea')).toBeFocused()
  expect((await detail(splitOne.id)).session.status).toBe('running')
  await typeInPane(splitOne.id, 'AFTER_APPEARANCE')
  expect(await terminalText(splitOne.id)).toContain('SPLIT_ONE_ONLY')
  const savedAppearance = (await page.evaluate(() => window.buddy.settings())).appearance
  const divider = page.getByRole('separator', { name: 'Resize columns', exact: true })
  await divider.focus()
  await page.keyboard.press('ArrowRight')
  await expect(divider).toHaveAttribute('aria-valuenow', '55')
  await page.screenshot({ path: path.join(artifacts, 'split-session-workspace.png') })
  await pane(splitTwo.id)
    .getByRole('button', { name: `Close pane ${splitTwo.title}`, exact: true })
    .click()
  await expect(page.locator('.session-pane')).toHaveCount(2)
  expect((await detail(splitTwo.id)).session).toMatchObject({ status: 'stopped', closed: true })
  await expect(page.locator('.session-row').filter({ hasText: splitTwo.title })).toHaveCount(0)
  const savedLayout = await page.evaluate(
    (id) => JSON.parse(localStorage.getItem(`buddy.panes.${id}`)),
    agent.id,
  )
  expect(savedLayout).toMatchObject({
    kind: 'split',
    direction: 'horizontal',
    ratio: 0.55,
    first: { sessionId: agent.id },
    second: { sessionId: splitOne.id },
  })
  await page.locator('.session-row').filter({ hasText: terminal.title }).click()
  await expect(pane(terminal.id)).toBeVisible()
  expect((await detail(terminal.id)).session.status).toBe('running')
  await page.locator('.session-row').filter({ hasText: agent.title }).click()
  await expect(page.locator('.session-pane')).toHaveCount(2)
  await expect(pane(agent.id).getByRole('textbox', { name: 'Message agent' })).toHaveValue(draft)
  await expect(page.getByRole('separator', { name: 'Resize columns', exact: true })).toHaveAttribute(
    'aria-valuenow',
    '55',
  )
  await page.locator('.session-row').filter({ hasText: terminal.title }).click()
  await page.locator('.session-row').filter({ hasText: agent.title }).click()
  await expect(page.locator('.session-pane')).toHaveCount(2)
  expect(
    await page.evaluate((id) => JSON.parse(localStorage.getItem(`buddy.panes.${id}`)), agent.id),
  ).toEqual(savedLayout)
  await page.locator('.changed-file').filter({ hasText: 'README.md' }).click()
  await expect(page.locator('.file-preview')).toContainText('+Sessions preserve context')
  await page.getByRole('button', { name: 'Close file preview', exact: true }).click()
  await page.locator('.right-tabs').getByRole('button', { name: 'Files', exact: true }).click()
  await page.locator('.file-entry').filter({ hasText: 'src' }).click()
  await page.locator('.file-entry').filter({ hasText: 'session-store.ts' }).click()
  await expect(page.locator('.file-preview')).toContainText('providerSessionId')
  await page.getByRole('button', { name: 'Close file preview', exact: true }).click()
  await page
    .locator('.right-tabs')
    .getByRole('button', { name: /^Changes/ })
    .click()
  await page.getByRole('button', { name: /RECENT COMMITS/ }).click()
  await expect(page.locator('.commit-list')).toContainText('Add the agent workspace foundation')
  await page.getByRole('button', { name: 'Stage all changes', exact: true }).click()
  await expect.poll(async () => (await git(['diff', '--cached', '--name-only'])).stdout.trim().split('\n').sort()).toEqual(['README.md', 'notes.md', 'src/session-store.ts'])
  await page.getByRole('button', { name: 'Unstage all changes', exact: true }).click()
  await expect.poll(async () => (await git(['diff', '--cached', '--name-only'])).stdout.trim()).toBe('')
  const bounds = await page.locator('.git-panel').evaluate((element) => ({right: element.getBoundingClientRect().right, viewport: window.innerWidth}))
  expect(bounds.right, JSON.stringify(bounds)).toBeLessThanOrEqual(bounds.viewport + 1)
  await nativeScreenshot('git-sidebar-dark.png')
  await page.getByRole('button', { name: 'Stage README.md', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Unstage README.md', exact: true })).toBeVisible()
  expect((await git(['diff', '--cached', '--name-only'])).stdout.trim()).toBe('README.md')
  await page.getByRole('button', { name: 'Unstage README.md', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Stage README.md', exact: true })).toBeVisible()
  expect((await git(['diff', '--cached', '--name-only'])).stdout.trim()).toBe('')
  await page.getByRole('button', { name: 'Stage README.md', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Unstage README.md', exact: true })).toBeVisible()
  await page.getByRole('textbox', { name: 'Commit message', exact: true }).fill('Document session continuity')
  await page.getByRole('button', { name: /^Commit staged changes/ }).click()
  await expect(page.locator('.commit-list')).toContainText('Document session continuity')
  await expect(page.locator('.changes-section:not(.git-branch-changes) .changed-file')).toHaveCount(2)
  await expect(page.locator('.git-branch-changes .changed-file')).toHaveCount(1)
  await expect(page.locator('.git-branch-changes')).toContainText('README.md')
  await expect(page.locator('.git-branch-changes .git-stage-actions')).toHaveCount(0)
  await page.getByRole('button', { name: 'Committed on branch', exact: true }).click()
  await expect(page.locator('.git-branch-changes .changed-file')).toHaveCount(0)
  await page.getByRole('button', { name: 'Committed on branch', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Review branch file README.md', exact: true })).toBeVisible()
  await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))))
  await expect(page.locator('.error-toast')).toHaveCount(0)
  await nativeScreenshot('git-committed-on-branch.png')
  await page.getByRole('button', { name: 'Review branch file README.md', exact: true }).click()
  await expect(page.locator('.file-preview')).toContainText('+Sessions preserve context')
  await page.getByRole('button', { name: 'Close file preview', exact: true }).click()
  const committedHash = (await git(['rev-parse', 'HEAD'])).stdout.trim()
  expect((await git(['show', '--pretty=format:', '--name-only', committedHash])).stdout.trim()).toBe(
    'README.md',
  )
  expect((await git(['diff', '--cached', '--name-only'])).stdout.trim()).toBe('')
  expect((await git(['status', '--porcelain'])).stdout).toContain(' M src/session-store.ts')
  expect((await git(['status', '--porcelain'])).stdout).toContain('?? notes.md')
  await page.getByRole('button', { name: `Review commit ${committedHash.slice(0, 7)}`, exact: true }).click()
  await expect(page.locator('.git-commit-files')).toContainText('README.md')
  await page.locator('.git-commit-files').getByRole('button').filter({ hasText: 'README.md' }).click()
  await expect(page.locator('.file-preview')).toContainText('+Sessions preserve context')
  await page.getByRole('button', { name: 'Close file preview', exact: true }).click()
  await Promise.all(Array.from({ length: 35 }, (_, index) =>
    writeFile(path.join(projectPath, 'src', `review-file-${String(index).padStart(2, '0')}.ts`), 'export const changed = true\n')))
  await page.getByRole('button', { name: 'Refresh files and Git', exact: true }).click()
  await expect(page.locator('.changes-section:not(.git-branch-changes) .changed-file')).toHaveCount(37)
  await expect.poll(() => page.locator('.git-review-scroll').evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true)
  await page.locator('.changed-file').last().scrollIntoViewIfNeeded()
  await expect(page.locator('.changed-file').last()).toBeVisible()
  await page.locator('.git-review-scroll').evaluate((element) => { element.scrollTop = 0 })
  await page.evaluate(() => window.buddy.updateAppearance({ uiFont: 'system', theme: 'dark' }))
  await nativeScreenshot('git-sidebar-dark.png')
  await page.evaluate(() => window.buddy.updateAppearance({ theme: 'light' }))
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await nativeScreenshot('git-sidebar-light.png')
  await page.evaluate((appearance) => window.buddy.updateAppearance(appearance), savedAppearance)
  await page.screenshot({ path: path.join(artifacts, 'manager-workspace.png') })
  await page.getByRole('button', { name: 'Computer & device', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'Computer & device' })).toContainText('Ready')
  expect(await page.evaluate(() => window.buddy.computerCommand('ping'))).toMatchObject({
    ok: true,
    result: { pong: true },
  })
  await page.evaluate(() => window.buddy.nativeAction('pause', { paused: true }))
  await expect(page.locator('.native-state')).toContainText('Paused')
  expect((await page.evaluate(() => window.buddy.computerCommand('ping'))).ok).toBe(false)
  await page.evaluate(() => window.buddy.nativeAction('pause', { paused: false }))
  await expect(page.locator('.native-state')).toContainText('Ready')
  await page.screenshot({ path: path.join(artifacts, 'unified-buddy-computer.png') })
  await page.getByRole('button', { name: 'Close dialog', exact: true }).click()
  const nativePids = await helperPids()
  expect(nativePids).toHaveLength(1)
  // Closing the workspace must preserve the native helper and running sessions.
  await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close())
  await expect.poll(() => application.windows().length).toBe(0)
  expect(await helperPids()).toEqual(nativePids)
  const reopened = application.waitForEvent('window')
  await application.evaluate(({ app }) => app.emit('activate'))
  page = await reopened
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await expect(page.locator('.app-shell')).toBeVisible()
  expect((await detail(terminal.id)).session.status).toBe('running')
  expect((await page.evaluate(() => window.buddy.nativeStatus())).available).toBe(true)
  const saved = await detail(agent.id)
  await application.close()
  application = undefined
  await expect.poll(() => nativePids.some(alive)).toBe(false)
  await launchApp()
  expect((await page.evaluate(() => window.buddy.settings())).appearance).toEqual(savedAppearance)
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await expect.poll(() => application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.getZoomFactor())).toBe(1.05)
  expect(await helperPids()).toHaveLength(1)
  await expect(pane(agent.id).locator('.session-identity')).toContainText('Build session orchestration')
  await expect(page.locator('.session-pane')).toHaveCount(2)
  await expect(pane(agent.id).getByRole('textbox', { name: 'Message agent' })).toHaveValue(draft)
  expect(
    await page.evaluate((id) => JSON.parse(localStorage.getItem(`buddy.panes.${id}`)), agent.id),
  ).toEqual(savedLayout)
  await expect(page.locator('.transcript-block.prompt')).toHaveCount(2)
  expect((await detail(agent.id)).session.providerSessionId).toBe(saved.session.providerSessionId)
  expect((await detail(agent.id)).events.filter((event) => event.type === 'prompt')).toEqual(
    saved.events.filter((event) => event.type === 'prompt'),
  )
  expect((await detail(terminal.id)).session.status).toBe('stopped')
  expect((await detail(splitOne.id)).session.status).toBe('stopped')
  expect((await detail(splitTwo.id)).session.status).toBe('stopped')
  const afterRestart = (await readFile(log, 'utf8')).trim().split('\n')
  expect(afterRestart).toHaveLength(4)
  // Persistent receipts remain effective across both helper reconnect and app restart.
  expect((await lampCommand('send', secondVoice)).ok).toBe(true)
  expect(
    (await lampCommand('session', { project_id: project.id, session_id: voice.id })).result.events.filter(
      (event) => event.type === 'prompt',
    ),
  ).toHaveLength(2)
  expect((await readFile(log, 'utf8')).trim().split('\n')).toHaveLength(4)
  // The real device Python router crosses HTTP, paired WS and Swift IPC to the
  // desktop. Speech recognition/model interpretation are represented by JSON.
  voiceApi = createServer(async (request, response) => {
    try {
      let body = ''
      for await (const chunk of request) body += chunk
      const command = JSON.parse(body)
      const result = await lampCommand(command.action.replace(/^agent\./, ''), command.params)
      response.setHeader('Content-Type', 'application/json')
      response.end(JSON.stringify({ status: 1, data: result }))
    } catch (error) {
      response.statusCode = 500
      response.end(JSON.stringify({ status: 0, message: String(error) }))
    }
  })
  voiceApi.listen(0, '127.0.0.1')
  await once(voiceApi, 'listening')
  const voiceEndpoint = `http://127.0.0.1:${voiceApi.address().port}/api/buddy/command`
  const routeVoice = async (params) => {
    const program = "import sys,json;sys.path.insert(0,sys.argv[1]);from buddy_agents import command;from voice_router import VoiceRouter;print(json.dumps(VoiceRouter(lambda a,p:command(a,p,endpoint=sys.argv[2]),sys.argv[3]).run(json.loads(sys.argv[4]))))"
    const { stdout } = await exec('python3', ['-c', program,
      path.resolve(desktop, '../../../../skills/agent-management/scripts'), voiceEndpoint,
      path.join(temporary, 'voice-context.json'), JSON.stringify(params)])
    return JSON.parse(stdout)
  }
  await page.locator('.session-row').filter({ hasText: 'Lamp voice session' }).click()
  await expect.poll(async () => (await lampCommand('list')).result.activeContext?.sessionId).toBe(voice.id)
  const routed = { target: 'active', request_id: 'routed-voice-1', prompt: 'Explain the current result from spoken intent.' }
  expect(await routeVoice(routed)).toMatchObject({ target: { sessionId: voice.id, worktreePath: projectPath }, result: { accepted: true } })
  expect(await routeVoice(routed)).toMatchObject({ result: { accepted: true } })
  await expect.poll(async () => (await detail(voice.id)).session.status).toBe('completed')
  await page.locator('.session-row').filter({ hasText: 'Workspace terminal' }).click()
  await expect.poll(async () => (await lampCommand('list')).result.activeContext?.sessionId).toBe(terminal.id)
  expect(await routeVoice({ request_id: 'routed-voice-2', prompt: 'Continue the same voice conversation.' })).toMatchObject({ target: { sessionId: voice.id }, result: { accepted: true } })
  await expect.poll(async () => (await detail(voice.id)).session.status).toBe('completed')
  const routedDetail = await detail(voice.id)
  expect(routedDetail.events.filter((event) => event.type === 'prompt')).toHaveLength(4)
  expect(routedDetail.session.providerSessionId).toBe(firstDetail.result.session.providerSessionId)
  await expect(routeVoice({ target: 'active', request_id: 'shell-must-reject', prompt: 'Never type this into the shell.' })).rejects.toThrow()
  expect((await readFile(log, 'utf8')).trim().split('\n')).toHaveLength(6)
  await page.evaluate((id) => window.buddy.restartInteractive(id), terminal.id)
  await expect.poll(async () => (await detail(terminal.id)).session.status).toBe('running')
  const projectToggle = page.getByRole('button', { name: 'Toggle project buddy-demo', exact: true })
  await projectToggle.click()
  await expect(projectToggle).toHaveAttribute('aria-expanded', 'false')
  await expect(page.locator('.tree-select').first()).toBeHidden()
  expect((await detail(terminal.id)).session.status).toBe('running')
  const contextBeforeCollapseReload = (await lampCommand('list')).result.activeContext
  await page.reload()
  await expect(projectToggle).toHaveAttribute('aria-expanded', 'false')
  await expect(page.locator('.tree-select').first()).toBeHidden()
  await expect.poll(async () => (await lampCommand('list')).result.activeContext?.sessionId).toBe(contextBeforeCollapseReload.sessionId)
  await nativeScreenshot('collapsed-project-sidebar.png')
  await expect(page.locator('.open-project')).toHaveCount(0)
  await projectToggle.focus()
  await page.keyboard.press('Enter')
  await expect(projectToggle).toHaveAttribute('aria-expanded', 'true')
  await expect(page.locator('.tree-select').first()).toBeVisible()
  expect(lampErrors).toEqual([])
  expect(pageErrors).toEqual([])
  console.log(
    'PASS: native Settings menu, persisted Appearance and live PTY continuity, unified app, real Swift helper IPC/ping/pause/exit, production UI, separate untracked/committed Git groups, Python voice routing through HTTP/Swift/WS with sticky follow-up and focused-pane context, scoped Git stage/unstage/commit/history review, recursive split PTYs/input isolation, persisted draft/layout, agent streaming, exact-ID follow-up, real Swift WebSocket lamp routing, reconnect and durable request dedup.',
  )
  console.log(`Screenshot: ${path.join(artifacts, 'manager-workspace.png')}`)
} catch (error) {
  if (page && !page.isClosed())
    await page.screenshot({ path: path.join(artifacts, 'manager-smoke-failure.png') }).catch(() => {})
  console.error('Electron process diagnostics:', processErrors.join('').slice(-6000))
  throw error
} finally {
  await application?.close().catch(() => {})
  for (const pending of pendingCommands.values()) clearTimeout(pending.timer)
  for (const socket of lamp?.clients ?? []) socket.terminate()
  if (lamp) await new Promise((resolve) => lamp.close(resolve))
  if (voiceApi) await new Promise((resolve) => voiceApi.close(resolve))
  await rm(temporary, { recursive: true, force: true })
}
