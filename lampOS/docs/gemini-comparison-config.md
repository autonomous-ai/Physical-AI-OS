# Gemini setup for a matched-model comparison

`lamp-gemini` can explicitly set thinking depth and speech language when preparing
a V1-main versus V2 cohort. Both fields remain absent by default. The existing
plain `gemini-3.8-live` / `Kore` request, manual activity boundaries, transcription
requests, instructions and absence of tools are unchanged. These setup options do
not change audio scheduling, admission, endpointing or runtime latency directly.

## Explicit setup

Select the model before requesting its thinking level:

```rust
use lamp_gemini::{Credential, SessionConfig, ThinkingLevel};

fn comparison(credential: Credential) -> lamp_gemini::Result<SessionConfig> {
    SessionConfig::google(credential)?
        .model("gemini-3.8-live-extended-thinking")?
        .voice("Kore")?
        .thinking_level(ThinkingLevel::Low)?
        .language_code("en-US")
}
```

The language above is an example, not the measured V1 value. Set it only when the
baseline explicitly sends the same tag; preserve absence and the exact tag rather
than guessing a locale. Use the already authorized endpoint and credential source
for both systems; no credential belongs in benchmark artifacts or setup JSON.
Runtime file provisioning is a separate caller responsibility.

The relevant wire fields are:

```json
{
  "setup": {
    "model": "models/gemini-3.8-live-extended-thinking",
    "generationConfig": {
      "responseModalities": ["AUDIO"],
      "speechConfig": {
        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}},
        "languageCode": "en-US"
      },
      "thinkingConfig": {"thinkingLevel": "LOW", "includeThoughts": false}
    }
  }
}
```

Other existing setup fields remain present. `thinkingConfig` belongs inside
`generationConfig`; `languageCode` belongs inside `speechConfig`, following the
[Google Live setup reference](https://ai.google.dev/api/live#bidigeneratecontentsetup)
and [generation configuration schema](https://ai.google.dev/api/generate-content#generationconfig).
The speech language field is distinct from input-transcription language hints;
this change adds no STT hints, search, tools, resumption or thought output.
The later [session reliability](gemini-session-reliability.md) work leaves this
initial setup byte-identical. Only a reconnect that presents a server-issued
handle adds `sessionResumption`; record that when a cohort includes a recovery.

`ThinkingLevel` supports the explicit protocol values `MINIMAL`, `LOW`, `MEDIUM`
and `HIGH`, with Rust variants `Minimal`, `Low`, `Medium` and `High`. Its `FromStr`
and Serde decoding are case-sensitive and reject unspecified/unknown values.
Absence, rather than an `UNSPECIFIED` value, retains provider defaults. The wire
includes `includeThoughts: false` only with an explicitly requested thinking level.
See Google's [thinking configuration](https://ai.google.dev/api/generate-content#thinkingconfig).

`language_code` accepts at most 63 ASCII bytes: a primary subtag of 2–8 letters,
then optional hyphen-separated subtags of 2–8 letters/digits. This covers ordinary
language, script, region and variant forms such as `en`, `en-US`, `vi-VN`,
`zh-Hant-TW` and `es-419`. It rejects spaces, underscores, empty subtags and control
characters. It preserves casing and does not expand short tags. This is bounded
shape validation, not a complete BCP-47 registry or extension/private-use parser;
model-specific supported languages remain server-validated. Google's documented
[speech configuration](https://ai.google.dev/api/generate-content#speechconfig)
lists supported language codes for the public API.

## Pinned V1 semantics and rejected combinations

The comparison basis is V1-main commit
`d5efe9d7b73cc529b34cd4abe97624682a82ca94`,
`hal/realtime/voice_agent/gemini_live.py::_build_config`. The audited V1 pilot uses
`gemini-3.8-live-extended-thinking`, `LOW` and `Kore`. The pinned code requests
`include_thoughts=False` with a thinking level and passes its optional speech
language through the SDK. No copy of that implementation is needed to run V2.

Two exact known model combinations are rejected locally with
`Error::InvalidConfiguration`:

- `gemini-3.8-live` with any explicit thinking level. V1 omits the field there.
- `gemini-3.8-live-extended-thinking` with `MINIMAL`. V1 clamps it to `LOW`; V2
  requires the caller to request `LOW` explicitly, so a cohort cannot be silently
  relabeled after normalization.

A subsequent `.model(...)` call also checks an already selected level. V1 uses
substring heuristics for older/native-audio models and their language handling;
V2 does not copy those broad assumptions or maintain a private-proxy model
allowlist. Other syntactically valid model IDs and fields are sent exactly as
requested for the provider to accept or reject. Unknown-model acceptance by the
builder is not evidence that the provider supports that configuration. The
existing transport reports server rejection without silently removing a field
or falling back to another model. No thinking-budget conversion is added.

## Comparison limits and verification

Record both effective setups, source revisions, model/voice/language/thinking,
prompt and history state, endpoint/admission behavior, audio hardware/gains and
stimulus identities before comparing a cohort. A matched-model whole-system test
still includes differences in DSP, endpointing, transport, prompts and runtime
coordination. It does not isolate a Rust-versus-Python language speedup. Report
all attempted turns, failures and the same acoustic speech-end-to-first-word
boundary; software dispatch/setup timing is not that acoustic measurement.

The offline tests verify the complete unchanged default setup, exact field
placement, strict level decoding, bounded language validation, known unsupported
combinations, model changes and unknown proxy aliases. Run:

```sh
cargo fmt --check -p lamp-gemini
cargo clippy -p lamp-gemini --all-targets --locked --offline -- -D warnings
cargo test -p lamp-gemini --locked --offline
```

These checks use no credentials or provider connection and establish no device
latency, provider model availability or acoustic acceptance.

## Private runtime caller JSON

`lamp_live::config::ProviderConfig::load(path).into_session()` now carries these
options into the same Gemini setup. A private caller file can contain:

```json
{
  "endpoint": "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent",
  "model": "gemini-3.8-live-extended-thinking",
  "voice": "Kore",
  "thinking_level": "LOW",
  "language_code": "en-US",
  "credential_file": "/absolute/private/path/gemini-key",
  "authentication": "api_key"
}
```

This is an example, not an instruction to change an endpoint or language for an
existing cohort. Both new keys may be omitted; explicit `null` also means absent.
The loader selects `model` before applying the optional thinking level, then
preserves any explicit language tag. It neither infers options from the model nor
loads legacy HAL/os-server configuration. Existing required fields and default
`api_key` authentication are unchanged; `bearer` and `ephemeral` remain supported.

Unknown JSON fields, malformed thinking levels and non-string language values
fail typed loading. Unsupported known model/level combinations and malformed
language-tag strings fail session construction before any provider connection.
The fixed chat instruction and manual input behavior are unchanged.

Both configuration and credential files must be regular files owned by the
process user, without group/other permissions (normally mode `0600`). Final
symlinks are refused. The credential path must be absolute; configuration is
bounded to 16,384 bytes and the credential file to 4,098 bytes. The credential is
still loaded only into the private authentication header and is absent from setup
JSON. Provision and record the selected non-secret options separately from keys.

`crates/live/tests/provider_config.rs` exercises the complete private-file path
with a disposable test-only credential marker. It checks exact setup fields,
omitted/null defaults, invalid options, unknown proxy aliases and existing file
privacy/size/symlink guards without a provider connection:

```sh
cargo test -p lamp-live --test provider_config --locked --offline
cargo clippy -p lamp-live --all-targets --locked --offline -- -D warnings
```
