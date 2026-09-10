#!/usr/bin/env python3
"""Prove the side-effect gate's tests actually defend it.

    python3 tests/mutation_check.py

For each check in gate/side_effects.py: delete it, run the suite, and require the
suite to go RED. A test that still passes with the mechanism removed is not
testing the mechanism — it is decoration that reports green forever, which is
exactly the failure DHRITI 3 names.

Exit 0 when every mutant was killed; 1 when any survived; 2 when this script
itself could not run (the same contract as the gate).

The file is restored from an in-memory copy in a `finally`, never with
`git checkout`: this repo may hold uncommitted work, and a checkout to undo a
mutation would take that work with it. The restore is then verified — a mutation
runner that leaves the file mutated has done more harm than the bug it hunted.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "gate" / "side_effects.py"

# (name, the exact call in analyse_tree() to neuter, what to leave behind)
#
# The call strings carry their arguments so they cannot collide with the `def`
# line, which is annotated and therefore spelled differently.
MUTANTS = [
    ("CHECK 1 mutated-argument",
     "findings.extend(check_mutated_arguments(tree, rel, info))"),
    ("CHECK 2 mutable-default",
     "findings.extend(check_mutable_defaults(tree, rel, info))"),
    ("CHECK 3 module-state-write",
     "findings.extend(check_module_state_writes(tree, rel, info))"),
    ("CHECK 4 impure-declared-pure",
     "findings.extend(check_impure_declared_pure(tree, rel, info))"),
    ("CHECK 5 import-time-effect",
     "findings.extend(check_import_time_effects(tree, rel, info))"),
    ("CHECK 6 monkey-patch",
     "findings.extend(check_monkey_patching(tree, rel, info))"),
]


def run_suite() -> bool:
    """True when the suite is green."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "tests"), "-q", "-x", "--no-header"],
        capture_output=True, text=True, cwd=ROOT, check=False)
    return result.returncode == 0


def main() -> int:
    original = GATE.read_text(encoding="utf-8")

    if not run_suite():
        print("the suite is RED before any mutation — fix that first", file=sys.stderr)
        return 2

    survivors: list[str] = []
    try:
        for name, call in MUTANTS:
            if call not in original:
                print(f"  ?? {name}: call not found in the gate — the mutation list "
                      f"is stale, which means this script is measuring nothing")
                survivors.append(f"{name} (stale)")
                continue
            GATE.write_text(original.replace(call, "pass", 1), encoding="utf-8")
            if run_suite():
                print(f"  SURVIVED  {name} — removed it and the suite stayed green")
                survivors.append(name)
            else:
                print(f"  killed    {name}")
    finally:
        GATE.write_text(original, encoding="utf-8")

    if GATE.read_text(encoding="utf-8") != original:
        print("the gate file was NOT restored cleanly", file=sys.stderr)
        return 2

    if survivors:
        print(f"\n{len(survivors)} mutant(s) survived: {', '.join(survivors)}")
        return 1
    print(f"\nall {len(MUTANTS)} mutants killed; gate restored and verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
