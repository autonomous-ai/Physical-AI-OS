# OpenClaw Jev integration contract

The integration uses the native `before_prompt_build` hook. On OpenClaw
2026.2.23, the event's `prompt` is the current request and `messages` contains
history. Selection must read only `prompt` (or the newer `currentUserMessage`
when present), never recover a request from history.

`prependContext` is added to the current user prompt and can remain in native
session history, just like reading a skill normally. The integration must not
cache or reuse a selection for a later request, or replace `systemPrompt`.

The host's session `skillsSnapshot.resolvedSkills`, intersected with the
advertised `skillsSnapshot.prompt`, provides the native-filtered roster. The
session ID must match the active hook context. No snapshot means no preload.
Fresh config and file checks must honor operator disables, agent filters and
tool/sandbox policies. Limit initial support to simple OS workspace skills;
leave metadata-dependent skills to the native loader rather than reproducing
native dependency or platform evaluation.

OpenClaw's persisted prompt can include preloaded context. OS run correlation
must strip only the exact, bounded, JSON-valid integration envelope before
matching a pending outgoing request.

Inspected local source: OpenClaw 2026.2.23
`dist/plugin-sdk/plugins/types.d.ts`,
`dist/plugin-sdk/config/sessions/types.d.ts`,
`dist/skills-zC8MIn-b.js` (`resolveWorkspaceSkillPromptState`), and
`dist/pi-embedded-54x4PM3A.js` (`resolvePromptBuildHookResult`).
Current upstream hook contract:
https://github.com/openclaw/openclaw/blob/main/src/plugins/hook-before-agent-start.types.ts

This file records the verified contract; it does not claim that an installed
plugin or end-to-end native gateway test is present.
