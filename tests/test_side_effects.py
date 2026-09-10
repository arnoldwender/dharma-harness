"""Tests for the side-effect gate.

Every check gets the same treatment: a file with the defect planted, which must
turn the gate RED, and the clean form of the same idea, which must leave it
GREEN. A suite that only ever sees defects proves the gate fires; a suite that
only ever sees clean code proves nothing at all. Both halves are the test.

    python3 -m pytest tests/ -q

The gate is invoked as a subprocess rather than imported, because the exit code
is part of the contract the whole conduct-harness family shares (0 clean,
1 findings, 2 the gate itself broke). Importing would exercise the functions and
leave the contract untested.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "gate" / "side_effects.py"

CLEAN = """\
from dataclasses import dataclass, field


def copy_and_add(xs):
    return [*xs, 1]


def accumulate():
    acc = []
    acc.append(1)
    return acc


@dataclass
class Box:
    items: list = field(default_factory=list)

    def add(self, value):
        self.items.append(value)
        self.count = len(self.items)


def main():
    return 0


if __name__ == "__main__":
    main()
"""


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HARNESS_ROOT": str(root)}
    return subprocess.run([sys.executable, str(GATE), *args],
                          capture_output=True, text=True, env=env, check=False)


def plant(root: Path, name: str, source: str) -> str:
    """Write a file into the scratch repo and return the path to hand the gate."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return str(path)


def check(root: Path, source: str, name: str = "subject.py") -> subprocess.CompletedProcess[str]:
    return run(root, "--files", plant(root, name, source))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An empty scratch repo: no allowlist, nothing inherited from the real one."""
    return tmp_path


# --- the control -------------------------------------------------------------

def test_clean_repo_passes(repo: Path) -> None:
    """Without this, every test below could pass because the gate always fails."""
    result = check(repo, CLEAN)
    assert result.returncode == 0, result.stdout
    assert "signature admits" in result.stdout


# --- CHECK 1: a mutated argument ---------------------------------------------

def test_appending_to_a_parameter_is_caught(repo: Path) -> None:
    result = check(repo, "def f(xs):\n    xs.append(1)\n")
    assert result.returncode == 1, result.stdout
    assert "mutated-argument" in result.stdout


def test_returning_a_copy_passes(repo: Path) -> None:
    """The declared form of the same intent. If this fires, the gate is unusable."""
    result = check(repo, "def f(xs):\n    return [*xs, 1]\n")
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("body", [
    "    xs.clear()",
    "    xs.update({'k': 1})",
    "    xs.sort()",
    "    xs.pop()",
    "    xs[0] = 2",
    "    xs[0] += 2",
    "    del xs[0]",
])
def test_every_shape_of_argument_mutation_is_caught(repo: Path, body: str) -> None:
    result = check(repo, f"def f(xs):\n{body}\n")
    assert result.returncode == 1, f"{body!r} did not fail the gate:\n{result.stdout}"
    assert "mutated-argument" in result.stdout


def test_mutating_self_in_a_method_passes(repo: Path) -> None:
    """Changing the receiver is what a method is FOR. Flagging it would be noise."""
    result = check(repo, "class C:\n    def m(self):\n        self.x = 1\n"
                         "        self.items.append(1)\n")
    assert result.returncode == 0, result.stdout


def test_mutating_a_local_passes(repo: Path) -> None:
    result = check(repo, "def f():\n    acc = []\n    acc.append(1)\n    return acc\n")
    assert result.returncode == 0, result.stdout


def test_copying_before_mutating_passes(repo: Path) -> None:
    """`xs = list(xs)` moves the name off the caller's object before touching it."""
    result = check(repo, "def f(xs):\n    xs = list(xs)\n    xs.append(1)\n    return xs\n")
    assert result.returncode == 0, result.stdout


