// Derive the update from the release DMG, including on BUDDY_SKIP_BUILD retries.
// Never trust a potentially stale app left in the packager's output directory.
import { execFileSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, readFileSync, renameSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { verifyBundledCua } from './cua-driver.mjs'
import { releaseVersion } from './update-feed.mjs'
import { verifyUpdateOwnerWritable } from './update-permissions.mjs'

const [dmg, output, version, arch, mode = 'notarized'] = process.argv.slice(2)
releaseVersion(version)
if (!['arm64', 'x64'].includes(arch)) throw new Error(`Unsupported architecture: ${arch}`)
if (!['notarized', 'verify-only'].includes(mode)) throw new Error(`Unsupported mode: ${mode}`)
if (process.platform !== 'darwin') throw new Error('Update packaging requires macOS')
const run = (command, args) => execFileSync(command, args, { encoding: 'utf8' })
const stage = mkdtempSync(join(tmpdir(), 'buddy-update-'))
const mount = join(stage, 'mount')
mkdirSync(mount)
let mounted = false

function verifyApp(app) {
  verifyUpdateOwnerWritable(app)
  verifyBundledCua(app)
  const plist = join(app, 'Contents/Info.plist')
  for (const key of ['CFBundleShortVersionString', 'CFBundleVersion']) {
    if (run('/usr/libexec/PlistBuddy', ['-c', `Print :${key}`, plist]).trim() !== version)
      throw new Error(`Release ${version} does not match ${key} in ${app}`)
  }
  if (run('/usr/libexec/PlistBuddy', ['-c', 'Print :CFBundleIdentifier', plist]).trim() !== 'network.autonomous.ai.buddy.manager')
    throw new Error('Unexpected update application identifier')
  const manifest = JSON.parse(readFileSync(join(app, 'Contents/Resources/app/package.json'), 'utf8'))
  if (manifest.version !== version) throw new Error('Packaged package.json version does not match release')
  for (const binary of [
    'MacOS/Autonomous Buddy', 'Resources/native/AutonomousBuddy',
    'Resources/app/node_modules/node-pty/build/Release/pty.node',
    'Resources/app/node_modules/node-pty/build/Release/spawn-helper',
  ]) run('lipo', [join(app, 'Contents', binary), '-verify_arch', arch === 'x64' ? 'x86_64' : 'arm64'])
  run('codesign', ['--verify', '--deep', '--strict', app])
  if (mode === 'notarized') {
    run('codesign', ['--verify', '-R', '=anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists', app])
    run('xcrun', ['stapler', 'validate', app])
    run('spctl', ['--assess', '--type', 'execute', app])
  }
}

try {
  run('hdiutil', ['attach', '-readonly', '-nobrowse', '-mountpoint', mount, resolve(dmg)])
  mounted = true
  const app = join(stage, 'Autonomous Buddy.app')
  run('ditto', [join(mount, 'Autonomous Buddy.app'), app])
  run('hdiutil', ['detach', mount])
  mounted = false
  if (mode === 'notarized') run('xcrun', ['stapler', 'staple', app])
  verifyApp(app)
  if (mode === 'notarized') {
    // ditto preserves symlinks, executable modes, resource forks and the app ticket.
    const zip = `${resolve(output)}.tmp.zip`
    try {
      run('ditto', ['-c', '-k', '--sequesterRsrc', '--keepParent', app, zip])
      const unpacked = join(stage, 'unpacked')
      run('ditto', ['-x', '-k', zip, unpacked])
      verifyApp(join(unpacked, 'Autonomous Buddy.app'))
      renameSync(zip, resolve(output))
    } finally {
      rmSync(zip, { force: true })
    }
    console.log(`Verified update ZIP: ${output}`)
  }
} finally {
  if (mounted) run('hdiutil', ['detach', mount])
  rmSync(stage, { recursive: true, force: true })
}
