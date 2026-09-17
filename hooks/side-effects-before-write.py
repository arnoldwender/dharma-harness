#!/usr/bin/env python3
"""The Dharma Harness — the effect you did not declare, caught before it lands.

A Claude Code `PreToolUse` hook for `Edit`, `Write` and `MultiEdit`. Before the
tool runs, it hands the file AS IT WILL BE after the change to the side-effect
gate (`gate/side_effects.py`) — the same six checks, the same allowlist — and,
if the change adds an effect the function's signature does not admit to (a
parameter mutated, a default shared between calls, a write to module state, an
act inside a function that calls itself pure, an effect at import time, a
patched module), tells the agent so in the tool result. It warns; it does not
block. See hooks/README.md for the wiring and the README for why.

    "hooks": {"PreToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [
        {"type": "command", "command": "python3 /abs/path/to/dharma-harness/hooks/side-effects-before-write.py",
         "timeout": 10}]}]}

WHY A HOOK AND NOT ONLY THE GATE
--------------------------------
The gate reads a diff, in CI, after the commit. That is the right place for a
verdict and the wrong place for a correction: by the time it runs, the
`xs.append(1)` on a parameter has been written, the suite that passes alone and
fails together has been run, and the summary that read "green" has been sent.
Shaucha (शौच) — what you leave behind — is broken at the moment of the act,
and the effect nobody wrote down is left in the running process rather than in
the diff, which is why a reviewer reading the diff does not see it. This hook
is the same gate at that moment, on the one change about to be made.

The rule it cites is Shaucha 4 — "No residue" — read past the diff: the README
places the gate under that axis, "carried from the residue a diff leaves in the
tree to the residue a call leaves in the running process". The numbered
falsifier AS WRITTEN (a debug log or dead block in a merged change) is not what
is decided here, and neither the gate nor this hook claims it; what is decided
is the residue a call leaves behind, which the six checks name exactly.

WHAT IT DOES
------------
1. Reads the hook payload from stdin: `tool_name`, `tool_input`, `cwd`,
   `session_id`. Ignores every tool but the three above, without a receipt.
   `Bash` is left out because a shell command is not a Python module, and
   `NotebookEdit` because a notebook is JSON — neither is what the gate reads.
2. Imports the gate with `HARNESS_ROOT` set to the session's working directory,
   so `.conduct/side-effects-allow.txt` is the working repository's, and file
   paths are judged relative to it — not to this repository. The gate is
   imported per call rather than cached: a module-level cache here would be a
   `module-state-write` by the gate's own definition, and it would be right.
3. Only the suffixes the gate reads (`SCANNED_SUFFIXES`, one definition, in the
   gate) are judged; any other file is skipped with a receipt saying so.
4. SIMULATES the change. The file is read from disk and every edit applied —
   `old_string` replaced by `new_string`, every occurrence with `replace_all`,
   a `MultiEdit` in order — and the WHOLE resulting file goes to the gate,
   because the gate parses a module (`ast.parse`) and a fragment is not a
   module: an indented body judged on its own is an `IndentationError`, not a
   finding. A `Write` is the content it will leave. When an `old_string` is not
   in the file the tool will refuse the edit; `new_string` is judged alone and
   the receipt says `simulated: false`.
5. Judges the DELTA, not the file. The gate's checks run on the file as it is
   on disk and on the simulated result, and only the findings NEW in the
   result are reported — identity is the check plus the gate's message (which
   names the function and the symbol), never the line number, so an edit that
   moves a pre-existing effect down three lines does not re-report it. What
   you found is not yet your debt; what you added is. Findings that describe
   the gate's own inability to judge — `unparseable`, `unreadable`,
   `bad-allow-pattern` — are never subtracted: a file that does not parse is
   reported on every edit, as the gate reports it on every diff, because
   nothing can be cleared in a file the gate could not read.
6. Applies the working repository's allowlist exactly as the gate does
   (`load_allowlist` + `apply_allowlist`, the gate's own functions). A
   malformed entry is what it is in the gate — a `bad-allow-pattern` finding,
   surfaced in the warning — never a silent pass.
7. If anything remains: prints `{"hookSpecificOutput": {"hookEventName":
   "PreToolUse", "additionalContext": "..."}}` and exits 0. Claude Code adds
   that text to the agent's context alongside the tool result. The permission
   flow is not touched. At most four findings are shown.
8. Appends one receipt line per run to `SIDE_EFFECTS_RECEIPTS` (default
   `~/.local/state/dharma-harness/side-effects-receipts.jsonl`, honouring
   `XDG_STATE_HOME`; `off` disables): `{ts, session, tool, path, verdict,
   checks, findings, judged, ms}` plus `mode`, `simulated` and `exempted` —
   names and counts, never a line of the file. `judged` is the number of lines
   the change wrote. The receipts are how the rate gets measured on real
   sessions, which is the number this hook needs before anyone lets it block.

`judge(tool, tool_input, root)` is importable and returns `(findings,
judged_lines)`, so that rate can be measured over recorded sessions without a
process per tool call.

WHAT IT DOES NOT SEE (stated so they stay decisions)
---------------------------------------------------
* Python only, and only `.py` — the gate's scope, imported. An edit to any
  other file is `skip`, which is not the same word as `ok`.
* The gate's own blind spots, inherited whole: `obj.attr = value` on a
  parameter, `xs += [1]` on a parameter, a closure mutating the enclosing
  function's parameter.
* The delta is by identity, and a message carries the function's name. Rename
  a function that already carried an effect and the effect is re-reported
  under the new name; change a signature so that an existing line becomes an
  effect (a local turned into a parameter) and it is reported as new — which
  it is.
* An `Edit` whose `old_string` matches more than once without `replace_all`:
  the tool refuses it; the hook judges the first match and says nothing about
  the refusal.
* A file outside the working directory is judged by its absolute path, so an
  allowlist entry anchored on a repository-relative path does not reach it.
* A Python script typed into the Bash tool through a heredoc. `Bash` is not
  judged here at all.
* Any runtime other than Claude Code. Its payload is the only shape that was
  run, so it is the only one this hook reads.

MODES AND FAIL-OPEN
-------------------
`SIDE_EFFECTS_HOOK_MODE=warn` (default) injects the text and exits 0.
`SIDE_EFFECTS_HOOK_MODE=block` writes it to stderr and exits 2, which Claude
Code treats as a denial. Block is shipped so the switch exists; it is not the
default, because a guard whose false-positive rate nobody has measured on real
sessions is switched off by the first person it wrongly stops.

Any error of the hook's own is a receipt with `verdict: error` and exit 0. The
gate can say "I broke" with exit 2 because exit 2 means nothing else there;
here exit 2 means "deny the tool call", and a hook that denies an agent's edit
because of its own bug is the hook that gets uninstalled, after which it
catches nothing. So it stays out of the way and leaves a receipt: the failure
is counted, never silent, and `verdict: error` in the receipts is the first
thing to grep when the warning goes quiet.

Tests: tests/test_side_effects_hook.py · mutants: tests/mutation_check_side_effects_hook.py
"""
from __future__ import annotations

