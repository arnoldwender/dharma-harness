"""Tests for the live hook, hooks/side-effects-before-write.py.

Same pair as the gate's own suite: the change that adds an effect the signature
does not declare must be WARNED about, and the declared form of the same intent
must pass in silence. The hook is run as a PreToolUse subprocess with the
payload on stdin, inside a scratch working directory, and the exit code, the
stdout JSON and the receipt are what is asserted.

Every defect below lives inside a Python string. The gate that guards this
repository parses Python with `ast`, so a fixture is data to it — which is also
why the hook simulates the whole file for an Edit: the fragment an agent types
is not a module, and a fragment judged alone is an IndentationError, not a
finding.

    python3 -m pytest tests/test_side_effects_hook.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# The mutation runner points this at a mutated COPY; the real hook is never rewritten.
HOOK = Path(os.environ.get("SIDE_EFFECTS_HOOK_UNDER_TEST") or ROOT / "hooks" / "side-effects-before-write.py")


# --- fixtures: what is on disk before the tool call --------------------------

PY_CLEAN = '''\
import functools
import json
import os

CONFIG = {"a": 1}


def read(k):
    return CONFIG[k]


def size(path):
    return 1


def total(items):
    return sum(items)
'''

# Pre-existing debt: `old` already mutates its argument. An edit elsewhere must not
# be charged for it.
PY_DEBT = '''\
def old(xs):
    xs.append(1)
    return xs


def size(path):
    return 1
'''

PY_TWO = '''\
def first(items):
    return list(items)


def second(items):
    return list(items)
'''

PY_BROKEN = "def f(:\n    pass\n"

PY_MUTATES = "def add(xs):\n    xs.append(1)\n    return xs\n"

# One defect per check, each as a Write would leave the file.
WRITES = {
    "mutated-argument": PY_MUTATES,
    "mutable-default": "def f(xs=[]):\n    return xs\n",
    "module-state-write": "SEEN = []\n\n\ndef f(x):\n    SEEN.append(x)\n",
    "impure-declared-pure": "import functools\n\n\n@functools.cache\ndef f(n):\n    print(n)\n    return n\n",
    "import-time-effect": "import os\n\nos.makedirs('/tmp/x')\n",
    "monkey-patch": "import json\n\n\ndef f():\n    json.dumps = None\n",
}

# The same six defects, each as an Edit into PY_CLEAN: (old_string, new_string).
EDITS = {
    "mutated-argument": ("def total(items):\n    return sum(items)\n",
                         "def total(items):\n    items.sort()\n    return sum(items)\n"),
    "mutable-default": ("def size(path):", "def size(path, seen=[]):"),
    "module-state-write": ("    return CONFIG[k]\n", "    CONFIG[k] = 1\n    return CONFIG[k]\n"),
    "impure-declared-pure": ("def size(path):\n    return 1\n",
                             "@functools.cache\ndef size(path):\n    print(path)\n    return 1\n"),
    "import-time-effect": ('CONFIG = {"a": 1}\n', 'CONFIG = {"a": 1}\nos.makedirs("/tmp/x")\n'),
    "monkey-patch": ("    return sum(items)\n", "    json.dumps = None\n    return sum(items)\n"),
}


# --- the runner --------------------------------------------------------------

@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A scratch working directory: no allowlist, nothing inherited from the real repo."""
    return tmp_path


def put(root: Path, rel: str, content: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return str(p)


def hook(root: Path, tool: str, tool_input: dict, env: dict[str, str] | None = None
         ) -> tuple[int, dict | None, str, dict | None]:
    receipts = root / "receipts.jsonl"
    payload = {"session_id": "test-session", "cwd": str(root), "hook_event_name": "PreToolUse",
               "tool_name": tool, "tool_input": tool_input, "tool_use_id": "toolu_x"}
    run_env = {**os.environ, "SIDE_EFFECTS_RECEIPTS": str(receipts)}
    run_env.pop("SIDE_EFFECTS_HOOK_MODE", None)
    run_env.update(env or {})
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, env=run_env, cwd=str(root),
                       check=False, timeout=60)
    out = json.loads(r.stdout) if r.stdout.strip() else None
    rec = None
    if receipts.is_file():
        rec = json.loads(receipts.read_text(encoding="utf-8").strip().split("\n")[-1])
    return r.returncode, out, r.stderr, rec


