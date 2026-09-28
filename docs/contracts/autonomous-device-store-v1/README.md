# Pinned OpenHarness Store v1 contract

The three JSON files are unmodified copies from OpenHarness commit
`f54a70a782b7a4215e50f000399d777eb689ff84`, branch `feat/autonomous-device-store`,
[PR #245](https://github.com/autonomous-ai/openharness/pull/245).

Owner contract: [autonomous-device-store.md](https://github.com/autonomous-ai/openharness/blob/f54a70a782b7a4215e50f000399d777eb689ff84/docs/autonomous-device-store.md).
Owner schemas: [autonomous-device-store-v1](https://github.com/autonomous-ai/openharness/tree/f54a70a782b7a4215e50f000399d777eb689ff84/docs/contracts/autonomous-device-store-v1).

These schemas describe decrypted application payloads over the existing E2EE
connection. `blender.fixture.json` is synthetic; its doctor output and agent IDs
do not prove any installed application or successful model task. The same flow
applies to other packages without application-specific setup code in OS.

Tests on both the Go transport and Python skill consume this bundle. Updating
field names, semantics, or schemas requires coordination with OpenHarness; do
not silently edit this copy independently. Runtime CLI availability is checked
through hello capabilities, not inferred from this source snapshot.

See the OS [implementation guide](../../harness-store.md) and
[Vietnamese guide](../../vi/harness-store_vi.md) for local commands and recovery.