import collections
import importlib.util
import json
import os
import pathlib
import re
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
GATE = pathlib.Path(os.environ.get("SIDE_EFFECTS_GATE") or HERE.parent / "gate" / "side_effects.py")


def _default_receipts() -> str:
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "dharma-harness", "side-effects-receipts.jsonl")


RECEIPTS = os.environ.get("SIDE_EFFECTS_RECEIPTS") or _default_receipts()
MODE = os.environ.get("SIDE_EFFECTS_HOOK_MODE", "warn")            # warn | block
TOOLS = {"Edit", "Write", "MultiEdit"}
# mutation-anchor: TOOLS
MAX_SHOWN = 4
MAX_MESSAGE = 260                # per finding; the runtime caps hook output at 10,000
# Findings that describe the gate's inability to judge rather than an effect. They
# are never subtracted by the delta: a file that does not parse is reported on every
# edit, as the gate reports it on every diff.
NOT_AN_EFFECT = frozenset({"unparseable", "unreadable", "bad-allow-pattern"})


# --- receipts ----------------------------------------------------------------

def receipt(**row: object) -> None:
    """One JSON line per run. Names and counts only — never a line of the file."""
    if RECEIPTS == "off":
        return
    try:
        pathlib.Path(RECEIPTS).parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **row}
        with open(RECEIPTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — a receipt never brings the hook down
        pass


# --- the gate ----------------------------------------------------------------

def load_gate(root: str):
    """Import the gate by path with `HARNESS_ROOT` = the session's working directory.
    The gate fixes its root — and with it the allowlist — at import time."""
    os.environ["HARNESS_ROOT"] = root
    spec = importlib.util.spec_from_file_location("dharma_side_effects_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                        # the dataclasses resolve their annotations here
    spec.loader.exec_module(mod)
    return mod


# --- the simulated change ----------------------------------------------------

def real_lines(text: str) -> int:
    """Lines a text really has: the "\\n"-separated segments, not counting an empty last one."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _occurrences(text: str, needle: str, everywhere: bool) -> list[int]:
    """Offsets of `needle` in `text`: the first, or all non-overlapping ones (`str.replace`)."""
    out: list[int] = []
    pos = text.find(needle)
    while pos != -1:
        out.append(pos)
        if not everywhere:
            break
        pos = text.find(needle, pos + len(needle))
    return out


def simulate_edits(text: str, edits: list[dict]) -> tuple[str, bytearray, bool]:
    """Apply the edits in order. Returns the result, a per-character origin map (1 = written
    by this tool call, 0 = already there) and whether every edit was applied.

    An `old_string` that is not in the text is what the tool itself will refuse; the hook
    does not guess where it would have gone. That edit's `new_string` is judged alone, every
    character new, and the edits after it are not applied — the tool would not have applied
    them either."""
    origin = bytearray(len(text))
    for e in edits:
        # mutation-anchor: MULTIEDIT
        old = str(e.get("old_string") or "")
        new = str(e.get("new_string") or "")
        hits = _occurrences(text, old, bool(e.get("replace_all"))) if old else []
        # mutation-anchor: SIMULATE
        if not hits:
            return new, bytearray(b"\x01") * len(new), False
        for s in reversed(hits):                 # back to front, so earlier offsets hold
            text = text[:s] + new + text[s + len(old):]
            origin = origin[:s] + bytearray(b"\x01") * len(new) + origin[s + len(old):]
    return text, origin, True


def added_from_origin(text: str, origin: bytearray) -> set[int]:
    """1-based lines of `text` that hold at least one character written by this call."""
    out: set[int] = set()
    pos = 0
    for n, line in enumerate(text.split("\n"), 1):
        end = pos + len(line) + 1                # the newline belongs to the line it ends
        if origin.find(b"\x01", pos, end) != -1:
            out.add(n)
        pos = end
    return out


def added_by_write(old: str | None, new: str) -> set[int]:
    """Lines of `new` that the old file did not hold, by exact text; all of them if there was
    no old file. The same reading the gate gives an untracked file: 100 % new code."""
    total = real_lines(new)
    if old is None:
        return set(range(1, total + 1))
    seen = set(old.split("\n"))
    return {n for n, line in enumerate(new.split("\n"), 1) if n <= total and line not in seen}


def edits_of(tool: str, given: dict) -> list[dict]:
    if tool == "MultiEdit" and isinstance(given.get("edits"), list):
        return [e for e in given["edits"] if isinstance(e, dict)]
    return [given]


def _disk(target: str) -> tuple[str | None, bool]:
    """(content, unreadable). None with False: the file does not exist. None with True: it
    exists and is not UTF-8 text."""
    p = pathlib.Path(target)
    if not p.is_file():
        return None, False
    try:
        return p.read_text(encoding="utf-8"), False
    except (OSError, UnicodeDecodeError):
        return None, True


def rel_path(target: str, root: str) -> str:
    """The path as the gate spells it: repository-relative, forward slashes. Outside the
    root it stays absolute — an anchored allowlist entry cannot reach it, and should not."""
    rel = os.path.relpath(target, root)
    if rel.startswith(".."):
        return target.replace(os.sep, "/")
    return rel.replace(os.sep, "/")


def prepare(ro, tool: str, given: dict, root: str) -> tuple[str, str, str, str | None, set[int], dict]:
    """(target, rel, before, after, added, meta): the file on disk, the file as this call
    will leave it, the lines this call wrote, and what the receipt should say. `after` is
    None with a `skip` in `meta` when there is nothing the gate would read."""
    target = str(given.get("file_path") or "")
    if not os.path.isabs(target):
        target = os.path.join(root, target)
    target = os.path.normpath(target)
    rel = rel_path(target, root)
    if pathlib.Path(target).suffix not in ro.SCANNED_SUFFIXES:
        # mutation-anchor: SCOPE
        return target, rel, "", None, set(), {"skip": "not-python"}   # the gate's own scope, one definition
    current, unreadable = _disk(target)
    meta: dict = {}
    if unreadable:
        # The disk cannot be subtracted, so nothing is: every effect in the result is
        # reported, and the receipt says why.
        meta["disk"] = "unreadable"
        current = None
    before = current or ""
    if tool == "Write":
        after = str(given.get("content") or "")
        return target, rel, before, after, added_by_write(current, after), meta
    if current is None:
        after = "\n".join(str(e.get("new_string") or "") for e in edits_of(tool, given))
        meta["simulated"] = False
        return target, rel, "", after, set(range(1, real_lines(after) + 1)), meta
    after, origin, simulated = simulate_edits(current, edits_of(tool, given))
    meta["simulated"] = simulated
    if not simulated:
        before = ""                                # judged alone: nothing to subtract
    return target, rel, before, after, added_from_origin(after, origin), meta


# --- the predicate -----------------------------------------------------------

def delta(before: list, after: list) -> list:
    """The findings of `after` that `before` did not carry, by identity: the check and the
    gate's message, which names the function and the symbol — never the line number. A
    multiset, so a second `xs.append` in a function that already had one is new. The
    gate's own inability to judge is never subtracted."""
    carried = collections.Counter((f.check, f.message) for f in before)
    fresh: list = []
    for f in after:
        key = (f.check, f.message)
        if f.check in NOT_AN_EFFECT:
            # mutation-anchor: UNPARSEABLE
            fresh.append(f)
        elif carried[key] > 0:
            carried[key] -= 1
            # mutation-anchor: DELTA
        else:
            fresh.append(f)
    return fresh


def run_gate(ro, rel: str, before: str, after: str, filename: str) -> tuple[list, int]:
    """The gate's checks on the file as it will be, minus what the file already carried,
    with the working repository's allowlist applied as the gate applies it.
    Returns (findings kept, findings exempted)."""
    found_after, _ = ro.analyse_source(after, rel, filename)
    found_before = ro.analyse_source(before, rel, filename)[0] if before else []
    fresh = delta(found_before, found_after)
    patterns, problems = ro.load_allowlist()            # a malformed entry is a finding, as in the gate
    kept, suppressed = ro.apply_allowlist(fresh, patterns)
    # mutation-anchor: ALLOWLIST
    return problems + kept, suppressed


def assess(tool: str, given: dict, root: str) -> tuple[list, int, int, dict]:
    """(findings, lines judged, findings exempted, receipt info) for one tool call."""
    ro = load_gate(root)
    target, rel, before, after, added, meta = prepare(ro, tool, given, root)
    meta["path"] = target
    if after is None:
        return [], 0, 0, meta
    kept, exempted = run_gate(ro, rel, before, after, target)
    return kept, len(added), exempted, meta


def judge(tool: str, tool_input: dict, root: str) -> tuple[list, int]:
    """Findings the gate raises for ONE tool call, judged from `root`, and the number of
    lines it judged. Importable, so the rate can be measured over real sessions without
    spawning a process per call. A tool or file the gate does not judge is `([], 0)`."""
    if tool not in TOOLS or not (tool_input or {}).get("file_path"):
        return [], 0
    findings, judged, _, _ = assess(tool, tool_input or {}, root)
    return findings, judged


# --- the warning -------------------------------------------------------------

def safe_name(path: str) -> str:
    """The base name without what could break the warning's markdown or smuggle text shaped
    like an instruction (backticks, line breaks, control characters). The agent already saw
    the name in its own tool input; this is depth, not a boundary."""
    base = os.path.basename(path)
    return re.sub(r"[`\r\n\t\x00-\x1f\x7f]", "?", base)[:120]


def message(findings: list) -> str:
    parts = [f"[{f.check}] `{safe_name(f.path)}`:{f.line}: {f.message[:MAX_MESSAGE]}."
             for f in findings[:MAX_SHOWN]]
    more = f" (+{len(findings) - MAX_SHOWN} more)" if len(findings) > MAX_SHOWN else ""
    return ("side-effects: this change leaves an effect its signature does not declare. "
            + " ".join(parts) + more
            + " Shaucha 4: no residue — read past the diff, the effect a signature does not "
            "admit to is residue left in the running process. Declare it in the name and the "
            "docstring, return a new value instead, or move the effect behind a function the "
            "caller invokes on purpose; a justified exception goes in "
            ".conduct/side-effects-allow.txt with its reason. Warning mode: this change is "
            "NOT blocked.")


# --- main --------------------------------------------------------------------

def main() -> int:
    t0 = time.time()
    payload = json.loads(sys.stdin.read() or "{}")
    tool = payload.get("tool_name")
    if tool not in TOOLS:
        return 0
    given = payload.get("tool_input") or {}
    if not isinstance(given, dict) or not given.get("file_path"):
        return 0                                     # nothing was handed in
    root = str(payload.get("cwd") or os.getcwd())
    session = str(payload.get("session_id") or "")[:8]
    common = {"session": session, "tool": tool, "mode": MODE}

    findings, judged, exempted, meta = assess(tool, given, root)
    ms = int((time.time() - t0) * 1000)
    common["path"] = meta.get("path")
    if "skip" in meta:
        receipt(verdict="skip", why=meta["skip"], ms=ms, **common)
        return 0
    for key in ("simulated", "disk"):
        if key in meta:
            common[key] = meta[key]
    if not findings:
        receipt(verdict="ok", judged=judged, exempted=exempted, ms=ms, **common)
        return 0
    # mutation-anchor: WARNING
    receipt(verdict="finding", judged=judged, exempted=exempted, findings=len(findings),
            checks=sorted({f.check for f in findings}), ms=ms, **common)
    text = message(findings)
    if MODE == "block":
        sys.stderr.write(text.replace("Warning mode: this change is NOT blocked.",
                                      "Block mode: this change was not applied.") + "\n")
        return 2
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "additionalContext": text}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open on purpose: the hook is never the blocker
        receipt(verdict="error", error=f"{type(exc).__name__}: {exc}"[:200])
        sys.exit(0)