def test_mutating_before_rebinding_is_still_caught(repo: Path) -> None:
    """The damage is done on line 2. Rebinding afterwards does not undo it.

    This is why the gate records WHERE a parameter was rebound instead of only
    whether it ever was: the cheap version of this check would clear the file.
    """
    result = check(repo, "def f(xs):\n    xs.append(1)\n    xs = None\n    return xs\n")
    assert result.returncode == 1, result.stdout
    assert "mutated-argument" in result.stdout


def test_a_nested_function_is_judged_on_its_own_parameters(repo: Path) -> None:
    """`g`'s `xs` shadows `f`'s. Without a scope boundary this invents a finding."""
    result = check(repo, "def f(xs):\n    def g(xs):\n        xs = list(xs)\n"
                         "        return xs\n    return g(xs)\n")
    assert result.returncode == 0, result.stdout


# --- CHECK 2: a mutable default ----------------------------------------------

@pytest.mark.parametrize("signature", ["xs=[]", "d={}", "s=set()", "xs=list()",
                                       "d=dict()", "*, kw=[]"])
def test_mutable_defaults_are_caught(repo: Path, signature: str) -> None:
    result = check(repo, f"def f({signature}):\n    return 1\n")
    assert result.returncode == 1, f"{signature!r} did not fail:\n{result.stdout}"
    assert "mutable-default" in result.stdout


def test_none_default_passes(repo: Path) -> None:
    result = check(repo, "def f(xs=None):\n    return list(xs or [])\n")
    assert result.returncode == 0, result.stdout


def test_dataclass_default_factory_passes(repo: Path) -> None:
    """The declared way to have a per-instance list. It must not read as the bug."""
    result = check(repo, "from dataclasses import dataclass, field\n\n\n"
                         "@dataclass\nclass C:\n    items: list = field(default_factory=list)\n")
    assert result.returncode == 0, result.stdout


# --- CHECK 3: a write to module state ----------------------------------------

def test_global_reassignment_is_caught(repo: Path) -> None:
    result = check(repo, "STATE = 0\n\n\ndef f():\n    global STATE\n    STATE = 1\n")
    assert result.returncode == 1, result.stdout
    assert "module-state-write" in result.stdout


def test_writing_into_a_module_level_container_is_caught(repo: Path) -> None:
    """No `global` needed, and that is what makes it easy to miss in review."""
    result = check(repo, "CACHE = {}\n\n\ndef f(k, v):\n    CACHE[k] = v\n")
    assert result.returncode == 1, result.stdout
    assert "module-state-write" in result.stdout


def test_mutating_a_module_level_list_is_caught(repo: Path) -> None:
    result = check(repo, "SEEN = []\n\n\ndef f(x):\n    SEEN.append(x)\n")
    assert result.returncode == 1, result.stdout
    assert "module-state-write" in result.stdout


def test_a_local_that_shadows_a_module_name_passes(repo: Path) -> None:
    result = check(repo, "CACHE = {}\n\n\ndef f(k, v):\n    CACHE = {}\n"
                         "    CACHE[k] = v\n    return CACHE\n")
    assert result.returncode == 0, result.stdout


def test_reading_module_state_passes(repo: Path) -> None:
    """A read leaves no trace. Only the write is the effect."""
    result = check(repo, "CONFIG = {'a': 1}\n\n\ndef f(k):\n    return CONFIG[k]\n")
    assert result.returncode == 0, result.stdout


# --- CHECK 4: declared pure, and not ------------------------------------------

def test_cached_function_that_prints_is_caught(repo: Path) -> None:
    """Memoised: the print happens on call one and silently never again."""
    result = check(repo, "import functools\n\n\n@functools.cache\ndef f(n):\n"
                         "    print(n)\n    return n\n")
    assert result.returncode == 1, result.stdout
    assert "impure-declared-pure" in result.stdout


def test_docstring_declaring_purity_over_a_file_write_is_caught(repo: Path) -> None:
    source = (
        "def f(n):\n"
        '    """Compute n. This function is pure."""\n'
        "    open('/dev/null', 'w').write(str(n))\n"
        "    return n\n"
    )
    result = check(repo, source)
    assert result.returncode == 1, result.stdout
    assert "impure-declared-pure" in result.stdout


