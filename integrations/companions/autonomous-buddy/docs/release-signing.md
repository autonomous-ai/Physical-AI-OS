# Autonomous Buddy — release signing & notarization

> **Unified product (September 2026):** Run build/signing commands from `integrations/companions/autonomous-buddy/`. `make build`, `app` and `install` package one Electron application containing the Swift helper, defaulting to the current Node architecture; use `make build BUDDY_ARCH=x64` for Intel. `make dmg`, `dmg-signed` and `notarize` default to `BUDDY_ARCHS="arm64 x64"`, producing or processing separate Apple Silicon and Intel DMGs sequentially. These are not universal binaries. App output: `desktop/artifacts/package/Autonomous Buddy-darwin-<arch>/Autonomous Buddy.app`; DMG: `dist/Autonomous-Buddy-<version>-<arch>.dmg`. Original Swift-only universal release recipes remain under explicit `native-*` targets and retain `AutonomousBuddy-<version>.dmg`. `make build`, `make install` and `make dmg` do not submit to Apple; `make dmg` is the local packaging flow, while the upload target defaults to notarized release artifacts. Cross-compilation does not establish Intel runtime compatibility; launch, terminal/native helper and permission behavior still need verification on target Macs.

## Bundled Cua Driver

The unified app packages official Cua Driver **0.28.2** under `Contents/Helpers/CuaDriver.app`, so users install only Buddy. The build downloads the pinned upstream release and verifies its SHA-256 checksum before staging it; no driver binary is committed. Preserve the nested app's vendor signature instead of re-signing it with Buddy's identity. Validate its signature and support for the selected architecture before signing the outer Buddy app. The updater ZIP carries the same nested driver as the DMG. The legacy Swift-only `native-*` targets remain a separate development flow.

At runtime Buddy starts the embedded driver lazily and owns its lifetime; users do not run `cua-driver serve` or install a second app. Embedded mode uses Buddy's macOS permission identity. Test Accessibility, Screen Recording, the existing **Restart computer use** control after a permission change, Pause/cancellation and child shutdown from the packaged app on each target architecture. A standalone CuaDriver permission grant or a successful local build does not establish that the packaged Buddy app has permission, is notarized, or passes Gatekeeper. Verify the final distribution using the checks below.

An older ad-hoc Screen Recording grant may still appear enabled while macOS rejects the current signature. If TCC reports `Failed to match existing code requirement`, remove the old Buddy row from Screen & System Audio Recording and add `/Applications/Autonomous Buddy.app` again, then restart Buddy. The installer never resets or edits macOS privacy grants.

## Architecture-specific uploads and OTA metadata

From the repo root, `make upload-autonomous-buddy` bumps the shared patch version in `VERSION_AUTONOMOUS_BUDDY` once, builds, signs, notarizes and staples both DMGs, verifies their tickets and Gatekeeper acceptance, then uploads each to `${BUCKET_PREFIX}/ota/autonomous-buddy/<arch>/<version>.dmg` (normally `os/ota/autonomous-buddy/...`). Each selected metadata entry, `autonomous-buddy.arm64` or `autonomous-buddy.x64`, has its own `version`, `url`, `sha256` and `updated_at`. A release for one architecture preserves the other architecture's version and all unrelated component entries. When migrating the old flat metadata, the publisher preserves it as `arm64` if that entry is absent, because the previous published DMG was Apple Silicon only. A new Intel-only release therefore retains that older Apple Silicon download, including its checksum when present. Selected architectures are then updated and the old ambiguous top-level Buddy version/URL fields are removed. Manual download consumers select the matching architecture. Signed releases also publish a ZIP and Squirrel feed for the [in-app updater](app-updates.md); the shared metadata remains the DMG download catalog.

```bash
# Use an existing notarytool Keychain profile (see one-time setup below).
export NOTARY_PROFILE=autonomous-notary
# Both Apple Silicon and Intel; one version bump, signed and notarized.
make upload-autonomous-buddy
# Release only Intel, preserving the Apple Silicon metadata entry.
BUDDY_ARCHS=x64 make upload-autonomous-buddy
# Retry with both existing notarized DMGs; no bump, rebuild or profile required.
BUDDY_SKIP_BUILD=1 make upload-autonomous-buddy
# Retry an Intel-only release.
BUDDY_SKIP_BUILD=1 BUDDY_ARCHS=x64 make upload-autonomous-buddy
```

Keep the selected architectures and `BUDDY_DMG_TARGET` consistent when retrying. The default `BUDDY_DMG_TARGET=dmg-signed` requires `NOTARY_PROFILE` and validates credentials with `notarytool history` before bumping or building. `BUDDY_SKIP_BUILD=1` reuses existing notarized DMGs without a version bump, rebuild, notary submission or profile; app stapling may retrieve an existing ticket. Before uploading anything, all selected DMGs must exist and pass version, architecture, signature, ticket and Gatekeeper checks; their updater ZIPs must also pass validation.

