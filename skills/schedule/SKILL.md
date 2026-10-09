---
name: schedule
description: "List and explain the device's scheduled tasks — the morning brief, reminders and other tasks shown in the Autonomous app and fired by the OS. Use for any question about them: how many there are, what is scheduled, when one runs next or last ran, why it ran at a given time, or why a run was skipped. Always run it first; never answer from memory."
---

# Scheduled tasks

## ⛔ Step −1: run `list` FIRST — never answer from memory

Any question about scheduled tasks — "what's scheduled?", "how many tasks do I
have?", "when does my morning brief run?", "why did it run at 8:03?", "did it
run today?" — requires running `list` **in this turn**, before you write a word
about it. Tasks change from the app at any time; nothing you remember from an
earlier turn is current.

```bash
python3 scripts/schedule.py list          # timezone, count, every task with last and next run
python3 scripts/schedule.py show <id>     # one task in full, with its instructions
```

Run it from this skill's folder. It only reads; it needs no credential.

**Never do this:**

- State a count, a name, a time or a status that is not in the output of this turn.
- Explain why a task ran at a given time without the listed last-run and next-run times in front of you.
- Count the lines yourself. Quote the `Scheduled tasks: N (…)` line.
- Say a task is scheduled when the output marks it `not confirmed yet` or `pending`.
- Present a failed call as "no tasks". If the helper printed an error, say it failed.

## Two places hold scheduled work

1. **The OS task list — this skill.** Tasks the owner sees in the Autonomous
   app (morning brief, inbox sweep, water reminder, custom tasks). The OS fires
   them, whatever agent runtime is active.
2. **Your own scheduler.** If your runtime has a cron or scheduler tool, jobs you
   create with it live inside the runtime only. They **do not appear in the
   app**, are **not** in the `list` output, and are **lost on a factory reset**.

So for "my scheduled tasks" or "what's in the app", use `list`. `list` is not a
total of everything that will run: say so when the owner asks for "everything",
and read your own scheduler too if you have a way to list it. Never merge the two
into one count you did not read.

When you create a recurring job with your own scheduler for the owner, tell them
plainly that it will not show in the app and will not survive a factory reset.
If they want a task in the app, they add it there.

## Reading the output correctly

- **Times** are shown in the device's timezone (the header names it).
- **Next run is not exactly the cadence time.** Daily, weekly and monthly tasks
  are shifted by up to 5 minutes earlier or later (fixed per device and task) so
  devices do not all fire together. `daily at 08:00` with `next run: … 08:03` or
  `07:58` is correct. Interval and one-time tasks are exact.
- **A run that was missed is skipped, not replayed.** If the device was off and a
  run is more than 30 minutes overdue, the OS moves to the next occurrence and
  does not run the old one.
- **`skipped (missing connector: gmail)` is not a failure.** The task needs a
  service the owner has not linked; it runs again once they link it. Point them to
  the app, and use the `connectors` skill if they ask about connection state.
- **`last run … success` means the OS handed the task to the agent**, not that
  the agent finished or the owner heard it.
- **`disabled`** tasks never run and have no next run.
- **`pending` / `not confirmed yet`** means a change was requested on the device
  and is waiting for confirmation; the old version still applies.

## Answering

- Voice: one or two sentences. Say the count, then the next one or two runs. Do
  not read the table.
- Give the time in the owner's words ("tomorrow at 8:03"), from the listed next run.
- Editing, adding, removing or running a task is done in the app. Say so.