def edit(root: Path, path: str, old: str, new: str, replace_all: bool = False, **kw):
    given = {"file_path": path, "old_string": old, "new_string": new}
    if replace_all:
        given["replace_all"] = True
    return hook(root, "Edit", given, **kw)


def write(root: Path, path: str, content: str, **kw):
    return hook(root, "Write", {"file_path": path, "content": content}, **kw)


def warning(out: dict | None) -> str:
    return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""


# --- the control -------------------------------------------------------------

def test_a_clean_edit_is_silent(repo: Path) -> None:
    """Without this, every test below could pass because the hook always warns."""
    p = put(repo, "svc.py", PY_CLEAN)
    rc, out, _, rec = edit(repo, p, "def size(path):\n    return 1\n",
                           "def size(path):\n    return len(path)\n")
    assert rc == 0 and out is None, (rc, out)
    assert rec["verdict"] == "ok" and rec["tool"] == "Edit"
    assert rec["judged"] == 2 and rec["simulated"] is True, rec


def test_a_clean_write_is_silent(repo: Path) -> None:
    rc, out, _, rec = write(repo, str(repo / "svc.py"), PY_CLEAN)
    assert rc == 0 and out is None, (rc, out)
    assert rec["verdict"] == "ok" and rec["tool"] == "Write" and rec["judged"] == 17


# --- each check, through a simulated Edit and through a Write ----------------

