# Harness Store from Autonomous OS

OS implements the decrypted Store v1 contract from OpenHarness [PR #245](https://github.com/autonomous-ai/openharness/pull/245), pinned to `f54a70a782b7a4215e50f000399d777eb689ff84`. The [shared schemas and synthetic fixture](contracts/autonomous-device-store-v1/README.md) are copied unchanged from that revision. [Tiếng Việt](vi/harness-store_vi.md).

The path remains `harness-use → /api/harness/request` on loopback → the existing authenticated direct E2EE connection → Harness CLI. No new credentials, pairing flow or generic app/admin dispatcher is introduced. Store calls require **all four** negotiated hello capabilities: `store.list`, `store.inspect`, `agent.prepare`, `operation.get`. Old CLIs keep their existing agent operations; Store requests explain that CLI must be updated, without falling back to shell setup or Buddy. Source compatibility is not evidence that the running CLI has been updated.

## Discovery and preparation

The model selects packages using `store.list` metadata and user requirements. Search matches all query terms in package ID/name/description/category: use the app or discipline as the query, not a whole task sentence. Results have at most ten entries per page; use `nextOffset` when needed. `store.inspect` returns a package and up to five candidate agents whose recorded `packageId` matches. `candidatesTruncated` means more candidates may exist in `agents.list`. Package identity is never inferred from a name or recap.

An existing candidate can be selected explicitly only for the user's intended project, using its actual package/workspace/runtime evidence. Otherwise preparation creates a new session. Workspace `{kind:"new"}` uses Harness's normal directory convention; an optional ASCII name is validated by the contract. `{kind:"existing",path:"..."}` must be an existing absolute path explicitly selected by the owner; do not invent desktop paths or reuse another agent's project. Preparation never carries the task text and never dispatches a model task.

Harness owns installation, reviewed dependency handling, setup, doctors, workspace materialization and agent launch. OS displays `installation`, `readiness`, error and guidance facts. `verified`/`installAllowed` do not certify safety or task success; community package/dependency review may require owner action in Desktop. `requirements.applications` is null in v1, not an invitation to synthesize dependency names. A passed package doctor is a dated observation, while `engineAuthentication` and task success remain unknown.

## Device helper commands

Run `python3 scripts/harness.py ACTION -` in `skills/harness-use` with JSON on stdin. Keep a stable `conversation_id` (default `voice`) and `intent_id` across retries. A supplied device response run ID is a suitable initial intent ID; later turns resume that saved ID, passing their current response route before dispatch if needed. `response` is local `{run_id,channel:"voice"|"web"}` metadata, not part of the Store wire schema.

| Action | Parameters |
|---|---|
| `store-list` | Optional `query`, `offset`, `limit` (1–10) |
| `store-inspect` | `packageId` from discovery |
| `prepare` | New intent: `intent_id`, original `text`, `packageId`, and `workspace`; or explicit existing `agentId` instead of workspace. Optional `response`. Resume: same `intent_id` without replacing immutable parameters. |
| `operation` | `intent_id`, optional `wait_seconds` (0–20) and current `response`; accepted/running polls use two-second spacing, rate-limit backoff rises to twenty seconds |
| `dispatch` | `intent_id` and optional current `response`; refresh readiness, then reserve and send once |
| `workflow-status` | Optional `intent_id`; local journal read, available offline |
| `workflow-receipt` | `intent_id`; reconcile the saved task without resending |
| `workflow-resolve` | `intent_id`, `resolution:"do_not_retry"`; user-authorized abandonment of task delivery, available offline; no remote cancellation and no redispatch of that intent |

The state file defaults to `~/.local/state/autonomous/harness-voice.json` (`HARNESS_VOICE_STATE` override). It retains up to 64 conversations and 256 Store intents per conversation without automatic eviction of delivery evidence. A full journal refuses new intents. An existing task blocks duplicate dispatch even after explicit abandonment. No preparation/task data is deleted by resolution. Read-only status/list/recap are available for inspecting the existing agent when delivery is uncertain.

## Durable intent and recovery

The device helper stores the original user text, stable intent ID, immutable preparation parameters/key, operation ID, returned machine/agent/workspace, a **separate task key**, delivery reservation/receipt and server instance information in its private state journal. It uses the same state path/conversation locking as normal `harness-use`, atomic replacement, file and directory fsync. Never delete this journal to retry a request.

Preparation timeout may mean the operation was accepted. Resume the saved intent, retrying identical preparation parameters and key if its operation ID is missing. With an ID, poll `operation.get`. Never manufacture a new intent/key because a timeout, disconnect, rate limit, missing operation or `needs_user_action` occurred. A deliberately new intent may create another session. Pairing/machine identity changes must not redirect saved work.

Task dispatch reserves its task key before sending the original text to the prepared agent. A ready preparation is refreshed before dispatch; the returned explicit target is independent of Desktop focus. Once a task send has been attempted, a repeated dispatch does not send again. Reconcile with `workflow-receipt`; an absent receipt after daemon restart cannot prove non-delivery. Surface uncertainty and inspect the existing agent/task instead of retrying. Receipt state and the observed server instance are retained; preparation idempotency does not make Harness's in-memory turn deduplication durable.

Known task receipts (`queued`, `delivered`, `started`, `completed`, `rejected`) end the skill turn with `NO_REPLY` when a response route is present. Preparation `accepted` or `ready` is not such a receipt and must not trigger silent task loss. `ready` only permits a separate dispatch; the actual agent result still arrives through the existing lifecycle/question/recap path.

## Progress and user action

There is no new Store progress event in v1. Progress comes from `agent.prepare` and `operation.get` snapshots. Poll around every two seconds and back off on rate limits. The helper exposes the saved state for follow-up/restart recovery; it is not an unattended background scheduler and does not automatically send work when connectivity returns.

Local `response:{run_id,channel}` metadata associates preparation observations with the current device turn. OS removes it before E2EE transmission, displays progress in the existing monitor/chat stream while the original main-agent run is active and records `harness_store_progress`. Preparation does not take ownership of the final reply or suppress the main agent, so it can explain `needs_user_action` and reported guidance. Repeated polls do not speak through TTS. Identical consecutive snapshots for the same operation and active run produce no extra chat text. Changed progress is separated by paragraph breaks; deduplication is in-memory and scoped to active turns, never task-delivery evidence. The main agent describes actual observed progress in the user's language; it never calls a ready agent a completed deliverable.

`accepted` means intent reserved, `running` means preparation in progress, `ready` means preparation completed, `failed` means inspect the error, and `needs_user_action` means show error/guidance and preserve the operation. An agent ID can already exist in an action-needed operation: direct the owner to that agent instead of creating another. Unknown errors remain visible and are not success. Tool approvals still belong in Desktop; `question.answer` cannot approve them.


Preparation waiting is bounded independently of individual RPC timeouts. The helper persists a **90-second budget per response run**; repeated invocations and reconnects do not renew it. `PREPARATION_WAIT_EXPIRED` ends polling and asks the main agent to explain that the task has not been sent; the remote preparation and journal remain intact. A new user turn can resume the same intent/operation/keys with its new response route. No timeout is permission to create another agent or dispatch a late task.

The OS additionally starts a **120-second deadline** on the first routed prepare/poll. Expiry and task admission are serialized: expired routes cannot dispatch; already admitted tasks are never cancelled by this preparation watchdog because their delivery may be uncertain. Native Hermes implements optional `RunExpirer`: expiry stops only the exact current owner and ends with `lifecycle.error`, normally after bounded remote cleanup (up to 30 additional seconds), not task success. A newer steered request cannot be cancelled by an older deadline. Other runtimes currently have the helper budget and dispatch gate but no guaranteed runtime cancellation; unsupported cancellation is logged. OS deadline bookkeeping is in memory; the helper budget is durable. The guard does not retry dispatch or alter the Store wire contract.

## Validation boundary

OS unit/contract tests use pinned schemas, the synthetic Blender fixture, fake request results and local temporary journals. They cover durable recovery and transport/progress behavior without installing Store packages, running Blender, creating paid agent sessions or connecting to a robot. These tests do not certify a live CLI, engine login or generated artifacts.

End-to-end acceptance waits for an explicitly authorized CLI update and a real hello advertising all four capabilities. Then verify discovery → inspect → prepare/poll → separate task send with the paired device, recording actual guidance and artifacts. This source change does not deploy to robots or perform that acceptance run.

Reproduce OS checks (temporary dependency environment only):

```sh
python3 -m unittest discover -s skills/harness-use/tests
uv run --no-project --with jsonschema==4.26.0 python -m unittest discover -s skills/harness-use/tests -p contract_validation.py
go test -race ./system/harness ./system/server ./system/server/agent/delivery/http ./system/server/sensing/delivery/http
```

The first command is dependency-free; the schema validation command is separate so missing `jsonschema` cannot silently skip contract validation. The Go fixture transport test uses a real localhost WebSocket and encrypted payload with a mock peer, not a running Harness CLI.