`BUDDY_DMG_TARGET=dmg` is an explicit local/test override: it validates the app version, architecture and signature, publishes manual DMG metadata only, and leaves updater feeds untouched. These are the only accepted targets; standalone `native-*` artifacts are excluded. Custom `GCS_PATH` or `BUDDY_URL` requires one architecture and a `.dmg` suffix; the URL must use HTTPS. ZIP and `latest.json` destinations are siblings of the DMG.

Failure to read existing metadata aborts metadata publication instead of replacing the shared feed. Updating signed shared metadata requires `OTA_SIGNING_PRIVATE_KEY`. Artifacts can already be uploaded when metadata publication fails.

Before signing, packaging adds owner-write permission to bundle files and directories, preserving executable bits and never following symlinks. This is required for Squirrel installation: the SwiftPM privacy manifest can otherwise retain mode `0444`, preventing replacement. ZIP release validation rejects read-only resources, including `BUDDY_SKIP_BUILD=1` retries; rebuild, sign and notarize the app instead of changing permissions inside an already signed release.

### Automatic-update ZIP and feed

The signed upload extracts the exact app from each notarized DMG, staples the app, then verifies Developer ID signing, its ticket and Gatekeeper acceptance. Both plist versions (`CFBundleShortVersionString`, `CFBundleVersion`) and `Contents/Resources/app/package.json` must match `VERSION_AUTONOMOUS_BUDDY`; Electron, Swift helper and node-pty binaries must match the selected architecture; the bundled CuaDriver must include that architecture and retain its validated vendor signature. `ditto` creates `dist/Autonomous-Buddy-<version>-<arch>.zip`, preserving the app bundle name; a fresh extraction is verified again. To create only this ZIP from an existing notarized DMG, run `make update-zip BUDDY_ARCH=arm64` from the Buddy directory. No app rebuild is involved.

Upload order is each architecture's `<version>.dmg` then `<version>.zip`, followed by shared `metadata.json`, then each architecture's `latest.json` last. The default ZIP path is `${BUCKET_PREFIX}/ota/autonomous-buddy/<arch>/<version>.zip`. The static Squirrel feed contains:

```json
{"currentRelease":"<version>","releases":[{"version":"<version>","updateTo":{"version":"<version>","name":"<release name>","url":"<HTTPS ZIP URL>","pub_date":"<timestamp>","sha256":"<ZIP SHA256>","size":12345}}]}
```

Squirrel consumes the ZIP feed; DMGs remain available for first installation and manual recovery. Buddy 0.0.21 and earlier need one manual installation to gain the updater. This source change does not publish a release or bump the version.

This is the handover doc for whoever owns the Apple Developer enrolment. Once the one-time setup is done, every release boils down to:

```bash
cd integrations/companions/autonomous-buddy
export DEV_ID_APP="Developer ID Application: <Your Org> (<TEAMID>)"
export NOTARY_PROFILE=autonomous-notary
make dmg-signed
```

Each output `dist/Autonomous-Buddy-<version>-<arch>.dmg` is signed, notarized, and stapled — users mount it, drag the app to Applications, double-click, and macOS opens it without any Gatekeeper warning or right-click dance.

The unified Electron packager selects signing identity before building. A nonempty `DEV_ID_APP` takes precedence (certificate name or SHA-1 fingerprint). Otherwise it reads usable identities with `security find-identity -v -p codesigning`, accepts only complete `Developer ID Application:` entries, and excludes Apple Development and revoked/expired entries. A single candidate is selected automatically. With multiple candidates it prefers the team of `/Applications/Autonomous Buddy.app`; ambiguous matches or a known installed team with no matching candidate fail with an actionable `DEV_ID_APP` override instead of silently switching teams. No certificate or person is hardcoded.

With no usable Developer ID identity, packaging retains ad-hoc fallback and explicitly warns that Accessibility/Screen Recording may need to be granted again after installation. A failed identity lookup aborts instead of silently downgrading. An empty variable enables auto-detection; `DEV_ID_APP=-` deliberately selects ad-hoc for the unified packager. Both the embedded helper and outer app use the selected identity. Routine builds therefore no longer silently replace a Developer ID build with an ad-hoc build merely because the shell did not export `DEV_ID_APP`. This stabilizes signing identity; it does not promise automatic restoration of TCC grants already invalidated by an earlier install.

The legacy `native-*` Makefile recipes retain their existing identity detection and override rules. For a multi-certificate release, explicitly export the intended identity so every packaging/signing entry point uses the same certificate.

