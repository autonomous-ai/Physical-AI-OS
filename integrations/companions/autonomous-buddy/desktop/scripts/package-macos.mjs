import { packager } from '@electron/packager'
import { execFileSync } from 'node:child_process'
import { accessSync, constants, cpSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve, join } from 'node:path'
import { discoverSigningIdentity } from './signing-identity.mjs'
import { prepareCuaDriver, preserveCuaSignature, verifyBundledCua } from './cua-driver.mjs'
import { releaseVersion } from './update-feed.mjs'
import { makeUpdateOwnerWritable, verifyUpdateOwnerWritable } from './update-permissions.mjs'

if (process.platform !== 'darwin') throw new Error('This packaging target requires macOS')
const version = releaseVersion(readFileSync('../VERSION_AUTONOMOUS_BUDDY', 'utf8').trim())
const arch = process.env.BUDDY_ARCH || process.arch
if (!['arm64', 'x64'].includes(arch)) throw new Error(`Unsupported BUDDY_ARCH: ${arch}`)
const electronVersion = JSON.parse(readFileSync('node_modules/electron/package.json', 'utf8')).version
const identity = discoverSigningIdentity()
const iconDirectory = resolve('artifacts/icon')
execFileSync('swift', ['scripts/generate-icon.swift', iconDirectory], { stdio: 'inherit' })
const icon = join(iconDirectory, 'AutonomousBuddy.icns')
execFileSync('iconutil', ['-c', 'icns', join(iconDirectory, 'AutonomousBuddy.iconset'), '-o', icon], {
  stdio: 'inherit',
})
const swiftDirectory = resolve('../macos')
const swiftArgs = ['build', '-c', 'release', '--arch', arch === 'arm64' ? 'arm64' : 'x86_64']
execFileSync('swift', swiftArgs, { cwd: swiftDirectory, stdio: 'inherit' })
const swiftOutput = execFileSync('swift', [...swiftArgs, '--show-bin-path'], {
  cwd: swiftDirectory,
  encoding: 'utf8',
}).trim()
const helper = join(swiftOutput, 'AutonomousBuddy')
accessSync(helper, constants.X_OK)
const cua = prepareCuaDriver(resolve('artifacts/dependencies/cua'))
const paths = await packager({
  dir: '.',
  name: 'Autonomous Buddy',
  platform: 'darwin',
  arch,
  appVersion: version,
  buildVersion: version,
  appBundleId: 'network.autonomous.ai.buddy.manager',
  appCategoryType: 'public.app-category.developer-tools',
  icon,
  extendInfo: {
    NSAppleEventsUsageDescription:
      'Autonomous Buddy uses Apple Events to control applications when requested.',
    NSLocalNetworkUsageDescription:
      'Autonomous Buddy connects to your paired Autonomous device on the local network.',
    NSBonjourServices: ['_autonomous._tcp'],
  },
  out: 'artifacts/package',
  overwrite: true,
  prune: true,
  // Keep node-pty and its spawn-helper on disk; no ASAR path translation is needed.
  asar: false,
  ignore: [/^\/(artifacts|src|tests|scripts)(\/|$)/, /^\/\.(git|vite)(\/|$)/],
  // Normalize Electron's extracted framework/resources before any app signing.
  afterExtract: [async ({ buildPath }) => { makeUpdateOwnerWritable(buildPath) }],
  // Embed before signing so the nested executable is covered by the app seal.
  afterCopy: [
    async ({ buildPath }) => {
      makeUpdateOwnerWritable(buildPath)
      cua.embed(resolve(buildPath, '..', '..', '..'))
      const packagePath = join(buildPath, 'package.json')
      const manifest = JSON.parse(readFileSync(packagePath, 'utf8'))
      writeFileSync(packagePath, `${JSON.stringify({ ...manifest, version }, null, 2)}\n`)

      // Rebuild inside the staged copy so packaging another architecture cannot
      // overwrite the development tree's native modules or a previous package.
      const { rebuild } = await import('@electron/rebuild')
      await rebuild({ buildPath, electronVersion, arch, force: true, onlyModules: ['node-pty'] })
      const nativeDirectory = resolve(buildPath, '..', 'native')
      mkdirSync(nativeDirectory, { recursive: true })
      cpSync(helper, join(nativeDirectory, 'AutonomousBuddy'))
      // SwiftPM dependencies currently link statically; copy resource bundles if present.
      for (const entry of readdirSync(swiftOutput)) {
        if (entry.endsWith('.bundle'))
          cpSync(join(swiftOutput, entry), join(nativeDirectory, entry), { recursive: true })
      }
      // Rebuild and Swift resource copies can introduce owner-read-only files.
      // Preserve executable bits while making Squirrel's quarantine cleanup safe.
      makeUpdateOwnerWritable(buildPath)
      makeUpdateOwnerWritable(nativeDirectory)
      execFileSync(
        'codesign',
        [
          '--force',
          '--sign',
          identity || '-',
          ...(identity ? ['--options', 'runtime', '--timestamp'] : []),
          '--identifier',
          'network.autonomous.ai.buddy',
          join(nativeDirectory, 'AutonomousBuddy'),
        ],
        { stdio: 'inherit' },
      )
    },
  ],
  osxSign: {
    identity: identity || '-',
    continueOnError: false,
    identityValidation: Boolean(identity),
    preAutoEntitlements: Boolean(identity),
    preEmbedProvisioningProfile: Boolean(identity),
    ignore: preserveCuaSignature,
    optionsForFile: () => ({ hardenedRuntime: Boolean(identity) }),
  },
}).finally(() => cua.cleanup())
for (const directory of paths) {
  const bundle = `${directory}/Autonomous Buddy.app`
  verifyUpdateOwnerWritable(bundle)
  accessSync(`${bundle}/Contents/Resources/native/AutonomousBuddy`, constants.X_OK)
  // Catch mixed-architecture bundles before they can be distributed.
  const machoArch = arch === 'x64' ? 'x86_64' : 'arm64'
  for (const binary of [
    'MacOS/Autonomous Buddy',
    'Resources/native/AutonomousBuddy',
    'Resources/app/node_modules/node-pty/build/Release/pty.node',
    'Resources/app/node_modules/node-pty/build/Release/spawn-helper',
  ]) {
    execFileSync('lipo', [`${bundle}/Contents/${binary}`, '-verify_arch', machoArch], { stdio: 'inherit' })
  }
  verifyBundledCua(bundle)
  execFileSync('codesign', ['--verify', '--deep', '--strict', bundle], { stdio: 'inherit' })
  console.log(`Built unified ${identity ? 'Developer ID' : 'ad-hoc'} signed app: ${bundle}`)
}
