# Realtime Voice Agent

Low-latency, speech-to-speech voice layer that runs **in parallel** with the
normal STT → agent pipeline. The realtime model handles casual conversation
directly (sub-second audio replies) and **delegates** anything that needs the
main agent (device control, skills, memory, real-time facts) back to the
OS-server flow.

Code lives in `hal/realtime/`; it is driven by
`hal/drivers/voice/voice_service.py`.

> **Source of truth:** this doc reflects the code. If they disagree, the code wins.

## Execution completion telemetry

For [voice KPI-3](voice-metrics.md#kpi-3-execution-completion-not-correctness),
`TurnDoneEvent.execution_completed` defaults to `false` and becomes true only
for a provider terminal signal: Gemini Extended Thinking uses `IDLE` with accepted answer text and no unresolved local work; sessions without status use normal `generation_complete` or
`turn_complete` without interruption, or OpenAI `response.done` with
`response.status == "completed"` and no interruption seen during that response, or GPT-Live's **synthesized** boundary
(`turn_gap_ms` of output silence with no user overlap — the Live wire has no
terminal, see *GPT-Live*). Connection close, send failure, synthetic
unblock, partial output followed by timeout, stale/replayed done, and an
abandoned receive are not completion evidence.

The base receive loop passes the observation to the orchestrator, which
snapshots it before recycling the session, then to
`RealtimeTurnResult.execution_completed`. HAL emits `realtime_turn_done` only
when the turn is handled and this flag is true. A memory-sync backend run
cannot establish completion for that realtime turn. This measures execution
ending, not answer correctness or full audio playback, and changes no routing
or playback behavior.

## Concept: handle vs. delegate

Every spoken turn is streamed to the realtime model *at the same time* as the
STT pipeline. At end-of-turn the model either:

- **Handles** the turn itself — chit-chat / quick answers — speaking back
  through TTS with no round-trip to the main agent, or
- **Delegates** by calling the `delegate_to_main` tool, which stops realtime
  output and forwards the current user's faithfully understood words, in their
  spoken language, to the OS server (→ the selected main runtime) for the work.
  The realtime model has already spoken its own filler by then, so os-server
  does not acknowledge a delegated turn (`[voice-instruction]` prefix) again:
  no opening filler, and the first dead-air filler only arms when the main
  agent's first tool starts (`FillerManager.MarkDelegatedVoiceRun`). A
  delegated turn the main agent ends with NO_REPLY therefore stays silent
  instead of promising an answer nobody delivers.
- **Explicitly rejects** a high-confidence non-user turn by calling
  `reject_turn`, which drops the turn before the main agent sees its STT text.
  This is deliberately different from a silent completion: silence, timeout,
  and transport failure still use the normal main-agent fallback.

**Finding things is an action.** "Find my keys", "where is my cup", "can you help
me find my pen", "do you see my pen anywhere" — any request to locate a physical
object or person is a camera-and-servo search the main agent runs
(`/servo/search`, see `robots/lamp/docs/vision-tracking.md`). The realtime layer
must delegate it in every phrasing. Device-observed 2026-09-15 (lamp-ac82, clean
memory): the bare imperative "Tìm cây bút cho tôi" was delegated and the pen was
found, while the question forms "Bạn có thấy cây bút của tôi đâu không?" / "Giúp
tôi tìm cây bút được không?" were handled by Gemini itself — it asked what the pen
looked like, guessed a location, or offered to look without looking. The rule lives
in three places that must agree: the shared `delegate_to_main` tool description,
the `look` tool description (a find is not a look), and a **Finding things is an
action** bullet in all four provider prompts; `hal/test/test_realtime_find_delegation.py`
pins the text. The decision itself is not enforced in code — only real speech on
a device exercises it.

### Research, analysis and documents delegate

Grounding answers ONE fresh public fact. Multi-step work — research, a
comparison, "which option should I pick", brainstorming, anything that ends in
a report or document — is not a lookup, and answering it in two spoken
sentences is the failure this rule removes. It is listed as a **Research,
analysis & documents** bullet in every provider prompt and as a sentence in the
shared `delegate_to_main` description. The two prompts that can search
in-session carry the matching "single facts only" boundary next to their own
search rule: Gemini's Google Search bullet, and the GPT-Live **backend** stage's
`web_search` rule (its voice stage has no search and simply delegates). What the main agent then does with the request is its own
business — it may have a research skill installed, or it may answer as best it
can and say what it could not verify; either way the work belongs on the main
lane. `hal/test/test_realtime_research_delegation.py` pins the text.

### Channels and connectors delegate

Sending anything out — a message, a photo or camera capture, a picture from the
web, a file, a link, or a recap / summary of the conversation — through
Telegram, email, Slack or any other channel or connector is main-agent work:
only the main agent holds the channels, so the realtime layer can neither send
nor know what is linked. The same goes for the capability question ("after we
talk, can you send me a recap somewhere?"): it is a question about the main
agent's channels, not about the voice persona. Device-observed 2026-09-21
(lamp-0c89): Gemini Live answered "send this picture to my Telegram" by itself
(describing the picture, or going silent) and produced nothing for the recap
question, so the request never reached the main agent. The rule is a
**Channels & connectors** bullet in every provider prompt (the GPT-Live voice
stage also lists channels under its backend tools, and its backend stage carries
the boundary next to `web_search`), a sentence in the shared `delegate_to_main`
description, and — in Gemini's Google Search bullet — the note that a lookup
ending in delivery ("find a photo of X and send it to my Telegram") is not a
lookup. The prompt must never claim it can or cannot send, never say it was
sent, and never describe the picture instead of sending it. Each prompt names
the target in its own vocabulary (the shared/OpenAI/Pipecat prompts say "the
main system", Gemini says "the main agent", GPT-Live says "the backend").

### Addressed speech before persona or actions

All realtime provider prompts give the addressed-speech policy priority over
`DEVICE IDENTITY` / SOUL instructions to respond to ambient speech, show empathy,
or express emotion. A meaningful question, a name mentioned to someone else,
or an open follow-up window does not establish that someone is talking to the
device. Genuine conversational follow-ups do not need to repeat its name.
Confidently overheard speech calls only `reject_turn` when available, with no
voice/text, emotion, movement, look, or delegation. The tool description allows
rejecting an overheard request even when the device could fulfill it. Uncertainty,
silent completion, and errors retain the existing fallback behavior.

The Gemini prompt additionally requires audio evidence before interpreting a
request: do not complete noise/echo into words or repair an unrelated transcript
using the date, location, memory, or conversation history. Unexpected foreign
language input does not authorize translation; proper names and technical
loanwords in a clear configured-language request remain valid. Examples cover
spurious Spanish travel-agency text and Korean text incorrectly answered as a
date question. Unclear input stays silent without changing uncertain-turn
fallback or `reject_turn` eligibility. This prompt change cannot guarantee
transcription accuracy or prevent all hallucinated history entries.

Voice startup and realtime share the context-manager registry for runtime identity. The manager declares the authoritative name file: Hermes uses `SOUL.md`; OpenClaw, PicoClaw, Codex, Claude Code and OpenCode use `IDENTITY.md` in their active workspace. HAL reads the explicit `**Name:**` card afresh on each voice start, so a HAL restart restores personalized wake aliases without waiting for an OS rename notification. Persona prose and inline examples do not supply a name; missing/empty identity falls back to the device type. Permanent device/Autonomous aliases remain alongside the agent name. The realtime identity instruction uses the same declared file. This fixes the observed UI “Mini” / HAL “lamp” mismatch after restart; it does not change STT hypotheses or wake/follow-up policy.

Gemini also distinguishes an isolated form of address (for example Vietnamese “anh”, “chị”, “em”) from a clear call to the device. Without a request, a clear summons or relevance to a pending question, it should reject silently instead of inventing an “I’m listening” greeting, emotion or completion call. Short commands and contextual answers remain valid, including the same word when it answers the device’s question. The final Gemini routing reminder repeats this rule after memory context. This is a prompt policy, not a deterministic blacklist or a guarantee of model compliance.

The address rule also prohibits inferring an identity/address correction or remembered preference from an isolated title solely because of earlier assistant wording or an interruption. Explicit corrections and answers to an immediately pending address question remain valid. This targets the observed “Chị” → apology/“I remember” misinterpretation; local prompt inspection does not establish model compliance, which still requires real-session evaluation.

Lamp's `robots/lamp/SOUL.md` applies the same addressed-speech prerequisite to
main-agent voice and `[ambient]` messages. Overheard speech or an unclear
addressee requires exactly `NO_REPLY`, without tool calls or physical/emotional
reactions. This overrides the persona's general reaction and expression rules.
These are model instructions, not a deterministic speaker-verification gate;
real-room conversation testing is still needed. Existing device SOUL files must
receive the updated policy before the main agent can use it.

The `delegate_to_main` tool is registered automatically by the orchestrator
(`orchestrator.py`, `DELEGATE_TOOL`).

**What the main agent is matched against.** `turn_dispatch.py` composes the
sensing message as `[voice-instruction] <delegate message>` followed by
`[transcript] <local STT text>` whenever a transcript exists — the paraphrase
leads, the user's own words follow. Both halves matter because every
`SKILL.md` trigger is matched on **vocabulary**: the same request reached the
lamp as *"maximum capability in scanning around"* (matched the servo skill) and
as *"movement demonstration … rotation/tilting"* (matched nothing and fell
through to a canned emotion). The transcript cannot rescue a turn whose STT was
garbage, so the tool description also tells the model to keep the user's key
words rather than renaming the request into a category — a prompt instruction,
not a code guarantee. `test_turn_routing_log.py` pins the composition; nothing
can pin the model's compliance.

### Voice control through Harness

OS Monitor offers **Harness-only voice**, a RAM mode defaulting to OFF after OS-server restart. Manual captures bypass Realtime and the main runtime, retaining existing focused-agent routing, output TTS and external-history synchronization. Gesture activation requires pairing, connection and valid app focus; `focus.ensure` can select the first local agent when no focus exists. Failure leaves mode off. Web/MQTT explicit sets retain their existing semantics.

Harness ON uses manual tap-to-record capture, not ambient listening. A tap while TTS is speaking only interrupts playback. Otherwise, the first tap starts capture; the ready beep plays only after the recorder/STT is ready. The next tap closes capture and sends one finalized STT transcript through the existing OS route to the focused Harness agent. Silence never sends automatically. Reaching `MAX_SESSION_DURATION_S` (`HAL_MAX_SESSION_DURATION_S`, default 30 seconds) cancels without dispatch. Idle mode does not record surrounding speech. Mode, generation or focus changes and privacy/stop events discard capture; a focus swipe cancels capture before changing focus. Sleep and hardware microphone privacy remain authoritative.

On MPR121-equipped lamps, Harness OFF retains the existing gestures: swipe **right to left** to enable Harness and **left to right** to sleep. Harness ON replaces the old click, triple-tap reboot, shutdown/reset holds, sleep and listening-cue actions: tap controls capture or interrupts TTS, holding **for 3 seconds** immediately disables Harness and announces the result (including while offline); the remaining contact is ignored until release, swipe **right to left** selects the next agent and **left to right** the previous agent. `hal/drivers/harness/gestures.py` owns this separate gesture policy; `hal/drivers/voice/_internal/harness_capture.py` tracks manual capture ownership. GPIO/TTP223 behavior is unchanged. Direction follows the physical left-to-right `swipe_axis` (Lamp defaults E0…E11; verify mounting). Python calls Go APIs; Go owns mode/focus and the existing voice route.

Each capture carries its mode generation. OS refuses a stale generation rather
than delivering an utterance to a newly focused agent. OS mirrors app focus even
while off without changing ordinary voice generations. When enabled, a focus
change advances the generation; dispatch refreshes focus and includes an opaque
`focusRevision` for the CLI to check before reservation. There is no web agent
selector or automatic fallback during voice dispatch. An active realtime live
session checks the mode every 500 ms and closes when it changes; an interrupted
utterance is not replayed. Mode lookup has a 500 ms timeout and fails closed if
the endpoint is unavailable or malformed, so OS and HAL must be deployed together.
The normal path below applies when the mode is off. See
[Harness integration](harness.md#harness-only-voice-mode) for management APIs,
structured question handling and uncertain-delivery recovery.

For the default Lamp persona, digital execution requests also take this route without naming Harness or an agent. Realtime silently calls `delegate_to_main` with the faithful current request; the main agent prefers an existing Harness agent or prepares a Store agent through `harness-use` when connected. If Harness is offline/unpaired before any dispatch of a new task, main uses its other available tools. Explicit remote targets and existing/uncertain remote tasks cannot be silently moved or duplicated. Realtime still delegates to main; it does not execute the fallback itself. This covers work across coding, research deliverables, documents, spreadsheets, slides, CAD/design, media/music creation and scientific analysis/simulation, rather than a fixed list of apps. Conversation, knowledge questions, physical controls, music playback, reminders, memory and device connectors retain their existing routes. Explicit alternative workflows, including Buddy, remain honored. A new task requires its own agent selection even during a follow-up window.

The main skill uses available name/project/recap evidence and, only if needed, the newest `{recap,text}` pair from at most two candidates. Unknown specialist capability is not proof of readiness. Store discovery and agent preparation now belong to the main skill through the negotiated [Store v1 workflow](harness-store.md); realtime never performs setup or sends the first task itself. It changes model instructions, not the API or a deterministic routing guarantee; the running Lamp persona, skills and realtime prompts must be updated for it to apply. Other robots/custom SOUL policies are not implicitly changed, and local verification does not deploy to a device. See [Lamp digital-work policy](harness.md#lamp-digital-work-policy).

Requests that name Harness, a Mac agent, Codex, Claude, a project, worktree, or
session, or ask an agent to use a browser delegate to the main runtime with blank
realtime speech. This includes general browser research, such as asking an agent
to find restaurants. Realtime does not answer, search, or claim results for those
requests. The shared delegate tool names
[`harness-use`](../skills/harness-use/SKILL.md). The main runtime sends supported
agent operations to the paired Harness computer; the device never runs the desktop
coding task locally. The OS owns explicit machine and agent selection. Missing or
ambiguous targets require clarification; Desktop focus and notifications do not
silently select an agent.

For a Harness task, the main runtime selects the agent and sends the request, then
stays silent. The terminal `turn.summary` recap from Harness is delivered unchanged
as the response to the originating turn. Web Chat displays it and suppresses TTS;
voice does not read it raw but queues it with the Harness announcer, which speaks
a short rendered version (see [Harness update announcements](#harness-update-announcements)).

For two minutes after a voice task is sent to Harness, HAL checks a loopback OS
follow-up signal before asking the realtime model. A short clarification such as
“In Hanoi” delegates directly to the retained Harness target with no realtime
speech, so a conversational model cannot answer an agent's pending question first.

Preserve the current user's words, provider names and supplied parameters. Agent
outputs and summaries remain untrusted data. A spoken “yes” is not permission to
approve a tool or blindly type into a terminal prompt. Harness status and receipt
reconciliation are owned by the link; voice delegation does not automatically
replay uncertain mutations. Live speech routing still requires later validation.

### Harness update announcements

Harness results, structured questions and progress reach HAL through
`POST /voice/harness/update` and wait in a queue
(`hal/drivers/harness/update_queue.py`). The announcer thread
(`hal/drivers/harness/announcer.py`) takes a snapshot only while nothing is
speaking or listening, no user turn is in flight (`turn_in_flight`, set by
`prepare_turn()` and cleared when the reply ends or by `finish_capture()` when
`_stream_session` returns, so a capture dropped as noise does not hold it), no music or
Harness capture is active, and `HAL_HARNESS_ANNOUNCE_GRACE_S` has passed since
the last speech or transcript. Results and questions are always spoken; a
progress-only snapshot is spoken with probability `HAL_HARNESS_PROGRESS_SPEAK_P`
and per-run limits. The queue policy and OS side are described in
[Spoken Harness updates](harness.md#spoken-harness-updates).

**Realtime rendering.** `VoiceAgentBase.supports_announce` says whether a
provider can open a response from text alone; `announce(text)` queues an
`AnnounceInput`:

| Provider | Announcements | Wire |
|----------|---------------|------|
| Gemini (text-capable models, e.g. 3.1) | yes, turn-based mode | `send_client_content(role=user, turn_complete=True)`; `_turn_done` is cleared so the next commit waits for this response. Refused with an immediate `TurnDoneEvent` while a tool call is pending or a user activity is open. |
| Gemini 2.5 native-audio | no | Same wire-shape guard that drops `send_text` (WS 1011). |
| `pipecat_v1` | yes, turn-based mode | New generation and user-turn ID (past any `end_turn()` fence), then `LLMMessagesAppendFrame(run_llm=True)`. Refused while a committed turn awaits its reply or a response or tool call is open; an uncommitted manual turn left by a noise-dropped capture does not block it. |
| OpenAI Realtime, GPT-Live | no | Not implemented; the fallback below speaks. |
| Any provider in live mode | no | The live pump owns the output queue. |

`RealtimeOrchestrator.prepare_announcement(allow_resume)` readies the session the
same way `prepare_turn()` does (parked resume, unresolved-tool rebuild, Gemini
pre-turn idle recycle) but refuses while a user turn is in flight, and never
resumes a parked session for progress. It waits (up to `PREWARM_JOIN_TIMEOUT_S`)
for an in-flight rebuild such as a noise-drop reconnect, and replaces a Gemini
session that still holds a manual-VAD activity no capture owns any more.
`announce(text, stop_event)` flushes stale
output, sends the input and yields exactly like `stream_output()`, with the
silent-turn watchdog raised to `ANNOUNCE_RECV_TIMEOUT_S` (20 s; the pipecat relay
measured 11.6 s to first token on a cold 12.5k-token context);
`realtime_announce.play_realtime_announcement` plays the reply like a normal
realtime turn (native model audio with owner `run:<run id>`, or sentence-split
TTS with `realtime_reply`), preceded by the Harness result chime. The spoken
reply is saved to realtime memory as a `[Harness update]` turn.

**User preemption.** `prepare_turn()` sets the running announcement's
`stop_event`. The announcement stops yielding, its queued speech is cancelled,
and the orchestrator drains the rest of that response (up to
`ANNOUNCE_DRAIN_S`, 3 s) and calls `end_turn()`. The user's `flush_output()`
waits for that drain, so the two never read the output queue together and none
of the announcement is spoken during the user's turn. An announcement that is
preempted before any speech (including one skipped because a capture began
right after `prepare_announcement()`) returns its results and questions to the
queue; progress is dropped.

**Fallback.** When no realtime rendering is possible or the model returns no
speech, the realtime summarizer model (`RealtimeSummarizer` with a speech
prompt, `HAL_HARNESS_ANNOUNCE_SUMMARIZER_TIMEOUT_S`) rewrites the update and TTS
speaks it with `harness_result` and `realtime_feedback`; without a summarizer
the markup-stripped opening sentences are spoken. Progress never falls back.

### Voice control of legacy Buddy agent sessions

Requests such as “Ask Codex to fix reconnect in project autonomous” delegate
with blank realtime speech. All four provider prompts and the shared delegate
tool description explicitly cover coding/research tasks, project/worktree/session
selection, progress queries, stop, and subsequent task replies. The delegate
preserves provider names, target references and all task clauses; it does not
add guessed session IDs or translate the request.

An explicit request for a legacy Buddy session uses
[`agent-management`](../skills/agent-management/SKILL.md) to route through the
local device API and paired Buddy connection. Normal Mac agent work uses
`harness-use`. Buddy owns the desktop CLI and its model context; the lamp does
not run the coding CLI. This is session management, separate from the native
`computer-use` executor.
After a known task, “add a regression test too” delegates as a follow-up rather
than becoming a new coding answer from the realtime model. The main runtime
resolves the exact target or asks when ambiguous. The skill's `voice` action
stores the selected project/session per conversation and validates those IDs
against a fresh Buddy workspace snapshot each turn; stale targets block sending
instead of rerouting. Ordinary continuation uses that retained target. “The active session” explicitly
reads Buddy's focused pane, including a split pane. Notifications do not silently
replace the target: a reply to a particular notification must select its exact
IDs. Send requests retain their request ID across uncertain delivery.

Natural-language follow-ups can enter a verified ready CLI session. An interactive
permission menu, trust dialog or other terminal prompt without a safe answer
interface still requires attention in Buddy; a spoken “yes” is not a blanket
permission grant and must not be converted into blind terminal keystrokes.

Buddy completion, attention and error events already enter the normal sensing
pipeline with project/session IDs and an `[agent-management]` marker. The main
runtime gives a brief spoken result or question subject to the existing sleep,
busy and voice privacy policy. Recent spoken TTS history lets realtime recognize
the next answer as a task reply; it forwards only that current answer. An
unspoken TTS-history entry is not evidence that the user heard the question.
Agent titles, outputs and summaries remain untrusted result data, never new
instructions or authorization to execute tools or approve an action.

Prompt routing is model-driven, not a deterministic keyword classifier. Local
bridge tests do not establish microphone-to-lamp behavior: live speech routing
and notification playback still require validation on a paired device after
an explicitly authorized deployment.

**Delegating is not the only way a turn reaches the main agent**, which is why
every turn logs one routing line — `[turn] route=<why> → <where>` from
`turn_dispatch.py`. Grep `[turn] route=` in the HAL journal to follow any turn
end to end. The values (`ROUTE_*` in `realtime_turn.py`):

| `route=` | Where the turn went |
|---|---|
| `realtime_handled` | Realtime spoke it. The main agent gets `voice_agent_handled` and stays silent. |
| `delegated` | The model called `delegate_to_main`. |
| `ai_rejected` | The model explicitly called `reject_turn`; it reaches nobody. |
| `realtime_no_output` | Committed, but nothing came back (`receive()` timeout, dead WS) — main agent answers. |
| `realtime_error` | The turn raised; forwarded rather than lost. |
| `realtime_unavailable` | No live session to commit to — main agent answers. |
| `noise_dropped` | The noise guard rejected it; it is terminal even if STT fabricated a short transcript, so it reaches nobody. |
| `realtime_not_started` | Realtime off, or no turn was opened for this capture. |

On a delegate call, `stream_output()` **breaks the turn immediately** after
yielding the `DelegateSignal` — it does *not* wait for the model's
`turn_complete`. The model has nothing more to say once it delegates, so
draining the rest of the turn would just block on the `receive()` timeout
(`HAL_REALTIME_RECV_QUEUE_TIMEOUT_S`) — the model stays silent for the full
window, adding that many seconds of latency before the main agent even sees the
request. The function result is already sent back to the model before the break;
the dangling open turn is cleared by the next turn's `flush_output()`.

Gemini can similarly emit `generation_complete` before `turn_complete`: the
latter is delayed while Gemini assumes the client is playing its audio in real
time. For BLOCKING models, HAL plays the generated response itself and ends the
consumer turn on `generation_complete` and releases the next manual-VAD commit immediately.
This avoids an otherwise unnecessary silent-watchdog delay after the reply;
any late `turn_complete` is discarded before the next turn.

**Lifecycle note (investigated 2026-09-22, implemented 2026-09-23):**
For Gemini Extended Thinking, `IN_PROGRESS` keeps the same interaction open
across fillers and tools. `IDLE` ends provider execution. When accepted answer
text exists and no local tool or withheld continuation remains, HAL closes the
turn without a second LLM classification. Explicit routing and interruption
retain priority; empty output or unresolved local work still falls back. This
is execution evidence, not a guarantee of semantic correctness. Sessions without
status keep the bounded tool grace and independent outcome check below.

For Gemini `extended-thinking` models, tools are `NON_BLOCKING`: a spoken filler
such as “I can help with that.” must not consume the user's task (#453).
The provider adds `complete_response`, which confirms that a direct spoken
answer fulfilled the request. Conversation, knowledge, or a completed public
lookup can use this confirmation; actions, promises, errors, and unresolved work
must delegate. A direct answer needs a confirmed outcome and a successful
provider terminal before it counts as handled/completed. Text/audio alone is
not completion evidence.

For a session without interaction status and a spoken response with no routing decision, HAL starts an independent text
check during the existing grace, using the configured realtime summarizer model,
endpoint and credentials. It checks the original request, spoken answer and public
search evidence, with no tools or speech output. Exact `COMPLETE` accepts a
fully answered conversational/public lookup turn. Exact `CLARIFICATION` also
accepts the current spoken turn when an information request needs missing user
input and the response asks a specific, necessary follow-up question. For example,
answering the Bitcoin price and asking which city to use for the weather keeps
the conversation in realtime. This handles the current turn; it does not mark
the whole request complete. Merely stating inability, without a necessary
follow-up question, remains `INCOMPLETE`. Fillers, errors, account access,
physical actions, research still to do, and mixed requests with unresolved
execution work (such as music playback or reminders) retain fallback, even if
the response also asks a question. Timeouts,
missing credentials and malformed replies provide no independent confirmation.
The initial check uses one small text-model call with a separate
`HAL_REALTIME_OUTCOME_TIMEOUT_S` deadline (default 10 seconds from its first
terminal). It overlaps the ordinary 6-second tool grace. A later, terminal-ended
answer gets a fresh bounded continuation check: at most the outcome timeout,
and no later than the last answer chunk plus the receive gap (default 8 seconds).
With verified progress, it also stops at the 15-second commit-based deadline
unless actual answer chunks continued past that deadline.
An empty initial terminal no longer leaves this check with a zero deadline.
Verified progress also bounds pending initial classification at the progress
deadline. A real routing
call cancels the check; an explicit delegate still takes priority over either
accepted check result. Explicit `INCOMPLETE` overrides an erroneous
`complete_response` after a filler; an unavailable check preserves the provider
decision, or fallback if there is none. If continuation speech is being withheld,
an unavailable check retains fallback even with `complete_response`: that speech
has not reached the user and cannot count as a delivered answer.
A confirmed answer or necessary clarification follows
`realtime_handled` → `voice_agent_handled` → history sync,
without main-agent execution. The semantic check is model-based, not proof that
all factual claims are correct.

Filler text/audio still streams immediately for KPI-1 (Voice Acknowledge).
HAL ordinarily waits for routing tools up to `HAL_REALTIME_NONBLOCKING_TOOL_GRACE_S`
(default **6 seconds**, `0` disables the wait, not the outcome requirement).
The grace starts at the first `generation_complete` or `turn_complete`; later
terminals alone do not extend it. Verified Google Search queries/chunks or
non-routing, non-emotion tool work can extend unfinished-outcome waiting until
`HAL_REALTIME_PROGRESS_TIMEOUT_S` (default **15 seconds from audio commit**).
Repeated progress does not restart that budget; pending work is capped there
even if the first terminal arrives late. An accepted outcome stops this progress
extension. Without
verified progress, non-Live extended-thinking keeps the ordinary **8-second**
output-gap watchdog: thoughts, usage frames and generic heartbeats are not
proof of useful progress. This change adds no new filler schedule; the existing
once-per-turn acknowledgement remains. The stricter progress watchdog applies
to Gemini extended-thinking with Live off; OpenAI, GPT-Live and Pipecat keep
their existing behavior.
HAL reopens the SDK's per-turn `receive()` iterator
on the same session after `turn_complete`, retaining the logical turn identity.
Delegate/reject/end-conversation calls end the grace early; `complete_response` records
confirmation but keeps the window open for a delegate in a subsequent frame; auxiliary
tools do not confirm completion or suppress a later delegate. After the first
provider terminal, HAL holds subsequent text/audio instead of playing it. A later
terminal or `complete_response` can trigger a check of the initial speech plus
this continuation. HAL releases the held output only at grace finalization,
without delegation or interruption, when the initial response was empty or independently
`INCOMPLETE` and the combined response receives an accepted semantic result.
An already complete initial answer keeps the duplicate-speech guard; an
unconfirmed check of existing initial speech does not release the continuation. An explicit late
delegate/reject wins while routing is still being received, so held speech is
never played before that decision.

The continuation buffer is capped at 2,000,000 PCM bytes and 16,000 text
characters; overflow prevents its release. Additional speech invalidates an
earlier continuation check until another terminal/confirmation permits a new
check. Actual answer audio/text chunks keep generation alive with a bounded
receive-gap idle window (default 8 seconds); an answer still arriving can finish
beyond the original grace and the 15-second progress budget. A terminal closes
that generation window and permits the bounded continuation check above; metadata
alone cannot keep it alive. This permits a verified
answer after a filler without replaying a completed answer or appending an
unconfirmed error/account-access denial to speech or history. A receive error
also requests main-agent fallback for this model family, even if a filler already
played.

If both waits finish without an outcome on an uninterrupted turn, HAL emits
`MainAgentFallbackOutput`, then `DelegateSignal`, preserving the original
provider transcript and turn identity. This local fallback invents no function
call and sends no tool ACK. It routes as `delegated`, never `[HANDLED]`, even
when a filler has already played; completion must come from the downstream task.
For delegated turns with a realtime spoken transcript, dispatch adds
`[realtime-handoff]`: this is an active, unresolved request, not handled history.
The main agent should resolve it or ask for missing context, and must not choose
`NO_REPLY` merely because realtime already spoke. This is scoped handoff context,
not a global override of silence/noise decisions. Both explicit delegation and
local fallback can also carry `[realtime-context]`: up to 6,000 characters of
available search queries, source titles/URLs/snippets, and already spoken text
(up to 2,000 characters). Source metadata is best effort, not a guaranteed full
search-result body. Dispatch keeps this JSON-quoted, untrusted reference separate
from the user transcript; it is neither an instruction nor proof of execution.
The main agent can check and reuse it and avoid repeating delivered speech.
After fallback with initial speech, buffered continuation or verified progress,
or an explicit delegate following realtime speech, Gemini marks the session
`requires_fresh_session`; the existing `prepare_turn` rebuilds it before the next
capture. This prevents old terminals or grace output from consuming the next
user input. Completed direct answers and empty handoffs without buffered output
or verified progress do not gain this recycle requirement.
BLOCKING models keep their existing immediate-completion behavior. The Gemini
prompt allows one immediate, brief acknowledgement before a delegate; rejection
remains completely silent. Email/account/connector requests, including "your
email", belong to the main agent: Gemini may acknowledge neutrally but must not
claim that accounts or access are present or absent, or use its device persona
as a reason to refuse. Only the main agent checks actual connector state.

Actual tool calls use ordinary function response acknowledgements, omitting
`scheduling`: on 2026-09-21 the device's Gemini 3.8 extended-thinking backend
rejected `SILENT` with WebSocket 1007,
`Function response scheduling is not supported for this model`. NON_BLOCKING
tool support does not imply response-scheduling support. Calls arriving after
the grace deadline are not guaranteed to be received, and the model can still
misclassify a request by explicitly calling `complete_response`; the outcome
gate prevents missing calls from silently consuming tasks, not all semantic errors.

The gate itself is `wakeword` in `config.json` (Settings → "Require a wake word
before handling speech"). A device being set up for the first time takes its
initial value from the body's `voice.wakeword` in `robots/<type>/ROBOT.md` —
lamp declares `true`; a body that declares nothing stays always-listening. A
device provisioned before the key existed keeps always-listening across
upgrades: os-server only adopts the ROBOT.md default while `config.json` has no
`wakeword` key at all.

Accepted phrases are `hello|hey|hi|alo|okay|ok|wake up` + `autonomous`, the
device type (`lamp`), or the agent name from IDENTITY.md — HAL resolves the
device type from the `DEVICE_TYPE` env first, then `config.json`, so the
runtime list matches the one Settings advertises.

A mic session is a continuous stretch of speech, not a single sentence, so the
match is **per sentence**: `starts_with_wake_word()`
(`hal/drivers/voice/_internal/speaker_decorate.py`) splits the transcript on
`.` `!` `?` and accepts a wake phrase at the **start or the end of any
sentence**. Mid-sentence occurrences are still rejected — a device name in the
middle of a sentence is people talking *about* the device ("this lamp is
nice"), and opening the gate there would make it barge into someone else's
conversation. The end-of-sentence position is accepted because calling the name
last is a natural vocative ("what time is it, hey lamp?"). Without the
per-sentence rule a turn like "What was the score of the Vietnam versus
Malaysia match? Hi lamp, can you hear me?" was dropped whole and the user just
heard silence. The function keeps its `starts_with_wake_word` name because
every caller reads it as "was this turn addressed to me?".

The final-result confirmation runs on the **assembled** transcript, which still
carries its punctuation. `merge_stt_hypothesis()` keeps only `\w+` tokens and
therefore strips sentence boundaries, which would collapse the whole turn into
one sentence and retract a gate that a partial had correctly opened. So before
dropping a turn whose partial armed the gate, the capture loop re-checks
`starts_with_wake_word(combined)` on the real transcript: a match sets
`wake_word_confirmed` and logs `Wake-word confirmed on assembled transcript`,
and only a genuine mismatch drops the turn.

All three names are sent to STT as boost terms (`_stt_boost_terms`), because a
mis-heard name silently drops the whole turn — "hi lamp" transcribed as "hi
lance" never arms the gate. Flux takes them as repeated `keyterm` parameters
with no weights; nova-3 uses `keyterm` too; older nova models use `keywords`
with the `:3` intensifier.

Every STT-final-confirmed wake-word turn reaches dispatch. It opens a 20-second
follow-up focus window, so the next spoken turn can omit the wake phrase and
is sent as `voice_followup`. For an authorized turn, the idle countdown starts
when processing and its owned TTS queue have both finished, not at dispatch or
the first filler. Vision/grounding, main-agent work, synthesis and queued answer
chunks therefore do not consume the user's reply window. The configured
`HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S` remains unchanged.

This applies to turn mode and LIVE mode, including native realtime playback.
Wake disabled still accepts speech normally; timeout 0 disables follow-up focus.
An unknown/unaddressed interaction, Harness capture, web reply, music or ambient
TTS cannot acquire a conversation hold. Button/gaze grants retain their existing
limits; a valid subsequent voice turn gets the normal follow-up window. A filler
cannot open focus or finish a still-processing turn. Cancellation releases only
that turn's hold, and stale completion cannot renew it. Stop/mute clears holds.

Main processing is paired through `POST /voice/followup/activity` with
`interaction_id`, `run_id` and `phase` (`start`, `end`, `cancel`). HAL accepts only
IDs already authorized by its voice gate. OS sends `end` after all reply TTS
submissions are admitted; HAL additionally waits for physical playback to drain.
Silent/error completion starts the idle timer without inventing audio. Holds
expire after five minutes if a terminal is lost; activity delivery is best effort
with a 250 ms HTTP timeout. Deploy HAL and OS together for the main-agent path.
The `[wake] Follow-up idle window started` log records the countdown boundary.
Voice KPI eligibility, timestamps and routing are unchanged.

After the speech-end gaze check, HAL refreshes the capture's focus latch before
opening realtime, including captures with a nonempty transcript. A gaze grant
at the end of a sentence therefore authorizes that same sentence for realtime,
instead of only authorizing downstream main-agent dispatch. Existing focus stays
latched if it expires mid-sentence; the noise guard still applies.

That window is latched at session start and refreshed during capture and at
speech end for **dispatch**, so a window that expires mid-sentence cannot cut
off someone already speaking. The cues that
claim to be the addressee — the listening LED, the backchannel — ask
`is_addressed()` instead, which re-reads the window **live**. Gaze is why: it
can open the window in the middle of the very sentence it acknowledges.
Device-observed 04/09/2026 on lamp-0c89 — at speech start the camera had no
face evidence yet (`of 0` samples) so the latch was False, and the watcher only
confirmed the user 3.6 s later at speech END. The whole turn ran unaddressed:
no listening cue, no realtime turn (`route=realtime_not_started`, so no
thinking cue either), and the device sat dark through the sentence and lit up
only for the next one. Reading live can only ADD an addressed turn, never
retract one — the latch is still consulted first. A follow-up has
the same user priority as `voice_command`, but remains separately observable.
When realtime already spoke, dispatch sends a `voice_agent_handled`
synchronization event so the main agent records the exchange but stays silent;
unavailable, failed, timed-out, or delegated realtime takes the normal
main-agent path. This also consumes a one-turn vision handoff, so a temporary
Gemini failure cannot drop a voice command or leak a frame into the next turn.

For a newly granted gaze wake, VAD has already confirmed both speech and visual
intent before STT can produce its first partial. HAL therefore paints a dim,
LED-only blue breathing acknowledgement immediately. It does not freeze the
body or claim `listening`; the first partial upgrades it to the normal listening
cue. A no-partial session restores the preceding LED state when it closes (or
after a 3-second safety timeout), so ordinary VAD/noise sessions remain dark.

A realtime-handled turn also takes the speaker away from the main-agent turn
still in flight, not just its own. Its own run is silenced by `MarkSilentRun`;
the older one is silenced by a second cancel watermark (`autoSpeechWatermarkMs`,
see `docs/os-server.md`), stamped the moment the event arrives. Without it the
device answers the user's newest question in the realtime voice and then, a
moment later, answers the previous one in the main agent's voice. The main
agent runs one turn at a time, so this is a single stale answer rather than a
backlog.

The hook sits **before** the busy fork in `PostEvent`, not next to
`MarkSilentRun`. `voice_agent_handled` counts as passive, so a busy agent
queues it and returns early — and "the agent is busy" is exactly the case with
an older turn in flight, which made the later placement a no-op precisely when
it was needed.

The mark is deliberately weaker than the physical click: it never drops the
older turn's `[HW:]` markers, because an action the user genuinely asked for
must still run. Its pending fillers **are** dropped, though — the dividing line
is speech-versus-hardware, not click-versus-auto. A filler is a promise that an
answer is coming, not something the user requested, and leaving it armed
reproduces what the click had to fix: the device answers the new question, then
says "one moment" about the old one and goes quiet. The behaviour is opt-in per body: set `OS_REALTIME_SUPERSEDES_MAIN_REPLY=1` in the body's
`/opt/hal/.env` (os-server loads that file too). The code default is OFF,
because that default is what every body which has never heard of the switch
gets — lamp, intern-v2, reachy-mini, and any body with no `.env` at all.

Known gap, shared with the physical click: an event queued while the agent was
busy is given its runID at **replay** time, so it lands on the far side of the
mark and speaks even though the question predates it.

### A muted reply still reaches the realtime agent

The realtime session learns what the main agent replied through
`VoiceService.feed_realtime_history` — it persists the full text with
`save_main_agent_reply_fragment` (survives a session recycle) and pushes a
capped `[TTS HISTORY]` line into the live socket (does not).

That feed used to hang off the `on_speak_end` hook alone, so it only fired for
text that actually played. A turn muted by the physical cancel gesture is
dropped in os-server's `deliverTTS` and never reaches HAL, which left the
realtime session holding `save_main_handoff`'s "its spoken reply follows"
placeholder and no reply — the next turn then reasoned from a question it
believed had gone unanswered. os-server now posts that text to
`POST /voice/realtime/history` instead, which feeds the same two sinks without
the speaker.

The persisted fragment is the full reply either way: it is the processed
result, and memory wants all of it. Only the in-session line differs — it is
labelled `[TTS HISTORY, not spoken]`, because that line exists to stop the
model repeating what the user ALREADY HEARD, and on a cancelled turn they heard
none of it.

HAL completion also checks the actual speech-write observation before feeding
history. If cancellation or a synthesis failure produces no speech frames, it
feeds `spoken=False` (`[TTS HISTORY, not spoken]`). The Harness result chime
never counts as speech. If speech started and was then cancelled, the marker
is `[TTS HISTORY, interrupted; only part may have been heard]`; the full result
is retained without claiming the user heard all of it. The snapshot is taken
before end callbacks can change playback ownership. This measures writes to
the audio device, not acoustic delivery, and does not automatically replay a
cancelled result.

The second way a reply goes unheard is inside HAL, and os-server cannot see it:
`speak_queue` drops a superseded turn (an older `turn_seq` arriving after a
newer turn already owns the queue) and **returns success**, so the caller
believes it was spoken. This is the delegate case — the realtime agent hands a
question to the main agent, the main agent is slow, a newer turn wins the
speaker, and the answer evaporates while `save_main_handoff`'s placeholder
stays. The drop sites therefore call `_on_unspoken_reply`, a hook `VoiceService`
injects next to `_on_speak_end`, which routes into the same
`feed_realtime_history(..., spoken=False)`. It is gated on `realtime_feedback`
for the same reason the playback feed is: only the agentic runtime's own reply
may enter the model's context, never a dropped filler or system notice.

`turn_seq` is os-server's counter, but the threshold it is compared against
lives in the HAL process, and the two restart independently. A deploy, OTA or
crash restarts the count at 1 while HAL still holds the old high-water mark, so
every turn of the new session looks like a late arrival and is dropped —
measured 03/09/2026: `seq=1` against `latest_seq=40` silenced the wake greeting
(its LED and servo still ran) and would have silenced the next 39 turns. The run
id carries its creation time (`device-chat-<n>-<unix-ms>`), so when a LOWER OR
EQUAL sequence arrives from a run created LATER than the one holding the speaker,
HAL treats the counter as restarted and adopts the new sequence. Equal counts
because a restart resets to 1, so the new run only falls BELOW the old mark when
that mark is high — with a low mark (two restarts in a row, or a restart early in
a session) the sequences collide instead. Measured 04/09/2026: `seq=2` met an
unrelated `seq=2` from before the restart and the wake greeting was dropped, the
same silent-device symptom one comparison away. Ids without a stamp
(`tg-<messageID>`) keep the plain sequence rule: with nothing to compare, a
genuinely stale POST must not be able to take the speaker back.

### Two silence clocks and provisional end of turn

The silence clocks produce an **endpoint candidate**, not proof that the whole
request is complete. A current STT final starts the short clock:
`ENDPOINT_SILENCE_S` (`HAL_ENDPOINT_SILENCE_S`, default 0.8s) runs **from final
arrival**, only while `final_ts >= last_confirmed_speech`. Continued confirmed
speech invalidates that clock until a new final arrives. Otherwise the loop uses
`SILENCE_TIMEOUT_S` (`HAL_SILENCE_TIMEOUT`, default 2.5s) from the last speech.
`HAL_ENDPOINT_SILENCE_S=0` disables the short clock. A final may describe a
mid-request breath or just “Hello.”; it does not authorize execution by itself.

With `HAL_TURN_END_ENABLED=true` (default), non-Live hands-free capture passes
that candidate through `_internal/turn_endpoint.py` before closing STT and
committing audio. This HAL gate is shared by Gemini, OpenAI Realtime, GPT-Live,
Pipecat turn-based mode, and the ordinary STT/main-agent route. It does not
change provider-managed Live endpointing, Pipecat Live's existing Smart Turn,
or manual Harness tap-to-record capture.

`_internal/smart_turn.py` runs Pipecat's bundled Smart Turn (`LocalSmartTurnAnalyzerV3`;
v3.2 model in the 1.11 wheel) locally on a
worker thread, using at most the last eight seconds of mono 16 kHz PCM16
through the current candidate, including its recorded quiet tail. Lamp setup, Pi/OrangePi images, and non-Reachy OTA automatically
install the `pipecat` extra, so normal device installs need no manual command.
Bare developer `uv sync` still requires selecting `--extra pipecat`; Reachy
excludes it because of the ONNX dependency conflict described below.
Inference does not fetch a model over the network. Renewed speech, changed
transcript, or a new STT final arrival invalidates a pending/cached decision.
Even a final with the same text as its partial gets a new token and current
audio snapshot, so a previous `INCOMPLETE` cannot hide that evidence. The
arrival-based silence gate still runs first; final alone does not close a turn.
Unchanged evidence does not resubmit inference on every silent frame. Results
from an old capture cannot close a new one; capture continues during inference.

When the model reports complete, the candidate can close the turn. EN/VI
hesitation or unfinished-conjunction hints such as “uhm”, “and”, “và”, or
“để tôi nghĩ” hold it until `HAL_TURN_END_MAX_PAUSE_S` (default 6s) of silence;
a short greeting waits at least `HAL_TURN_END_FALLBACK_S` (default 2.5s).
An incomplete model prediction waits until new evidence permits reevaluation
or the maximum pause is reached. Without the
optional model, or during loading, failure or stalled inference, ordinary text
uses the conservative fallback: at least 2.5s of silence with defaults, never
earlier than the original candidate. Healthy pending inference gets up to 0.5s
of grace from the first candidate before fallback can close; a missing or
failed detector bypasses that grace, and the 6s maximum pause still applies.
These are bounded heuristics, not a
guarantee that the user finished. This gate adds no speculative execution or
new barge-in behavior. Disabling it restores the previous silence-clock path.

Recognized words let enhanced hands-free capture continue up to
`HAL_TURN_END_MAX_DURATION_S` (default 180s), so a 1–2 minute request can span
multiple STT segments. Reaching this hard ceiling **discards the unfinished
request without dispatch**. Manual capture and sessions without recognized
words retain `HAL_MAX_SESSION_DURATION_S` (code default 30s; lamp config 20s).
If an enhanced hands-free session reaches that shorter ceiling before words
arrive, it is discarded too. The long ceiling is not a minimum listening time:
a valid endpoint can finish a short request normally.

The rest of this section is about the clock itself, and applies to both. RMS alone is not enough in a noisy room: room noise sits
above `RMS_THRESHOLD`, so every frame refreshed the clock, the turn ran to
`MAX_SESSION_DURATION_S`, and mostly-noise audio went to STT — the 18/08/2026
observation was 8–25 second sessions coming back with `transcript='(empty)'`.
Energy VAD misses roughly half of the real speech frames in that environment,
and production voice stacks (Pipecat, LiveKit, Deepgram) all put a neural VAD
on this decision.

RMS stays as the cheap first gate, but the silence clock is only refreshed once
Silero also confirms speech. Silero runs per **window**
(`SILENCE_VAD_WINDOW_FRAMES`), not per frame: it costs ~20 ms/frame on ARM and
its LSTM needs more than one 64 ms frame to settle. It uses its **own** Silero
instance — a third one, alongside the entry gate and the realtime noise guard —
so the other paths' LSTM state stays clean, and it resets that state at the
start of every session. It fails open: a model error counts as speech, so the
device never cuts anyone off.

### The mic ignores our own backchannel cue

Backchannel listening cues ("Ok", "Mm", "Oh") are played on purpose **without**
setting the TTS `speaking` flag, because that flag ends the running STT session —
the one the cue exists to keep alive. But `speaking` is also the only thing that
normally keeps the mic off while the device talks, so the cue reached the mic
unfiltered and the entry VAD opened a **new** session on it about a second later.
Device-observed 19/08/2026: `'Ok'` came back as `transcript='Okay.'` and `'Oh'` as
`transcript='no'`, each running as a real turn that no user spoke.

`Backchannel.self_audio_active` closes this without touching `speaking`. `_play()`
arms a deadline (clip length + `HAL_BACKCHANNEL_ECHO_TAIL_S`) *before* the first
sample leaves, then re-anchors the tail to when playback actually ended. While it
holds, the VAD loop drops those frames from the speech test **and** from the
pre-roll lookback — keeping them in lookback would just replay the cue as the next
session's opening audio — and resets Silero's LSTM on resume, the same cleanup the
warm-mic drain does. Only session *opening* is suppressed; a session already
streaming is untouched, which is the whole point of the feature.

Each cue is also bound to the STT-session epoch that scheduled it. If normal TTS
holds the output stream long enough for that source session to end, the queued cue
is cancelled immediately before playback; it cannot leak into a newer mic session
as a fabricated transcript. This cancels only optional device speech — it never
closes, clears, or mutes user microphone capture, so the user can still talk
over it.

`robots/lamp/rootfs/opt/hal/.env` lowers `HAL_MAX_SESSION_DURATION_S` to `20`
(the code default stays `30`). Enhanced non-Live hands-free capture with
recognized words uses the separate 180s turn ceiling described above; speech
can legitimately last longer than 20 seconds. The same file previously wrote
`WAKEWORD_FOLLOWUP_TIMEOUT_S=60` without the `HAL_` prefix, so it did nothing
and the device ran the 20 s default; the key is now
`HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S=60`.

If the **initial** provider connection fails during HAL startup, the
orchestrator creates fresh sessions in a background retry loop (an immediate
fresh attempt, then 2s exponential backoff capped at 60s). This is separate from provider send/receive reconnects,
which do not exist until the first `connect()` succeeds. No HAL restart or new
audio is required; voice turns keep using the main-agent fallback until the
connection recovers.

## Echo cancellation (AEC)

`hal/drivers/voice/aec.py` runs the mic through WebRTC's APM (AEC3) with the
audio being played as the reference. It is **provider-independent**: the
reference is tapped in `_WatchedStream.write` (`tts/service.py`), the single
point every playback path reaches the device through — synthesized speech, the
`speak_queue` drain, and realtime **native audio**. Tapping there rather than at
synthesis is deliberate: TTS renders a sentence far faster than real time, while
the output stream writes at playback rate, which is the timing the mic sees.

**On by default** (`HAL_AEC_ENABLED=true`). Absent the binding below every AEC
entry point degrades to a no-op, so defaulting it on cannot break a device that
lacks it. It needs the `aec-audio-processing` binding, which is **not** a base
hal dependency — PyPI ships no Linux wheels for it, so a device builds it from
source. It lives behind the `aec` extra (`uv sync --extra aec`), deliberately
kept out of `dependencies` and out of `hardware`: the build needs meson/ninja,
which the lamp image does not install, so a hard dep would break both the image
build and `software-update hal` for a feature that degrades to a no-op without
it. When the import fails, `configure()` logs once and every entry point becomes
a no-op; the voice path behaves exactly as before, so the default is safe on a
device without the binding.

The canceller cleans the mic; it does not decide interruption. Nothing on the
turn path listens to the cancelled mic for the user talking over a reply — a
local detector was tried and removed after the measurement recorded below. The
user interrupts by tapping (GPIO button, TTP223 touchpad), and inside a live
session the provider's VAD owns interruption (see *Live mode*).

| Env | Default | Meaning |
|-----|---------|---------|
| `HAL_AEC_ENABLED` | `true` | Master switch |
| `HAL_AEC_DELAY_MS` | `205` | Speaker→mic delay hint. **Per-device** — measure it, don't inherit it |
| `HAL_AEC_NS` | `true` | Also run APM noise suppression. Carries most of the cancellation on this hardware |
| `HAL_AEC_TAIL_S` | `2.0` | Keep cancelling this long after the last speaker write, then bypass the APM |
| `HAL_AEC_REF_MS` | `500` | Echo-reference FIFO depth |
| `HAL_AEC_DUMP_DIR` | — | Write `aec_mic/ref/out.wav` for offline ERLE analysis |

### Installing the binding

PyPI publishes **Windows wheels only** for `aec-audio-processing`, so every
other platform builds from its sdist. That sdist vendors the full
webrtc-audio-processing + abseil sources and a pre-generated SWIG wrapper, so
the build is self-contained: it needs no system `libwebrtc-audio-processing`
and no system SWIG. Its build requirements (`swig`, `meson`, `ninja`, `cmake`)
all ship wheels on PyPI, so **no `apt install` is required** — which matters,
because an end user cannot run apt on a shipped device.

The build itself is the problem: measured on a lamp (A523, 8 cores) it takes
**5m35s wall / 36m CPU**. That is fine once, on a developer's device; it is not
fine on every device, every image build, and every `software-update hal`. So the
project builds one wheel and attaches it to a GitHub release:

```bash
scripts/release/build-aec-wheel.sh <device-ip>   # → dist/aec/*.whl
make upload-aec-wheel                            # → CDN, prints URL + sha256
```

`build-aec-wheel.sh` compiles on the device, in `/tmp`, in a throwaway venv with
meson/ninja from PyPI — `/opt/hal` and the system packages are never touched —
then copies the wheel back, installs it into a clean venv to prove it imports,
and deletes its scratch directory.

**Build on the OLDEST target, not the newest.** The wheel links only
`libstdc++/libm/libgcc_s/libc` and requires **glibc ≥ 2.34**. glibc is forward
compatible, so a wheel built on the lamp (Debian 12, glibc 2.36) also runs on
Reachy Mini (Debian 13, glibc 2.41) — the reverse does not hold. The wheel is
tagged `cp312-cp312-linux_aarch64`: `uv` on every body runs CPython 3.12, so
that tag covers the fleet, and `upload-aec-wheel.sh` refuses to publish anything
else rather than let the mismatch surface on a customer device.

The asset lives on a per-wheel tag (`wheels/aec-<version>`), never the OS
version tag — the wheel does not move with OS releases, and a dedicated tag is
not re-pointed, so a pinned URL cannot change content under the lockfile. It is
a GitHub release rather than the OTA bucket on purpose: this repo is public, so
a fork can build and host its own wheel, while the bucket is org-only.

`hal/pyproject.toml` pins that URL under `[tool.uv.sources]`, scoped to
linux/aarch64/CPython 3.12. Measured on `lamp-0c89`, installing the hosted wheel
takes **1.9 s** against **5m35s** to compile. Anything outside that marker —
a dev Mac, a future 3.13 — falls back to the PyPI sdist and compiles, so
`uv sync --extra aec` always works; only the fast path is pinned.

The main VAD loop is wrapped, and with `HAL_WARM_MIC=true` (now the default)
the mic stays open through playback, so cancellation runs during the device's
own speech. The reverb gate is deliberately left uncancelled so its timing is
unchanged.

**Measured on a lamp** (OrangePi sun60 / A523, USB mic + USB speaker — two
independent clock domains). The delay hint is per-device because the two USB
clocks free-run: on `lamp-ee17` the real lag is 204 ms median over one 93 s take
and 192 ms over another, drifting 154→215 ms within a single take (~667 ppm).
Correcting 150→205 raised achieved ERLE from 15.2 to 17.9 dB. An earlier
80→150 correction on the same unit took it from 10.9 to 18.6 dB.

Cost is ~3.9 % of one A523 core at realtime. The MacBook reference figure for
the same canceller is ~42 dB; the gap is the hardware — two free-running USB
clocks and a cheap analog path. Only ~1.6 dB of the echo here is *linearly*
predictable (coherence 0.31), so nearly all cancellation is suppression, which
is why turning `HAL_AEC_NS` off costs ~10 dB of ERLE and triples the residual.

### Known limitation: the reference starves

`EchoReference` is a FIFO tapped when ALSA **accepts** audio, but the mic hears
that audio a full output buffer later, and TTS writes in network-paced bursts.
When writes run further ahead than the FIFO is deep, the oldest bytes — exactly
the ones the mic is about to hear — are dropped, and the reference then runs dry
for the rest of the burst. Measured on `lamp-ee17`, the reference underran on
**30–86 % of processed frames** during a reply, and ERLE per window swings from
−25.1 dB to 23.2 dB accordingly. On the frames where a reference *is* present
the canceller reaches 15–23 dB, so the deficit is starvation, not the APM.

Deepening the FIFO does not fix it and makes it worse (`HAL_AEC_REF_MS=1500`
measured 3.6 / 2.3 dB against 23.2 / 19.1 dB at 500) because the lead becomes
variable and exceeds AEC3's alignment window. The real fix is to pace the
reference to playback time rather than write time, plus a dedicated capture
thread so the mic stops draining `arecord` in bursts.

The pacing half is implemented: `_WatchedStream.write` slices each caller
buffer into `TTS_REF_SLICE_S` (40 ms) pieces and publishes the reference only
after the device has accepted that slice, so the loop advances at roughly
speaker rate. The slice size is a GIL tradeoff, not an acoustic one — every
slice costs one blocking PortAudio write plus one reference write in Python,
and at 10 ms the ~1600 round trips per reply were audible as playback stutter
on a board whose main thread is already saturated by vision. The dedicated
capture thread is still not implemented.

Cached WAV cues (including the single-click acknowledgement) also submit 40 ms
blocks to this wrapper. Their previous outer 10 ms loop defeated the wrapper's
batching and kept 100 device/AEC calls per second under vision load. Cached
playback now uses 25 calls per second, plus a final partial block, and checks
cancellation between blocks. This can add up to 30 ms to the interval between
stop checks compared with the old loop. This is not an acoustic stop bound:
audio already queued in the output device can remain audible after a stop.

Regular provider TTS (including ElevenLabs PCM at 24 kHz played at 44.1 kHz)
uses continuous linear resampling across network PCM chunks within each
synthesis request. It retains the boundary sample and sample clock instead of
restarting interpolation at every chunk. Normal EOF flushes the held final
sample to preserve `ceil(N * output_rate / input_rate)` output samples for `N`
input samples; cancellation does not flush a tail. Head, tail, and queued
synthesis requests each have independent resampling state. Native realtime
resampling and whole-file cached WAV resampling are unchanged.

AEC reference resampling caches SciPy's default Kaiser FIR coefficients by
reduced sample-rate ratio and dtype (up to 32 entries), avoiding filter design
on every speaker write. A streaming causal FIR preserves filter history and
resampling phase across writes, producing `ceil(N * output_rate / input_rate)`
samples for the accumulated `N` input samples. This removes per-chunk rounding
drift and repeated filter boundaries. The same anti-alias filter adds about
0.625 ms of delay when the lower sample rate is 16 kHz; FIFO pacing is unchanged.
`EchoReference.clear()` or a source-rate switch resets the resampling state.
Acoustic improvement still requires an A/B check on the device.
Before the first speaker write of a playback, HAL prepares the reference filter
so a cold SciPy import/filter design cannot stall playback after its first 40 ms. This
preparation is skipped when AEC is inactive or sample rates match. Cancellation
is checked between slices, including after preparation; a stop acknowledgement
chime can still play while the speech stop flag is set.

Playback stream creation requests `max(0.120s, default_high_output_latency)`
and logs the actual negotiated latency. The previously observed device default
of 43.5 ms barely covered one 40 ms write slice. The 120 ms request provides
three slices of scheduling headroom while preserving larger defaults such as
Bluetooth outputs; it is neither a guaranteed buffer size nor a fix for network
jitter. More queued audio can extend the audible tail after cancellation.

`_WatchedStream.write` combines PortAudio's underflow results across all slices.
Mid-playback underflows are logged at most once every 5 seconds with a cumulative
count, `writer_gap_ms`, and `previous_aec_ms`; playback boundaries (which may
follow idle) are debug-only and
keepalive writes are excluded. These output underflows report playback starvation,
whereas an AEC reference underrun only reports missing cancellation-reference
samples and does not establish a speaker underrun. These local playback changes
still require listening verification on hardware.

`aec.uncancelled()` reports whether the frame just read went through *without*
real cancellation — reference underrun, bypassed stream, or mic overrun. The
live uplink gate keys on it in `cancelled` mode so it never sends raw echo up
as the user. Note what it does **not** say:
it reports whether a reference *arrived*, not whether cancellation *worked*, so
a frame with 0.9 dB of ERLE still counts as cancelled.

### ElevenLabs v3 playback speed

For ElevenLabs HTTP requests whose effective model is `eleven_v3` (including
the `tts-1` fallback), HAL requests provider `speed=1.0` and applies
`config.json`'s `tts_speed` locally through the existing `get_tts_speed` path.
A streaming ffmpeg `atempo` filter changes duration while preserving pitch,
before resampling, speaker output, and AEC reference capture. Speed `1.0`
bypasses the filter. ffmpeg is already included in device provisioning.
The filter process starts before the HTTP fetch, overlapping startup with
network/provider wait, and uses one filter thread for mono speech.

Cancellation is polled while waiting for filter output, so a pending HTTP fetch
does not keep ffmpeg alive after cancellation. Head/tail producers retain a
cancellation generation and use bounded queue writes; clearing the shared stop
event for a new turn cannot revive an old producer. A synchronous HTTP read
already in flight may remain until data arrives or its configured timeout;
its late audio is discarded and its source is closed.


The v3 WAV cache key includes a discriminator so previously synthesized v3
audio is not reused under this speed policy. This changes playback duration;
it does not reduce provider time to first byte (TTFB). Other TTS backends keep
their existing speed behavior.

Realtime text playback (turn mode and LIVE, regardless of provider) releases a complete sentence even when the same text delta also starts an unfinished next sentence. The exact unfinished tail stays buffered; partial tags, numeric periods and common abbreviations are held conservatively. Existing complete-buffer playback, native audio, provider completion/delegation and cancellation gates remain unchanged. `[tts-timing] stage=realtime_first_text` marks the first text received by the playback path; compare its owner with `speak_requested`/`queue_requested` and HTTP timing to isolate text buffering from synthesis latency.

TTS diagnostics use `[tts-timing]`: request/queue admission, playback worker,
HTTP start/headers/first decoded bytes, first 4096-byte buffer, first tempo
output, and first completed speaker write. A per-HTTP `request` ID separates
parallel fetches; `text_key` (SHA-256 prefix) links synthesis and playback logs.
Repeated identical text shares a key, so also use timestamps and playback owner.
HTTP durations include proxy/network/provider time; they cannot isolate provider
compute. First write is not acoustic onset. Logging preserves PCM chunk boundaries
and does not change KPI definitions or playback/cancellation policy.

### Why there is no voice-driven interrupt on the cancelled mic

A local detector ("barge-in": stop TTS when the user talks over it) shipped
between 25/08 and 13/09/2026 and was removed. The measurements are kept here so
it is not retried from scratch. The residual that survives cancellation is loud
enough to look like a user interrupting, and it **is** speech, so neither a
level gate nor a speech classifier can reject it. Measured in a silent room,
echo ceiling against real interruptions:

| Speaker volume | Mixer | Echo ceiling | Real interruption |
|---|---|---|---|
| 25 % (`lamp-ee17`) | −45 dB | 9804 | 8027 |
| 40 % (`lamp-0c89`) | −36 dB | 9969 | 6956–8027 |
| 65 % (`lamp-0c89`) | −21 dB | 13560 | 6956 |

The echo ceiling sits **above** the real interruptions at every volume, so a
threshold below it self-interrupts and one above it misses ordinary speech.
Lowering the speaker is not a workaround either: 24 dB of mixer range moved the
ceiling by under 3 dB, because the coupling is not dominated by the airborne
path.

The last defence tried was an envelope test on the **raw** mic: align the
candidate window against the retained reference, subtract it plus the learned
coupling gain, and read the *skew* of the residual rather than its size — a
person can only add energy, so a one-sided residual is someone else in the
room. Labelled on `lamp-0c89` at 40 %:

| | Residual skew |
|---|---|
| Echo, silent room (15 windows) | −2.8 … +2.1 dB |
| Echo, mixed run (~40 windows) | −50.0 … **+4.8** dB |
| Confirmed real interruption | **+8.4** … +40.4 dB |

That looked separable, and 12 replies into a silent room fired zero false
interruptions. It did not hold: 27/08/2026, 20 replies into a silent room, the
lamp cut itself off 4 times, and scoring 79 labelled windows (69 echo, 10
confirmed interruptions) against every feature available — envelope
correlation, residual skew, coupling offset, APM suppression, and the textbook
magnitude-squared coherence — gave a best AUC of 0.72, with every threshold
that reached 0 % self-interruption missing 90–100 % of real interruptions:

| feature | echo | person |
|---|---|---|
| coherence | 0.03 … 0.60 | 0.04 … 0.57 |
| correlation | 0.25 … 0.99 | 0.41 … 0.95 |
| skew (dB) | −6.5 … 67.1 | −1.0 … 119.7 |

The classes overlap almost completely, so no classifier on this signal will do
better. The cause is upstream: AEC3 reaches only ~6 dB ERLE on this hardware
because the speaker and mic are separate USB devices with free-running clocks,
and during double talk the APM does not attenuate the near-end talker — it
removes them (a frame reading 7426 on the raw mic left the APM at 5). Comparing
the cancelled signal instead of the raw one was also tried and is worse: the APM
is a time-varying gain and eats the loudness contour.

Re-attempt only after the echo path itself improves — a neural canceller
(DTLN-aec runs real-time on a Pi 3 B+) or one sound card for both directions —
and gate the attempt on the acceptance test in *Live mode*: replay
`barge-in-captures/full40-bargein-off` through the canceller and require the
peak residual under the real-interruption floor (6956) with margin. Until then,
tap-to-interrupt and the provider's VAD are the two interruption paths.

`process()` buffers to the APM's fixed 10 ms frames and returns exactly as many
samples as the caller asked for (priming once with up to 10 ms of silence), so
hal's 64 ms framing is unaffected. ERLE is logged periodically while the
speaker is active — **0 dB means the canceller is doing nothing**.

> The image already loads PulseAudio's `module-echo-cancel` (`setup.sh`), but
> nothing reaches it: a udev rule sets `PULSE_IGNORE=1` on the speaker codec so
> hal can own it, and capture goes through `arecord -D plughw:` directly. That
> module has no reference and no client; it is not what cancels echo here.

## Emotion expression (fire-and-forget)

If the device declares the `expression` capability
(`ROBOT.md` → `expression: { routes: [emotion] }`), the orchestrator also
registers an `express_emotion` tool (`orchestrator.py`, `EMOTION_TOOL`).
Devices with no face (e.g. mic + speaker only) never get the tool, so the
realtime model can't set an emotion — the registration is gated end-to-end:
`server.py` (`"expression" in _profile.capabilities`) →
`VoiceService(enable_expression=…)` →
`RealtimeOrchestrator(enable_expression=…)`.

On GPT-Live the tool is registered but can never fire: the Live layer has no
tools (`GPTLiveAgent` logs it as unavailable once at construction), so on that
provider the face changes only through the main agent after a delegation.

`express_emotion` is **fire-and-forget** — the model calls it *in parallel*
with speaking. It does not replace delegation: for a request requiring the main
agent, an optional spoken acknowledgment or emotion call must still accompany
`delegate_to_main` in the same turn. There is no binary "tool OR speech" rule
in this tool description. When `stream_output()` sees the call
(`_handle_emotion_call`), it:

1. calls the HAL emotion handler **in-process** (`_fire_emotion` →
   `routes/emotion.py` `express_emotion`) on a daemon thread — the realtime agent
   runs inside the HAL process, so there is no HTTP loopback / serialization. It
   runs parallel to the audio already streaming, so the face changes without
   blocking speech;
2. answers the call with `FunctionCallResultInput`, whose `trigger_response`
   depends on whether the model has already spoken this turn
   (`orchestrator.py`, `_handle_emotion_call`). If it **has** spoken
   (`trigger_response=False`), the result is recorded without spawning a second
   model response — for OpenAI this skips `response.create`
   (`openai_realtime.py`); on Gemini the ack is not sent at
   all, because `send_tool_response` there *continues* the turn and makes the
   model re-speak its whole reply. If it has **not** spoken yet, the tool call is
   the entire generation so far and Gemini pauses until answered, so the ack is
   sent (`trigger_response=True`) or the turn deadlocks until the watchdog fires.
   Net added latency to speech ≈ 0.

### Pending-tool-call session quarantine (Gemini)

Gemini Live **refuses `send_realtime_input` while a tool call it emitted is
unanswered**, and enforces this by closing the session with WebSocket **`1008`**
("The operation was aborted"). This is a deliberate policy close by the provider,
not a dropped stream — a transport drop shows up as `1006` with an empty reason
and is handled by the proxy, not here.

`gemini_live.py` therefore quarantines the **whole client side** of that
session, rather than merely gating microphone audio:

- receiving a `tool_call` registers every `call_id` in `_pending_tool_calls` and
  makes the session non-sendable;
- while any call remains unresolved, **all client input is suppressed**:
  `AudioInput`, manual-VAD `activityStart`, `activityEnd`, commits, and other
  client messages. Nothing is buffered for replay, because it would turn speech
  captured during an invalid provider state into a stale later turn;
- for a normal `FunctionCallResultInput`, the call stays pending until Gemini
  has accepted `send_tool_response`. Only that successful provider acknowledgement
  clears the call and makes the same session usable again. A failed or rejected
  acknowledgement leaves the session quarantined and it is discarded;
- the fire-and-forget `express_emotion` path above deliberately sends no Gemini
  acknowledgement after speech has started, because doing so makes Gemini repeat
  the reply. Such a session can never become valid again: it remains
  non-reusable and the next `prepare_turn()` rebuilds a fresh session;
- there is no expiry or other timeout that reopens a quarantined session. A
  fresh/rebuilt session has no inherited pending calls.

In particular, `_async_commit` suppresses `activityEnd` while quarantined too.
Completing an old activity bracket is not safe when Gemini is waiting for the
tool result; the replacement session starts its next activity cleanly.

A valid `delegate_to_main` handoff also marks the session for replacement
**before** publishing the tool event, even if Gemini has said nothing yet.
The successful `delegated` acknowledgement clears the pending call but does
not clear this replacement requirement. The next `prepare_turn()` rebuilds
before streaming new capture. This prevents delayed post-handoff speech
(including provider-generated system-error apologies) from leaking into the
next live session with an empty turn ID. Empty/invalid delegation messages do
not set this flag. Rebuilding adds connection overhead after a handoff; this
is not an error-text filter and does not fix upstream tool execution errors.

On receive timeout, `[realtime][transport]` logs connection/sender state,
queued input count, age of the last successful audio send, pending tools and
tool-gated frames. These are transport metadata, not recorded audio. The live
capture loop's `last_upload_ms` measures enqueue time, not a successful wire send.

The model is told (`resources/system_prompt*.md`, "Expression Exception") to
never wait for, announce, or speak the emotion aloud. Note this is distinct from
the non-realtime path, where the agent emits a `[HW:/emotion:…]` text marker that
the Go layer parses and strips — the realtime path never uses text markers.

## Google Search grounding (Gemini only)

By default Gemini Live is given a built-in **Google Search** tool
(`HAL_GEMINI_GOOGLE_SEARCH`, default on; wired in `gemini_live.py` as a separate
`types.Tool(google_search=…)` alongside the function-declaration tools). This
lets the realtime model answer **public live-data** questions — weather, news,
sports, prices, "what time is sunset" — by grounding in-session and speaking the
result itself, instead of calling `delegate_to_main` and paying a full main-agent
round-trip. The Gemini system prompt (`system_prompt_gemini.md`) lists these
public lookups under *Direct Home Run* and routes only **account/private** live
data (the user's calendar, their smart-home device states, their messages) to
`delegate_to_main`.

Trade-offs:

- **Gemini only as a hosted tool.** OpenAI Realtime has no equivalent built-in
  tool, so its prompt (`system_prompt_openai.md`) still delegates all external
  lookups; GPT-Live has no tools at the Live layer at all, so
  `system_prompt_gptlive.md` delegates them too (its Responses backend searches
  instead). Pipecat v1 has no hosted search either, but gets a **client-side**
  `web_search` function tool that answers through the campaign-api
  Google-Search relay — see *Web search* under the Pipecat v1 section.
- **Cost.** Grounding bills per grounded request on top of tokens, but only when
  Gemini actually decides to search. The prompt tells it to ground *only* for
  genuine fresh/public facts, not general knowledge it already holds. Net effect
  vs. before is mostly a **shift** of cost (and latency) off the main agent.
- **Read-only.** Grounding answers questions; it never performs actions. Music,
  hardware, memory writes, and skills still delegate.

## In-session vision — the `look` tool (Gemini only)

When the user asks about what the device **sees** ("what is this?", "look at this",
"look at what I'm holding", "what am I holding?", "read this label", "what colour is
this?"), the realtime model answers in-session instead of delegating. Note "look at
this" routes here, **not** to the camera privacy toggle — `skills/camera/SKILL.md`
disambiguates the verb by what follows it, since "look at me" means "turn the camera
on" while "look at this" is a question about an object. This only applies to turns
that are **purely** a question about what it sees: if the same turn also contains an
action ("turn to the right, hold it there, and tell me what you see"), the prompt
requires a single `delegate_to_main` covering both halves — no `look` — so the
movement is never silently dropped. The tool description and the Gemini prompt
both exclude finding a specific object ("where is my pen", "do you see my keys") —
that is a delegated search, not a look. The model can still ignore that advice
("Find my mouse" reached `look` on 3.8 extended-thinking, #481), so the
orchestrator enforces it: when `look` is called and the turn's provider transcript
matches `hal/realtime/find_intent.py` (English: find / locate / search / look for /
look around / where is / do or can you see my; Vietnamese: tìm / ở đâu / đâu rồi),
`_redirect_find_look` does **no** aim or capture. It acks the call with
`{"result": "delegated"}`, clears `realtime_look_frame_path`, ends the turn and
delegates the transcript, so the main agent runs `/servo/search` with no
`[vision-image]`. An empty transcript at call time skips the guard (log line
`look: no transcript at call time — find guard skipped`). The orchestrator
registers a `look` tool (`orchestrator.py`, `LOOK_TOOL`) and handles the call in
`_handle_look_call`:

1. **Aim the head at the subject first**, on devices that can move — otherwise a
   confident answer gets given about whatever the head happened to face. See
   [Look-aim](../robots/lamp/docs/vision-tracking.md#look-aim--pointing-the-head-before-a-visual-question-captures)
   for the aim loop, how it picks which person is the one asking, and the
   remembered bearing it falls back on when nobody is visible.
2. Grab a **sharp** camera frame **in-process** (`_capture_frame` calls
   `capture_still` — no HTTP loopback; servos are frozen (animation loop +
   tracker worker both honor the flag) and the frame is only accepted once its
   capture timestamp is past the settle after the last servo bus write, so motion
   blur can't reach the model. The settle is 0.3s, scaled up with the size of the
   last aim correction to a 0.5s ceiling — an aim that exits on its deadline does
   so straight after a large swing, and the arm is still ringing past a flat
   300ms; zero added latency when the servos are already still or the device has
   none), downscaled to `HAL_GEMINI_VISION_MAX_WIDTH`
   (default 768px) to bound image tokens.
3. **Extended-thinking sessions** (the session has reported `interaction_status`,
   so `supports_look_continuation` is true — 3.8 extended-thinking): the JPEG goes
   **inside the `look` tool response** (`FunctionCallResultInput.image` →
   `FunctionResponse.parts`, log line `Sent look image in tool response
   (call_id=… bytes=…)`), and there is no replay. The ack triggers the next
   generation, so a frame sent separately after it could still be processing
   when that generation began, and the model answered "I can't see" (#481).
   `send_realtime_input` gives no ordering guarantee. One message removes the
   race without any wait. google-genai 2.12.1 does not serialize the nested image
   bytes in `send_tool_response`, so only this result goes out as an explicit
   base64 WebSocket payload, and it is discarded if its look call was cancelled,
   resolved or cleared by a session reset (see the interaction-status section).
   Proxy check 2026-09-23 with the same mechanism: 3.8 extended-thinking answered
   from the in-response image twice out of two, and plain 3.8-live three out of
   three with a neutral prompt. Plain 3.8-live does not report interaction status,
   so it still takes the path below.

   **Other sessions** (3.1, plain 3.8-live) take the original path: enqueue the
   frame as realtime **video input** (`ImageInput` → `send_realtime_input(video=…)`),
   then **replay the turn**: the Live API queues a frame sent mid-turn for the
   NEXT turn (device-proven: the tool-ack → continue-turn flow answered every
   look from the *previous* look's image — a one-image lag no ack delay fixes),
   so instead of acking the tool call, the orchestrator yields `LookReplaySignal`
   and `run_realtime_turn` re-appends the turn's audio and commits again on the
   SAME session. The queued frame joins the replayed turn.
4. (Replay path only.) The replayed turn re-triggers `look`, which hits the reuse guard
   (`VISION_MIN_INTERVAL_S`) and is acked with `trigger_response=True` — the
   model answers from the frame that is now genuinely in context.

Replay support plumbing: `receive()` swallows ONE stale `turn_complete` (the
cancelled turn's, which lands after the replay commit and would otherwise end
the replayed turn empty — `skip_next_turn_done()`); a pending idle/turn-cap
session recycle is deferred while a replay is pending (a rebuild would orphan
the just-sent image); and any session rebuild resets the look reuse guard
(images live in the session — a fresh session has none). Cost: the question's
audio is billed twice on look turns; the image once.

This replaces the slow path (delegate → main → skill lookup → `/camera/snapshot`
→ vision LLM, several seconds) with one in-session round-trip.

Gating (all three required, else visual questions fall back to delegation):

- **Capability:** a camera is present (`app_state.camera_capture` is set). This is
  the device's `vision` capability at runtime — `server.py` only creates
  `camera_capture` when ROBOT.md declares `vision`. The orchestrator reads that
  one signal (`_camera_present()`), so it's correct for every construction path.
- **Flag:** `HAL_GEMINI_VISION` / `realtime.gemini.vision` (default **on**).
- **Provider:** Gemini only (the image-inject → continue-turn flow is
  implemented + tested for Gemini Live; OpenAI keeps delegating visual
  questions). GPT-Live cannot have it either: no Live-layer tools, and
  `gpt-live-1` accepts no image input, so a stray frame is dropped with one
  `[realtime] GPT-Live has no image input — frame dropped` warning. The Gemini
  system prompt (`system_prompt_gemini.md`) describes when to call `look`.

Cost: one frame per call (tool-triggered, **not** a video stream), so the added
tokens are marginal next to the turn's audio. A 768px frame is a few hundred
image tokens. To stop an over-eager model from re-billing images, `_handle_look_call`
sends **at most one image per turn** and **none within `HAL_GEMINI_VISION_MIN_INTERVAL_S`
(default 10s) of the last send** — repeat looks reuse the frame already in context.

**Frame handoff on delegate / timeout.** When a `look` turn ends up delegating or
falling back to the main agent (most importantly when Gemini times out *mid*-look),
the frame `look` already captured is handed to the main agent so it answers from
that exact image instead of taking a fresh snapshot (faster, and it answers about
the moment the user pointed at). `_handle_look_call` persists the frame to
`_SNAPSHOT_DIR` and records it in `app_state.realtime_look_frame_path`;
`turn_dispatch._take_vision_handoff()` consumes it **once per turn** (strictly: a
handled turn that already used it clears it so a later delegate can't pick up a
stale image) and, when fresh (`HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S`, default 45s),
prepends a `[vision-image] <path>` hint line to the message and ships the frame
as base64 in the sensing POST's `image` field. What os-server
then does with the image is decided by the **describe-first gate** in
`system/vision` (see `server/sensing/delivery/http/handler.go`): when the
active main model does NOT declare image input in the model catalog (the
Auto-AI case — a raw attachment 404s at the smart-agent-router with "No
endpoints found that support image input"), the frame is described by the
catalog's `default_image_model` (falling back to `system/vision`
`DefaultImageModel` — the same model openclaw's `imageModel` uses for Telegram
photos) and the agent receives an `[image description] …`
text line instead — and the `[vision-image]` hint is rewritten to drop the
file path, plus the snapshot file itself is deleted (best-effort). Neither
may survive alongside a description: the snapshot lives inside the agent's
media allow-list, so any path the agent gets hold of — the hint, an old hint
in session history, an `ls` of the dir — can be `read` into an image block
that sticks in the session history and 404s every later turn the router
sends to a text-only model (even fully-text turns). The describe call gets
two attempts (20s + 15s, 35s total — a hung upstream request is retried on a
fresh connection); if both fail the image is **dropped**, the snapshot file
still deleted, and the hint rewritten to have the agent tell the user it
couldn't see the photo — never sent as a raw attachment, because when the
router lands on a text-only model that attachment poisons the whole session,
which costs far more than one degraded turn. When the catalog says
the model takes images, the raw attachment is forwarded directly and the
hint keeps the path. The gate re-reads the catalog every 30 min,
so a backend catalog flip migrates devices automatically. The same gate covers
web-monitor-chat image uploads — both image sources converge on this one
handler. The `camera` skill instructs the agent to answer from the
description/attachment and skip `/camera/snapshot`. If the timeout happens
*before* the frame is captured, there's nothing to hand off and the agent
snapshots normally.

## Providers

Four interchangeable backends, selected by `HAL_REALTIME_PROVIDER` /
`realtime.provider` (`none` | `gemini` | `openai` | `gptlive` | `pipecat_v1`)
and built in `orchestrator._make_agent`; Go `RealtimeProviders` and the web
dropdown (`RealtimeSection.tsx`) list the same values, in that order, before
`none`:

| Provider | Class | Threading model | Default model | Sample rate |
|----------|-------|-----------------|---------------|-------------|
| Gemini Live | `voice_agent/gemini_live.py` `GeminiLiveAgent` | private asyncio loop on a `gemini-io` thread; send/recv threads submit coroutines via `run_coroutine_threadsafe` | `gemini-2.5-flash-native-audio-preview-12-2025` | 16000 Hz |
| OpenAI Realtime | `voice_agent/openai_realtime.py` `OpenAIRealtimeAgent` | fully synchronous; one `RealtimeConnection` shared by send/recv threads, serialized by a reentrant lock | `gpt-realtime-2` | 24000 Hz in and out |
| GPT-Live | `voice_agent/gpt_live.py` `GPTLiveAgent` | fully synchronous; one `LiveConnection` shared by send/recv threads, serialized by a reentrant lock, plus a `gptlive-watchdog` thread that synthesizes the turn boundary the wire never sends | `gpt-live-1` | 16000 or 24000 Hz, **one** PCM format for both directions (default 24000) |
| Pipecat v1 | `voice_agent/pipecat_v1.py` `PipecatV1Agent` (+ `pipecat_pipeline.py`, `pipecat_stt.py`) | **no vendor session**: a Pipecat pipeline on a private asyncio loop (`pipecat-io` thread) inside HAL; the send thread submits frames with `run_coroutine_threadsafe`, the pipeline's `EventSink` writes straight to the recv queue, the recv thread only watches pipeline health | `qwen/qwen3.6-35b-a3b` via the campaign-api Qwen relay (any OpenAI-compatible chat endpoint) | 16000 Hz in; **text out** (HAL's TTS speaks) |

Gemini Live uses `google-genai` and keeps its private asyncio loop owned by its
`gemini-io` thread. Teardown first closes/cancels the provider receive task,
then joins workers; a failed handshake rolls back that loop/thread immediately.
This prevents a stalled receive from surviving a session rebuild. Teardown also
runs queued completion callbacks even when no tasks remain pending, so a receive
that just finished can deliver its result/error to the waiting thread before
loop closure. This avoids stranded futures and `Task exception was never retrieved`
during close/rebuild, including SDK `APIError(1000)` for normal WebSocket closure;
unexpected receive errors still reach the existing error/reconnect handling. For the
native-audio family, HAL sends a 20 s websocket ping but sets no ping timeout:
outbound traffic keeps the proxy path alive without treating its missing pong as
a client-side failure. HAL also recycles Gemini synchronously before streaming audio when
the current session has been idle for more than `HAL_GEMINI_PRE_TURN_RECYCLE_S`
seconds, so post-idle speech does not land on a proxy-dropped session. Idle is
measured from the later of the last turn's end and the session's connect
(`_idle_since_monotonic`). Measuring from the last turn alone recycled a session
that had just connected after a privacy-switch unmute, and the turn fell back to
the main agent during the ~10 s reconnect (device 2026-09-23).

**Idle parking.** A Gemini session nobody is talking to is closed by the server
with WS `1008` "The operation was aborted" (measured idle lifetimes: 86-198 s).
That close costs no turn — the pre-turn recycle above already replaces the
session before any post-idle turn streams — but the backend logs it as an error
and alerts on it, so the device closes first. After
`HAL_GEMINI_IDLE_PARK_S` seconds with no turn activity, an `rt-idle-park`
watchdog thread disconnects the transport and marks the session *parked*. A
parked orchestrator still reports `available`: the next `prepare_turn()`
reconnects a fresh session synchronously (`idle-park-resume`) before any audio
is streamed, which is exactly what the pre-turn recycle would have done for that
turn anyway, and `voice_service` buffers the capture across the ~1 s handshake.
Parking is skipped while a turn is in flight, and a resume that cannot connect
reports unavailable (turn falls back to the main agent) while staying parked so
the next turn retries.
In wake-word mode the resume is overlapped with the user's sentence: `Session
START` calls `prewarm()`, which starts the `idle-park-resume` handshake in the
background (`idle-park-prewarm`, thread `rt-prewarm`), and the `prepare_turn()`
running on the capture preparation worker joins it (up to
`PREWARM_JOIN_TIMEOUT_S` = 4 s)
instead of connecting serially — device-measured on lamp-4ace 22/09/2026 that
serial connect cost ~2 s per post-idle turn. A capture that is never dispatched
just leaves an idle session the park watchdog closes again.

All providers treat teardown as terminal: once `disconnect()` sets the stop
signal, send/receive workers neither reconnect nor emit transport-failure logs
while their closed socket unwinds.

All subclass `voice_agent/base.py` `VoiceAgentBase`, which defines the
queue-based contract:

- **Two threads per agent**: `_send_loop` drains `_send_queue` → API;
  `_recv_loop` reads API → `_recv_queue`. Both reconnect on error.
- **Fail-fast on backend error** (all drivers): when `_recv_loop` hits a real
  error (Gemini Live: proxy `go_away`, quota / resource-exhausted, unexpected WS
  close — anything that is **not** a benign idle close `1000`; OpenAI: a
  Realtime API `error` event that is not one of the benign codes listed under
  *OpenAI Realtime: errors* below, or a dropped socket; GPT-Live: a server
  `error` with no `client_event_id` or one on `hal-start`, or the event
  iteration ending on `session.closed` — see *GPT-Live* below), it pushes a `TurnDoneEvent` immediately
  (`_fail_fast_turn`) so `receive()` unblocks now and the turn falls back to the
  main agent **without** waiting out the full `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S`.
  Benign idle closes still reconnect quietly (Gemini code `1000`; OpenAI ends the
  event iteration cleanly, never an error; GPT-Live has no benign close — every
  `session.closed` fail-fasts whatever is in flight and reconnects). Only fires
  while a turn is awaiting output (`_turn_done` clear; on GPT-Live, while a reply
  is streaming or an input is still waiting for one); reconnect still runs in the
  background to heal the session for the next turn.
- **Non-blocking**: `append_audio()`, `commit_audio()`, `send()` (queue puts,
  gated on `available`).
- **Blocking**: `connect()`, `disconnect()`, `receive()` (a generator yielding
  `OutputBase` until a `TurnDoneEvent`, or until no event arrives within
  `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` — default 8 s — which ends the turn quietly
  so a silent/no-response turn falls back to the main agent without long dead-air).
  Gemini non-Live extended-thinking uses verified progress and actual answer
  chunks as described above: search/tool progress can extend waiting to the
  15-second commit-based deadline, while answer chunks have an 8-second idle
  window. Generic inbound traffic does not extend that model's silent turn.
  Other provider/mode paths retain their existing liveness policy: providers call
  `note_server_activity()` on inbound messages, and `receive()` may extend a
  silent turn while those messages arrive, up to `HAL_REALTIME_TURN_MAX_SILENCE_S`
  (default 20 s). A turn with no inbound traffic still ends on the first gap.
- `available` ⇔ the websocket/session is connected (`_connected`).
- **Provider user-turn key.** Every `OutputBase` and the `TurnDoneEvent` carry
  `user_turn_id`, the provider's key for the user input that the reply answers.
  All providers freeze it per response (OpenAI at `response.created`, or at
  the first output if that was missed; GPT-Live at the first output of a reply
  burst), so a late input transcription can never re-own an earlier reply. Empty means ownership could not be established.
- **Live-mode metadata (`HAL_LIVE_MODE=true` only, all providers).**
  `UserSpeechOutput` publishes the provider's own view of the user's speech:
  a new key at speech onset, `endpoint_at` + `method="server_vad"` at the
  provider's speech endpoint, and incremental `transcript` chunks with
  `method="provider_transcript"` (a `transcript_finished` flag marks the
  provider's completion; completion-only metadata cannot create an input turn).
  GPT-Live has no VAD, so its keys carry transcript chunks only — never an
  `endpoint_at` / `method="server_vad"`.
  `ExecutionOutput` keeps completion evidence when a terminal is dropped during
  an interruption. Neither is emitted on the turn path.
- **Interruptions.** `InterruptedOutput(reason="output_reset")` precedes the
  first transcript chunk of every reply (the live pump resets its TTS queue so
  a reply never plays behind a stale one); `InterruptedOutput(reason=
  "server_interrupt", at=<monotonic>)` announces a provider-side barge-in and
  stops playback at once (LIVE_MODE only; on GPT-Live the barge-in is inferred
  locally — see *GPT-Live*). `receive()` always passes
  `UserSpeechOutput`, `ExecutionOutput` and `server_interrupt` through.
- **Output generations.** `OutputEvent.gen` is bumped by the provider at every
  turn boundary, including an interruption; `receive()` drops any output whose
  `gen` is older than the newest one it has yielded (logged as
  `dropped N output(s) from generation A, superseded by B`), so audio still
  queued from a cancelled reply never reaches the speaker. Filtered in the base
  class so the turn path and the live pump both get it for free.
- **Liveness.** Providers call `note_server_activity()` on every inbound event
  (see the silent-turn watchdog above). GPT-Live is the one exception: it does
  **not** note `session.usage.updated`, because a usage tick says nothing about
  whether the model is working on a reply.
- **`FunctionCallOutput.user_transcript`** carries the provider's input
  transcription of the utterance that produced the tool call — the source of
  the `[transcript]` line in a live-mode delegation.

### OpenAI Realtime: live-mode parity with Gemini

`voice_agent/openai_realtime.py` speaks the **GA** Realtime API
(`openai.resources.realtime`, SDK pin `openai>=3.14.1` in `hal/pyproject.toml`;
`hal/uv.lock` holds 3.14.1. Older 1.x SDKs have no
`openai.resources.realtime` module and the adapter does not import). Session
shape is `type: realtime` + `audio.input` / `audio.output`; the event names are
`response.output_audio.delta` / `response.output_audio_transcript.delta`.
Everything the live pump and the turn path key on is emitted exactly as
`gemini_live.py` emits it:

| Contract item | OpenAI source event |
|---|---|
| new `UserSpeechOutput` key (`openai-<hex>`) | `input_audio_buffer.speech_started` |
| `UserSpeechOutput.endpoint_at`, `method="server_vad"` | `input_audio_buffer.speech_stopped` (timed when HAL receives it) |
| `UserSpeechOutput.transcript` chunks, `method="provider_transcript"` | `conversation.item.input_audio_transcription.delta`; `.completed` emits only the remainder not already streamed, with `transcript_finished=True`. Each chunk is logged `[realtime] <<< user said: '…'` |
| late transcription attribution | an `item_id → (turn_id, emitted text)` map (`_user_items`, bounded to 16 entries) binds every user audio item to the live turn it was captured in, so a transcription that lands after the **next** `speech_started` is still attributed to the input it transcribes. Manual turns learn the item id from `input_audio_buffer.committed` |
| `user_turn_id` on outputs / `TurnDoneEvent` | frozen at `response.created` from the live key being captured |
| `InterruptedOutput(reason="output_reset")` | before the first `response.output_audio_transcript.delta` of a reply |
| `AudioOutput` / `TextOutput` | `response.output_audio.delta` / `response.output_audio_transcript.delta` (`response.output_text.delta` is ignored — the session is audio-only, so the audio transcript is the text source; emitting both would double-speak the reply) |
| `FunctionCallOutput` (+ `user_transcript`) | `response.function_call_arguments.done`; the accumulated input transcription is attached and cleared |
| `TurnDoneEvent.execution_completed` | `response.done` with `response.status == "completed"` **and** no interruption seen during that response |
| `OutputEvent.gen` | bumped once per receive turn and once per interruption |
| `note_server_activity()` | every inbound event |

`end_turn()` stays a no-op and `requires_fresh_session` stays `False` on
OpenAI: `response.done` arrives even for a function-call-only response, so
`_turn_done` is always released by the recv loop, and a `function_call_output`
item can be recorded without a response, so an unanswered tool call never
poisons the session the way Gemini's `1008` quarantine does.

Input transcription is **always on** — it is the only source of the user's words
on this path (`UserSpeechOutput.transcript`, live history, the delegate
message). `conversation.item.input_audio_transcription.failed` is logged as a
warning and the turn continues without words.

### OpenAI Realtime: barge-in and server-side truncate

A barge-in is detected on `input_audio_buffer.speech_started` **while a response
is active**, or — when no `speech_started` of ours was seen — on a
`response.done` whose `status` is `cancelled` (a client `response.cancel`, or
semantic VAD deciding mid-reply). With `interrupt_response` (the API default)
the server cancels the response itself; the local side (`_handle_interrupt`)
then:

1. drains the recv queue, keeping `UserSpeechOutput`, `ExecutionOutput` and any
   queued `server_interrupt`; a queued `TurnDoneEvent` becomes an
   `ExecutionOutput` in LIVE_MODE so completion evidence survives without
   replaying a control terminal;
2. bumps `OutputEvent.gen` so in-flight deltas of the cancelled response are
   dropped by `receive()` (the recv loop also skips further
   `response.output_audio.delta` / `response.output_audio_transcript.delta`
   carrying the cancelled `response_id`);
3. emits `InterruptedOutput(reason="server_interrupt", at=now,
   user_turn_id=<owner of the cancelled reply>)` (LIVE_MODE only);
4. sends `conversation.item.truncate` for the assistant audio item being
   streamed, with `audio_end_ms` = audio received − audio still queued (best
   effort; HAL's own playback buffer makes it an over-estimate by a few hundred
   ms). The client `event_id` is `hal-truncate`, so a truncate past the real
   audio length only yields a benign `error` (below). This keeps the model's
   context aligned with what the user actually heard;
5. releases `_turn_done` and logs
   `[realtime] Response interrupted — dropped N queued output(s), gen=G`.

The new input then takes over the live key (`speech_started` always resets the
capture state before `_observe_user_speech()` publishes the new
`UserSpeechOutput`).

### OpenAI Realtime: session configuration

`_build_session()` sends one `session.update` on connect:

| Field | Value |
|---|---|
| `type` | `realtime` |
| `instructions` | the assembled system prompt |
| `output_modalities` | `["audio"]` (adding `"text"` would make the model emit `response.output_text.delta` as well and double-speak the reply) |
| `audio.input.format` / `audio.output.format` | `{type: "audio/pcm", rate: 24000}` |
| `audio.output.voice` | `HAL_OPENAI_REALTIME_VOICE` |
| `audio.input.transcription` | `{model: HAL_OPENAI_TRANSCRIBE_MODEL, language: <ISO-639-1>}` — the language is derived from `stt_language` by taking the part before the first `-` and lower-casing it (`vi-VN` → `vi`); omitted when `stt_language` is empty |
| `audio.input.noise_reduction` | `{type: HAL_OPENAI_NOISE_REDUCTION}` for `far_field` / `near_field`; the key is omitted for `off` |
| `audio.input.turn_detection` | `null` when `HAL_REALTIME_TURN_DETECTION=off` (manual, client-bracketed turns). `server_vad`: `threshold` = `HAL_OPENAI_VAD_THRESHOLD` when non-zero, else `0.7` for `HAL_LIVE_VAD_START_SENSITIVITY=low` / `0.3` for `high` (API default 0.5 — a low sensitivity needs louder evidence, the OpenAI counterpart of Gemini's `START_SENSITIVITY_LOW` echo defence); `prefix_padding_ms` = `HAL_LIVE_VAD_PREFIX_PADDING_MS` when > 0; `silence_duration_ms` = `HAL_LIVE_VAD_SILENCE_MS` when > 0. `semantic_vad`: `eagerness` = `HAL_LIVE_VAD_END_SENSITIVITY` when `low` / `high`. Only the knobs that are set are sent; the API default stands for the rest. Logged once at connect as `[realtime] server VAD: type=… threshold=… prefix_padding=…ms silence=…ms eagerness=…` |
| `tools` / `tool_choice` | the registered function tools, `auto` |
| `reasoning.effort` | `HAL_OPENAI_REASONING_EFFORT` |
| `truncation` | `{type: "retention_ratio", retention_ratio: 0.5}` |

**Commit.** `commit_audio()` is a no-op on OpenAI whenever server VAD is on
(`_sync_commit` logs `Commit skipped — server VAD brackets the turn` at DEBUG):
the server commits the buffer and creates the response on its own
`speech_stopped`, and a client commit would land on an empty buffer while a
second `response.create` collided with the server's — the same rule as
Gemini's `activityEnd`. In manual mode it sends `input_audio_buffer.commit`,
logs `[realtime] Turn timing: local_end->commit_sent=Nms`, then waits up to
`response_wait_s` (10 s) for any active response before `response.create`
(`[realtime] Timed out waiting for active response to finish — forcing new response`
otherwise). First-audio latency is logged as
`[realtime] Response latency: Nms (speech_end->first_audio; commit_sent->first_audio=Nms)`
in manual mode and `… (speech_end->first_audio; server VAD)` with server VAD.

**Errors.** An `error` event whose code is `input_audio_buffer_commit_empty`
(local VAD ended a turn shorter than the 100 ms minimum),
`conversation_already_has_active_response` (server VAD and a client commit
crossed) or `response_cancel_not_active` (the response already finished), or
whose `event_id` is `hal-truncate`, is logged at INFO as
`[realtime] Realtime API notice (<code>): …` and ignored. Any other `error`
still fails fast: `OpenAIRealtimeError` → `_fail_fast_turn("api error")` pushes
the `TurnDoneEvent` now and the reconnect (2 s → 60 s exponential backoff) runs
in the background.

Text (`send_text`) and image inputs are `conversation.item.create` user
messages (`input_text` / `input_image` data URI); tool results are
`function_call_output` items, followed by `response.create` only when
`trigger_response` is set.

Still Gemini-only, unchanged: the `look` in-session vision tool, Google Search
grounding, session resumption, `requires_fresh_session`, and the `end_turn()`
override.

`hal/test/test_openai_live_provider_metrics.py` (21 tests) mirrors
`test_live_provider_metrics.py` for OpenAI: shared input key, late-transcription
attribution, barge-in drain / announce / gen bump / truncate, benign vs. fatal
errors, the usage line, and the GA session payload validated against the SDK's
`RealtimeSessionCreateRequest`.

### OpenAI connection safety

The OpenAI agent shares a single `RealtimeConnection` between its send and recv
threads. All connection writes, the connection swap during reconnect, and
teardown run under a reentrant lock (`_conn_lock`); the long blocking recv
iteration runs **outside** the lock on a connection snapshot so audio sends are
never starved mid-turn. The recv thread takes the lock only briefly for its one
write — the `conversation.item.truncate` on barge-in — and re-checks that the
snapshot is still the current connection before sending. Reconnect is idempotent
(re-checks `_connected` under the lock) and `_drop_connection()` only nulls a
connection that is still current, so the two threads can't tear down or rebuild
each other's connection.

### GPT-Live (`gptlive`): full-duplex, synthesized turns

`voice_agent/gpt_live.py` `GPTLiveAgent` speaks OpenAI's **Live API**
(`gpt-live-1`, GA in the API on 2026-09-10) — **a different API** from the
Realtime API above, not a new model name for `openai_realtime.py`. The wire has
none of the things the `VoiceAgentBase` contract was built around (no turn
boundary, no VAD, no interruption event, no tools), so the adapter
**synthesizes** them; every item below says whether it is on the wire or
derived. Selected by `realtime.provider = gptlive` in
`orchestrator._make_agent`; its prompt is `system_prompt_gptlive.md`
(`PROVIDER_PROMPT_PATHS`). API facts from
`developers.openai.com/api/docs/guides/live`, `live-migration`,
`live-delegation` and the openai 3.14.1 SDK (`openai.types.live`).

> **Device-untested as of 2026-09-16.** There is no OpenAI key on the device or
> on the dev Mac, and the `campaign-api` proxy does not serve the
> `…/ws/openai/live/sessions` route yet (verified 2026-09-16: every `/ws/openai`
> variant 404s as well), so the adapter has been validated by SDK-schema checks
> and the unit tests in `hal/test/test_gptlive_live_provider_metrics.py` only
> (22 tests, one of them through `RealtimeOrchestrator.stream_output`) — never
> against the live API. The proxy route is being added; nothing on the device
> changes when it lands (see *Wire* below).

**Wire.** GPT-Live uses the **same base URL as OpenAI Realtime**:
`HAL_GPTLIVE_BASE_URL` > `realtime.gptlive.base_url` > `REALTIME_OPENAI_BASE_URL`
(`HAL_OPENAI_REALTIME_BASE_URL` > `realtime.base_url` > `<llm_base_url>/ws/openai`).
The SDK appends `/live/sessions` and switches to `wss`, so through the proxy the
two OpenAI providers dial sibling paths with the same `Authorization: Bearer`
header:

| Provider | Dials |
|---|---|
| OpenAI Realtime | `wss://…/ws/openai/realtime?model=<model>` |
| GPT-Live | `wss://…/ws/openai/live/sessions` (model in the `session.start` payload, not the URL) |

**Relay status and contract** (BFF *GPT-Live voice sessions — WebSocket* device
integration doc): the route is on **staging**
(`wss://campaign-api.staging.autonomousdev.xyz/api/v1/ai/v1/ws/openai/live/sessions`,
branch `feat/openai-live-proxy`), not on production yet. To test from a device
whose `llm_base_url` is production, set
`HAL_GPTLIVE_BASE_URL=https://campaign-api.staging.autonomousdev.xyz/api/v1/ai/v1/ws/openai`
in `/opt/hal/.env`. Until production serves the path the connect fails with HTTP
404, the agent logs `Reconnect failed … next retry in ~Ns` and stays in its
2 s → 60 s backoff (turns fall back to the main agent). What the relay does and
what the adapter does about it:

- **Auth.** The device never holds an OpenAI key: the SDK sends the resolved
  key (`llm_api_key`, the device's lobster key) as `Authorization: Bearer`, the
  BFF swaps in its own OpenAI credential upstream, meters the session under
  provider `openai_live` (`ceil(seconds × 17)` tokens per voice row) and checks
  the device's rate limit at connect and after every usage row. No query string
  is forwarded (GPT-Live takes none); only this exact path is exposed — the
  sideband/fork sockets are not.
- **Correlation.** Every connect sends `x-request-id: hal-<12 hex>`; the BFF
  prefixes the session's usage rows with it (`<id>:voice-N`). The id is in the
  HAL `Connecting to GPT-Live (… x-request-id=…)` line and in every
  `gptlive_usage.log` line (`request=`), so a device log joins `lobster_usage`.
- **Close codes.** HTTP `401` at the handshake = no key; `4001` no key, `4002`
  GPT-Live not configured on this BFF, `4029` device over its usage limit
  (`GPT-Live usage limit reached`, also mid-session) — these will not change on
  their own, so `_note_close_code` logs the meaning and jumps the reconnect
  backoff straight to its 60 s ceiling instead of ramping. `1011` = the BFF
  could not reach OpenAI (retried with the normal backoff); `1000`/`1008`/other
  are OpenAI's own close codes forwarded unchanged; a `1006` with no close
  frame means upstream vanished (reconnect = a brand-new session).
- **Graceful close.** `disconnect()` sends `session.close` and keeps reading
  until `session.closed` (up to `close_timeout_s`, 5 s) before closing the
  socket, so the relay records the final usage as confirmed
  (`final_confirmed: true`) instead of billing from its own clock. The
  reconnect path never waits (the recv thread is the one that would read it).
- **Continuous audio.** The relay reaps idle hops (~126 s seen on the Gemini
  path), so in LIVE mode the mic stream must keep flowing, silence included;
  the idle park (`HAL_GPTLIVE_IDLE_PARK_S`) closes a session cleanly before that.

**Transport (on the wire).** `client.live.connect()` (`openai>=3.14.1`,
`openai.resources.live`) opens the primary WebSocket to `/v1/live/sessions`,
not `/v1/realtime`. The adapter sends `session.start` (client `event_id`
`hal-start`) and waits for `session.started` before any other command — a send
blocks up to `start_timeout_s` (10 s) and is then dropped
(`[realtime] GPT-Live session not started within 10s — dropping send`). The
`session.start` payload (`_build_session`, validated against the SDK
`SessionConfig` in the tests) is

```
{model, instructions, audio: {format: {type: "audio/pcm", rate}, output: {voice}}, delegation: {type: "client"}}
```

— no `tools`, no `turn_detection`. One PCM format serves both directions
(`HAL_GPTLIVE_SAMPLE_RATE`, 16000 or 24000), so `output_sample_rate ==
sample_rate`. Mic audio goes out as `session.input_audio.append` (base64 PCM16
mono, `event_id` `hal-audio`). `delegation: {type: "client"}` keeps the work in
this process (→ the main agent); `responses` would hand it to an OpenAI-hosted
model instead of our brain. The SDK gets no `on_reconnecting`, so it never
reconnects on its own; the adapter's send/recv loops own recovery with the same
2 s → 60 s backoff and fail-fast discipline as the other providers (`_conn_lock`
serializes writes and connection swaps, the blocking recv iteration runs outside
it, as in the OpenAI agent). Connect logs
`Connecting to GPT-Live (base_url=…, model=…)` (`(SDK default)` when the URL is
empty), `[realtime] GPT-Live session.start sent (voice=…)`, then
`[realtime] GPT-Live session open (id=…, expires_at=…, voice=…)`.

**Server events (on the wire).** `session.started`,
`session.input_transcript.delta` (fragments with `start_ms` / `end_ms` that
explicitly "do not define complete turns"), `session.output_audio.delta`,
`session.output_transcript.delta`, `session.delegation.created` (metadata only:
`id`, `target`, `offset_ms`), `session.usage.updated`, `session.closed`
(reasons `close_requested` | `expired` | `content` | `remote_hangup` |
`connection_lost`), `error`, `info`. Everything else (`session.updated`,
`session.*.appended`, `input_audio.muted` / `unmuted`, `response.event`) is
ignored. There is no `response.done` / turn-complete, no `turn_detection`
config, no audio commit and no interruption event: the model is full-duplex and
handles barge-in itself, so turn state is tracked client-side. Image input is
unsupported by `gpt-live-1`; a stray `ImageInput` is dropped with one
`[realtime] GPT-Live has no image input — frame dropped`.

**Synthesized contract.** How each item of the base contract is derived
(`_on_input_transcript`, `_on_output`, `_fire_boundary`, `_on_delegation`):

| Contract item | GPT-Live source |
|---|---|
| **turn boundary** (`TurnDoneEvent`) | none on the wire. A watchdog thread (`gptlive-watchdog`, 50 ms tick) calls `_fire_boundary()` → `TurnDoneEvent(execution_completed=True, user_turn_id=<owner>)` when no `session.output_audio.delta` / `session.output_transcript.delta` has arrived for `turn_gap_ms` (`HAL_GPTLIVE_TURN_GAP_MS`, 800). `OutputEvent.gen` is bumped at the start of every reply burst and on interruption |
| new `UserSpeechOutput` key (`gptlive-<hex>`) | no VAD events. LIVE_MODE only, from `session.input_transcript.delta` fragments (`method="provider_transcript"`, **never** an `endpoint_at`). A new input turn opens when there is no open turn, when the previous one was answered, or when the fragment's `start_ms − previous end_ms > input_gap_ms` (`HAL_GPTLIVE_INPUT_GAP_MS`, 1500). Each fragment is logged `[realtime] <<< user said: '…'` and appended to the turn's transcript |
| `UserSpeechOutput(transcript_finished=True)` | completion-only, emitted when the model starts answering the open input — the answer is the evidence that the input is complete. Completion-only metadata cannot open an input turn |
| `user_turn_id` on outputs / `TurnDoneEvent` | frozen at the first output of a reply burst from the open, unanswered input; empty for an unsolicited remark. When the boundary closes an answered input its key is released, so a later remark is not attributed to it |
| `InterruptedOutput(reason="output_reset")` | before the first `session.output_transcript.delta` of every reply |
| `AudioOutput` / `TextOutput` | `session.output_audio.delta` / `session.output_transcript.delta` |
| **barge-in** (`InterruptedOutput(reason="server_interrupt")`) | no interruption event. An input fragment arriving while a reply is streaming marks an overlap and shortens the boundary deadline to `interrupt_gap_ms` (`HAL_GPTLIVE_INTERRUPT_GAP_MS`, 400) — measured from the **last output**, so a model that talks straight through the user's words keeps pushing the deadline and is never cut off by HAL. If the model then stays silent for that long the reply is **interrupted**: `_drain_for_interrupt` drops what is still queued from the abandoned reply, keeping `UserSpeechOutput` / `ExecutionOutput` / a queued `server_interrupt` (a queued `TurnDoneEvent` becomes an `ExecutionOutput` in LIVE_MODE), bumps `gen`, emits `InterruptedOutput(reason="server_interrupt", at=<monotonic>, user_turn_id=<owner>)` (LIVE_MODE) and logs `[realtime] Reply interrupted by the user — dropped N queued output(s), gen=G`; the boundary is then `TurnDoneEvent(execution_completed=False)`. If the model keeps talking past `interrupt_gap_ms`, the overlap was a **backchannel** and the reply completes normally. The barge-in utterance stays open as its own input turn |
| `FunctionCallOutput` (+ `user_transcript`) | **no tools at the Live layer.** The session runs client delegation: `session.delegation.created` with `target: client` becomes `FunctionCallOutput(name="delegate_to_main", call_id=<delegation.id>, arguments={"message": <accumulated input transcript>}, user_transcript=<same>)`, which the orchestrator handles exactly like Gemini's tool call (`DelegateSignal`). A `responses`-target delegation is ignored. The event carries no task text and **can arrive before the sentence is complete in the transcript** (BFF doc §6), so the forward is never immediate: the watchdog forwards it once the input transcript has been quiet for 250 ms (`_DELEGATION_SETTLE_S`), or at the hard deadline `delegation_wait_ms` (`HAL_GPTLIVE_DELEGATION_WAIT_MS`, 500) with whatever has been heard — log `[realtime] Delegation <id> — settling the transcript (N chars so far, up to 500ms)`; a still-empty message is forwarded anyway and the orchestrator's "message must not be empty" result is relayed to the model as a failed handoff. Logged `[realtime] Delegation <id> → delegate_to_main(message='…')`. The transcript is emitted on the turn path too (no live metadata there) |
| `TurnDoneEvent.execution_completed` | `True` only for a `turn_gap_ms` boundary with no overlap; `False` on an interruption or a fail-fast |
| `note_server_activity()` | `session.started`, input/output transcript, output audio and delegation events — **not** `session.usage.updated` |

**Unavailable tools.** `express_emotion`, `reject_turn`, `end_conversation` and
`look` cannot exist on this provider. They are logged once at construction
(`[realtime] GPT-Live has no Live-layer tools — [...] unavailable on this
provider (only delegate_to_main, via client delegation)`), and a
`FunctionCallResultInput` for a call the adapter never emitted is ignored. So on
GPT-Live there is no in-session emotion, no AI reject filter, no explicit
hang-up tool and no in-session vision; visual questions delegate.

**Feedback to the model** goes through `session.thinking.append` (silent
context, `event_id` `hal-context` for `send_text` / `hal-delegation` for tool
results; content clipped to `_APPEND_MAX_CHARS` = 1600 chars ≈ the API's
500-token cap on `session.*.append`, rather than letting the whole line be
rejected):

- the orchestrator's `{"result": "delegated"}` ack → "The device's main agent
  has taken this request and will speak its own answer to the user. Do not
  answer it yourself and do not mention the handoff again; keep listening."
  with `delegation_id` = that delegation, which stays **pending**;
- `[TTS HISTORY] …` via `send_text` (the main agent's spoken reply) → attached
  to the **newest** pending delegation, which closes it, so the model knows the
  user has been answered and by whom;
- any other `send_text` (`[TURN CONTEXT]`, speaker corrections) →
  `delegation_id: null` (general session context);
- a non-`delegated` result → "The handoff to the main agent failed (…). Answer
  the user directly if you can, or tell them briefly that it did not work." and
  the delegation is dropped.

At most `_MAX_PENDING_DELEGATIONS` = 8 pending delegations are remembered
(oldest evicted). `session.commentary.append` (spoken) and
`session.instructions.append` exist in the API but are not used.

**Commit.** There is no commit on Live — the model decides when the user is
done. `commit_audio()` is a no-op in LIVE_MODE (the mic keeps streaming). On the
turn path it appends `commit_silence_ms` (`HAL_GPTLIVE_COMMIT_SILENCE_MS`, 600)
of PCM silence (`event_id` `hal-silence`) so the model hears the utterance end
instead of waiting for room noise to tell it.

**Errors and fail-fast.** A server `error` whose `client_event_id` is one of
ours (`hal-audio`, `hal-silence`, `hal-context`, `hal-delegation`) means a
command of OURS was rejected (context over 500 tokens, audio before
`session.started`, …) → `[realtime] GPT-Live rejected <id> (<code>): …` at
WARNING and the session continues. An error with **no** client id, or one on
`hal-start`, → `[realtime] GPT-Live error (<code>): …` → `GPTLiveError` →
`_fail_fast_turn("api error")`: a reply in flight is closed with
`execution_completed=False`, a pending input gets a bare `TurnDoneEvent`, and
the background reconnect (2 s → 60 s) runs. The event iteration ending
(`session.closed`, any reason — logged `[realtime] GPT-Live session closed
(reason=…)`) → `_fail_fast_turn("session closed")` + reconnect; any other
exception → `_fail_fast_turn("unexpected")`. A reopened session inherits no
input ownership and no pending delegations (`_reset_turn_state`). Disconnect is
graceful (`session.close` first, so the server answers with the final usage).

**Latency log.** `[realtime] Response latency: Nms
(last_input_transcript->first_audio; full duplex)` — approximate, since the
input transcript itself lags the speech.

**Base hooks left at their defaults, and why.** `end_turn()` no-op: there is
no `_turn_done` gate — nothing waits for a provider turn end before the next
send (no `response.create`). `requires_fresh_session` `False`: a delegation the
client never answers does not make the session refuse input (unlike Gemini's
`1008`). `output_sample_rate == sample_rate`: a Live WebSocket has one PCM
format for both directions.

**Pricing.** $0.05 per session-minute, billed per second (plus the delegated
main-agent work separately) — versus token pricing on the Realtime API. Per-
minute billing means an **open idle session costs money**, so the
orchestrator's idle park covers this provider too: `_idle_park_threshold()`
returns `HAL_GPTLIVE_IDLE_PARK_S` (default `30`; `0` disables) for `gptlive`,
`HAL_GEMINI_IDLE_PARK_S` for Gemini and `0` (never) for OpenAI Realtime, whose
idle sessions are free. After that many seconds without turn activity
`_maybe_park_idle_session` closes the transport (`[realtime] Ns idle (>= 30s) —
parking gptlive session …`); the next turn's `prepare_turn()` reconnects
synchronously, audio buffered across the handshake, exactly as for Gemini. The
Gemini-only pre-turn recycle is not applied. Usage lines go to
`gptlive_usage.log` — see *Pricing & usage logs*.

**Prompt.** `system_prompt_gptlive.md` follows the Live prompting-guide
structure: Role and tone → Backchannel policy → Interruption policy → When NOT
to speak → Delegation policy (*Backend tools* / *Delegate to the backend when*
/ *Do not delegate when*) → the context streams it receives → examples. It
carries no tool-call syntax and no ElevenLabs voice tags (the model's output
transcript is a transcript of speech, not TTS input). The
`delegate_to_main(message=…)` lines in its examples name the silent backend
handoff so the shared find-delegation test
(`hal/test/test_realtime_find_delegation.py`) can pin the rule text; the prompt
says it is never spoken.

### Pipecat v1 (`pipecat_v1`): on-device pipeline, text out

`voice_agent/pipecat_v1.py` `PipecatV1Agent` is the provider that has no
provider: the "server" is a [Pipecat](https://github.com/pipecat-ai/pipecat)
pipeline running inside HAL, built on a private asyncio loop (`pipecat-io`
thread) by `voice_agent/pipecat_pipeline.py`. Audio goes in, **text** comes out
and HAL's own TTS speaks it — the model never produces audio, so
`REALTIME_NATIVE_AUDIO` is meaningless here and there is no voice knob. It was
built after the audio-path analysis of 2026-09-18 (AEC, the playing-state and
barge-in decisions can only live on the device that plays the audio; see
`audio-path-design.md`), so the whole pipeline stays on the robot and only the
model calls leave it. Selected by `realtime.provider = pipecat_v1`; its prompt
is `system_prompt_pipecat.md` (`PROVIDER_PROMPT_PATHS`) — the OpenAI prompt
with a preamble saying the model reads an STT **transcript**, not audio, and
writes text for TTS.

**Install.** `pipecat-ai` is the optional extra `pipecat` in
`hal/pyproject.toml`. Lamp setup and Pi/OrangePi images select
`uv sync --python 3.12 --extra hardware --extra aec --extra pipecat`;
non-Reachy OTA preserves this selection. A bare developer `uv sync` does not
select the extra automatically. It is not a hard dependency: the core package drags `numba` +
`llvmlite` (~140 MB), `resampy`, `nltk` and pins `onnxruntime ~=1.24`, which
conflicts with the `reachy` extra's `onnxruntime==1.27.0` — the two extras are
declared mutually exclusive (`[tool.uv] conflicts`) because they never share a
body. Without the package `PipecatV1Agent` raises `PipecatV1Error` at connect
and the orchestrator treats the provider as unavailable (main-agent fallback),
exactly like a vendor outage. Silero VAD, Smart Turn v3 (bundled
`smart-turn-v3.2-cpu.onnx`, 8.7 MB) and the OpenAI-compatible LLM service ship
in the core package; no vendor STT extra is needed (below).

**Pipeline** (one per provider session, rebuilt on every `recover_session`):

```
PipelineWorker.queue_frame ─▶ HALSTTService ─▶ user aggregator ─▶ OpenAILLMService ─▶ EventSink ─▶ assistant aggregator
   InputAudioRawFrame          (device STT)     (VAD / turns)      (text + tools)      (→ _ev_* on the agent)
```

There is **no transport**: HAL owns the mic and the speaker, and the agent's
send thread pushes each `AudioInput` (float32 → PCM16) into the pipeline head
with `worker.queue_frame`. `EventSink` is a pass-through `FrameProcessor` that
reports frames to the agent as plain method calls (`_ev_text`,
`_ev_response_started/ended`, `_ev_calls_started`, `_ev_call_result`,
`_ev_user_turn_started/stopped`, `_ev_interruption`, `_ev_error`, metrics), so
all of the `VoiceAgentBase` contract is produced in `pipecat_v1.py` without a
`pipecat` import and is unit-tested there
(`hal/test/test_pipecat_v1_agent.py`, 17 tests).

**STT is the pipeline's, on the device's own provider.** `pipecat_stt.py`
`HALSTTService` is a Pipecat `STTService` over HAL's `STTProvider`
(`AutonomousSTT` — the Deepgram-compatible relay behind campaign-api on the
`llm_api_key` — or `DeepgramSTT`): Pipecat's stock STT services dial the
vendor's public endpoint with a vendor key the device does not have.
`VoiceService` hands its provider to the orchestrator (`stt_provider=`), which
passes it to the agent, so transcription uses the same relay, key, model and
boost terms as the turn-based path; without one the agent builds an
`AutonomousSTT` from `HAL_PIPECAT_STT_*`. All provider I/O (open, send, close)
runs on one `pipecat-stt` sender thread so the blocking `close()` (which joins
the recv thread for up to 15 s while the server flushes the last transcript)
never stalls the asyncio loop; transcripts are handed back to the loop with
`call_soon_threadsafe` as `TranscriptionFrame(finalized=True)` for finals and
`InterimTranscriptionFrame` for interims, and to the agent (`_ev_transcript`)
for `FunctionCallOutput.user_transcript` and live-mode `UserSpeechOutput`.

**Both `HAL_LIVE_MODE` shapes, one class** — the mode is read at construction
(`config.LIVE_MODE`), the same way the rest of HAL is exclusive by design:

| | `HAL_LIVE_MODE=false` (turn-based) | `HAL_LIVE_MODE=true` (live) |
|---|---|---|
| who ends the user's speech | HAL's VAD + shared provisional turn gate (`HAL_TURN_END_*`) → `append_audio` × N, `commit_audio` | the pipeline: Silero VAD opens the turn (`HAL_PIPECAT_VAD_*`), Smart Turn v3 closes it (`HAL_PIPECAT_SMART_TURN`, else a `HAL_PIPECAT_SILENCE_TIMEOUT_S` silence timeout) |
| turn strategies | `ExternalUserTurnStrategies`: the first frame after a commit queues `ProposedUserStartedSpeakingFrame` (which also broadcasts an interruption, cancelling a reply still streaming from the previous turn); `commit_audio` queues `ProposedUserStoppedSpeakingFrame` **and** an `STTFinalizeFrame`. A subclassed stop strategy (`_CommittedTurnStopStrategy`) finalizes the moment the committed STT final lands instead of waiting the stock 0.5 s aggregation timeout | Pipecat defaults: start on VAD / transcription, stop on the turn analyzer; an onset while the model is answering broadcasts an interruption |
| STT session | **per turn**: opened on the first frame, closed by the `STTFinalizeFrame` — a system frame pushed *through the pipeline* behind the audio so it reaches the STT stage only after every frame of the utterance (signalling the sender thread directly raced the audio still in flight and finalized an empty session; fixed 2026-09-18). `close()` sends CloseStream, the server flushes the final. A turn whose session closes with no text at all ends at once (`TurnDoneEvent(execution_completed=False)`) so HAL falls back with its own transcript instead of waiting out `REALTIME_RECV_QUEUE_TIMEOUT_S` | **one long session** for the pipeline's life, reopened if the relay drops it, kept alive with `KeepAlive` every 5 s while nobody talks; the provider's own end-of-turn (Flux `TurnInfo`) just arrives as a final |
| `UserSpeechOutput` | none (no live metadata on the turn path) | turn start (`method="server_vad"`), each STT **final** as the part not emitted yet (`method="provider_transcript"`, `transcript_finished=True`), turn stop (`endpoint_at`) — the same keys the live pump reads from Gemini/OpenAI. Interims are never surfaced: STT hypotheses are cumulative and get rewritten mid-utterance (`place a music` → `play some music`), and the pump's `LiveHistory.input` concatenates chunks it cannot retract — on lamp-ee17 the rewrite landed in the `[HANDLED]` text. Interims still feed MinWords and `note_server_activity()` |
| barge-in | the next utterance's first frame interrupts the pipeline; no `TurnDoneEvent` is emitted for it (the orchestrator left the old turn long ago and a late terminal could only end the new turn empty) | `InterruptedOutput(reason="server_interrupt", at=…)` + `TurnDoneEvent(execution_completed=False)`, `gen` bumped. While the model is generating, a new turn needs `HAL_PIPECAT_MIN_WORDS` transcribed words to open (`_BusyAwareMinWordsStrategy`); otherwise a one-word burst right after the question cancels the reply |
| commit | proposes the stop + finalizes STT; a commit with **no audio** since the last one ends the turn immediately | ignored (a live session never commits) |

Note the turn-based path runs STT **twice** on the same utterance — HAL's own
session (wake word, noise guard, `[transcript]`, dispatch) and the pipeline's —
which doubles STT cost on that path; live mode has no HAL STT session, so
there the pipeline's is the only one. The mode this provider is really for is
live.

**Tools are bridged one-for-one.** Every orchestrator tool
(`delegate_to_main`, `reject_turn`, `express_emotion`, `end_conversation`,
`web_search` — pipecat-only, below; `look` is Gemini-only and never registered)
is registered on the LLM service as a `FunctionSchema` + handler. The handler publishes `FunctionCallOutput(name,
arguments=<JSON>, call_id=<tool_call_id>, user_transcript=<STT finals of this
turn>)` and awaits the orchestrator's `FunctionCallResultInput` for that
`call_id` (bounded by `HAL_PIPECAT_TOOL_RESULT_TIMEOUT_S`, after which the
model gets `{"error": "no result from the device"}` and no follow-up so the
pipeline never wedges); the result's `trigger_response` becomes Pipecat's
`run_llm`. Two exceptions are decided by name: **`delegate_to_main` and
`reject_turn` never run the model again** (`_NO_FOLLOWUP_TOOLS`). The
orchestrator acknowledges those with `trigger_response=True` (a Gemini
pending-tool-call rule, not a wish for a reply) and then leaves the turn
(`end_turn()` + `DelegateSignal` / `RejectSignal`), so a follow-up could only
ever be spoken as a stale answer on the *next* turn — the local smoke run
answered "How are you?" with the music-genre follow-up of the previous
delegation before this rule existed. `express_emotion` (`trigger_response=True`
since the ack patch) and `end_conversation` do run the model again. Because a
35B-A3B model weighs recency, the action rule is stated twice: as a `## FINAL
RULE` appended to the system instruction, and as a user-role `[RULE] …` line
(`_TOOL_RULE`, ~60 tokens) re-injected right before every utterance
(`PipelineHandle.remind_tools`, from the first frame on the turn path and from
`_ev_user_turn_started` in live mode, so it lands before the aggregator appends
the transcript). It is a user-role line because the Qwen relay rejects a system
message anywhere but first (`System message must be at the beginning`, HTTP
400). Without it, on lamp-ee17 the boot whose realtime-memory summary was 8 k
chars answered "please play some music" with "You got it, what vibe?" while the
same pipeline with a 3 k summary delegated; with it, the request delegates.

**Web search — the `web_search` tool (pipecat only).** The Qwen relay has no
hosted search, so until this tool every public live fact (weather, news, scores,
prices) cost a `delegate_to_main` round-trip. With `HAL_PIPECAT_WEB_SEARCH` on
(default) and `realtime.provider = pipecat_v1`, the orchestrator registers one
more function tool, `web_search(query)` (`WEB_SEARCH_TOOL`, gate
`_web_search_available()`), bridged like the others. Its handler
(`_handle_web_search_call`) runs `hal/realtime/web_search.py`
`grounded_search`: one POST to the campaign-api Google-Search relay
(`HAL_PIPECAT_SEARCH_URL`, a Gemini *Interactions* endpoint) with
`{"model": <HAL_PIPECAT_SEARCH_MODEL>, "input": <query>, "tools":
[{"type": "google_search"}]}` and the chat key as Bearer. The reply is an
Interaction — `status`, and `steps[]` of `google_search_call` (the queries Google
ran), `google_search_result`, `thought`, `model_output` — and
`parse_interaction` takes the `model_output` text (an already-grounded
**answer**, not a list of links), strips Gemini's markdown, caps it at
`MAX_ANSWER_CHARS` (1200) on a sentence boundary and collects the distinct
citation titles (`wikipedia.org`, …). The model receives `{"result": <answer>,
"sources": [...]}` with `trigger_response=True` → `run_llm=True`: the follow-up
response is the spoken answer, in the user's language, and its end closes the
turn. The lookup blocks the turn's output loop (~4 s measured 2026-09-21) while
the pipeline's tool future waits, which is why `HAL_PIPECAT_SEARCH_TIMEOUT_S`
(10 s) must stay below `HAL_PIPECAT_TOOL_RESULT_TIMEOUT_S` (15 s); on the
turn-based path the dead-air filler (`REALTIME_FILLER_DELAY_S`, 1.5 s) and the
thinking cue already cover the wait. Every failure (timeout, non-200 from the
relay, empty answer) is still acknowledged — `{"error": …, "hint":
"…delegate_to_main…"}` — so the model either says it could not check or
delegates the question, which is where it went before the tool existed; an empty
`query` is refused without a request. `system_prompt_pipecat.md` lists public
lookups under *Direct Home Run* (search, then speak in the same turn; no text
before the call; one search per question; private/account live data still
delegates) and no longer routes weather/news to main when the tool exists.
Gemini and GPT-Live never see this tool: they ground on their own side.
`realtime.pipecat_v1.web_search` in config.json (`PipecatV1Realtime.WebSearch
*bool`, nil → HAL default on) keeps an operator's override across os-server
re-saves. The web Settings page (`/setting#realtime`, provider *Pipecat v1*)
exposes it as the **Web search** checkbox: it rides the `realtime` block of
`PUT /api/device/config` / MQTT `realtime.set` as `web_search` (pipecat_v1
only — sent by the page only for that provider, rejected server-side for any
other), lands in the sub-object as an explicit override, restarts HAL like
every realtime edit, and is read back resolved as `realtime.web_search` in
`GET /api/device/config` (`RealtimeWebSearch()`; omitted for other providers).
Pinned by `hal/test/test_realtime_web_search.py` (20 tests: the relay's
response shape, the ack contract, the registration gate).

**Turn boundary and generations.** `OutputEvent.gen` is the **user-turn**
generation — bumped when a user turn starts and on an interruption, never per
response — so a tool-result follow-up stays in its turn. `TurnDoneEvent` is
emitted at `LLMFullResponseEndFrame` only when no tool call is pending
(`FunctionCallsStartedFrame` is broadcast **before** the end frame, the
`FunctionCallInProgressFrame`s may land after it); a call whose result came
back with `run_llm=False` ends the turn itself once the last pending call is
answered, one with `run_llm=True` hands it to the follow-up response's end.
`InterruptedOutput(reason="output_reset")` precedes the first reply of every
user turn (not every response — a tool-result follow-up must not reset the
TTS queue of the sentence already playing). `end_turn()` **fences** the current
generation: `_newest_output_gen` is raised past it and its `TurnDoneEvent` is
swallowed, so whatever the fenced turn still produces is dropped by
`receive()` instead of ending or being spoken on the next turn.
`note_server_activity()` fires on every transcript fragment, text delta,
tool frame and metrics frame, so a turn whose STT or LLM is still working is
kept alive by the silent-turn watchdog. Pipeline errors reach the agent through
the worker's `on_pipeline_error` event (Pipecat pushes `ErrorFrame`s
*upstream*, so a sink behind the LLM never sees one): a non-fatal one while a
response is open ends the turn with `execution_completed=False` (main-agent
fallback) instead of letting the response end frame report a completed empty
turn; a fatal one, or the pipeline run ending, drops the session
(`_recv_loop` polls `PipelineHandle.alive` every second and rebuilds with the
usual 2 s → 60 s backoff). Idle parking does not apply (`_idle_park_threshold` → 0): there is
no vendor session to park, and the STT socket only lives during a turn or a
live call.

**LLM.** `OpenAILLMService` against `HAL_PIPECAT_BASE_URL` (default
`https://campaign-api.autonomous.ai/api/v1/ai/v1/qwen/v1`, the low-latency
Qwen relay; the EternalAI gateway `https://vibe-agent-gateway.eternalai.org/v2`
serves the same model without a key and is what the local smoke tests use),
model `HAL_PIPECAT_MODEL` (`qwen/qwen3.6-35b-a3b`), `temperature` 0.7,
`max_tokens` 300, the system prompt as the service's `system_instruction`
(an initial system message in `LLMContext` is deprecated since Pipecat 1.9).
Qwen3 models can emit `reasoning` before `content` and Pipecat streams only
`content`, so thinking is disabled through
`extra_body.chat_template_kwargs.enable_thinking=false`
(`HAL_PIPECAT_DISABLE_THINKING`, set `false` for an endpoint that rejects the
extra body). `[TURN CONTEXT]` / `[TTS HISTORY]` `TextInput`s become
`LLMMessagesAppendFrame(role=user, run_llm=False)` — recorded, never a turn.
`ImageInput` is dropped with a warning. Measured on a dev Mac through the
EternalAI gateway (fake STT, so this is LLM only): first text 0.8–1.0 s after
end of turn, ~5.8 k prompt tokens per turn (the prompt), 2–30 completion
tokens.

**Usage log.** `pipecat_usage.log` (logger `hal.realtime.usage.pipecat`,
`server_support/log_setup.py`): one `first text +N.NNs after turn end` line per
reply (turn-based: after the commit; live: after end-of-speech), Pipecat's
per-stage `ttfb` lines, and `llm usage model=… in=N out=N` from the LLM
service's usage metrics — token counts only, no cost estimate.

**Device verification (lamp-ee17, 2026-09-18, `HAL_LIVE_MODE=true`,
uplink `mute`).** Pipeline build (Silero + Smart Turn ONNX load) ~12 s on the
A55; a live call opened by HAL's doorbell VAD, Flux transcribed "Hey, lamb.
What is the capital of France?", the pipeline answered `Paris.` with first
text 1.3–1.8 s after end of speech (11.5 k prompt tokens through the Qwen
relay) and ElevenLabs first audio ~1 s later; "Hey, lamp. Please play some
music for me." became `delegate_to_main(message="Please play some music for
me.")` → `[voice-instruction]` + `[transcript]` to os-server → the main agent
answered. Three things this run changed: `HAL_LIVE_UPLINK_DURING_PLAYBACK`
must be `mute` on this hardware — on `cancelled` the pipeline transcribed the
lamp's own reply (`Its parents` for "It's Paris") and answered itself for four
rounds, the same 7–13 dB AEC limit measured with GPT-Live; the VAD floor moved
to the far-field values (`0.85` / `0.7`) after the lamp answered a conversation
across the room; and `MinWords` / the `[RULE]` line above. Install on the lamp
is `uv sync --python 3.12 --extra hardware --extra aec --extra pipecat`
(pulls `onnxruntime 1.24.4`, `numba`, `llvmlite`; ~30 s from the cache).
`/var/log/hal/pipecat_usage.log` holds the per-turn lines.

**Known limits (2026-09-18).** Smart Turn v3 CPU cost on the A55 is not
measured yet (it runs once per pause, not per frame); the Qwen relay drops the
`tool_calls` payload from a **non-streaming** completion (`finish_reason:
"tool_calls"` with an empty message) — Pipecat streams, so the pipeline is
unaffected, but a non-streaming probe will look tool-less; whether the model
delegates still depends on that boot's realtime-memory summary (see the
`[RULE]` note) — `HAL_PIPECAT_TEMPERATURE` is the remaining knob; no image
input; `test_pipecat_v1_agent.py` covers the contract, not the pipeline — the
pipeline was verified by the local smoke runs (turn mode with a fake STT: "What
is two plus two?" → `Four.`, "Please play some music" →
`delegate_to_main(message="Play some music")`; live mode with synthesized
speech through Silero + Smart Turn: "What is the capital of France?" →
`Paris.`, "Please turn the brightness up a bit." → delegated with the
transcript) and the device run above.

## Pricing & usage logs

Every turn writes one token/cost line to a per-provider log under
`/var/log/hal/` (rotating, 5 MB × 3, configured in
`server_support/log_setup.py`): `gemini_usage.log` (logger
`hal.realtime.usage`), `openai_usage.log` (logger
`hal.realtime.usage.openai` — a child of the Gemini logger with
`propagate=False`, so the files stay separate and comparable
line-for-line), `gptlive_usage.log` (logger `hal.realtime.usage.gptlive`,
same pattern) and `pipecat_usage.log` (logger `hal.realtime.usage.pipecat`:
latency + token counts, no cost — see *Pipecat v1*). None reaches
`server.log`. The Gemini and OpenAI lines carry
per-modality token counts **and** an estimated USD cost, so a wrong rate can
always be re-derived later from the logged counts.

The OpenAI line (`_log_usage`, from the `response.usage` of every
`response.done`) is

```
[realtime] OpenAI usage: model=… in_text=N($…) in_audio=N($…) out_text=N($…) out_audio=N($…) +unattr(Nin/Nout) | cached=Ntok total=Ntok est_full>=$… est_cached>=$…
```

`+unattr` counts tokens OpenAI billed but did not tag text/audio (image input)
— unpriced, so `est_*` is a floor (`>=`). `cached` is the prompt-cache hit
(`input_token_details.cached_tokens`), re-billed at the discounted rate;
`est_cached` subtracts that saving from `est_full`. `cached=0` on every turn
means the cache is not hitting (session churn) — that is the cost red flag.
`in_text` is the input **context** billed this turn: it grows with a long-lived
session and should drop right after an idle recycle.

GPT-Live is billed per session-minute, not per token, so its line is written
on every `session.usage.updated` and on `session.closed` (not per turn):

```
[realtime] GPT-Live usage: session=<id> request=<x-request-id> seconds=<s> est>=$<s/60*0.05> context=<usage_ratio %|-> (cumulative|final; $0.05/min billed per second, delegated main-agent work billed separately)
```

`cumulative` on a usage tick, `final` on the close. The rate is
`_GPTLIVE_USD_PER_MINUTE` = 0.05 in `voice_agent/gpt_live.py`
(developers.openai.com/api/docs/models/gpt-live-1, verified 2026-09-16); the
main agent's own tokens for delegated work are billed on top.

Rate tables live in code, keyed `(direction, modality)` in USD per 1M tokens —
`_GEMINI_RATES` in `voice_agent/gemini_live.py`, `_OPENAI_RATES` in
`voice_agent/openai_realtime.py`. Unknown models fall back to the most expensive
table (cost ceiling, never an under-report). `_OPENAI_RATES` is matched **in
order as a substring** of the configured model: `mini` first (so
`gpt-realtime-2-mini` never resolves to the full-size row), then
`gpt-realtime-2`, then `gpt-realtime`.

| Model | text in | audio in | text out | audio out | cached text / audio in | audio↔token | Source |
|---|---|---|---|---|---|---|---|
| `gemini-2.5-flash-native-audio` | $0.50 | $3.00 | $2.00 | $12.00 | — | 25 tok/s | ai.google.dev pricing (verified 2026-06-29) |
| `gemini-3.1-flash-live` | $0.75 | $3.00 | $4.50 | $12.00 | — | 25 tok/s | ai.google.dev pricing (verified 2026-06-29) |
| `gemini-3.8-live` / `-extended-thinking` | $0.75 | $3.00 | $4.50 | $12.00 | — | 25 tok/s | ai.google.dev pricing (verified 2026-09-17; promo through 2026-12-31, doubles after) |
| `*mini*` (e.g. `gpt-realtime-2-mini`) | $0.60 | $10.00 | $2.40 | $20.00 | $0.06 / $0.30 | — | developers.openai.com/api/docs/pricing (verified 2026-09-16) |
| `gpt-realtime-2` | $4.00 | $32.00 | $24.00 | $64.00 | $0.40 / $0.40 | — | developers.openai.com/api/docs/pricing (verified 2026-09-16) |
| `gpt-realtime` | $4.00 | $32.00 | $16.00 | $64.00 | $0.40 / $0.40 | — | developers.openai.com/api/docs/pricing (verified 2026-09-16) |
| unknown OpenAI model | $4.00 | $32.00 | $24.00 | $64.00 | $0.40 / $0.40 | — | falls back to the table with the highest text-out rate (`gpt-realtime-2`) — a cost ceiling |

Cost anatomy is the same on both token-billed providers: `in_text` dominates (the ~7-10k
token system prompt plus accumulated session context is re-billed every turn
and grows until a session recycle — see `HAL_REALTIME_SESSION_IDLE_RESET_S` /
`HAL_REALTIME_SESSION_MAX_TURNS`); audio tokens are comparatively marginal.
Gemini additionally bills Google Search per grounded request on top of tokens.

## Orchestrator

`orchestrator.py` `RealtimeOrchestrator` wraps a single agent session and is the
only surface `voice_service` talks to:

| Method | Purpose |
|--------|---------|
| `start()` / `stop()` | Build the agent from config, connect, summarize memory on shutdown |
| `append_audio(frame)` | Queue one mic frame (non-blocking) |
| `commit_audio()` | Signal end-of-utterance (non-blocking) |
| `stream_output()` | Yield `AudioOutput` / `TextOutput` / `FunctionCallOutput`, or a `DelegateSignal` (then stop) |
| `send_text(text)` | Inject context (turn context, TTS history) as a non-response user message. Gemini Live skips this to avoid SDK `clientContent`/audio turn collisions; OpenAI still accepts it; GPT-Live sends it as silent `session.thinking.append` context (a `[TTS HISTORY]` line closes the newest pending delegation). |
| `send_function_result(call_id, output)` | Return a tool result to the model (on GPT-Live: silent `session.thinking.append` on the delegation — see *GPT-Live*) |
| `save_turn(user, agent)` | Persist a turn to realtime memory |
| `available` / `sample_rate` | Readiness + provider audio rate |
| `rebuilding` / `wait_until_available()` | Observe and briefly wait for an already-running replacement session without starting another rebuild |

## Context managers

The system prompt, device identity, device memory, and skills catalog are
assembled per agent gateway (`HAL_AGENT_GATEWAY`):

| Gateway | Class | Workspace |
|---------|-------|-----------|
| `openclaw` | `context_manager/openclaw.py` `OpenClawContextManager` | `HAL_OPENCLAW_WORKSPACE_DIR` (`/root/.openclaw/workspace`) |
| `hermes` | `context_manager/hermes.py` `HermesContextManager` | `HAL_HERMES_WORKSPACE_DIR` (`/root/.hermes`) |
| `picoclaw` | `OpenClawContextManager` (same layout) | `HAL_PICOCLAW_WORKSPACE_DIR` (`/root/.picoclaw/workspace`) |
| `codex` | `OpenClawContextManager` (same layout) | `HAL_CODEX_WORKSPACE_DIR` (`/root/.codex/workspace`) |
| `claudecode` | `context_manager/claudecode.py` `ClaudeCodeContextManager` — OpenClaw layout except skills, read from `.claude/skills/` (native claude CLI dir) | `HAL_CLAUDECODE_WORKSPACE_DIR` (`/root/.claudecode/workspace`) |
| `opencode` | `OpenClawContextManager` (same layout; like codex its skills live in a non-workspace dir `~/.config/opencode/skills`, so the workspace skills catalog is empty — identity + memory load correctly) | `HAL_OPENCODE_WORKSPACE_DIR` (`/root/.opencode/workspace`) |

`ContextManagerBase` (`context_manager/base.py`) handles prompt assembly
(`build_instructions`), turn persistence (`add_turn`), memory loading/trimming,
and summarization; subclasses implement `load_device_context`,
`load_device_memory`, `load_skills_catalog`, and `summarize_device_memory`.
Base prompts live in `resources/` (`system_prompt.md` plus per-provider
`system_prompt_openai.md` / `system_prompt_gemini.md` /
`system_prompt_gptlive.md`, registered in the context manager's
`PROVIDER_PROMPT_PATHS` map).

### Memory & summarization

Realtime turns are appended to a JSONL log (`HAL_REALTIME_MEMORY_PATH`, default
`<workspace>/realtime/memory.jsonl`), trimmed to `HAL_REALTIME_MAX_MEMORY_ENTRIES`
(keeping `HAL_REALTIME_MEMORY_TRIM_KEEP`). `RealtimeSummarizer` (`summarizer.py`)
condenses device + realtime memory via the **Anthropic Messages API**
(`HAL_REALTIME_SUMMARIZER_MODEL`, default `claude-haiku-4-5-20251001`).

The call is **retried** (`HAL_REALTIME_SUMMARIZER_RETRIES`, default 2, backoff
`HAL_REALTIME_SUMMARIZER_RETRY_BACKOFF_S`) because the failures come from the
gateway, not from the input: measured on lamp-0c89 03/09/2026, the same payload
returned 404 once and then succeeded on four of the next five attempts (one
timed out), while the LARGER payload containing it went through first time. A
dropped call otherwise costs the whole summary until the next session rebuild.
This includes a turn delegated or fallen back to the main agent: HAL persists the
user request before dispatch, then persists every opted-in main-agent TTS reply
fragment when it finishes speaking. `[TTS HISTORY]` still updates the current
live session immediately, but it is not treated as durable memory: an idle or
tool-call session replacement starts from the JSONL/summary instead.
For OpenClaw-layout runtimes (OpenClaw, PicoClaw, Codex, Claude Code, OpenCode),
the context manager also loads the workspace-root `MEMORY.md` in addition to the
derived device summary and recent `memory/*.md` files. Hermes instead uses its
native `memories/MEMORY.md`.
Summarization runs at `start()` (catch-up) and `stop()` (flush). The `start()`
catch-up runs in a **background thread** (after `connect()`), so the Anthropic
call never blocks the session from becoming `available` — otherwise an early
turn ("hello") right after a restart would leak to the main agent.

The summarizer prompt (`resources/summarize_prompt.md`) tells the model to put
any user request the entries don't show as answered, done or cancelled under a
final `## Open requests` heading, one timestamped bullet each. Those bullets
are not permanent: `expire_open_requests()` (`context_manager/base.py`) drops
every bullet whose leading `[<ISO-8601>]` stamp is
`HAL_REALTIME_SUMMARY_OPEN_REQUEST_TTL_S` (default 3600s) or more in the past
(a naive stamp is read as UTC), and the heading with them once none is left.
Expiry is per bullet, not per file: `summary.md` is rewritten on every session
with new entries, so on an active device its mtime never ages past the TTL —
the file age is only the fallback for a bullet without a parseable stamp. This
runs both where the summary is re-fed as `[Previous summary]` to the next
summarize and where it is loaded into session context — deterministic backstop
so a pending task can't sit in context indefinitely and get "answered" from
stale memory by a content-free nudge (#419, #421). `0` disables expiry.

## Live mode (full duplex)

**What it changes.** The local VAD stops being an endpointer and becomes a
**doorbell**: it decides when to OPEN a session, and once one is open it does
not run at all. The mic streams continuously and the **provider** owns turn
taking, end-of-turn and interruption. Enable with `HAL_LIVE_MODE=true`.

Measured on `intern-v2-6286` (2026-09-07, Gemini 3.1 Flash Live): **97 ms** from
last audio sent to first audio received, against 3-6 s commit→first spoken
sentence on the turn path.

**Exclusive by design.** The turn path and a live session need *opposite* turn
detection, and that setting is baked into the provider session at connect time.
Supporting both at once would mean a runtime override plus a session rebuild on
every entry and exit; making live mode a whole-process choice removes that
machinery entirely, at the cost of a restart to switch. So `HAL_LIVE_MODE=true`
**forces** `HAL_REALTIME_TURN_DETECTION` from `off` to `server_vad`
(`hal/config.py`) — that value is read at import by `GeminiConfig.vad_enabled`,
so it must be settled before those models are defined. An explicit non-`off`
value is left alone. Getting this wrong produces a device that streams audio
forever and never answers.

### Main-agent speech during LIVE

`live_active` still means the microphone session is open. The separate
`live_speaker_busy` property identifies active realtime playback (native PCM or
a synthesized realtime reply); an open idle mic does not suppress main TTS.
`/voice/speak-queue` evaluates that property during queue admission. A main
reply arriving during LIVE playback waits in the existing HAL pre-synthesis
queue rather than preempting LIVE or returning `suppressed`. Native, streamed
text and cached playback drain waiting speech after releasing their output.
A newer main run replaces older queued main segments; delayed older runs keep
the existing stale-turn rejection. No OS queue or retry mechanism is added.

LIVE output reset/session cleanup preserves pending main replies. A new
non-empty addressed input stops playback once per provider turn and clears the
queue; an explicit stop also clears it, including speech retained by a preceding
model reset. Existing OS cancellation policy remains unchanged. Confirmed-input
interruption requires `UserSpeechOutput`: Gemini, OpenAI and GPT-Live all emit
it in live mode (see *OpenAI Realtime: live-mode parity with Gemini* and
*GPT-Live*; GPT-Live's comes from transcript fragments, never a VAD endpoint). Mic streaming, server VAD, delegate routing and LIVE OFF
admission remain unchanged. Playback handoff tests use fake audio; acoustic
barge-in still needs a device test.

### Wake-word and focus gate at live entry

When `HAL_WAKEWORD_ENABLED` is enabled, local VAD alone cannot open live audio. After the music/noise checks, `_live_decision` requires an active focus window before preparing a live realtime session. Focus granted by the speech-start gaze check, a button or a prior accepted wake-word turn permits entry. If focus is missing or expired, the decision returns `turn` and uses the ordinary `_stream_session` STT path, independently of whether gaze is enabled or in shadow mode.

That STT path gives a provisional listening cue when a partial matches a wake phrase such as “Hello Lamp”; the final/assembled transcript must confirm it before normal turn processing. With Live ON, an authorized, non-noise opener enters the existing full-duplex live session when realtime is available. Its captured audio is sent once as pre-roll, then the same microphone keeps streaming while the provider answers or delegates. The regular manual-commit path stays disabled; no partial transcript starts live audio. The opener retains its STT interaction ID rather than creating a second metric turn. An unavailable provider or an unanswered opener falls back to the main agent once; an answered, rejected, delegated or superseded opener is not dispatched again.

Disabling wake-word gating permits VAD-only live entry. Disabling gaze or setting `HAL_GAZE_SHADOW=true` does not bypass the wake-word gate: shadow `WOULD_WAKE` is observational and grants no focus. The gate is checked only at entry; server VAD owns turn-taking inside an accepted live session. After hangup, the next entry checks focus again. Harness voice continues through its existing separate route.

Accepted LIVE follow-ups also refresh `HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S`,
once at a confirmed successful provider terminal or after delegation. The turn
must contain user speech and be addressed (including focus latched at live
entry). Rejected/interrupted turns, receive timeouts, empty input, model-only
output and Harness voice do not refresh it. A duplicate terminal cannot extend
it again. The deadline remains finite: after the configured idle interval,
the next session needs a wake phrase or another explicit focus grant.

### Completed Gemini live history

With `HAL_LIVE_MODE=true`, Gemini input transcription chunks now travel with their provider turn ID. `hal/drivers/voice/_internal/live_history.py` joins only input/output with that same ID and sends one `voice_agent_handled` notification after a successful provider terminal. Receive timeouts keep the partial turn open; duplicate terminals do not resend. Rejected, delegated, interrupted, unowned, or transcript-less replies are not recorded as completed exchanges. Input and output remain separate from the audio playback path; the existing output-reset behavior also resets the collected answer.

A single background worker sends completed exchanges using the existing interaction ID, reply-length cap and Harness routing snapshot. It drains completed notifications after live hangup without blocking playback. OS handles the notification through `externalhistory`, with silent delivery, disk persistence and the existing **History sync · Realtime → Main** web card. HAL buffering is bounded (64 incomplete turns, 64 queued notifications, 128 recent closed IDs); overflow/transport errors are logged. Durability starts only after OS accepts the notification. The OpenAI adapter now emits the same keyed input transcription chunks and terminals, so the join is provider-agnostic; it has only been device-verified on Gemini.

### HW emotion feedback in live mode

LIVE uses the same HW emotion calls as regular realtime turns, including their
LED, display and body behavior. `listening` requires nonblank provider input
transcription and the regular addressing rule: wake-word detection, active
focus, disabled wake-word gating, or the existing Harness listening allowance.
Transcript chunks are accumulated per provider turn. Addressing stays latched
for that turn only; focus arriving during the utterance can authorize it.
Opening a LIVE session, local RMS/Silero activity, blank provider VAD events and
local silence never start `listening` or `thinking`.

After a recognized, addressed utterance, an actual provider speech endpoint or
an explicit transcription-finished flag switches to the existing thinking
helper. An endpoint arriving before words waits for those words and addressing
evidence. A finished-only notification requires an existing input key and bypasses
metric/history speech observation; it does not create metric endpoints, events,
or change execution eligibility.
Without end evidence, listening expires after 8 seconds without transcript
updates; thinking expires after 25 seconds. Playback, rejection, interruption,
delegation and session exit clear the matching turn with the existing guarded
helpers. Stale outputs cannot clear a newer turn. Hardware calls run in order on
a worker; playback waits for its cleanup. Mic streaming, idle timing, server
VAD, barge-in, routing and metric calculations are unchanged.

### Voice metrics in live mode

Gemini live sessions use `hal/telemetry/live_voice.py` to map provider user turns to HAL
interaction IDs, tag native playback and carry the same ID into delegated OS
tasks. Only a correlated successful provider terminal completes realtime
execution; timeout, synthetic done and interruption alone are not completion
proof. Without a successful terminal the task remains incomplete. Server
interruption records a targeted `server_barge_in` boundary for stale-playback tracking.

Snapshots identify `mode=live` and whether a speech endpoint is known. Gemini's
activity receive timestamp remains available to voice cues (`server_vad_receive`)
but is not accepted as acoustic speech end for KPI-1. Raw/SDK endpoint counts
and offsets are traced without changing VAD configuration. Gemini transcript-only
or receive-only turns remain eligible for execution metrics
but are excluded from latency KPI-1 with `speech_endpoint_unavailable` and null
latencies. Valid earlier endpoints delivered late can amend playback latency.
Unowned output is not assigned to the newest utterance; LIVE look fillers pin
the exact provider-turn owner for the aim operation. Session-close
`voice_metrics_live_coverage` counters expose this missing coverage; these hooks
do not alter noise filtering, uplink or routing settings. See
[voice metrics](voice-metrics.md#live-session-coverage) for the event contract.

### Entry: two gates, three outcomes

`_vad_loop` confirms speech as usual, then `_live_decision()` returns one of:

| outcome | when | cost |
|---|---|---|
| `live` | trigger is speech and realtime is available | one live session |
| `turn` | wake focus closed, or realtime disabled/unavailable | STT confirms the wake phrase; an authorized opener enters live when available, otherwise uses the main-agent fallback |
| `skip` | music playing, or the trigger was **not speech** | nothing at all |

The second gate is not optional on a device with a wide entry VAD. A live
session bills upstream audio for as long as it stays open, so it must not fire
on a click — and the entry gate cannot be trusted with that alone: `webrtcvad`
accepted 7/7 non-speech probes where Silero rejected all 7, and
`HAL_SILERO_ENABLED` is `false` on some devices. `_rt_noise_is_speech()`
therefore carries its **own** Silero instance. Device-observed on
`intern-v2-6286`: a clipped codec transient opened a session every ~25 s all
evening. `skip` (never `turn`) is also why a rejected trigger costs no STT
session either.

### The uplink gate

Every mic frame reaches the model. The only per-frame decision is whether the
canceller can vouch for it, and a frame it cannot is **replaced by silence of
the same length, never dropped** — the uplink is a clock, and a splice is
exactly what a server-side VAD reads as an onset. Substitution happens *before*
resampling, so frames-out == frames-in by construction.

`HAL_LIVE_UPLINK_DURING_PLAYBACK`:

- **`mute` (default)** — substitute silence for the whole playback window.
  Ships today. Costs barge-in entirely: the user cannot interrupt until the
  device stops talking.
- **`cancelled`** — send the cancelled frame; substitute only frames
  `aec.uncancelled()` flags. True full duplex.

`cancelled` is the intended end state and is **not** the default, because the
canceller does not yet earn it. Measured 2026-09-04 (`barge-in-captures/`): mean
ERLE 14-19 dB but **peak ERLE ~5 dB** — mic peaks of 29264 leave the APM at
25269, against a real-interruption floor of 6956. The provider's VAD sees peaks
and has no echo defence of its own (it cannot see `uncancelled()` and applies
no duration floor), so it reads the device's own onsets as the user
interrupting. Flip to `cancelled` once a replay of `full40-bargein-off` puts
peak residual under that floor with margin.

Two independent "is the speaker live" signals feed the gate, because neither
alone suffices: `tts.speaking` is authoritative and works with **no canceller at
all** (with none, `aec.reference_idle_for()` returns `inf` and the gate would
never fire), while the reference tail adds the acoustic decay after the flag
drops. The gate also holds a monotonic tail from the observed end of TTS,
so disabling AEC or a missing binding cannot reopen the mic immediately onto
room echo. This also covers short gaps between queued playback segments.
`HAL_LIVE_PLAYBACK_TAIL_S` (default 0.35 s) is the *acoustic* tail, deliberately not
`AEC_TAIL_S` (2.0 s): keyed on the longer one, `mute` swallows the first two
seconds of every reply the user gives.

**Music is excluded by asking `music_service`, never by asking the gate.**
`aplay`/`paplay` write straight to ALSA and never reach the echo tap, so during
music `reference_idle_for()` reads as a perfectly quiet room.

### The output pump

In LIVE mode, input transcription may arrive after the model has already
started answering. Once output carries an input turn ID, a later transcript
for that same ID must not stop its realtime playback or clear queued external
TTS sentences (including ElevenLabs). A different addressed input can still
interrupt; main-agent playback retains its existing interruption behavior.

`_live_out_pump` loops `orchestrator.stream_output()` rather than reading the
agent queue directly. That reuses the entire existing tool surface — `look` +
replay, `express_emotion`, `reject_turn`, `delegate_to_main` — instead of
reimplementing it, and it re-reads `self._agent` on every outer iteration, so a
session rebuild cannot leave the pump reading a dead queue. `stream_output()`
returns once per model reply (`turn_complete`) and also on a quiet stretch when
`receive()` times out having yielded nothing; both simply mean "go round again".

`InterruptedOutput(reason="server_interrupt")` is emitted by `gemini_live` on
`content.interrupted` and by `openai_realtime` on
`input_audio_buffer.speech_started` while a response is active (or a
`response.done` with status `cancelled`), and is **the only voice-driven
interruption there is** — nothing local decides it. It stops playback at once. The turn-based path never sees one: manual VAD
gives the server no opening to emit it.

**Outputs are flushed once at session start.** Without it the session opens on a
stale output from the preceding turn — device-observed 2026-09-07: a
`reject_turn` queued by the previous noise turn was read in the same second as
session START, and the session then sat silent for its whole idle timeout.
`run_realtime_turn` flushes before every commit for this reason; a live session
commits nothing, so it flushes on entry instead. Once only — flushing per reply
would discard output the model is still streaming.

### Session recycling is suppressed

`set_live_active(True)` suppresses every post-turn recycle in `stream_output()`
for the duration of the session, because all three reasons are wrong inside one:

- **zombie** — a quiet stretch produces no output, so
  `REALTIME_ZOMBIE_RECONNECT_AFTER` of them (~24 s of the user simply not
  talking) would force a reconnect mid-conversation.
- **turn-cap** — swaps the session every `REALTIME_SESSION_MAX_TURNS` replies.
- **idle** — same swap, same open uplink.

A rebuild is also not survivable here the way it is between turns: the mic pump
keeps appending straight through the swap. Deferred, not cancelled —
`set_live_active(False)` resets the counters at hangup.

### What live mode does not do

Everything below hangs off the turn boundary and has no supply in a live
session. These are product trade-offs, not bugs:

| lost | consequence |
|---|---|
| STT transcript | no `[TURN CONTEXT]`, no transcript-based filters |
| per-turn wake word | checked at entry through STT when wake-word gating is enabled and focus is absent; a confirmed opener can enter live on the same capture. No local STT wake-word checks run inside an accepted live session |
| speaker ID, speech emotion | a session produces neither |
| local STT on delegation | `delegate_to_main` ends the session and forwards `[voice-instruction]` + the provider's own input transcription as `[transcript]` (Gemini `inputTranscription`, OpenAI `conversation.item.input_audio_transcription.*`, GPT-Live `session.input_transcript.delta`; `FunctionCallOutput.user_transcript` → `DelegateSignal.transcript`, read by `_live_out_pump`); the main agent's reply plays after hangup. A turn whose transcription never arrived forwards the instruction alone |

Also not run inside a session: the RMS entry gate, `SPEECH_HOLDOFF_S`, the
silence clock, `MAX_SESSION_DURATION_S`, the per-turn STT socket and its
keepalive (not even pre-connected — `stt_keepalive_on` is false in live mode),
the noise guard, the warm-mic drain and echo-skip, and `commit_audio`. None are deleted: the turn path still uses every one of them,
and gets them back the moment a session ends.

### Configuration

| Env | Default | Meaning |
|-----|---------|---------|
| `HAL_LIVE_MODE` | `false` | Whole-process live mode. Forces `HAL_REALTIME_TURN_DETECTION=server_vad` when that is `off` |
| `HAL_AEC_ENABLED` | `true` | Software AEC; setting `false` together with live mode selects the shared adaptive path for hardware-processed audio |
| `HAL_LIVE_UPLINK_DURING_PLAYBACK` | `mute` | On the software-AEC path: `mute` silences playback-time input; `cancelled` passes only AEC-cancelled frames; `always` passes every frame, including residual echo. The adaptive path applies its own gate |
| `HAL_LIVE_PLAYBACK_TAIL_S` | `0.35` | Acoustic tail after the last reference write or observed TTS end, including without AEC |
| `HAL_LIVE_IDLE_HANGUP_S` | `15` | Hang up after this long without user activity, playback or provider text/audio progress; measured from the latest of those events, including output before TTS starts |
| `HAL_LIVE_MAX_UNPROMPTED_REPLIES` | `3` | Hard ceiling on consecutive model replies with no user speech between them — breaks a self-talk loop without cutting one long answer short |
| `HAL_LIVE_MAX_S` | `600` | Absolute ceiling on one session |

The shared adaptive live path is selected by `HAL_LIVE_MODE=true` and
`HAL_AEC_ENABLED=false`. No extra flag or hardware-name detection is involved:
explicitly disabling software AEC opts into the path for already hardware-
processed audio. Software AEC defaults to `true` and retains its existing path.
Lamp `pro-respeaker-lite` and `pro-xvf3800` both select the shared path with
live mode on, software AEC off and `HAL_LIVE_UPLINK_DURING_PLAYBACK=always`;
standard/pro keep software AEC on. On Lite, voice capture and playback use ALSA
card `Lite` at 16 kHz; sensing uses `sndi2s4` through the profile's `asound.conf`.

`live_gate.py` (`AdaptiveLiveGate`), `live_playback.py` and `live_reply.py`
(`LiveReplyGuard`) implement the shared path. The gate uses an RMS energy
heuristic, not a speech classifier. It leaves capture running and substitutes
silence during playback until local speech is
confirmed, then sends up to 300 ms of buffered onset audio before live frames.
It holds the echo gate for 300 ms after playback ends.

For live entry, the shared adaptive path bypasses WebRTC VAD, the Silero entry
check and the realtime noise guard only when realtime is enabled and wake-word
checking is disabled or wake focus is already granted. Sustained energy above
the adaptive idle threshold for at least 160 ms still gates entry. With wake
checking enabled and no focus, the existing STT wake-word confirmation remains
required. The software-AEC path retains its existing entry checks.
Inside an adaptive live session, the idle-hangup clock also uses the
adaptive gate's speech state directly, without a second Silero check.

The idle RMS threshold is the larger of −42 dBFS (about 260 PCM16 RMS) and
four times the tracked noise floor, with a 160 ms onset. During playback the
threshold is the maximum of −27 dBFS (about 1,464 PCM16 RMS), five times the
noise floor, and the 500 ms output envelope multiplied by a learned echo
coupling factor plus an 8 dB margin. Playback onset confirmation takes 240 ms;
500 ms below threshold ends local speech. Like the demo, local barge-in waits
for 3 seconds of cumulative speaker audio for AEC warm-up. HAL counts frames
successfully written to the speaker, excluding TTS/network wait, from the
capture stream opening. Logical listening windows and individual sentences do
not restart this clock. During warm-up playback audio is gated and cannot
duck the speaker. A burst that starts during warm-up remains unaccepted even
when playback crosses 3 seconds: local VAD tracks the same burst until 500 ms
below threshold, then requires a new 240 ms onset. This matches the demo's
onset-time permission latch, avoiding false ducking at approximately 3.2 seconds
from sustained startup echo. Below-threshold playback frames also update the
noise floor when local VAD is not speaking, as in the demo. After playback ends,
the 300 ms echo tail uses idle speech thresholds and does not require playback
warm-up; a fresh 160 ms onset can pass with its buffered prefix. Idle mic audio remains available. `live-aec`
reports `played_s` and `aec_ready`; these indicate the time allowance, not a
measurement of AEC convergence. These are not measured end-to-end latencies.

The output envelope is estimated after temporary ducking and ALSA mixer
attenuation, using the PCM sent to the speaker and actual `amixer` dB readback.
Like the demo, the reference follows the duck ramp. Mixer
state is read once at startup outside the audio path and updated by volume-
control routes. Without a dB readback, the reference conservatively uses unity
gain; it does not infer gain from saved percentages or a hardware-specific
softvol curve. This avoids a pre-mixer level inflating the speech threshold
when attenuation is known. It estimates the output reference, not measured
acoustic sound pressure or exact audio latency.

Echo gating starts only after a successful speaker write and expires 250 ms
after the last write, followed by the gate's acoustic tail. Pending ElevenLabs
synthesis and failed/retrying TTS are not playback and must not mute capture.
A standalone Gemini `<no speech>` marker is discarded before TTS, including
when it arrives split across text events.

Playback ducking uses `HAL_LIVE_DUCK_GAIN` (default `0.12`, linear PCM gain,
not the system volume percentage) with 15 ms down / 80 ms up ramps.
Set it in the device's `/opt/hal/.env` and restart HAL. Valid values are
greater than zero and at most one; invalid values fall back to `0.12`.
This setting applies only to the hardware-AEC live path. The default comes
from the demo and is not a measured optimum for every lamp or room.
Both `pro-respeaker-lite` and `pro-xvf3800` profile `.env` overlays explicitly
ship `HAL_LIVE_DUCK_GAIN=0.12`; Standard/Pro software-AEC profiles do not set it.
A 500 ms stale
control watchdog restores gain. Local energy detection only ducks playback;
it does not cancel TTS or discard subsequent text. This matches the demo's
`duck` mode: only a provider interruption cancels playback and suppresses late
fragments of that reply, and clears both the playback and local duck latch.
Without confirmation, the first unduck check runs 1.5 seconds after onset, then
every 400 ms while VAD still reports speech (which ends after 500 ms below
threshold). Leaving the echo-risk window also releases duck. If Gemini has already finished generating, an
interruption event is not guaranteed during remaining ElevenLabs playback;
local ducking alone does not guarantee a complete stop in that case.
Native audio remains a separate setting and
is not enabled by selecting this path (except with Gemini TTS selected, where
Gemini Live speaks natively in the TTS voice — see `docs/os-server.md` Gemini
TTS); ElevenLabs remains supported. The threshold
reported through SSE follows the adaptive gate, and bounded `live-aec` logs
report active-session state at most once per second. This implementation is
inspired by the standalone hardware test, not an identical algorithm or a
claim of its measured interruption latency.

Changing `/etc/autonomous/hardware-profile` alone does not apply its files.
Select `pro-respeaker-lite` before installing/updating the device package;
a HAL restart alone reloads the existing `/opt/hal/.env` and ALSA configuration.

### Known limitation: `mute` can self-trigger

With `mute`, the uplink carries **digital silence** for the whole playback
window and real room audio after it. That transition is an amplitude onset, and
a server-side VAD reads an onset as somebody starting to talk — so the device
can answer *itself*. Device-observed 2026-09-07 on `intern-v2-6286` at 70 %
volume: four unprompted replies in 35 s with nobody in the room ("What's up?",
"I'm here. What can I do for you?"), each one re-arming the K hold, and one
logged `barge-in: model interrupted by the user` with no user present.

`HAL_LIVE_MAX_UNPROMPTED_REPLIES` bounds the damage — it ends a session once the
model has produced that many replies in a row with nothing from the user — but
it is a backstop, not a cure. It counts replies rather than seconds precisely so
that a single answer running for minutes is never mistaken for a loop. The cure is `cancelled` mode over a canceller good enough to earn it,
so the model always hears the true room with no artificial transitions. Lower
speaker volume makes it markedly less likely in the meantime.

The `pro-respeaker-lite` and `pro-xvf3800` profiles retain the hardware
demo server VAD start/end sensitivity `high` and prefix padding **100 ms**,
but use silence duration **1000 ms** instead of the demo's **500 ms** to allow
longer pauses within a sentence, at the cost of a later response. These use the existing `HAL_LIVE_VAD_*` settings;
software-AEC profile defaults are unchanged. Matching the local echo gate alone
does not match server speech detection.

### Ending a session

Hangup cancels the session's output reader and waits for it to exit before
another live session can reuse the provider queue. Queue waits check cancellation
at most every 100 ms without shortening the normal receive timeout. This prevents
an old reader from consuming and discarding the next session's transcript or
reply. A cancelled reader must not enqueue an unfinished ElevenLabs sentence.
An already-running tool handler finishes before the reader releases ownership.
HAL then queues Gemini `audio_stream_end` behind previously sent audio, bound to
the same transport. Manual-VAD, pending-tool and quarantined sessions skip it;
a replacement socket never receives the old end marker. This marks actual mic
uplink shutdown, not a commit on normal pauses.

A session ends after `HAL_LIVE_IDLE_HANGUP_S` (K, default 15 s) without user
activity, playback or provider text/audio progress. The mic goes straight back
to the VAD, which opens a new session on the next real speech. Two details
make this behave:

- **The model's reply is not user action**, but the clock is *held* while the
  device is speaking and refreshed by accepted provider text/audio output,
  including text arriving before ElevenLabs produces its first audio. The
  window starts at the latest user activity, playback end or model output.
  Thinking without new text/audio does not grant an indefinite exemption.
  `HAL_LIVE_MAX_S` and the unprompted-reply limit still apply to every live path.
- **RMS alone cannot carry this clock.** In a noisy room the floor sits above
  `HAL_VAD_THRESHOLD`, so every frame reads as "the user is talking" and the
  session never hangs up. Device-observed 2026-09-07 on `intern-v2-6286`: a
  ~10500 noise floor against a threshold of 500 held one session open
  indefinitely, and because a live session owns the mic, the VAD never ran
  again — the device went deaf until restart. So RMS is the cheap first gate and
  **Silero confirms** before the clock is refreshed, batched over
  `HAL_SILENCE_VAD_WINDOW_FRAMES`, exactly as the turn path's silence clock does.

`HAL_LIVE_MAX_S` is the backstop for a room so noisy that even Silero keeps
agreeing.

When transcript-only idle detection is enabled, recent hardware-AEC confirmed speech defers an expired idle hangup by one additional `HAL_LIVE_IDLE_HANGUP_S` window (15 s by default) while its transcript arrives. Recent means within the configured server silence duration (at least 1 s) plus 1 s delivery allowance. Local candidates cannot renew this extra deadline; accepted transcript or reply progress resets it. This prevents the idle timer from splitting a newly started utterance without letting continuous noise keep a session open indefinitely.

Read the counters in the session-END log line: `substituted` at ~100 % of
`during_playback` is `mute` working as designed.

## Turn flow (in `voice_service.py`)

1. **Construct + start.** `RealtimeOrchestrator(gateway=AGENT_GATEWAY)` is built;
   `start()` runs in a daemon thread (`realtime-start`) when `HAL_REALTIME_ENABLED`.
   TTS `on_speak_end` is hooked to feed spoken text back as `[TTS HISTORY]`,
   but **only when that speech opted in** (`TTSService.realtime_feedback`, set by
   the `realtime_feedback` flag on `/voice/speak[-queue]`). Only the agentic
   runtime's actual reply opts in — os-server sends it via `hal.SpeakReply` /
   `hal.SpeakQueueReply` (which `SendToHALTTS` / `SendToHALTTSQueue` use).
   The same opted-in fragments are appended to realtime memory, so a new Gemini
   session retains a main-agent answer rather than relying on the old socket.
   Hardcoded TTS (dead-air fillers, ambient mumble, backchannel, reconnect /
   health notices, local chitchat) goes through plain `hal.Speak` and is **never**
   fed back — otherwise the model would echo lines it never generated.
2. **Stream.** While the STT session is open, each mic frame is also resampled to
   the provider rate and sent via `append_audio()` (parallel, non-blocking), and
   buffered in `rt_audio_buffer`.
   When optional STT keepalive is enabled, a pre-connected STT socket that closes
   normally (WS 1000) at speech start is replaced before streaming continues and
   the complete pre-roll is replayed once on the fresh socket. This preserves the
   opening words; a recovered normal close is a warning, not an error.
   Gemini Manual VAD cannot cancel an already-streamed activity. Therefore an
   empty-STT/noise turn starts a clean replacement session rather than letting
   its noise contaminate the next user turn. That reconnect runs in the
   background: if the user speaks immediately, HAL keeps the entire next turn
   locally, then sends it once in order when the replacement session is ready.
   A slow/failed reconnect falls back to the main agent with the STT transcript;
   it never drops the opening audio or commits it to the old activity.
3. **Turn context + speaker-ID prepass.** `[TURN CONTEXT]` (time, reply-language
   reminder, current user) is sent as non-response text. The **current user is the
   VOICE speaker** identified this turn — it overrides the face-derived
   `current_user`, and falls back to the face identity when there is no voice ID
   (unknown / gate-reject / no transcript).

   **When each part runs depends on the mode**, because a voiceprint needs the
   completed utterance and therefore cannot exist at session open:

   | Mode | `[TURN CONTEXT]` sent | Speaker known? |
   |------|----------------------|----------------|
   | Always-listening (`wakeword=false`) | during capture once preparation completes, before uploading retained audio | Face fallback; corrected only if identity is ready before commit |
   | Open focus / gaze granted during capture | during that same capture once preparation completes | Face fallback; corrected only if identity is ready before commit |
   | Closed wake-word window | after capture and final wake-phrase confirmation | Any identity ready before commit |
   | Deferred (noise-drop rebuild) | on the ready replacement session, before replaying retained audio | Any identity available then |

   Opening or flushing a turn after capture is additionally gated on it **not**
   being noise. The noise guard has already classified the capture, so an
   empty-STT non-speech turn opens nothing: no `[TURN CONTEXT]`, no audio, and no
   replacement session. Skipping the send is what makes the skip-commit path free
   — otherwise the turn's whole buffer entered (and was billed by) an open
   activity that the very next step discarded. Sessions opened *earlier* in the
   capture (always-listening or authorized focus) have already streamed audio
   and are still discarded.

   In always-listening mode the speaker-ID prepass (`identify_and_decorate`, run
   **once** at session end) resolves the voice speaker *after* the context already
   went out with the face name. HAL then sends a `[TURN CONTEXT UPDATE]` correction
   naming the real speaker — still **before** `commit_audio()`, so it is part of the
   same turn. When the reply starts during STT final drain, identity resolves
   afterwards for downstream dispatch; HAL does not inject a correction into
   that already-processed reply. A short transcript in the AI-rejection ambiguity range defers this
   external embedding call until after realtime decides; an explicit rejection
   avoids the call entirely, while every non-rejected downstream turn still gets
   the same one-time identity result. It is skipped when the context already carried
   the right name, or when the turn is noise.

   **Bounded identity waits.** Speaker recognition runs on its own thread.
   Turn-based realtime waits at most `HAL_SPEAKER_PREPASS_COMMIT_JOIN_S`
   (default **0.2s**) before commit, so an external embedding call no longer adds
   up to 2s before Gemini can respond. A result ready before commit still updates
   this turn's context; a later result is used for downstream speaker decoration,
   not injected into an already-generating reply. Before downstream dispatch,
   HAL joins the same worker with `HAL_SPEAKER_PREPASS_JOIN_S` (default **2.0s**).
   Live and non-realtime paths retain that normal identity wait. Short ambiguous
   transcripts retain their deferred identity policy.

   For an authorized, non-noise turn, the existing neutral filler timer is armed
   before the pre-commit wait, even when realtime already started streaming during
   capture. The same timer is passed to the response consumer, preventing a second
   filler timer. Its delay and ownership remain unchanged; silence endpointing
   remains 0.8s after STT final, with the separately configured fallback clock.

   In hands-free LIVE OFF capture, optional interruptible speech (OS fillers and
   gesture cues, excluding agent replies and realtime-owned audio) cannot claim
   TTS from before STT connection until microphone capture ends. Otherwise a
   delayed filler sets `speaking` and the echo guard terminates the user's next
   sentence with `tts_started`. The reservation is released before STT final
   drain and on every early return/error, so processing feedback can still play
   after capture. Button listening-cue retries expire if a capture starts; they
   must not reappear after the user finishes. LIVE ON and manual Harness capture
   retain their existing behavior. The endpoint metric clock is unchanged.

   **LIVE OFF capture and final drain.** With wake-word gating disabled or focus
   already granted (including gaze granting it during this capture), HAL can
   upload audio before STT closes. `prepare_turn()` runs on `rt-capture-prepare`,
   leaving the microphone thread free to retain the complete ordered audio
   buffer while a session connects. Upload starts only after preparation and
   `bind_audio_turn()` succeed. The binding owns one agent and, for Gemini, one
   provider socket across queued audio, commit and output. If that session is
   found invalid at the pre-commit boundary, recovery uses a fresh agent and
   replays the complete buffer; queued Gemini frames/commit cannot migrate to a replacement socket.
   A session failure after commit follows the error/fallback path rather than
   automatically replaying potentially executed tools.

   At a normal endpoint (`smart_turn`, `turn_fallback`, `turn_pause_limit` or
   `silence_clock`), an authorized capture can process realtime as soon as
   recognized final words pass the existing noise guard, including a final
   arriving while `stt_session.close()` drains on a worker. The final callback
   wakes the capture owner; it does not commit or dispatch on the STT thread.
   A partial alone cannot enable this overlap, and a closed wake-word window
   still requires final confirmation. Stop during the drain prevents dispatch.
   A pending Harness follow-up also retains the complete-transcript path.
   STT close still completes before downstream dispatch: the final assembled
   transcript supplies history and main-agent input, and the early reply is
   consumed once. The metric retains the original speech-end timestamp and
   interaction ID. LIVE ON and manual Harness capture retain their existing
   paths.

   The validated audio commit is queued before the synchronous HW thinking cue,
   once per turn (not again on a retry or camera replay). Provider processing
   can overlap hardware work. Output consumption still waits for that cue;
   there is no detached emotion worker that could overwrite a newer turn.
   First-output timing includes the hardware wait, and commit failure does not
   start thinking.

   For non-native realtime TTS in both LIVE modes, sentence-end detection uses
   text after the existing voice/HW marker removal. A reply such as
   `I'm right here! [cheerfully]` is sent to TTS while the provider is still
   streaming/waiting for routing tools, rather than held until turn completion.
   Split markers remain buffered until they can be removed in full. Routing
   grace, output ownership, rejection filters and cancellation remain unchanged.

   **The verdict is cached.** Recognition used to run once per turn, every turn:
   a ten-turn conversation paid for ten external calls to be told the same name.
   `SpeakerDecorator` now reuses the last verdict for `SPEAKER_ID_CACHE_S`
   (`HAL_SPEAKER_ID_CACHE_S`, default 90s), and for `SPEAKER_ID_CACHE_FOLLOWUP_S`
   (default 300s) inside a wake-word follow-up window, where the turns are one
   conversation by definition. **Unknown is cached too** — an utterance the
   recognizer could not place is the case most likely to repeat, and retrying it
   every turn pays the full latency for the same non-answer; the only thing lost
   on a cached unknown is the enrolment WAV path for that turn. `POST
   /speaker/current-user/reset` clears the cache along with the current voice
   user, since it is the same presence state one layer down.

   **The replaced session is closed in the background.** A pre-turn rebuild
   swaps in a fresh session and then tears the old one down; doing that inline
   made the turn wait for `aclose()` plus the IO-thread join — 0.79s measured
   between the new session opening and this turn's `[TURN CONTEXT]` going out
   (lamp-0c89, 03/09/2026). Nothing needs the old socket to be closed before the
   model can hear the user, so the close runs on its own thread (failures are
   still logged; a thread that cannot start falls back to closing inline rather
   than leaking the socket).

   **Gemini native-audio caveat:** `send_text()` drops **all** non-response text on
   Gemini `*native-audio*` models (`gemini_needs_idle_workaround()`), because
   repeated SDK `clientContent(turn_complete=False)` messages collide with later
   audio turns and close with WS 1011. On those models neither the context nor the
   correction reaches the reply, and the model falls back to whatever identity its
   session memory holds. `gemini-3.1-flash-live` and OpenAI accept both. Every drop
   is logged (`[realtime->model] DROPPED …`).

   **This does not apply to the shipped default.** `REALTIME_GEMINI_MODEL` defaults to
   `gemini-3.8-live` (`hal/config.py`), which is not native-audio, so
   the guard is off and both the context and the correction reach the model. It
   re-engages only when a `*native-audio*` model is configured. The default is the
   plain `gemini-3.8-live`, not `-extended-thinking`: cost-lean, it takes the
   default BLOCKING tools and omits thinking (it rejects `thinkingLevel`, so
   `gemini_live._build_config` sends none). The extended variant is usable too — it
   accepts only NON_BLOCKING tool declarations, which `_build_config` now sets for
   it (a BLOCKING one made it error mid-turn with a spoken "I'm sorry, an error
   occurred.", device-observed 2026-09-17) — but plain live stays the default.
4. **Commit.** At session end, if enabled + `available` + audio buffered,
   `commit_audio()` fires. A `thinking` emotion cue fires with the commit
   (face + servo + a FORCED LED pulse — `thinking` is normally a
   background emotion whose LED yields to the user's saved color; the
   realtime cue bypasses only that guard, user-LED-off still wins) and is
   cleared back to `idle` at the first output (first TTS sentence or first
   native audio frame) or when the turn dies with no output — unless the
   model already expressed its own emotion. This fills the 1-3s
   model-latency gap where the device otherwise looked frozen.

   The **dead-air filler** (`_WaitFiller`) is the audible half of that cue.
   On the wake-word / follow-up path it is armed **before** the post-capture
   session handshake (`start_realtime_turn()` → `prepare_turn()`), not at the
   commit: on lamp-dbda (2026-09-18) that reconnect took 2–3 s, and arming at
   commit pushed the first acknowledgement to 4–5 s after the user stopped
   speaking. The armed filler is handed to `run_realtime_turn(wait_filler=…)`,
   which keeps the one-filler-per-turn rule (`arm()` is idempotent) and cancels
   it on every exit path. Sessions opened during capture (always-listening) arm
   at the commit as before. After `HAL_REALTIME_FILLER_DELAY_S` (default 1.5 s) with
   still no output, HAL calls `POST /api/sensing/filler` and os-server speaks
   one dedicated realtime filler from its cache — a quiet non-lexical thought
   such as "Mm...", distinct from the main-agent's opening acknowledgement.
   Os-server owns the phrase pools, language, and WAV cache. Whether this fires
   on every turn or only on slow ones is a
   property of the model, and the default assumes a fast one: a chit-chat reply
   arriving in ~1 s never reaches the timer, while a turn grounded with Google
   Search does. Measure before trusting that on a given body — on `lamp-0c89`
   (26/08/2026, `gemini-3.1-flash-live-preview` behind the campaign-api proxy)
   no turn reached its first sentence in under 3.0 s (median 4.0 s, n=31), so
   the filler is the only thing the user hears early and the lamp lowers the
   delay to 0.5 s in its device `.env`. Set it from measured
   time-to-first-sentence, not from the default. **It is not armed for a short transcript in the noise-guard
   ambiguity range** (up to `HAL_REALTIME_NOISE_GUARD_MAX_WORDS`, default 3):
   the model may explicitly reject `o`, `you.`, or `Yeah.` shortly after commit,
   and an early filler would turn that silent rejection into an audible nuisance.
   The filler is interruptible, so the model's first sentence
   cuts it off; every exit path (reply, delegate, empty turn, exception)
   cancels the timer, and delegate cancels explicitly because the main-agent
   hop that follows fires its own filler. `0` disables.

   Speaking the filler is TTS, so it stops the thinking pulse and runs the
   speaking wave. To keep the rest of the wait visible, the cue marks the
   strip as its own (`app_state._thinking_cue_active`): the LED restore that
   follows TTS repaints the thinking pulse instead of settling on the user
   state. The flag is dropped when the cue clears and by any other emotion
   coming through `POST /emotion`, so an expressed emotion is never stomped.

   A **delegated** turn keeps the cue on purpose — the main-agent hop that
   follows is the longer wait, and its own hook re-fires `thinking` anyway. A
   turn that raises does *not* count as that handover (nothing in HAL is still
   driving the face), so the exception path clears the cue before falling
   through to the OS server.

   Because `thinking` is only ever ended by the emotion the reply expresses, a
   turn that produces none — a delegate the agent answers without an emotion
   marker, a forward that never happens — used to leave the face and, through
   `_thinking_cue_active`, every later LED restore stuck on the pulse until the
   user spoke again. Two things end it now.

   **The reply finishing is the end of the wait.** `_on_tts_speak_end`
   (`hal/app_state.py`) clears `thinking` when TTS ends, gated on
   `tts_service.realtime_feedback` — the flag only the agentic runtime's own
   reply sets. Dead-air fillers, mumble and system notices leave it False, so
   the TTS that plays *during* a wait (exactly what the cue flag exists to
   survive) does not end the cue. This covers the common case at the right
   moment: the face is correct the instant the device stops talking, whether or
   not the agent bothered with an emotion marker.

   **The watchdog is the net for turns that never speak.** `POST /emotion` arms
   a last-resort timer whenever
   the emotion is `thinking`: after `HAL_EMOTION_THINKING_RESET_S` (default
   25 s, `0` disables) of *continuous* thinking it drops the cue flag, expresses
   `idle`, and restores the user's LED state. Any other emotion cancels the
   timer; a fresh `thinking` re-arms it. The window clears the longest real hold
   measured on device (realtime replies clear in 0.4-8.6 s; a delegated
   event-forwarded → assistant-turn-done runs 6-22 s), so it cannot blink idle
   in the middle of a live turn.
5. **Consume.** `for output in stream_output()`:
   - `TextOutput` → sentences are flushed to TTS (`speak` / `speak_queue`).
     By default (`HAL_REALTIME_FIRST_CHUNK_MAX_CHARS=0`), the first complete
     sentence is spoken immediately; later sentences continue through the
     pre-synthesis queue. This does not wait for the whole reply. A positive
     value opts into splitting the first incomplete sentence at its last clause
     boundary (`,` `;` `:` `—`), or at a word break beyond that character limit.
     Complete sentences bypass this early splitter. It preserves square-bracket
     voice tags, including during whitespace fallback, and does not split at
     numeric commas/colons or URL colons. A candidate needs at least 8 visible
     characters outside voice tags. Early splitting can reduce initial waiting
     but still create gaps between synthesis requests; it is an opt-in tradeoff.
     Native audio is unaffected and continues to stream frame by frame.
     If `speak` returns busy (another non-interruptible TTS holds the
     speaker, e.g. an ambient nudge), the sentence falls back to
     `speak_queue` so the reply plays after it instead of being lost.
     Queued agent speech is turn-aware: each entry carries a `turn_id` and
     monotonically increasing `turn_seq`.
     Accepting a newer run stops the active older utterance and removes its
     pending entries, so the user hears the reply that is relevant now. A
     delayed request from the superseded run is dropped rather than rejoining
     the queue. `POST /tts/stop` likewise cancels both active playback and
     every pending entry.
   - `DelegateSignal` → stop; forward `[voice-instruction] …` + transcript to the
     OS server with the original `event_type`.
   - Otherwise the turn was handled locally → the OS server is told
     `voice_agent_handled` (so OpenClaw replies `NO_REPLY` and skips dead-air
     filler), and the turn is saved to realtime memory.

## Configuration

The realtime agent is configured from the **`realtime` block in the device's
`config.json`** (operator-facing knobs), with HAL's `HAL_*` environment variables
as a dev override and built-in defaults as the floor. Precedence per knob:

```
HAL_* env var  >  config.json "realtime" block  >  built-in default
```

os-server **seeds** the block into `config.json` and keeps it equal to the code
defaults (`DefaultRealtimeConfig`) on every start **until an operator edits it**:
any web UI / MQTT `realtime.set` write sets `realtime.pinned: true`, after which
os-server never touches the block again. So a fleet default change (say gemini →
openai) reaches every device that never chose, and a device that did chooses
wins. To un-pin, delete `pinned` from `config.json`. HAL
reads it directly (same as `llm_api_key` / `stt_language`), no push down. Because
HAL reads `config.json` at import, a config change needs a **HAL restart** to take
effect. A live edit triggers that restart immediately (`restartHAL` in
`system/device/service.go`).

### STT model and language

`stt_language` selects the persisted `stt_model`: English uses
`flux-general-en`; Vietnamese and the other supported non-English languages use
`nova-3-general` with the selected BCP-47 language code. That pair is passed to
the AutonomousSTT proxy, including a healthwatch voice-pipeline restart. This
makes a saved Vietnamese configuration effective after a proxy restart; it does
not claim that one model provides arbitrary Vietnamese-English code-switching.

**Restart only when the config changed.** os-server does *not* restart HAL on
every os-server restart — that would needlessly drop the voice pipeline. Instead
it hashes `config.json` and stores the hash in `config/.hal_config_hash` whenever
it (re)starts HAL. On boot (`handleSetUpCompleteChange` in `server/config_watch.go`)
it restarts HAL only when the current hash differs from that snapshot — i.e. the
config actually changed while os-server was down (fresh setup, OTA config swap, an
edit during downtime), or no snapshot exists yet (first boot). A plain os-server
restart with unchanged config leaves the already-running HAL untouched. If HAL is
genuinely down, `hal.service` (`Restart=always`, `RestartSec=5`) brings it back
independently, so skipping the restart is safe. The `restartHAL` path refreshes the
snapshot after it restarts HAL, so a live change followed by an os-server restart
doesn't double-restart. Hashing the whole file (rather than the HAL-read subset)
keeps the signal self-maintaining as HAL's read set evolves; the only cost is one
spurious HAL restart on the next boot after an os-server-only field changes.

### `config.json` `realtime` block

Modelled in Go at `system/server/config/realtime.go`; read in HAL at
`hal/config.py`. Shared fields sit at the top; per-provider knobs live in
`gemini` / `openai` / `gptlive` / `pipecat_v1` sub-objects (Go structs
`GeminiRealtime` / `OpenAIRealtime` / `GPTLiveRealtime` / `PipecatV1Realtime`),
with `provider` selecting the active one
(`none` / `off` / `disabled` → realtime off; absent or empty → `gemini`, in both
the Go accessor `RealtimeProvider()` and HAL's `REALTIME_PROVIDER` default).
Empty `api_key` / `base_url` fall back to `llm_api_key` / `llm_base_url` for
Gemini and OpenAI. GPT-Live resolves its key as `OPENAI_API_KEY` env >
`realtime.gptlive.api_key` > shared `realtime.api_key` > `llm_api_key`, and its
base URL as `HAL_GPTLIVE_BASE_URL` > `realtime.gptlive.base_url` > the OpenAI
Realtime base URL (`realtime.base_url` > `<llm_base_url>/ws/openai`), to which
the SDK appends `/live/sessions`. The `GPTLiveRealtime.api_key` / `base_url`
fields only persist a per-provider override; `realtime.set` writes credentials
to the shared fields. Pipecat v1 resolves its key as `HAL_PIPECAT_API_KEY` >
`realtime.pipecat_v1.api_key` > shared `realtime.api_key` > `llm_api_key`, and
its chat base URL as `HAL_PIPECAT_BASE_URL` > `realtime.pipecat_v1.base_url` >
the Qwen relay default — the shared `realtime.base_url` is deliberately **not**
consulted (it carries a `/ws/...` relay shape, not a chat-completions
endpoint), which is why the web Settings page hides the Base URL field for this
provider and points at `realtime.pipecat_v1.base_url` instead.

> **Leave `base_url` blank unless you have a non-proxy endpoint.** When empty, HAL
> derives `<llm_base_url>/ws/gemini` (or `/ws/openai`) — the WS suffix the
> `campaign-api` proxy routes on. A `base_url` set to the bare `llm_base_url`
> (no `/ws/...`) is handed verbatim to the provider SDK and **404s at the Live
> handshake**. The web Settings "Base URL" field is therefore display-bound to the
> *explicit override only* (`RealtimeBaseURLOverride`, not the resolved value), so
> "leave blank to derive" stays blank and a save never re-persists the bare URL.
> GPT-Live follows the same rule: blank derives `<llm_base_url>/ws/openai` and the
> SDK adds `/live/sessions`.

```json
{
  "wakeword": false,
  "realtime": {
    "enabled": true,
    "provider": "gemini",
    "gemini": { "model": "gemini-3.8-live", "voice": "Kore", "thinking_level": "LOW" },
    "openai": { "model": "gpt-realtime-2", "voice": "alloy", "reasoning_effort": "minimal" },
    "gptlive": { "model": "gpt-live-1", "voice": "marin" },
    "pipecat_v1": { "model": "qwen/qwen3.6-35b-a3b" }
  }
}
```

The reasoning knobs (`thinking_level` / `reasoning_effort`) default to the
**cheapest** tier (`MINIMAL` / `minimal`), not the providers' max — raise them
explicitly for deeper reasoning. GPT-Live has **no** reasoning knob (the Live
model exposes none): `RealtimeReasoning()` returns empty for it, the options
endpoint returns an empty `reasoning.gptlive` list so the web hides the
selector, and `ValidateRealtimeKnobs` rejects any reasoning value for `gptlive`
(`gptlive realtime has no reasoning knob`). Its voice must be one of the 13
voices `gpt-live-1` accepts at `session.start` (`RealtimeGPTLiveVoiceList`,
HAL `GPTLiveVoice`, from the BFF integration doc): `marin` (default), `quartz`,
`ripple`, `vesper`, `willow`, `stone`, `gleam`, `meridian`, `bossa`, `tempo`,
`beacon`, `delta`, `cinder` (`bossa`/`tempo` are Portuguese, the rest English
with regional accents). The SDK's wider `BuiltInVoice` literal also carries
Realtime-only names (`alloy`, `ash`, …) that a Live session rejects — they are
deliberately not listed; the web Settings page labels the provider "GPT-Live".
Pipecat v1 has **neither** a voice nor a reasoning knob (the pipeline emits
text and HAL's TTS voice speaks it): `RealtimeVoice()` and
`RealtimeReasoning()` return empty, the options endpoint returns empty
`voices.pipecat_v1` and `reasoning.pipecat_v1` lists so the web hides both
selectors, `ValidateRealtimeKnobs` rejects any voice (`pipecat_v1 realtime has
no voice`) or reasoning value for it, and `realtime.set` writes only `model`
and `web_search` (the in-session search toggle — `validateRealtimeSet` rejects
it for any other provider) into the `pipecat_v1` sub-object; the web label is
"Pipecat v1 (on-device)" and the page shows a **Web search** checkbox for it.
HAL additionally reads
`realtime.openai.transcribe_model` and `realtime.openai.noise_reduction` from
the same sub-object (env `HAL_OPENAI_TRANSCRIBE_MODEL` /
`HAL_OPENAI_NOISE_REDUCTION` win); these two are not modelled in the Go
`OpenAIRealtime` struct, so set them by hand or via env. Knobs NOT in the block
(turn detection, VAD threshold, session resumption, memory, summarizer) stay
env/default-only.

**CoT-leak filter.** On `gemini-3.1-flash-live-preview` thinking cannot actually
be disabled: `thinking_level=MINIMAL` and `thinking_budget=0` are both accepted
but ignored (measured `thoughts_token_count` 125–168 on reasoning turns with
every config). Normally the thoughts stay internal, but on grounding/vision/tool
turns the server sometimes streams the model's whole text channel — English
planning ("The user is insisting…", "Phrasing draft:", "Delivery guidance:")
plus the real answer — into `output_audio_transcription`, while the model's own
audio carries only the clean answer. With native audio off HAL speaks the
transcription, so without a guard the leak is read aloud (burning TTS
characters) and forwarded as `[REPLY]`, where it re-enters context and
self-reinforces. `drivers/voice/_internal/cot_leak_filter.py` drops the leak at
sentence granularity before TTS and before the transcript is forwarded/saved,
in three tiers: TRIGGER markers (verb-bound third-person "the user is/wants…",
planning labels like "Phrasing draft:") always drop and switch the turn into
CoT mode; SECONDARY markers ("persona", "system prompt", "emotion tool", …)
drop only once CoT mode is on, so a legit reply about the device itself is
safe; in CoT mode, English planning sentences (non-English devices only —
non-Latin scripts like Vietnamese/Chinese/Japanese use an ASCII-ratio check,
Latin scripts like French/Indonesian additionally require English function
words so the real answer survives), quoted drafts, plan runts, and fuzzy
near-duplicates (CJK tokenized per character) drop too. The language check
ignores quoted spans, so an English planning sentence that embeds
reply-language text in quotes ("The search query 'cách dùng…' didn't yield…")
is still caught, while a reply-language sentence quoting English is not.
Every dropped sentence is logged as `CoT leak dropped`.

The main-agent path (openclaw/hermes replies spoken via os-server) has a Go
port of this filter — `system/server/agent/delivery/http/cot_leak_filter.go`
(adds a snake_case-identifier TRIGGER for the DeepSeek leak corpus); see
`docs/flow-monitor.md` § "CoT-leak filter (agent path)". Keep the two in sync
when hardening either side.

### Runtime configuration (`hal/config.py` + `config.json`)

Each `HAL_*` environment variable overrides its corresponding setting; `wakeword`
is a top-level `config.json` flag:

| Variable | Default | Notes |
|----------|---------|-------|
| `HAL_REALTIME_ENABLED` | `true` | Master gate for the realtime pipeline |
| `wakeword` | ROBOT.md `voice.wakeword` on a fresh config, else `false` | Top-level config-file wake-word gate. When true, a matching interim transcript is provisional only: HAL commits buffered audio to realtime or forwards a command only after an STT **final** result confirms a configured wake phrase. The transcript is split into sentences (`.` `!` `?`) and the phrase is accepted at the start **or the end** of any sentence; mid-sentence occurrences are rejected. The confirmation re-checks the assembled, still-punctuated transcript so the `\w+`-only merge step cannot retract a gate a partial opened. If that exact re-check fails but a partial had already matched exactly, the name alone may differ by one letter and the gate still confirms: STT rewrites its own hypothesis in the final, and on lamp-0c89 (04/09/2026) the partial `hello lamp` came back as `Hello, lamb.`, which dropped the whole turn — no realtime turn, no thinking cue, and the question fell through to the much slower main agent. The prefix (`hello`, `hey`, …) must still match exactly and the loose rule can never OPEN a gate, only confirm one an exact partial opened, so a near-miss word in ambient speech still wakes nothing. It is logged as `Wake-word confirmed with a one-letter STT slip` so the rate stays countable — many of them means the STT boost terms are not doing their job. The supported prefixes are `hello`, `hey`, `hi`, `alo`, `okay`, `ok`, and `wake up`, applied to the permanent common alias (`hey autonomous`), device type (`hey lamp`), and current agent name (`hey Luna`). A runtime rename updates only the agent-name aliases. Bare names and other prefixes do not arm the gate. A rejected utterance is discarded and its transient listening LED restores to the normal resting state; it never leaves the persistent idle effect active. A confirmed turn opens the follow-up focus window; turns in that window are forwarded as `voice_followup` without another phrase. Every authorized turn dispatches to os-server: a spoken realtime reply becomes a silent `voice_agent_handled` sync event; unavailable, silent, failed, or delegated realtime follows the normal path. If realtime is disabled or unavailable, the confirmed final transcript follows the normal os-server/main-agent path. With Live ON and realtime available, the confirmed capture enters full-duplex live without a manual audio commit. Missing/false preserves the pre-gate always-listening flow unchanged. On a config.json os-server creates, the initial value comes from the body's `voice.wakeword` (see Wake-word gate above); a config loaded without the key stays `false`. HAL restarts after a local Settings save or MQTT `wakeword.gate`. |
| `HAL_HARNESS_PROGRESS_SPEAK_P` | `0.15` | Chance a progress-only Harness snapshot is spoken; `0` disables spoken progress. See [Harness update announcements](#harness-update-announcements). |
| `HAL_HARNESS_PROGRESS_MIN_GAP_S` | `60` | At most one spoken progress line per Harness run within this window. |
| `HAL_HARNESS_PROGRESS_QUIET_START_S` | `15` | No spoken progress this soon after the request. |
| `HAL_HARNESS_PROGRESS_MAX_AGE_S` | `30` | Queued progress older than this is dropped. |
| `HAL_HARNESS_UPDATE_MAX_AGE_S` | `600` | Unspoken results/questions older than this are dropped. |
| `HAL_HARNESS_ANNOUNCE_GRACE_S` | `1.5` | Quiet time after any speech or user transcript before the next snapshot. |
| `HAL_HARNESS_ANNOUNCE_CONTENT_MAX_CHARS` | `4000` | Harness text handed to the renderer is cut to this length. |
| `HAL_HARNESS_ANNOUNCE_SUMMARIZER_TIMEOUT_S` | `12` | Fallback summarizer bound before sanitized text is spoken instead. |
| `HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S` | `20` | Idle seconds for the short post-command focus window. Each accepted `voice_command` or `voice_followup` refreshes it. `0` disables follow-ups and requires a wake phrase for every mic session. Ignored when `wakeword` is false. |
| `HAL_ENDPOINT_SILENCE_S` | `0.8` | Silence needed after STT final arrival, only while `final_ts >= last_confirmed_speech`. Continued confirmed speech after that final restores the 2.5s fallback until a new final arrives. `0` disables the short clock, leaving `HAL_SILENCE_TIMEOUT`. With the shared gate enabled this only proposes an endpoint; `HAL_TURN_END_*` decides closure. |
| `HAL_TURN_END_ENABLED` | `true` | Shared provisional endpoint gate for non-Live hands-free capture before commit; no change to Live or manual capture. `false` restores legacy silence clocks and session ceiling. |
| `HAL_TURN_END_FALLBACK_S` | `2.5` | Minimum silence for ordinary text when Smart Turn is unavailable/pending, and for a short greeting; the original silence candidate must also fire. |
| `HAL_TURN_END_MAX_PAUSE_S` | `6.0` | Maximum silence before closing a candidate with hesitation hints or an incomplete model result; clamped to at least the fallback. |
| `HAL_TURN_END_MAX_DURATION_S` | `180` | Capture ceiling for enhanced hands-free sessions with recognized words. Reaching it discards the request without dispatch; manual/no-text capture retains `HAL_MAX_SESSION_DURATION_S`. |
| `HAL_SILENCE_VAD_ENABLED` | `true` | Require Silero to confirm speech before the end-of-turn silence clock is refreshed. RMS remains the cheap pre-gate; set `false` to fall back to pure-RMS silence detection. |
| `HAL_SILENCE_VAD_WINDOW_FRAMES` | `3` | Number of frames batched per Silero run for that check — Silero costs ~20 ms/frame on ARM and its LSTM needs more than one 64 ms frame to settle. |
| `HAL_REALTIME_PROVIDER` | `gemini` | `none` \| `gemini` \| `openai` \| `gptlive` \| `pipecat_v1` |
| `HAL_REALTIME_TURN_DETECTION` | `off` | `server_vad` \| `semantic_vad` \| `off` (Gemini: off = manual activity detection). Ignored by GPT-Live, which has no `turn_detection` |
| `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` | `8.0` | Max seconds `receive()` waits for the next output event before ending a silent turn (fallback to main agent) |
| `HAL_REALTIME_NONBLOCKING_TOOL_GRACE_S` | `6.0` | Ordinary routing grace after the first Gemini extended-thinking terminal; verified progress or actual answer continuation may extend it. `0` disables ordinary grace, not the outcome requirement. |
| `HAL_REALTIME_PROGRESS_TIMEOUT_S` | `15.0` | Gemini extended-thinking search/tool progress extension deadline, measured from audio commit; repeated progress does not reset it. `0` disables the extension. Actual answer chunks may continue with the receive-gap idle bound. |
| `HAL_REALTIME_GROUNDING_DEBUG` | `false` | Dump every field of Gemini's `grounding_metadata` verbatim, once per grounded turn (`grounding_chunks`, `grounding_supports`, `search_entry_point`, …). Diagnostic only and verbose; it exists to tell a turn whose search genuinely returned nothing from one whose payload was stripped in transit. Measured on lamp-0c89 04/09/2026 over four grounded turns, the payload always arrived complete, so a `chunks=0` turn means the model did not ground that answer. |
| `HAL_REALTIME_TURN_MAX_SILENCE_S` | `20.0` | Legacy liveness ceiling for provider/mode paths other than Gemini non-Live extended-thinking (which uses verified progress). `receive()` extends a turn past `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` only while inbound traffic proves the model is still working (a grounded search emits nothing until it returns); this stops a server that chatters without ever producing output from hanging the turn. `0` disables the keep-alive and restores the plain gap watchdog. |
| `HAL_REALTIME_LOOK_RECV_TIMEOUT_S` | `20.0` | Silent-turn watchdog used instead of the default for turns where a `look` fired (per-turn, via `extend_recv_timeout()`). Gemini's forced thinking over a text-dense frame can stay silent >8 s right before the answer — the default watchdog was killing those turns. Raising it delays the look-frame handoff, so keep `HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S` above it |
| `HAL_REALTIME_REQUIRE_TRANSCRIPT` | `true` | Never commit an empty-STT turn to the model. A final transcript containing only punctuation or symbols (for example `.`) is normalized to empty before gaze, speaker-ID, realtime, dispatch, or follow-up refresh; it cannot create a `voice_followup`. Real speech that nova-3 missed (short utterances) is voiced and passes the VAD/Silero guards, so committing its raw audio makes the model invent a reply to silence (a generic greeting, often with a name nobody said). When `true`, any empty-STT turn is dropped regardless of duration/voicing — silence beats a wrong reply. Set `false` to fall back to the Silero-gated audio-only path below. |
| `HAL_REALTIME_AI_REJECT_FILTER` | `true` | Registers `reject_turn` and enables the isolated `should_drop_realtime_rejection()` policy gate. An explicit tool call drops a transcript before OS dispatch; a silent model completion, timeout, or error still falls back to the main agent. The separate deterministic noise guard is also terminal for audio it already classified as non-speech. Set `false` to disable this experimental AI filter without changing the rest of realtime routing. |
| `HAL_REALTIME_FIRST_CHUNK_MAX_CHARS` | `0` | Default: speak the first complete sentence immediately and pre-synthesize later sentences in the queue, without waiting for the whole reply. Positive values opt into first-clause splitting, with a word-break fallback beyond this limit; complete sentences bypass the splitter. Keeps bracketed voice tags intact (also at whitespace fallback), skips numeric commas/colons and URL colons, and requires 8 visible characters outside tags. Early splitting may leave gaps between synthesis requests. |
| `HAL_REALTIME_MIN_COMMIT_DURATION_S` | `0.8` | Sessions shorter than this with no STT transcript are treated as VAD noise and not committed to the model. Only consulted when `HAL_REALTIME_REQUIRE_TRANSCRIPT=false`. |
| `HAL_REALTIME_NOISE_GUARD_MAX_WORDS` | `3` | Extends the Silero voiced-ratio guard to turns that DO have a transcript, up to this many words. STT invents a short filler out of room noise and reports full confidence for it, so such a turn used to bypass every guard (they all only ran on an empty transcript) and commit pure noise to the model. A transcript of at most this many words is re-checked against `HAL_REALTIME_NOISE_SPEECH_RATIO` and dropped when the audio was never voiced; a real short command is voiced and still commits. The ratio is measured over the voiced SPAN — first to last voiced chunk — not the whole buffer, because a capture carries VAD pre-roll at the front and a 200ms tail at the back, and that fixed padding dilutes a short utterance far more than a long one. Measuring the whole buffer dropped a real `Yes, that's right.` at 0.500 (`peak=1.000`), inverting the guard's purpose against the very turns it screens. Sustained noise still fails, since its voiced chunks are sparse within the span too. Longer transcripts are never re-checked, so the floor can't silence a real utterance. `0` disables. |
| `HAL_REALTIME_SESSION_IDLE_RESET_S` | `240` | Cost control: when a turn arrives after this many seconds of silence (measured from the later of the last turn and the current session's connect), recycle (rebuild) the session **after** that turn so the next turn drops the per-turn context the provider re-bills on a long-lived session. A post-pause turn is effectively a new conversation; long-term continuity survives via the reloaded `summary.md`. For native-audio Gemini, this is skipped when a successful pre-turn recycle already made the same idle gap fresh. `0` disables. Reuses the zombie-recovery rebuild path. |
| `HAL_GEMINI_SESSION_RESUMPTION` | `false` | Resume the same Gemini session across reconnects. OFF by default — the `campaign-api` proxy doesn't forward the resumption handshake, so resuming through it yields a zombie session (cold reconnects work). Enable only against an endpoint that supports it. |
| `HAL_GEMINI_IDLE_PARK_S` | `45` | Gemini idle parking: close the session's transport after this many seconds without turn activity, so the server never closes it with WS `1008` (which the backend logs as an error and alerts on). The orchestrator stays `available` while parked; the next turn's `prepare_turn()` reconnects synchronously before streaming audio. Must stay below the shortest observed idle death (86 s). `0` disables. |
| `HAL_GEMINI_PRE_TURN_RECYCLE_S` | `60` | Gemini transport guard: when a new spoken turn starts after this much session idle time (from the later of the last turn and the session's connect), rebuild the Gemini session **before** streaming pre-roll/audio so the turn does not hit a proxy/SDK idle-dead socket. `0` disables. A successful pre-turn recycle suppresses the generic post-turn idle recycle for that same turn, so one idle gap creates at most one cost/transport rebuild. |
| `HAL_AGENT_GATEWAY` | `openclaw` | Selects the context manager (also from `agent_runtime` in config.json) |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | — | Gemini key; falls back to `llm_api_key` |
| `HAL_GEMINI_LIVE_MODEL` | `gemini-2.5-flash-native-audio-preview-12-2025` | |
| `HAL_GEMINI_LIVE_VOICE` | `Kore` | |
| `HAL_GEMINI_LIVE_BASE_URL` | `<llm_base_url>/ws/gemini` | |
| `HAL_GEMINI_THINKING_LEVEL` | `LOW` | `MINIMAL` \| `LOW` \| `MEDIUM` \| `HIGH`. `gemini-3.8-live-extended-thinking` has no MINIMAL (HAL clamps it to LOW); plain `gemini-3.8-live` rejects thinkingLevel, so HAL omits it there |
| `HAL_GEMINI_GOOGLE_SEARCH` | `true` | Google Search grounding (Gemini only). Lets the realtime model answer public live-data questions (weather, news, lookups) in-session instead of delegating. Bills per grounded request on top of tokens; fires only when Gemini decides to search. Also settable via `realtime.gemini.google_search` in config.json. |
| `HAL_GEMINI_VISION` | `true` | In-session `look` tool (Gemini only). Lets the realtime model capture one camera frame and answer visual questions ("what is this?") in-session instead of delegating. Default on; only registered when the device also has the `vision` capability. Also settable via `realtime.gemini.vision` in config.json. |
| `HAL_GEMINI_VISION_MAX_WIDTH` | `768` | Max width (px) the captured frame is downscaled to before sending — bounds image tokens. |
| `HAL_GEMINI_VISION_MIN_INTERVAL_S` | `10` | Cost guard: minimum seconds between two image **sends**. Repeat `look` calls within this window (or a second call in the same turn) reuse the frame already in context instead of sending a new one. `0` = always send fresh. |
| `HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S` | `45` | Max age of a `look` frame still handed off to the main agent on a delegate/timeout fallback so it reuses the image instead of re-snapshotting. **Must stay above `HAL_REALTIME_LOOK_RECV_TIMEOUT_S` plus dispatch time** — the timeout fallback only fires after that watchdog expires, so equal values expire every frame (both were `20` from 2026-07-06 to 2026-08-24 and the handoff never once fired). `0` disables the age guard (frame is still cleared per-turn). |
| `OPENAI_API_KEY` | — | OpenAI key, for OpenAI Realtime **and** GPT-Live; falls back to `llm_api_key`. On GPT-Live the order is env > `realtime.gptlive.api_key` > `realtime.api_key` > `llm_api_key` |
| `HAL_OPENAI_REALTIME_MODEL` | `gpt-realtime-2` | |
| `HAL_OPENAI_REALTIME_VOICE` | `alloy` | |
| `HAL_OPENAI_REALTIME_BASE_URL` | `<llm_base_url>/ws/openai` | |
| `HAL_OPENAI_REASONING_EFFORT` | `minimal` | `minimal` \| `low` \| `medium` \| `high` \| `xhigh` — cost-lean default (was `xhigh`) |
| `HAL_OPENAI_TRANSCRIBE_MODEL` | `gpt-4o-mini-transcribe` | `audio.input.transcription.model`. Input transcription is always on: it is the only source of the user's words on the OpenAI path (`UserSpeechOutput.transcript`, live history, the delegate message). `gpt-4o-mini-transcribe` streams deltas; `whisper-1` only sends the completed transcript. Also `realtime.openai.transcribe_model` in config.json |
| `HAL_OPENAI_NOISE_REDUCTION` | `far_field` | `far_field` (room mic — the lamp) \| `near_field` (headset) \| `off` (key omitted). Server-side `audio.input.noise_reduction`, applied before VAD and the model — the OpenAI half of the echo defence Gemini gets from VAD sensitivity. Also `realtime.openai.noise_reduction` in config.json |
| `HAL_OPENAI_VAD_THRESHOLD` | `0` | `server_vad` activation threshold (0..1, API default 0.5). `0` derives it from `HAL_LIVE_VAD_START_SENSITIVITY` (`low` → `0.7`, `high` → `0.3`); a non-zero value wins. `HAL_LIVE_VAD_PREFIX_PADDING_MS` / `HAL_LIVE_VAD_SILENCE_MS` map to `prefix_padding_ms` / `silence_duration_ms` when > 0, and `HAL_LIVE_VAD_END_SENSITIVITY` to `semantic_vad` `eagerness` |
| `HAL_GPTLIVE_BASE_URL` | *(empty → the OpenAI Realtime base URL: `HAL_OPENAI_REALTIME_BASE_URL` > `realtime.base_url` > `<llm_base_url>/ws/openai`)* | Also `realtime.gptlive.base_url`. The SDK appends `/live/sessions`, so the proxy must serve `…/ws/openai/live/sessions` (pending as of 2026-09-16; 404 until then). Set `https://api.openai.com/v1` to go direct |
| `HAL_GPTLIVE_MODEL` | `gpt-live-1` | Also `realtime.gptlive.model` |
| `HAL_GPTLIVE_VOICE` | `marin` | One of the 13 Live voices listed under the `config.json` block. Also `realtime.gptlive.voice` |
| `HAL_GPTLIVE_SAMPLE_RATE` | `24000` | `16000` \| `24000`. One PCM format for **both** directions on a Live WebSocket (`output_sample_rate == sample_rate`); 24000 keeps the model's voice at full quality, 16000 halves uplink bandwidth |
| `HAL_GPTLIVE_TURN_GAP_MS` | `800` | Synthesized turn boundary: a reply is over when no `session.output_audio.delta` / `session.output_transcript.delta` has arrived for this long (the wire has no `response.done` / `turn_complete`) |
| `HAL_GPTLIVE_INTERRUPT_GAP_MS` | `400` | Barge-in verdict: after the user spoke over a reply, output silence this long counts the reply as interrupted; output continuing past it means the overlap was a backchannel |
| `HAL_GPTLIVE_INPUT_GAP_MS` | `1500` | Input transcript fragments further apart than this (session-timeline `start_ms − previous end_ms`) open a new user turn even when the model has not answered in between |
| `HAL_GPTLIVE_COMMIT_SILENCE_MS` | `600` | Turn path only: PCM silence appended at `commit_audio()` (`event_id` `hal-silence`) so the model hears the utterance end — there is no commit on Live. No-op in LIVE_MODE; `0` disables |
| `HAL_GPTLIVE_DELEGATION_WAIT_MS` | `500` | Hard deadline for forwarding a `session.delegation.created` as `delegate_to_main`: the adapter forwards as soon as the input transcript has been quiet for 250 ms, and at this deadline at the latest with whatever was heard (empty → the orchestrator's rejection is relayed as a failed handoff) |
| `HAL_GPTLIVE_IDLE_PARK_S` | `30` | GPT-Live idle parking: the session is billed per minute while open, so after this many seconds without turn activity the orchestrator closes the transport (`_maybe_park_idle_session`, same mechanics as `HAL_GEMINI_IDLE_PARK_S`) and reconnects on the next turn. `0` disables. |
| `HAL_PIPECAT_API_KEY` | *(empty → `realtime.pipecat_v1.api_key` > `realtime.api_key` > `llm_api_key`)* | Bearer key for the chat endpoint |
| `HAL_PIPECAT_BASE_URL` | `https://campaign-api.autonomous.ai/api/v1/ai/v1/qwen/v1` | Also `realtime.pipecat_v1.base_url`. Any OpenAI-compatible chat-completions base; the shared `realtime.base_url` is never used (WS relay shape). `https://vibe-agent-gateway.eternalai.org/v2` serves the same model keyless |
| `HAL_PIPECAT_MODEL` | `qwen/qwen3.6-35b-a3b` | Also `realtime.pipecat_v1.model` |
| `HAL_PIPECAT_TEMPERATURE` | `0.7` | LLM sampling temperature |
| `HAL_PIPECAT_MAX_TOKENS` | `300` | Reply cap per response |
| `HAL_PIPECAT_DISABLE_THINKING` | `true` | Sends `extra_body.chat_template_kwargs.enable_thinking=false` (Qwen3 / vLLM); Pipecat streams only `content`, so thinking would be dead air. `false` for an endpoint that rejects the extra body |
| `HAL_PIPECAT_STT_API_KEY` / `HAL_PIPECAT_STT_BASE_URL` / `HAL_PIPECAT_STT_MODEL` | `llm_api_key` / `llm_base_url` / `stt_model` | Only when the agent must build its own `AutonomousSTT` (no `VoiceService` provider injected — tests, `/voice/start` before STT); on a running device the pipeline uses `VoiceService`'s STT provider |
| `HAL_PIPECAT_SAMPLE_RATE` | `16000` | Mic PCM rate the pipeline expects (no resample: Silero, Smart Turn and the STT relay take 16 kHz natively) |
| `HAL_PIPECAT_SMART_TURN` | `true` | Live mode: Smart Turn v3 (bundled ONNX, CPU) decides end-of-turn after each Silero stop; `false` → a `HAL_PIPECAT_SILENCE_TIMEOUT_S` silence timeout instead. Ignored on the turn-based path |
| `HAL_PIPECAT_SMART_TURN_STOP_SECS` | `3.0` | Live mode: longest silence Smart Turn waits before forcing the turn closed |
| `HAL_PIPECAT_VAD_CONFIDENCE` | `0.85` | Live mode Silero confidence (Pipecat default 0.7 — the far-field value: at 0.8 the lamp still answered a conversation across the room, 2026-09-18) |
| `HAL_PIPECAT_VAD_START_SECS` | `0.2` | Live mode: speech must persist this long before Silero reports an onset |
| `HAL_PIPECAT_VAD_STOP_SECS` | `0.2` | Live mode: silence before Silero reports a stop — the value Smart Turn's built-in latency figures assume; the model, not this timer, decides whether the turn is over |
| `HAL_PIPECAT_VAD_MIN_VOLUME` | `0.7` | Live mode Silero volume floor (Pipecat default 0.6; far-field value) |
| `HAL_PIPECAT_SILENCE_TIMEOUT_S` | `0.8` | Live mode with Smart Turn off: silence after speech that ends the turn |
| `HAL_PIPECAT_MIN_WORDS` | `2` | Live mode: **while the model is generating** (or a tool call is in flight) a new user turn — and the interruption it broadcasts — starts only once the STT has transcribed this many words; otherwise one word opens a turn, so "yes" / "stop" still work. `_BusyAwareMinWordsStrategy` keys Pipecat's `MinWordsUserTurnStartStrategy` on the agent's LLM state because the stock strategy needs `BotStartedSpeakingFrame`s this pipeline never has. On lamp-ee17 a one-word burst (`do.`) right after a question opened a turn and cancelled the reply mid-generation; `0` = Pipecat's default VAD/transcription start |
| `HAL_PIPECAT_TURN_STOP_TIMEOUT_S` | `5` | Watchdog on a user turn whose transcript never arrives: the aggregator finalizes it anyway (turn-based: an empty committed session already ended the turn earlier) |
| `HAL_PIPECAT_TOOL_RESULT_TIMEOUT_S` | `15` | How long a bridged tool call waits for the orchestrator's `FunctionCallResultInput` before the model gets `{"error": "no result from the device"}` (no follow-up) |
| `HAL_PIPECAT_WEB_SEARCH` | `true` | Registers the client-side `web_search` tool (pipecat only): public live facts are answered in-session through the Google-Search relay instead of delegating to main. Also `realtime.pipecat_v1.web_search` in config.json, or the **Web search** checkbox on `/setting#realtime` |
| `HAL_PIPECAT_SEARCH_URL` | `https://campaign-api.autonomous.ai/api/v1/ai/v1/google-search/v1beta/interactions` | The Gemini Interactions endpoint the tool POSTs to (`tools: [{"type": "google_search"}]`) |
| `HAL_PIPECAT_SEARCH_MODEL` | `gemini-3.7-flash` | Model the relay grounds with |
| `HAL_PIPECAT_SEARCH_API_KEY` | *(empty → the chat key, `HAL_PIPECAT_API_KEY` resolution)* | Bearer key for the search relay |
| `HAL_PIPECAT_SEARCH_TIMEOUT_S` | `10` | HTTP timeout of one lookup (~4 s measured). Blocks the turn's output loop, so keep it below `HAL_PIPECAT_TOOL_RESULT_TIMEOUT_S` or the bridge's generic error answers first |
| `HAL_REALTIME_MEMORY_PATH` | `<workspace>/realtime/memory.jsonl` | |
| `HAL_REALTIME_MAX_MEMORY_ENTRIES` / `_TRIM_KEEP` | `1000` / `500` | |
| `HAL_REALTIME_SUMMARIZER_ENABLED` | `true` | |
| `HAL_REALTIME_SUMMARIZER_MODEL` | `claude-haiku-4-5-20251001` | Anthropic Messages API |
| `HAL_REALTIME_SUMMARIZER_RETRIES` | `2` | Extra attempts per summarize; `0` disables |
| `HAL_REALTIME_SUMMARIZER_RETRY_BACKOFF_S` | `1.5` | Wait before the first retry, doubled each time |
| `HAL_REALTIME_SUMMARY_OPEN_REQUEST_TTL_S` | `3600` | The summariser puts unanswered requests under a final `## Open requests` section (timestamped bullets). HAL drops each bullet from `summary.md` once its `[<ISO-8601>]` stamp is this many seconds old (a bullet without a parseable stamp falls back to the file's age; the heading goes when no bullet is left), both when re-feeding it as `[Previous summary]` and when loading it into session context — a stale pending task in context is what let a content-free nudge make Gemini "answer" it from memory (#419, #421). `0` disables. |

## Code map

| File | Role |
|------|------|
| `orchestrator.py` | Session lifecycle, `delegate_to_main` + `express_emotion` + `look` tools, turn streaming |
| `voice_agent/base.py` | Abstract agent: two-thread queue contract, `receive()` |
| `voice_agent/gemini_live.py` | Gemini Live provider (asyncio IO loop) |
| `voice_agent/openai_realtime.py` | OpenAI Realtime provider (GA schema, sync, lock-serialized connection; live-mode contract parity with Gemini, barge-in truncate, `_OPENAI_RATES` cost table → `openai_usage.log`) |
| `voice_agent/gpt_live.py` | GPT-Live provider (`/v1/live/sessions`, `gpt-live-1`; sync, lock-serialized `LiveConnection`; synthesizes turn boundary / user speech / barge-in from output timing and transcript fragments via the `gptlive-watchdog` thread, client delegation → `delegate_to_main`, `session.thinking.append` feedback, per-minute usage → `gptlive_usage.log`) |
| `voice_agent/pipecat_v1.py` | Pipecat v1 provider: the `VoiceAgentBase` contract (threads, queues, user-turn generations, `end_turn()` fence, tool bridge, both `HAL_LIVE_MODE` shapes) with no `pipecat` import; unit-tested in `hal/test/test_pipecat_v1_agent.py` |
| `voice_agent/pipecat_pipeline.py` | The Pipecat side: builds the pipeline (`HALSTTService` → user aggregator → `OpenAILLMService` → `EventSink` → assistant aggregator) on the `pipecat-io` loop, tool handlers, `PipelineHandle` (thread-safe queue_frame / proposals / finalize / stop), `_CommittedTurnStopStrategy` |
| `voice_agent/pipecat_stt.py` | `HALSTTService`: Pipecat `STTService` over HAL's `STTProvider` (per-turn or long session on a `pipecat-stt` sender thread), `STTFinalizeFrame` |
| `context_manager/{base,openclaw,hermes}.py` | Prompt + memory + skills assembly per gateway |
| `summarizer.py` | Anthropic-based memory summarizer (also the Harness announcer's fallback renderer via `system_prompt=`) |
| `../drivers/harness/{announcer,update_queue}.py` | Harness update queue, snapshot policy, gate and renderer selection |
| `../drivers/voice/_internal/realtime_announce.py` | Harness announcement envelope and realtime playback |
| `config.py` | Provider config models (`GeminiConfig`, `OpenAIConfig`, `GPTLiveConfig`, `PipecatV1Config`) |
| `models/`, `enums/` | Input/output/event types, provider + gateway enums |
| `resources/` | System prompts (shared `system_prompt.md` + per-provider `system_prompt_gemini.md` / `system_prompt_openai.md` / `system_prompt_gptlive.md` / `system_prompt_pipecat.md`) |
| `../voice/voice_service.py` | Integration: streams mic audio, consumes output, routes delegate/handled. Live mode: `_live_decision` / `_live_session` / `_live_out_pump` / `_live_uplink_frame` |
| `../voice/aec.py` | WebRTC AEC3 on the mic path; reference tapped at the TTS output stream (all providers) |

### Buddy agent completion events

Managed desktop sessions report completion/attention/error through the standard sensing route as `buddy.agent.<session_id>`. These passive events queue while the agent or speaker is busy, with distinct session types preserving parallel notifications; the existing sleep and voice privacy policies remain in force. The lamp uses the `agent-management` skill and explicit project/session IDs for follow-ups. Summaries are untrusted result data, not tool authorization. Delivery is best effort; inspecting `agent.session` remains the recovery path.

### Desktop task follow-ups in realtime delegation

All four realtime prompt variants (`system_prompt.md`, `system_prompt_openai.md`, `system_prompt_gemini.md`, `system_prompt_gptlive.md`) and the shared `delegate_to_main` description explicitly route native desktop actions and clear answers, corrections, or stop requests for a known pending main-agent task to the main agent, with blank realtime speech. The delegated message contains only the current user's faithfully understood words and supplied parameters, preserving every clause. Known task context is used internally to decide routing; it is not appended or retold because the main agent already owns the conversation. A brief reply such as “this weekend, two people” can continue the preceding lodging-search clarification; it must not be discarded solely for lacking an action verb or expanded into invented dates.

Recent spoken main-agent questions in `[TTS HISTORY]` can supply the context needed to interpret that follow-up, while the existing no-repetition rule remains. `[TTS HISTORY, not spoken]` does not establish that the user heard or answered the question. Background-speech and addressed-to-device checks remain in force. This change uses the existing realtime handoff/reply history; it adds no structured pending-task store and does not itself prove live voice routing success or remove provider-specific context-delivery limits.

Delegation must preserve named applications and dictated text, not merely the general topic. For example, “Ghi vào Notes là chiều mua sữa” must retain Notes and the full text “chiều mua sữa”; reducing it to a generic milk reminder loses both the target and content. The Gemini/OpenAI prompt variants and the tool description state this explicitly; on GPT-Live the model writes no message at all — the adapter forwards the accumulated input transcript verbatim, so the user's words survive structurally rather than by prompt. A synthetic-audio observation showed that loss in a delegated message; without an input transcript it does not isolate audio recognition from summarization, and the wording change still requires behavioral verification.

The current request or follow-up is forwarded in the language the user just spoke, without commentary or a summary of previous turns. Translating it to English can incorrectly switch the main agent’s reply language because the delegated instruction is its primary input.

A subsequent isolated Gemini 3.1 Live synthetic-audio comparison reused identical PCM clips across the baseline and final prompt/tool definitions. The final delegate messages were “Ghi vào Notes là sáng mai tưới cây.”, “Mở Airbnb tìm chỗ ở Đà Nẵng giúp mình.”, and the follow-up “cuối tuần này hai người”. The baseline had changed the Notes request into “Remember to water the plants tomorrow morning.”, losing the named app and changing language. The Airbnb follow-up used the same provider session after a controlled `[TTS HISTORY]` clarification; the previous server completion boundary and new audio commit were confirmed. This establishes the observed delegation behavior for those synthetic clips, not microphone/wake-word performance, an actual main-agent clarification, or main-agent/desktop end-to-end completion. The isolated final result was recorded at `/tmp/buddy-rt-final/result.json` on the test device; no production prompt or service was changed by that evaluation.

Realtime and Harness-only voice now share the `system/externalhistory` journal and silent delivery worker. HAL still sends `voice_agent_handled` with `[HANDLED]` / `[REPLY]`; OS atomically persists the completed realtime exchange before acknowledging it and resumes never-sent pending history after restart. The existing speaker-supersession hook runs before persistence, and silent/TTS suppression is unchanged. Busy runtimes with active-turn steering retain that capability for realtime history; others wait durably for idle. Ambiguous sends are retained as `uncertain`, not automatically replayed. Flow Monitor displays the sync as **History sync · Realtime → Main**, with the original question/answer as Context. See [external conversation history](os-server.md#external-conversation-history).

LIVE input classification is also sent as observational `voice_turn_type` metadata. It uses the regular wake-phrase classifier and the focus that authorized the input. Direct realtime answers retain the `voice_agent_handled` routing event, while the monitor can display command/follow-up independently.

Diagnostics: `[realtime][timing]` records queued audio commits, first verified progress, grounding receipt, first buffered continuation, continuation release/discard, deferred wait expiry, and receive timeout. Timing uses monotonic seconds since the latest queued commit (not microphone speech end), plus generation and remaining progress/output budgets. A late provider event may follow a newer commit; these fields do not prove which request initiated a search. `Google Search metadata received (search start unknown)` marks receipt of grounding metadata, not search start. The provider does not expose search start here; do not infer search duration from this log.

Gemini extended-thinking camera replay: a successful explicit replay audio commit wakes the receiver and retires the filler's grace, outcome checks and buffered continuation, whether or not Gemini emits `interrupted`. The replay receives a fresh response generation and bounded progress budget while preserving the user question. The provider consumes the old response boundary before new speech; the queue consumer no longer swallows the replay's own fallback terminal. Ordinary commits and LIVE mode do not trigger this reset; user interruption after replay speech still cancels the response. The new response still needs a confirmed outcome; this does not force visual requests to succeed or disable fallback. Diagnostic: `look_replay_response_started`.

Gemini delegation ordering: for work requiring main (including music, specific memory recall and Harness/code tasks), request only the actual `delegate_to_main` call, without Gemini speech or emotion before the handoff. HAL waiting cues remain available; main owns the substantive reply. A compact Gemini-only routing reminder follows identity and memory in the assembled instructions so examples of spoken receipts do not stand in for execution. Greetings remain direct answers; visual questions still use `look`. This changes model instructions, not deterministic routing or fallback deadlines. Validate model compliance using provider `Function call: delegate_to_main` events, not `route=delegated`, which also includes HAL fallback.

In Live ON, an accepted `reject_turn` also installs a persistent rejection barrier before publishing the tool to its consumer. The barrier survives receive-loop boundaries and the tool ACK: provider audio/text from that rejected turn cannot become a new unowned reply or trigger main fallback. A fresh provider speech-start event or nonempty input transcript releases it; protocol terminals and empty transcription-finished metadata do not. Reconnect resets the barrier. This protects turn ownership independently of response language; it does not prevent the remote backend from generating an error after an ACK.

Provider rejection is enforced using the turn's protocol decision, not a language-specific list of error sentences. LIVE ON honors `reject_turn` even after text was generated: the pump drops that turn's buffered/future output and history, cancels only its active/pending realtime TTS, and does not report successful execution. Main speech and newer realtime turns retain their owners. Gemini's rejection barrier also suppresses speech caused by the tool ACK until fresh user input; LIVE OFF retains its existing late-rejection routing policy. The former provider-error phrase table and prefix matching are removed from both text-to-TTS paths; wording alone no longer decides whether a reply is rejected.

Device evidence (lamp-0c4e, 2026-09-25): a silent `generation_complete` at 07:37:17.193 was followed by `IN_PROGRESS`, error text at 18.281/18.955, and `reject_turn` at 19.165. HAL had queued TTS at 19.004 and ignored the rejection because output had begun. For LIVE Extended Thinking, output after a silent terminal preceding the first interaction status is now held in the existing bounded continuation buffer (2 MB audio / 16k text characters). `IN_PROGRESS` alone cannot release it; an accepted `IDLE` without rejection, delegation, interruption, pending tools or overflow can. This delays speech on that specific path until IDLE; ordinary initial responses still stream. No new classifier, sleep, VAD setting or language heuristic is added. A rejection cannot retract audio already heard on the ordinary streaming path; it stops remaining output. A generated apology without a rejection/error signal is not semantically classified by this fix.

Leading `<no speech>` and `{pause}` markers are still removed before TTS and history sync, including split prefixes across receive timeouts of the same turn. Marker-only replies create no `voice_agent_handled`/Main exchange; quoted or embedded mentions remain intact. Existing history is not deleted. Validation replays the captured protocol order with arbitrary-language text, verifies matching-turn cancellation before PCM readiness, and checks valid IDLE output, initial streaming, new-turn recovery, interruption and LIVE OFF. Device deployment/real playback verification remains separate from local tests.

Gemini tool acknowledgements retain the original function name alongside the call ID and return both in `FunctionResponse`. Missing `name` violates the provider contract and reproduced a spoken system-error response after a successful `look` capture on Gemini 3.8. The name is retained until the acknowledgement succeeds and cleared on session reset. Image transport and audio replay remain unchanged.

For NON_BLOCKING Gemini tools, `complete_response` received before any answer text is acknowledged but does not confirm completion or suppress subsequent speech. The normal answer/outcome checks and late-delegation precedence still apply; an empty response still falls back to main. This also applies to the fresh generation after a `look` replay.

Local/Jev voice completion (`handledLocally=true`) releases the retained
realtime thinking cue without waiting for TTS. Muted or silent replies therefore
do not leave the cue active. Cleanup preserves newer emotions and restores the
saved LED state (including off/dim); active speech/music retains its overlay
until normal playback teardown. Agent-owned turns retain their thinking cue.

### Gemini Extended Thinking interaction lifecycle

For `gemini-3.8-live-extended-thinking`, `serverContent.interactionStatus` is the provider lifecycle signal: an utterance terminal with `IN_PROGRESS` does not finish the interaction. HAL preserves this field through a per-session compatibility adapter because google-genai 2.12.1 drops unknown response fields. Subsequent speech streams through the same turn, including after filler and advisory `complete_response` calls. At `IDLE`, HAL finishes a spoken response without an independent classifier when no local tool or withheld continuation remains. Empty output or unresolved local work still falls back; this terminal proves execution ended, not semantic correctness. Explicit delegation/rejection and interruption still take priority. A stalled interaction has a silence bound using the larger of `REALTIME_TURN_MAX_SILENCE_S` and `REALTIME_RECV_QUEUE_TIMEOUT_S`, renewed by output/tool activity, not repeated status heartbeats. Models or sessions without the signal retain the existing bounded grace path. When a session has reported this async status, a fresh `look` returns its JPEG in `FunctionResponse.parts` with the original tool call ID and name, continuing the current interaction without replaying user audio. A separate realtime video frame sent after `activityEnd` was not consumed by the current visual answer in proxy tests; attaching the image to the tool result fixes that association. Only this multimodal result uses an explicit base64 WebSocket payload because google-genai 2.12.1 does not serialize the nested bytes in `send_tool_response`. The result is discarded if its look call was cancelled, resolved, or cleared by a session reset. Other tool ACKs keep the SDK path; sessions without async status keep ACK → video → replay. Standalone image inputs retain their existing pending-tool gate.

### Raw Gemini output transcription diagnostics

`[realtime][wire-output]` logs each `serverContent.outputTranscription` at
WebSocket receipt, before SDK conversion and HAL sentence buffering. Each entry
includes a local session ID, received-frame sequence (`rx`), the status carried
in that frame, terminal/interruption flags, transcription `finished`, and the
full text with escaped newlines. Compare these chunks with `speak_queue` to
distinguish repeated upstream text from downstream duplication. This observes
the Autonomous proxy output, not the model before the proxy; it cannot by itself
distinguish model repetition from proxy replay. No audio/image payload or
credentials are logged. Routing and playback are unchanged.

All realtime prompts (default, OpenAI Realtime, Gemini, Pipecat and both
GPT-Live stages) delegate clearly addressed reports of headache, fatigue, dizziness,
stuffiness or difficulty concentrating, even without a question/action verb
and even with a trailing “okay”. Current-room air/CO₂/temperature/humidity/
ventilation questions and discomfort follow-ups (“Could it be because the room
is too airtight?”) also belong to main's wellbeing/environment skills. The
provider system prompts, Gemini post-memory routing reminder and shared
delegate tool description make this exception explicit: no generic advice, causal explanation, relief
promise, disclaimer, search or `complete_response` in place of handoff.
Urgent reports hand off immediately; main decides whether a sensor check is
appropriate. Unrelated educational questions remain direct answers, and
addressed-speech/audio-fidelity checks still apply. This is model guidance,
not a deterministic symptom classifier. GPT-Live voice uses its native backend
handoff; its backend then calls `delegate_to_main` instead of answering or
searching. Neither stage speaks a wellbeing acknowledgment. It ships with HAL; uploading skills
alone does not update the realtime prompts.

Gemini input transcription language hints are opt-in through the existing `HAL_GEMINI_USE_LANGUAGE_CODES=true` flag. With pinned google-genai 2.12.1, HAL sends `input_audio_transcription.language_hints.language_codes`, not the unsupported Developer API top-level `language_codes`. The hint follows `stt_language` (`vi` becomes `vi-VN`); an empty language or disabled flag retains automatic detection. Output transcription stays unhinted. This biases recognition, not a language lock. The `pro-respeaker-lite` and `pro-xvf3800` profiles enable this flag; other profiles retain the disabled default. On the Lite device with 3.8 extended-thinking, the provider accepted the hint and transcribed the user's gold-price, weather and stop requests in a live test; this is not a general accuracy measurement or XVF3800 acoustic validation.
