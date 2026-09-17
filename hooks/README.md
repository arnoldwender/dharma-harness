# Hooks — keeping the virtues present

The Codex only works if it's *in context* when the agent acts. A one-time paste into
`AGENTS.md` works; a hook makes it automatic, every session, and opens each run with the
precept.

## `session-start.sh`

Emits, to stdout:

1. The **opening precept** + a rotating **precept of the day** (`bin/precept`, drawn from
   `precepts.txt`).
2. The **conduct block** — the four virtues, precedence, and the gate limit (`codex-block.md`).

It's harness-agnostic: any harness that can run a command at session start can use it, and
its stdout is plain readable text.

## Wiring it into Claude Code

Claude Code injects a `SessionStart` hook's stdout into the session context. Add to your
`settings.json` (use the **absolute** path, and check your Claude Code version's hook docs —
the schema evolves):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "/abs/path/to/dharma-harness/hooks/session-start.sh" }
        ]
      }
    ]
  }
}
```

## Wiring it into any other harness

Run `hooks/session-start.sh` as the first step of your session bootstrap and prepend its
output to the system prompt. The precept goes first, the virtues stay present.

## `side-effects-before-write.py` — the effect you did not declare, caught before it lands

A Claude Code `PreToolUse` hook for `Edit`, `Write` and `MultiEdit`. Before the tool runs, it
hands the file **as it will be after the change** to [`gate/side_effects.py`](../gate/side_effects.py)
— the same six checks, the same allowlist — and, if the change adds an effect the function's
signature does not admit to, tells the agent so in the tool result. It **warns**; it does not
block:

> side-effects: this change leaves an effect its signature does not declare. [mutated-argument]
> `subject.py`:2: add_one(): `items.append(...)` mutates a received argument — the caller keeps
> that object and its signature promises nothing of the sort; return a new value instead.
> Shaucha 4: no residue — read past the diff, the effect a signature does not admit to is
> residue left in the running process. Declare it in the name and the docstring, return a new
> value instead, or move the effect behind a function the caller invokes on purpose; a
> justified exception goes in .conduct/side-effects-allow.txt with its reason. Warning mode:
> this change is NOT blocked.

Why a hook when the gate exists: the gate reads a diff, in CI, after the commit — a verdict,
not a correction. By the time it runs the `xs.append(1)` on a parameter has been written, the
suite that passes alone and fails together has been run, and the summary that read "green" has
been sent. *Śauca* — what you leave behind — is broken at the moment of the act, and the effect
nobody wrote down lands in the running process rather than in the diff, which is why a reviewer
reading the diff does not see it. This is the same gate at that moment.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          { "type": "command",
            "command": "python3 /abs/path/to/dharma-harness/hooks/side-effects-before-write.py",
            "timeout": 10 }
        ]
      }
    ]
  }
}
```

What it hands the gate: the **whole file, simulated** — read from disk, `old_string` replaced by
`new_string` (`replace_all` honoured, a `MultiEdit` applied in order), and the result parsed as
the gate parses it — because the gate reads a module with `ast`, and the fragment an agent types
is not a module: an indented body judged on its own is an `IndentationError`, not a finding. A
`Write` is the content it will leave. When an `old_string` is not in the file the tool will
refuse the edit; `new_string` is judged alone and the receipt says `simulated: false`. Only the
suffixes the gate reads are judged — `SCANNED_SUFFIXES`, one definition, imported from the gate,
which today means `.py` — and any other file is skipped with a receipt that says `skip`, which is
not the same word as `ok`. `Bash` and `NotebookEdit` are not judged: a shell command is not a
Python module, a notebook is JSON.

**It judges the delta, not the file.** The gate's checks run on the file as it is on disk and on
the simulated result, and only the findings new in the result are reported. Identity is the
check plus the gate's message — which names the function and the symbol — never the line
number, so an edit that pushes a pre-existing `xs.append` three lines down does not re-report it,
and a second `xs.append` in a function that already had one does. What you found is not yet your
debt; what you added is. The gate's own inability to judge — `unparseable`, `unreadable`,
`bad-allow-pattern` — is never subtracted: a file that does not parse is reported on every edit,
as the gate reports it on every diff.

The gate is imported with the session's working directory as its root, so
`.conduct/side-effects-allow.txt` — and the repository-relative paths its entries anchor on — is
the repository the agent is working in, not this one. A malformed entry is what it is in the
gate: a `bad-allow-pattern` finding, surfaced in the warning on every run, never a silent pass.
Suppressions are counted in the receipt (`exempted`), as the gate prints them on every run.

Every run leaves a receipt in `~/.local/state/dharma-harness/side-effects-receipts.jsonl`
(`XDG_STATE_HOME` honoured; `SIDE_EFFECTS_RECEIPTS=…` to move it, `off` to disable) — verdict,
checks, counts, the lines the change wrote, never a line of the file. `SIDE_EFFECTS_HOOK_MODE=block`
makes it deny the change instead (exit 2); shipped so the switch exists, not the default. Any
error of its own is a receipt with `verdict: error` and exit 0 — the hook is never the reason a
session cannot proceed. `SIDE_EFFECTS_GATE=…` points it at another copy of the gate; the tests use
it to run a mutated copy of the hook against the real one.

Limits, stated in the file's header so they stay decisions: Python only, by the gate's scope; the
gate's own blind spots inherited whole (`obj.attr = value` on a parameter, `xs += [1]`, a closure
mutating the enclosing parameter); a renamed function that already carried an effect is
re-reported under its new name, because the message is the identity; an `Edit` whose
`old_string` matches twice without `replace_all` is judged on the first match while the tool
refuses it; a file outside the working directory is judged by its absolute path, out of the
allowlist's reach; a Python script typed into `Bash` through a heredoc is not seen at all.

Tests: [`tests/test_side_effects_hook.py`](../tests/test_side_effects_hook.py) ·
mutants: [`tests/mutation_check_side_effects_hook.py`](../tests/mutation_check_side_effects_hook.py) ·
end to end, the runtime's own payload shape: [`tests/live_hook_smoke.py`](../tests/live_hook_smoke.py)
on [`tests/fixtures/planted-edit.json`](../tests/fixtures/planted-edit.json), which is where the
quotation above comes from — the suite holds this file to it byte for byte.

## Just want to see it?

```sh
./hooks/session-start.sh
```