def test_environment_write_inside_a_declared_pure_function_is_caught(repo: Path) -> None:
    source = (
        "import os\n\n\n"
        "def f(v):\n"
        '    """Set it. No side effects."""\n'
        "    os.environ['K'] = v\n"
    )
    result = check(repo, source)
    assert result.returncode == 1, result.stdout
    assert "impure-declared-pure" in result.stdout


def test_cached_function_without_effects_passes(repo: Path) -> None:
    result = check(repo, "import functools\n\n\n@functools.cache\ndef f(n):\n"
                         "    return n * 2\n")
    assert result.returncode == 0, result.stdout


def test_an_undeclared_function_that_prints_passes(repo: Path) -> None:
    """This check judges the DECLARATION, not printing. A function that never
    claimed to be a value is free to act — that is the whole point of the axis."""
    result = check(repo, "def f(n):\n    print(n)\n    return n\n")
    assert result.returncode == 0, result.stdout


# --- CHECK 5: an effect at import time ----------------------------------------

def test_network_call_at_import_time_is_caught(repo: Path) -> None:
    result = check(repo, "import requests\n\nrequests.get('https://example.invalid')\n")
    assert result.returncode == 1, result.stdout
    assert "import-time-effect" in result.stdout


@pytest.mark.parametrize("statement", [
    "load_dotenv()",
    "CONN = psycopg2.connect('dsn')",
    "os.makedirs('/tmp/x')",
    "FH = open('/tmp/x')",
])
def test_effects_bound_at_import_time_are_caught(repo: Path, statement: str) -> None:
    source = f"import os\nimport psycopg2\nfrom dotenv import load_dotenv\n\n{statement}\n"
    result = check(repo, source)
    assert result.returncode == 1, f"{statement!r} did not fail:\n{result.stdout}"
    assert "import-time-effect" in result.stdout


def test_effect_inside_a_try_at_import_time_is_still_caught(repo: Path) -> None:
    """Wrapping it in a `try:` hides the traceback, not the effect."""
    source = ("import os\n\ntry:\n    os.makedirs('/tmp/x')\nexcept OSError:\n    pass\n")
    result = check(repo, source)
    assert result.returncode == 1, result.stdout
    assert "import-time-effect" in result.stdout


def test_definitions_only_module_passes(repo: Path) -> None:
    result = check(repo, "import os\n\nROOT = os.path.dirname('/a/b')\n\n\n"
                         "def f():\n    return ROOT\n")
    assert result.returncode == 0, result.stdout


def test_main_guard_passes(repo: Path) -> None:
    """`if __name__ == "__main__":` is the declared place for effects."""
    source = ("import os\nimport sys\n\n\ndef main():\n    return 0\n\n\n"
              "if __name__ == '__main__':\n    os.makedirs('/tmp/x', exist_ok=True)\n"
              "    sys.exit(main())\n")
    result = check(repo, source)
    assert result.returncode == 0, result.stdout


# --- CHECK 6: monkey-patching -------------------------------------------------

def test_patching_an_imported_module_is_caught(repo: Path) -> None:
    result = check(repo, "import json\n\n\ndef f():\n    json.dumps = None\n")
    assert result.returncode == 1, result.stdout
    assert "monkey-patch" in result.stdout


def test_setattr_on_an_imported_module_is_caught(repo: Path) -> None:
    result = check(repo, "import json\n\n\ndef f():\n    setattr(json, 'dumps', None)\n")
    assert result.returncode == 1, result.stdout
    assert "monkey-patch" in result.stdout


def test_attribute_write_on_a_local_object_passes(repo: Path) -> None:
    result = check(repo, "def f():\n    class Box:\n        pass\n"
                         "    box = Box()\n    box.x = 1\n    return box\n")
    assert result.returncode == 0, result.stdout


# --- robustness: the gate must not become the outage --------------------------