@pytest.mark.parametrize("check", sorted(EDITS))
def test_each_check_fires_on_an_edit_into_a_clean_file(repo: Path, check: str) -> None:
    """The fragments are indented or depend on the imports above them: judged alone they
    are an IndentationError or a clean module. Only the simulated whole file lets the
    gate see the effect for what it is."""
    p = put(repo, "svc.py", PY_CLEAN)
    old, new = EDITS[check]
    rc, out, _, rec = edit(repo, p, old, new)
    assert rc == 0
    assert f"[{check}]" in warning(out) and "NOT blocked" in warning(out), warning(out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in out["hookSpecificOutput"], "the permission flow is not touched"
    assert rec["verdict"] == "finding" and rec["checks"] == [check] and rec["simulated"] is True


@pytest.mark.parametrize("check", sorted(WRITES))
def test_each_check_fires_on_a_write(repo: Path, check: str) -> None:
    _, out, _, rec = write(repo, str(repo / "svc.py"), WRITES[check])
    assert f"[{check}]" in warning(out), warning(out)
    assert rec["verdict"] == "finding" and rec["checks"] == [check] and rec["tool"] == "Write"


def test_the_declared_form_of_the_same_intent_is_silent(repo: Path) -> None:
    """Return a new value instead of moving the caller's. If this fires, the hook is noise."""
    p = put(repo, "svc.py", PY_CLEAN)
    _, out, _, rec = edit(repo, p, "    return sum(items)\n", "    return sum(sorted(items))\n")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok"


# --- the delta: what you found is not yet your debt -------------------------

def test_a_pre_existing_effect_is_not_reported_when_the_edit_touches_another_line(repo: Path) -> None:
    """`old` mutated its argument before this edit existed, and the edit never went near it."""
    p = put(repo, "svc.py", PY_DEBT)
    _, out, _, rec = edit(repo, p, "    return 1\n", "    return len(path)\n")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok" and rec["judged"] == 1


def test_a_new_effect_beside_an_old_one_is_reported_alone(repo: Path) -> None:
    p = put(repo, "svc.py", PY_DEBT)
    _, out, _, rec = edit(repo, p, "def size(path):\n    return 1\n",
                          "def size(path):\n    return 1\n\n\ndef fresh(ys):\n    ys.clear()\n")
    w = warning(out)
    assert "fresh()" in w and "old()" not in w, w
    assert rec["findings"] == 1 and rec["checks"] == ["mutated-argument"]


def test_a_second_copy_of_an_old_effect_in_the_same_function_is_new(repo: Path) -> None:
    """Identity is the check and the message, as a multiset: the gate's message for a second
    `xs.append` in `old` is byte-identical to the first, and it is still a new effect."""
    p = put(repo, "svc.py", PY_DEBT)
    _, out, _, rec = edit(repo, p, "    xs.append(1)\n", "    xs.append(1)\n    xs.append(2)\n")
    assert "old()" in warning(out) and rec["findings"] == 1, warning(out)


def test_moving_an_old_effect_to_another_line_is_silent(repo: Path) -> None:
    """The line number is not part of the identity: a pre-existing effect pushed three lines
    down by an insertion above it is the same effect."""
    p = put(repo, "svc.py", PY_DEBT)
    _, out, _, rec = edit(repo, p, "def old(xs):\n", "import os\n\n\ndef old(xs):\n")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok"


def test_removing_an_effect_is_silent(repo: Path) -> None:
    p = put(repo, "svc.py", PY_DEBT)
    _, out, _, rec = edit(repo, p, "    xs.append(1)\n    return xs\n", "    return [*xs, 1]\n")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok"


def test_a_file_that_did_not_parse_before_the_edit_is_still_reported_unparseable(repo: Path) -> None:
    """The gate's inability to judge is never subtracted: nothing can be cleared in a file
    it could not read, before or after."""
    p = put(repo, "svc.py", PY_BROKEN)
    _, out, _, rec = edit(repo, p, "    pass\n", "    return 1\n")
    assert "[unparseable]" in warning(out), warning(out)
    assert rec["checks"] == ["unparseable"] and rec["simulated"] is True


def test_a_write_that_keeps_old_debt_and_adds_clean_code_is_silent(repo: Path) -> None:
    p = put(repo, "svc.py", PY_DEBT)
    _, out, _, rec = write(repo, p, PY_DEBT + "\n\ndef name(path):\n    return path\n")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok" and rec["judged"] == 2


def test_a_write_that_adds_a_new_effect_to_a_file_with_old_debt_reports_only_the_new(repo: Path) -> None:
    p = put(repo, "svc.py", PY_DEBT)
    _, out, _, rec = write(repo, p, PY_DEBT + "\n\ndef fresh(ys):\n    ys.clear()\n")
    w = warning(out)
    assert "fresh()" in w and "old()" not in w, w
    assert rec["findings"] == 1


# --- replace_all, MultiEdit, a missing old_string, a new file ------------------

def test_replace_all_judges_every_occurrence(repo: Path) -> None:
    """Two functions share a line. With `replace_all` both gain the effect and both are
    reported; without it only the first does."""
    p = put(repo, "svc.py", PY_TWO)
    _, out, _, rec = edit(repo, p, "    return list(items)\n",
                          "    items.reverse()\n    return list(items)\n", replace_all=True)
    assert rec["findings"] == 2 and "second()" in warning(out), warning(out)
    _, out, _, rec = edit(repo, p, "    return list(items)\n",
                          "    items.reverse()\n    return list(items)\n")
    assert rec["findings"] == 1 and "second()" not in warning(out), warning(out)


def test_multiedit_applies_every_edit_in_order(repo: Path) -> None:
    """The second edit's `old_string` only exists once the first has been applied. Applied
    out of order, or only the first, it is not found and the fragment is judged alone."""
    p = put(repo, "svc.py", PY_CLEAN)
    edits = [
        {"old_string": "    return 1\n", "new_string": "    result = 1\n    return result\n"},
        {"old_string": "    result = 1\n", "new_string": "    path.clear()\n    result = 1\n"},
    ]
    _, out, _, rec = hook(repo, "MultiEdit", {"file_path": p, "edits": edits})
    assert "[mutated-argument]" in warning(out) and "size()" in warning(out), warning(out)
    assert rec["tool"] == "MultiEdit" and rec["simulated"] is True and rec["judged"] == 3


def test_an_old_string_not_found_judges_the_new_string_alone(repo: Path) -> None:
    """The tool will refuse this edit; the hook does not guess where the fragment would have
    gone. Judged alone, an indented fragment does not parse, and the gate's answer to that
    is a finding, not silence."""
    p = put(repo, "svc.py", PY_CLEAN)
    _, out, _, rec = edit(repo, p, "    return NOTHING_LIKE_THIS\n", "    items.sort()\n")
    assert "[unparseable]" in warning(out), warning(out)
    assert rec["simulated"] is False and rec["checks"] == ["unparseable"]


def test_an_old_string_not_found_with_a_fragment_that_parses_is_judged_as_written(repo: Path) -> None:
    p = put(repo, "svc.py", PY_CLEAN)
    _, out, _, rec = edit(repo, p, "    return NOTHING_LIKE_THIS\n", PY_MUTATES)
    assert "[mutated-argument]" in warning(out), warning(out)
    assert rec["simulated"] is False and rec["judged"] == 3


def test_an_edit_of_a_missing_file_judges_the_new_string_alone(repo: Path) -> None:
    _, out, _, rec = edit(repo, str(repo / "new.py"), "", PY_MUTATES)
    assert "[mutated-argument]" in warning(out), warning(out)
    assert rec["simulated"] is False


def test_a_write_that_creates_a_file_judges_every_line(repo: Path) -> None:
    _, out, _, rec = write(repo, str(repo / "new.py"), PY_MUTATES)
    assert "[mutated-argument]" in warning(out) and "`new.py`:2:" in warning(out)
    assert rec["judged"] == 3, "every line of a file that did not exist is new"


# --- scope: the gate's own -----------------------------------------------------

@pytest.mark.parametrize("rel", ["app.ts", "NOTES.md", "config.json", "run.sh", "Makefile"])
def test_a_file_the_gate_would_not_read_is_skipped_not_cleared(repo: Path, rel: str) -> None:
    """The gate reads Python. It says so rather than clearing a file it cannot read — and
    the receipt says `skip`, which is not the same word as `ok`."""
    rc, out, _, rec = write(repo, str(repo / rel), "export const f = (xs) => xs.push(1);\n")
    assert rc == 0 and out is None, (rc, out)
    assert rec["verdict"] == "skip" and rec["why"] == "not-python"


def test_a_relative_file_path_is_resolved_against_cwd(repo: Path) -> None:
    put(repo, "sub/svc.py", PY_CLEAN)
    old, new = EDITS["mutated-argument"]
    _, out, _, rec = edit(repo, "sub/svc.py", old, new)
    assert "[mutated-argument]" in warning(out) and "`svc.py`:" in warning(out)
    assert rec["path"] == str(repo / "sub" / "svc.py")


def test_a_file_that_is_not_utf8_is_not_subtracted_and_the_receipt_says_so(repo: Path) -> None:
    """The disk cannot be read, so nothing is subtracted: every effect in the content is
    reported, and the receipt names the reason rather than skipping in silence."""
    p = repo / "svc.py"
    p.write_bytes(b"\xff\xfe\x00 = 1\n")
    _, out, _, rec = write(repo, str(p), PY_MUTATES)
    assert "[mutated-argument]" in warning(out), warning(out)
    assert rec["disk"] == "unreadable" and rec["verdict"] == "finding"


# --- the allowlist -----------------------------------------------------------

def allow(root: Path, body: str) -> None:
    put(root, ".conduct/side-effects-allow.txt", body)


def test_an_allowlisted_check_is_silent_and_counted(repo: Path) -> None:
    """A silent suppression is the cheap rescue. A counted one is a decision."""
    allow(repo, "# svc.py: the caller passes a scratch list on purpose, 2026-09-17\nsvc\\.py::mutated-argument\n")
    p = put(repo, "svc.py", PY_CLEAN)
    old, new = EDITS["mutated-argument"]
    _, out, _, rec = edit(repo, p, old, new)
    assert out is None, warning(out)
    assert rec["verdict"] == "ok" and rec["exempted"] == 1


def test_the_allowlist_leaves_the_other_checks_live(repo: Path) -> None:
    allow(repo, "svc\\.py::mutated-argument\n")
    p = put(repo, "svc.py", PY_CLEAN)
    old, new = EDITS["mutable-default"]
    _, out, _, _ = edit(repo, p, old, new)
    assert "[mutable-default]" in warning(out), warning(out)


def test_a_malformed_allowlist_entry_is_reported_as_the_gate_reports_it(repo: Path) -> None:
    """The gate's answer to an entry nobody can parse is a `bad-allow-pattern` finding on
    every run; the hook inherits it — never a silent pass, never a warning it did not earn."""
    allow(repo, "*[unclosed\n")
    p = put(repo, "svc.py", PY_CLEAN)
    rc, out, _, rec = edit(repo, p, "    return 1\n", "    return len(path)\n")
    assert rc == 0 and "[bad-allow-pattern]" in warning(out), warning(out)
    assert rec["verdict"] == "finding" and rec["checks"] == ["bad-allow-pattern"]


# --- fail-open and modes -----------------------------------------------------

def test_other_tools_are_ignored_without_a_receipt(repo: Path) -> None:
    p = put(repo, "svc.py", PY_MUTATES)
    for tool in ("Read", "Grep", "Bash"):
        rc, out, _, rec = hook(repo, tool, {"file_path": p, "command": "python3 -c 'x=1'"})
        assert rc == 0 and out is None and rec is None, tool


def test_notebook_edit_is_left_out(repo: Path) -> None:
    p = put(repo, "a.ipynb", "{}")
    rc, out, _, rec = hook(repo, "NotebookEdit", {"notebook_path": p, "new_source": PY_MUTATES})
    assert rc == 0 and out is None and rec is None


def test_a_missing_path_is_ignored(repo: Path) -> None:
    rc, out, _, rec = hook(repo, "Edit", {"old_string": "a", "new_string": PY_MUTATES})
    assert rc == 0 and out is None and rec is None


def test_a_missing_gate_fails_open_with_a_receipt(repo: Path) -> None:
    rc, out, _, rec = write(repo, str(repo / "svc.py"), PY_MUTATES,
                            env={"SIDE_EFFECTS_GATE": str(repo / "none.py")})
    assert rc == 0 and out is None and rec["verdict"] == "error"


def test_block_mode_exits_2_with_the_text_on_stderr(repo: Path) -> None:
    rc, out, err, rec = write(repo, str(repo / "svc.py"), PY_MUTATES,
                              env={"SIDE_EFFECTS_HOOK_MODE": "block"})
    assert rc == 2 and out is None and "Block mode" in err and rec["mode"] == "block"


def test_receipts_can_be_switched_off(repo: Path) -> None:
    rc, out, _, _ = write(repo, str(repo / "svc.py"), PY_MUTATES, env={"SIDE_EFFECTS_RECEIPTS": "off"})
    assert rc == 0 and warning(out)
    assert not (repo / "receipts.jsonl").exists()


# --- the text and the receipt ------------------------------------------------

def test_a_hostile_file_name_is_neutralised_in_the_warning(repo: Path) -> None:
    p = str(repo / "a`b\nc.py")
    _, out, _, _ = write(repo, p, PY_MUTATES)
    w = warning(out)
    assert "`a?b?c.py`" in w and "\n" not in w


def test_the_warning_is_capped_and_stays_far_below_the_runtime_cap(repo: Path) -> None:
    many = "".join(f"def f{i}(xs):\n    xs.append({i})\n\n\n" for i in range(30))
    _, out, _, rec = write(repo, str(repo / "many.py"), many)
    assert "(+26 more)" in warning(out) and rec["findings"] == 30
    assert 0 < len(warning(out)) < 2000               # the runtime caps hook output at 10,000


def test_the_receipt_carries_names_and_counts_never_the_line(repo: Path) -> None:
    p = put(repo, "svc.py", PY_CLEAN)
    edit(repo, p, "    return sum(items)\n", "    secret_token_value = 1\n    items.sort()\n    return sum(items)\n")
    raw = (repo / "receipts.jsonl").read_text(encoding="utf-8").strip().split("\n")[-1]
    rec = json.loads(raw)
    for key in ("ts", "session", "tool", "path", "verdict", "checks", "findings", "judged", "ms", "mode"):
        assert key in rec, (key, rec)
    assert "secret_token_value" not in raw and "items.sort" not in raw, "a receipt never carries the file"


def test_judge_is_importable_for_measurement(repo: Path) -> None:
    """The measurement over recorded sessions calls `judge` directly, one process for all."""
    spec = importlib.util.spec_from_file_location("side_effects_hook_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    p = put(repo, "svc.py", PY_DEBT)
    findings, judged = mod.judge("Edit", {"file_path": p, "old_string": "    return 1\n",
                                          "new_string": "    return len(path)\n"}, str(repo))
    assert (findings, judged) == ([], 1)
    findings, judged = mod.judge("Write", {"file_path": str(repo / "x.py"), "content": PY_MUTATES}, str(repo))
    assert judged == 3 and [f.check for f in findings] == ["mutated-argument"]
    assert mod.judge("Write", {"file_path": str(repo / "x.go"), "content": "x"}, str(repo)) == ([], 0)
    assert mod.judge("Bash", {"command": "python3 -c 'x=1'"}, str(repo)) == ([], 0)


# --- the README quotes the hook, verbatim ------------------------------------

def quoted(markdown: Path) -> list[str]:
    """Every blockquote in the file that starts with the hook's own prefix, joined the way
    markdown joins continuation lines."""
    text = markdown.read_text(encoding="utf-8")
    quotes = []
    for block in re.findall(r"(?:^> .*\n?)+", text, re.M):
        joined = " ".join(line[2:].rstrip() for line in block.strip("\n").split("\n"))
        if joined.startswith("side-effects: "):
            quotes.append(joined)
    return quotes


def test_the_readmes_quote_the_hook_verbatim() -> None:
    """A quotation drifts the way this repo's own epigraphs drifted: one word at a time.
    Both READMEs quote the warning the planted fixture produces; this holds them to it."""
    fixture = json.loads((ROOT / "tests" / "fixtures" / "planted-edit.json").read_text(encoding="utf-8"))
    smoke = subprocess.run([sys.executable, str(ROOT / "tests" / "live_hook_smoke.py"),
                            str(ROOT / "tests" / "fixtures" / "planted-edit.json"), "mutated-argument",
                            "--print"], capture_output=True, text=True, check=False)
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr
    emitted = smoke.stdout.strip().split("\n")[-1]
    assert emitted.startswith("side-effects: ") and fixture["tool_name"] == "Edit"
    for readme in (ROOT / "hooks" / "README.md", ROOT / "README.md"):
        quotes = quoted(readme)
        assert quotes, f"{readme.name} does not quote the hook"
        for q in quotes:
            assert q == emitted, f"{readme.name} quotes the hook with drift:\n{q}\n!=\n{emitted}"