What `make dmg-signed` adds on top of `make dmg` is notarization, stapling and Gatekeeper assessment, which needs `NOTARY_PROFILE`. Apple instructs developers to notarize the outermost distribution container: submitting each final DMG covers its nested app and binaries. The updater ZIP is built from that exact app after app-level stapling and validation; it does not require a separate ZIP submission. See [Apple: Packaging Mac software for distribution](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution).

## What changes vs the ad-hoc build

| | Ad-hoc (no cert installed) | Production (`make dmg-signed`) |
|---|---|---|
| Signing identity | None (`-` placeholder) | Developer ID Application cert from Apple |
| Hardened runtime | Off | **On** (`--options runtime`, required by Apple) |
| Secure timestamp | No | **Yes** (`--timestamp`) |
| Notarized by Apple | No | **Yes** (notarytool submit + wait) |
| Stapler ticket | No | **Yes** (`stapler staple`) — works offline too |
| First-launch UX | Right-click → Open Anyway | Just double-click |
| TCC reset on rebuild | Every build (cdhash changes) | Stable across rebuilds (identifier-based cert) |
| User permission grants | Re-grant Accessibility + Screen Recording each release | Granted once, persists across releases |

Stable TCC is the single biggest user-facing win — without it you re-burn through ~3 permission dialogs every time a tester gets a new build.

## One-time setup (the dev who owns the cert does this)

### 1. Enrol in the Apple Developer Program

`https://developer.apple.com/programs/enroll/` — $99/year. Individual or organisation account both work; organisation is preferable so the cert isn't tied to a single Apple ID.

After enrolment, note the **Team ID** (10-char alphanumeric, e.g. `ABCDE12345`) — it appears in your Apple Developer account header and is needed below.

### 2. Create a Developer ID Application certificate

