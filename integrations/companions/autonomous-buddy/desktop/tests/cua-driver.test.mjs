import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { CUA_VERSION, preserveCuaSignature, verifyCuaApp, verifySHA256 } from '../scripts/cua-driver.mjs'

function verifier(overrides = {}) {
  const calls = []
  return { calls, run(command, args) {
    calls.push([command, args])
    if (command === '/usr/libexec/PlistBuddy') {
      const key = args[1].split(':')[1]
      return overrides[key] ?? ({ CFBundleIdentifier: 'com.trycua.driver', CFBundleExecutable: 'cua-driver' }[key] || CUA_VERSION)
    }
    if (overrides.reject === command) throw new Error('verification failed')
    return ''
  } }
}

test('pinned app validation enforces vendor identity and both architectures', () => {
  const mock = verifier()
  verifyCuaApp('/Buddy.app/Contents/Helpers/CuaDriver.app', mock.run)
  const signing = mock.calls.find(([command]) => command === 'codesign')[1]
  assert.match(signing.join(' '), /YCK386LBJ7/)
  assert.ok(signing.includes('--strict'))
  const architectures = mock.calls.filter(([command]) => command === 'lipo')
  assert.equal(architectures.length, 2)
  for (const [, args] of architectures) assert.deepEqual(args.slice(-3), ['-verify_arch', 'arm64', 'x86_64'])
})

test('wrong version, identity, signature or architecture fails packaging', () => {
  for (const overrides of [{ CFBundleVersion: '0.28.1' }, { CFBundleShortVersionString: '0.28.1' }, { CFBundleIdentifier: 'other' }, { CFBundleExecutable: 'other' }, { reject: 'codesign' }, { reject: 'lipo' }])
    assert.throws(() => verifyCuaApp('/app', verifier(overrides).run))
  assert.throws(() => verifyCuaApp('/missing', () => { throw new Error('missing bundle') }))
})

test('only the nested vendor bundle is excluded from Buddy signing', () => {
  assert.equal(preserveCuaSignature('/Buddy.app/Contents/Helpers/CuaDriver.app'), true)
  assert.equal(preserveCuaSignature('/Buddy.app/Contents/Helpers/CuaDriver.app/Contents/MacOS/cua-driver'), true)
  assert.equal(preserveCuaSignature('/Buddy.app/Contents/Resources/native/AutonomousBuddy'), false)
  assert.equal(preserveCuaSignature('/Buddy.app/Contents/Helpers/CuaDriver.app-other'), false)
  assert.equal(preserveCuaSignature('/Buddy.app'), false)
})

test('modified dependency bytes are rejected even when a cached file exists', () => {
  const directory = mkdtempSync(join(tmpdir(), 'buddy-cua-test-'))
  try {
    const file = join(directory, 'archive')
    writeFileSync(file, 'abc')
    const hash = 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    verifySHA256(file, hash)
    writeFileSync(file, 'changed')
    assert.throws(() => verifySHA256(file, hash), /checksum mismatch/)
  } finally { rmSync(directory, { recursive: true, force: true }) }
})
