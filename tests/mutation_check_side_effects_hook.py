#!/usr/bin/env python3
"""Prove the live hook's tests actually defend it.

    python3 tests/mutation_check_side_effects_hook.py

For each mechanism in hooks/side-effects-before-write.py: neuter it in a
scratch COPY, run the hook's suite against the copy, and require the suite to
go RED. The real hook is never rewritten; its bytes are compared before and
after anyway. The gate's own six checks are defended by tests/mutation_check.py;
this runner covers only what the hook adds on top of the gate — which tools it
judges, how it simulates the change, that only the NEW findings are reported,
and the scope and allowlist it must honour.

Exit 0 when every mutant was killed; 1 when any survived; 2 when this script
itself could not run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "side-effects-before-write.py"
SUITE = ROOT / "tests" / "test_side_effects_hook.py"

# (name, exact text to replace, replacement). Each `old` is unique in the file.
MUTANTS = [
    ("TOOLS every tool is judged, not only the three",
     'TOOLS = {"Edit", "Write", "MultiEdit"}\n# mutation-anchor: TOOLS',
     'TOOLS = {"Edit", "Write", "MultiEdit", "Read", "Grep", "Bash", "NotebookEdit"}\n# mutation-anchor: TOOLS'),
    ("SIMULATE the edit is never simulated; new_string is judged on its own",
     '        hits = _occurrences(text, old, bool(e.get("replace_all"))) if old else []\n'
     "        # mutation-anchor: SIMULATE",
     "        hits = []\n        # mutation-anchor: SIMULATE"),
    ("REPLACE_ALL only the first occurrence is replaced",
     '        hits = _occurrences(text, old, bool(e.get("replace_all"))) if old else []\n'
     "        # mutation-anchor: SIMULATE",
     "        hits = _occurrences(text, old, False) if old else []\n        # mutation-anchor: SIMULATE"),
    ("MULTIEDIT only the first edit is applied",
     "    for e in edits:\n        # mutation-anchor: MULTIEDIT",
     "    for e in edits[:1]:\n        # mutation-anchor: MULTIEDIT"),
    ("DELTA every finding in the result is reported, pre-existing ones included",
     "        elif carried[key] > 0:\n            carried[key] -= 1\n            # mutation-anchor: DELTA",
     "        elif False:\n            carried[key] -= 1\n            # mutation-anchor: DELTA"),
    ("UNPARSEABLE a file that did not parse before the edit is subtracted like an effect",
     "        if f.check in NOT_AN_EFFECT:\n            # mutation-anchor: UNPARSEABLE",
     "        if False:\n            # mutation-anchor: UNPARSEABLE"),
    ("SCOPE a file the gate does not read is judged as Python",
     "    if pathlib.Path(target).suffix not in ro.SCANNED_SUFFIXES:\n        # mutation-anchor: SCOPE",
     "    if False:\n        # mutation-anchor: SCOPE"),
    ("ALLOWLIST the working directory's allowlist is ignored",
     "    kept, suppressed = ro.apply_allowlist(fresh, patterns)\n    # mutation-anchor: ALLOWLIST",
     "    kept, suppressed = list(fresh), 0\n    # mutation-anchor: ALLOWLIST"),
    ("WARNING a finding produces no text",
     '    if not findings:\n        receipt(verdict="ok", judged=judged, exempted=exempted, ms=ms, **common)\n        return 0',
     '    if True:\n        receipt(verdict="ok", judged=judged, exempted=exempted, ms=ms, **common)\n        return 0'),
]


def run_suite(hook: Path) -> bool:
    # The mutant lives in a scratch directory with no gate/ beside it: it must still
    # find the REAL gate, or every mutant dies of fail-open and this measures nothing.
    env = {**os.environ, "SIDE_EFFECTS_HOOK_UNDER_TEST": str(hook),
           "SIDE_EFFECTS_GATE": str(ROOT / "gate" / "side_effects.py")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(SUITE), "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=ROOT, env=env, check=False)
    return result.returncode == 0


def main() -> int:
    original = HOOK.read_text(encoding="utf-8")
    if not run_suite(HOOK):
        print("the suite is RED before any mutation — fix that first", file=sys.stderr)
        return 2
    survivors: list[str] = []
    with tempfile.TemporaryDirectory() as scratch:
        mutant = Path(scratch) / "side-effects-before-write.py"
        for name, old, new in MUTANTS:
            if original.count(old) != 1:
                print(f"  ?? {name}: anchor appears {original.count(old)} times — the "
                      f"mutation list is stale, so this script is measuring nothing")
                survivors.append(f"{name} (stale)")
                continue
            mutant.write_text(original.replace(old, new, 1), encoding="utf-8")
            if run_suite(mutant):
                print(f"  SURVIVED  {name} — removed it and the suite stayed green")
                survivors.append(name)
            else:
                print(f"  killed    {name}")
    if HOOK.read_text(encoding="utf-8") != original:
        print("the real hook file changed during the run — it must never be touched",
              file=sys.stderr)
        return 2
    if survivors:
        print(f"\n{len(survivors)} mutant(s) survived: {', '.join(survivors)}")
        return 1
    print(f"\nall {len(MUTANTS)} mutants killed; the real hook was never rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
