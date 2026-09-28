import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { accessSync, constants, existsSync, mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, copyFileSync } from 'node:fs'
import { join } from 'node:path'
import { makeUpdateOwnerWritable } from './update-permissions.mjs'

// Pin the upstream release and source license; never package a developer's installation.
// Archive digest published by github.com/trycua/cua/releases/tag/cua-driver-rs-v0.28.2.
export const CUA_VERSION = '0.28.2'
export const CUA_BUNDLE_PATH = 'Contents/Helpers/CuaDriver.app'
export const CUA_ARCHIVE_SHA256 = 'e273181b26709c88b1d809474deb3c592b4efae3530b11d76318f1887fc3fbb1'
const licenseSHA256 = 'c0779290c1d4783169aa3dbfb55feb505e563ef8a004bbf55298ceffcfbda8d9'
const releaseName = `cua-driver-rs-${CUA_VERSION}-darwin-universal`
const archiveURL = `https://github.com/trycua/cua/releases/download/cua-driver-rs-v${CUA_VERSION}/${releaseName}.tar.gz`
const licenseURL = 'https://raw.githubusercontent.com/trycua/cua/fc188250b4ca8549b8e61f937fdb1fb560770e86/LICENSE.md'
const licensePath = 'Contents/Resources/licenses/CuaDriver-MIT.txt'
const run = (command, args) => execFileSync(command, args, { encoding: 'utf8' })

export function verifySHA256(file, expected) {
  if (createHash('sha256').update(readFileSync(file)).digest('hex') !== expected)
    throw new Error(`Cua dependency checksum mismatch: ${file}`)
}

function downloadPinned(url, file, digest) {
  if (!existsSync(file)) {
    const temporary = `${file}.${process.pid}.download`
    try {
      run('curl', ['--fail', '--location', '--silent', '--show-error', '--retry', '3', '--connect-timeout', '15', '--max-time', '300', '--proto', '=https', '--proto-redir', '=https', url, '-o', temporary])
      verifySHA256(temporary, digest)
      renameSync(temporary, file)
    } finally {
      rmSync(temporary, { force: true })
    }
  }
  verifySHA256(file, digest)
}

export function verifyCuaApp(app, execute = run) {
  const plist = join(app, 'Contents/Info.plist')
  const fields = { CFBundleIdentifier: 'com.trycua.driver', CFBundleShortVersionString: CUA_VERSION, CFBundleVersion: CUA_VERSION, CFBundleExecutable: 'cua-driver' }
  for (const [key, value] of Object.entries(fields)) {
    if (execute('/usr/libexec/PlistBuddy', ['-c', `Print :${key}`, plist]).trim() !== value)
      throw new Error(`Unexpected CuaDriver ${key}; rebuild with pinned ${CUA_VERSION}`)
  }
  execute('codesign', ['--verify', '--deep', '--strict', '-R', '=anchor apple generic and identifier "com.trycua.driver" and certificate leaf[subject.OU] = "YCK386LBJ7" and certificate leaf[field.1.2.840.113635.100.6.1.13] exists', app])
  for (const binary of ['cua-driver', 'cua-cursor-theme'])
    execute('lipo', [join(app, 'Contents/MacOS', binary), '-verify_arch', 'arm64', 'x86_64'])
}

export function verifyBundledCua(app, execute = run) {
  verifyCuaApp(join(app, CUA_BUNDLE_PATH), execute)
  verifySHA256(join(app, licensePath), licenseSHA256)
}

// Preserve upstream provenance and its seal; embedded permissions belong to Buddy.
export function preserveCuaSignature(path) {
  return /\/Contents\/Helpers\/CuaDriver\.app(?:\/|$)/.test(path)
}

export function prepareCuaDriver(cacheDirectory) {
  mkdirSync(cacheDirectory, { recursive: true })
  const archive = join(cacheDirectory, `${releaseName}.tar.gz`)
  const license = join(cacheDirectory, 'CuaDriver-MIT.txt')
  downloadPinned(archiveURL, archive, CUA_ARCHIVE_SHA256)
  downloadPinned(licenseURL, license, licenseSHA256)
  const stage = mkdtempSync(join(cacheDirectory, 'stage-'))
  try {
    run('tar', ['-xzf', archive, '-C', stage])
    const app = join(stage, releaseName, 'CuaDriver.app')
    accessSync(join(app, 'Contents/MacOS/cua-driver'), constants.X_OK)
    verifyCuaApp(app)
    return {
      embed(buddyApp) {
        const target = join(buddyApp, CUA_BUNDLE_PATH)
        mkdirSync(join(buddyApp, 'Contents/Helpers'), { recursive: true })
        run('ditto', [app, target])
        makeUpdateOwnerWritable(target)
        mkdirSync(join(buddyApp, 'Contents/Resources/licenses'), { recursive: true })
        copyFileSync(license, join(buddyApp, licensePath))
        verifyBundledCua(buddyApp)
      },
      cleanup() { rmSync(stage, { recursive: true, force: true }) },
    }
  } catch (error) {
    rmSync(stage, { recursive: true, force: true })
    throw error
  }
}