def test_syntax_error_is_reported_and_the_run_continues(repo: Path) -> None:
    """A file the gate cannot parse is never reported clean, and never stops it.

    The second file carries a real defect: if the crash aborted the run, that
    finding would go missing and the diff would look better than it is.
    """
    broken = plant(repo, "broken.py", "def f(:\n    pass\n")
    dirty = plant(repo, "dirty.py", "def f(xs):\n    xs.append(1)\n")
    result = run(repo, "--files", broken, dirty)
    assert result.returncode == 1, result.stdout
    assert "unparseable" in result.stdout
    assert "mutated-argument" in result.stdout, "the run stopped at the broken file"
    assert "Traceback" not in result.stderr


def test_missing_file_is_a_finding_not_a_pass(repo: Path) -> None:
    """When the input is absent the answer is never 'clean'."""
    result = run(repo, "--files", str(repo / "never-written.py"))
    assert result.returncode == 1, result.stdout
    assert "missing-file" in result.stdout


def test_non_python_file_is_counted_as_skipped(repo: Path) -> None:
    """The gate reads Python. It says so rather than clearing a file it cannot read."""
    other = plant(repo, "app.ts", "export const f = (xs: number[]) => xs.push(1);\n")
    result = run(repo, "--files", other)
    assert result.returncode == 0, result.stdout
    assert "1 other file(s) skipped" in result.stdout


def test_unresolvable_base_ref_is_gate_failure_not_a_verdict(repo: Path) -> None:
    """Exit 2 is the whole reason the contract has three codes.

    Returning 1 here would read as 'I found something'; returning 0 would read as
    'clean' and fail OPEN, which is the sin SATYA names as never tradeable.
    """
    result = run(repo, "--base", "origin/main")
    assert result.returncode == 2, result.stdout
    assert "gate failure" in result.stderr


# --- the allowlist, and its own failure modes ---------------------------------

def test_allowlist_suppresses_and_says_it_did(repo: Path) -> None:
    """A silent suppression is the cheap rescue. A counted one is a decision."""
    (repo / ".conduct").mkdir()
    (repo / ".conduct" / "side-effects-allow.txt").write_text(
        "# vendored\nsubject\\.py\n", encoding="utf-8")
    result = check(repo, "def f(xs):\n    xs.append(1)\n")
    assert result.returncode == 0, result.stdout
    assert "1 finding(s) suppressed" in result.stdout


def test_allowlist_can_silence_one_check_and_leave_the_others_live(repo: Path) -> None:
    (repo / ".conduct").mkdir()
    (repo / ".conduct" / "side-effects-allow.txt").write_text(
        "subject\\.py::mutable-default\n", encoding="utf-8")
    result = check(repo, "def f(xs=[]):\n    xs.append(1)\n    return xs\n")
    assert result.returncode == 1, result.stdout
    assert "mutated-argument" in result.stdout
    assert "mutable-default" not in result.stdout


def test_unreadable_allowlist_entry_is_reported(repo: Path) -> None:
    """An entry nobody can parse suppresses nothing, so it must not look like it did."""
    (repo / ".conduct").mkdir()
    (repo / ".conduct" / "side-effects-allow.txt").write_text("*[unclosed\n", encoding="utf-8")
    result = check(repo, CLEAN)
    assert result.returncode == 1, result.stdout
    assert "bad-allow-pattern" in result.stdout


# --- SARIF --------------------------------------------------------------------

def test_sarif_is_written_and_well_formed(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.sarif"
    result = run(repo, "--files", plant(repo, "subject.py", "def f(xs):\n    xs.append(1)\n"),
                 "--sarif", str(out))
    assert result.returncode == 1, result.stdout
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["version"] == "2.1.0"
    assert doc["runs"][0]["results"], "SARIF carries no results for a failing run"
    assert doc["runs"][0]["results"][0]["ruleId"] == "mutated-argument"
    assert doc["runs"][0]["results"][0]["locations"][0][
        "physicalLocation"]["region"]["startLine"] == 2