The straight path is from inside Xcode (download Xcode if you don't have it):

1. Xcode → **Settings → Accounts** → "+" → sign in with the enrolment Apple ID.
2. Pick the team → **Manage Certificates…**.
3. "+" → **Developer ID Application**. Xcode generates the CSR + downloads the `.cer` + installs the private key into your login Keychain in one step.

Manual path (no Xcode) — only if Xcode is not available:

1. Keychain Access → **Certificate Assistant → Request a Certificate From a Certificate Authority…** → save CSR to disk.
2. `https://developer.apple.com/account/resources/certificates` → "+" → **Developer ID Application** → upload CSR → download `.cer`.
3. Double-click the `.cer` → installs into login Keychain; private key was generated in step 1 alongside the CSR.

Verify the cert is usable:

```bash
security find-identity -v -p codesigning
```

Look for `Developer ID Application: <Your Org> (<TEAMID>)` in the output. The full quoted string is what you pass as `DEV_ID_APP` to `make`.

### 3. Set up notarytool credentials

Notarization runs against an Apple-issued **app-specific password**, not your iCloud password. Create one once:

1. `https://account.apple.com/account/manage` → Sign-In and Security → **App-Specific Passwords** → Generate (label it `autonomous-buddy-notary` or similar).
2. Copy the password (format `xxxx-xxxx-xxxx-xxxx`).
3. Store the credential trio (Apple ID, Team ID, app-specific password) in the macOS Keychain so `notarytool` can pull it without prompts:

```bash
xcrun notarytool store-credentials autonomous-notary \
  --apple-id "your-apple-id@example.com" \
  --team-id "ABCDE12345" \
  --password "xxxx-xxxx-xxxx-xxxx"
```

`autonomous-notary` is the profile alias — pass it as `NOTARY_PROFILE` to `make`. You can pick any name; just stay consistent.

Smoke test:

```bash
xcrun notarytool history --keychain-profile autonomous-notary
```

Empty history is fine — it means auth works.

## Per-release flow

```bash
cd integrations/companions/autonomous-buddy

# Persist these in your shell rc once, or export per session.
export DEV_ID_APP="Developer ID Application: Autonomous Inc (ABCDE12345)"
export NOTARY_PROFILE=autonomous-notary

# VERSION is read from VERSION_AUTONOMOUS_BUDDY; the upload target bumps it once.

make dmg-signed
# For Intel only: make dmg-signed BUDDY_ARCHS=x64
```

For each selected architecture, the make target does, in order:

1. Compile the Electron main/renderer. Rebuild node-pty for the selected Electron architecture in a staged dependency copy, preserving the development dependencies.
2. Cross-compile the Swift helper in release mode for the selected architecture (`x64` maps to Swift `x86_64`; Apple Silicon uses `arm64`).
3. Package `desktop/artifacts/package/Autonomous Buddy-darwin-<arch>/Autonomous Buddy.app`, embedding the helper and SwiftPM resources under `Contents/Resources/native/` and the verified CuaDriver app under `Contents/Helpers/`.
4. `codesign` the app with Developer ID, hardened runtime, secure timestamp. Packaging verifies the signature and uses `lipo` to check the target architecture of Electron, the Swift helper, node-pty and its spawn helper, plus architecture support and the vendor signature of bundled CuaDriver.
5. `hdiutil create` the DMG (drag-to-Applications layout).
6. `codesign` the DMG with Developer ID.
7. `xcrun notarytool submit … --wait` — uploads to Apple, blocks 1-5 minutes until verdict.
8. `xcrun stapler staple` — embeds the notarization ticket so Gatekeeper can verify offline.
9. `xcrun stapler validate` — verify the embedded DMG ticket, then `spctl --assess --type open --context context:primary-signature` checks Gatekeeper acceptance. Run the remaining app checks below separately before distribution.

The default outputs are `dist/Autonomous-Buddy-<version>-arm64.dmg` and `dist/Autonomous-Buddy-<version>-x64.dmg`. Distribute the file matching the user’s Mac. `make dmg BUDDY_ARCHS=x64` builds only Intel; `make notarize BUDDY_ARCHS=x64` notarizes only its existing DMG.

## Verifying a build before shipping

```bash
# 1. App signature is well-formed.
codesign --verify --deep --strict --verbose=2 "desktop/artifacts/package/Autonomous Buddy-darwin-<arch>/Autonomous Buddy.app"

# 2. Gatekeeper accepts the app.
spctl --assess --type execute --verbose=4 "desktop/artifacts/package/Autonomous Buddy-darwin-<arch>/Autonomous Buddy.app"
#   expected: "accepted source=Developer ID notarized"

# 3. DMG itself has a stapled ticket.
xcrun stapler validate "dist/Autonomous-Buddy-<version>-<arch>.dmg"
#   expected: "The validate action worked!"

# 4. Real Gatekeeper dry-run on the DMG.
spctl --assess --type open --context context:primary-signature --verbose=4 "dist/Autonomous-Buddy-<version>-<arch>.dmg"
#   expected: "accepted source=Notarized Developer ID"
```

Run all four checks for each architecture before distribution. Also test launch and native features on an Intel Mac and an Apple Silicon Mac; cross-compilation and signature checks do not replace those runtime checks.

## Common failure modes

**`errSecInternalComponent` during codesign.** Your private key is missing or locked. Open Keychain Access → login → search for "Developer ID Application" → confirm the private key sibling exists. If only the cert is there, you imported the `.cer` on a different machine than the one that generated the CSR — re-do step 2 on this machine.

**`Hardened Runtime is not enabled` from notarytool log.** The `--options runtime` flag is missing. The Makefile target sets this; if you ran `codesign` by hand, re-sign with `--options runtime`.

**`The signature does not include a secure timestamp`.** Missing `--timestamp` flag. Same fix — the Makefile sets it; manual codesign needs it explicitly.

**Notarization status `Invalid` with log mentioning `disallowed-entitlement`.** You're using an entitlement Apple doesn't allow for Developer ID distribution. Buddy currently doesn't set entitlements, so this should not happen. If it does, fetch the full log:

```bash
xcrun notarytool log <submission-id> --keychain-profile autonomous-notary
```

…and check which entitlement was rejected.

**Notarization status `Accepted` but Gatekeeper still warns on the user's Mac.** The DMG wasn't stapled. Either re-run `make notarize` against the existing DMG (re-staples) or rebuild with `make dmg-signed`.

**User reports "app is damaged".** Usually means the quarantine xattr is set and the staple ticket is missing or invalid. Have the user run `xattr -d com.apple.quarantine /Applications/AutonomousBuddy.app` as a one-off; permanent fix is to ship a stapled DMG.

## When to re-notarize

You need a fresh notarization any time the .app binary or the DMG changes — i.e. every release. Notarization is bound to the exact bits; you can't "transfer" a ticket from one build to another. The Makefile handles this automatically by running the full chain.

Stapling is offline-capable, so users who first install when offline still get the no-warning experience as long as the DMG itself has the ticket embedded.

## Things this doc deliberately does NOT cover

- **Mac App Store distribution.** Different cert (`Apple Distribution`), App Sandbox required, separate submission flow via App Store Connect. Out of scope for now.
- **App updates.** The unified Electron app uses Squirrel.Mac with signed ZIP feeds; see [app updates](app-updates.md). The legacy native-only app has no updater.
- **CI signing.** Doable (GitHub Actions with cert + notarytool keychain profile encrypted as secrets), but the current handoff assumes one dev signs locally. Set up CI when build cadence justifies it.
