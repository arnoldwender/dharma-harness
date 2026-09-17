#!/usr/bin/env python3
"""Run the live hook once against a planted payload, the way the runtime would.

    python3 tests/live_hook_smoke.py tests/fixtures/planted-edit.json mutated-argument
    python3 tests/live_hook_smoke.py tests/fixtures/planted-edit.json mutated-argument --print

Reads a `PreToolUse` payload from the JSON fixture, writes the files its `plant`
key names into a fresh scratch directory (that key is the smoke's own, removed
before the payload is sent — the runtime never carries it), fills `cwd` with
that directory, pipes the payload to hooks/side-effects-before-write.py exactly
as Claude Code would, and requires three things: exit 0, a
`hookSpecificOutput.additionalContext` that names the expected check, and no
`permissionDecision` — warning mode never touches the permission flow. Exit 0
when all three hold, 1 otherwise, printing what came back. With `--print` the
warning itself is the last line of stdout, so a README can be held to it.

The payload lives in a JSON fixture rather than in this file on purpose: this
repository's own gate parses every Python file and would, correctly, report the
planted effect as a finding if it were written here.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

HOOK = pathlib.Path(__file__).resolve().parent.parent / "hooks" / "side-effects-before-write.py"


def main(fixture: str, check: str, show: bool = False) -> int:
    payload = json.loads(pathlib.Path(fixture).read_text(encoding="utf-8"))
    plant = payload.pop("plant", {})
    with tempfile.TemporaryDirectory() as scratch:
        for rel, content in plant.items():
            target = pathlib.Path(scratch) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        payload["cwd"] = scratch
        env = {**os.environ, "SIDE_EFFECTS_RECEIPTS": "off"}
        env.pop("SIDE_EFFECTS_HOOK_MODE", None)
        r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                           capture_output=True, text=True, env=env, cwd=scratch,
                           check=False, timeout=60)
    out = json.loads(r.stdout) if r.stdout.strip() else {}
    context = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    if r.returncode != 0:
        print(f"exit {r.returncode}, expected 0: {r.stderr.strip()[:300]}")
        return 1
    if f"[{check}]" not in context:
        print(f"no [{check}] in the warning: {out!r}")
        return 1
    if "permissionDecision" in (out.get("hookSpecificOutput") or {}):
        print(f"warning mode touched the permission flow: {out!r}")
        return 1
    print(f"planted [{check}] was warned about and not blocked")
    if show:
        print(context)
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--print"]
    if len(args) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(args[0], args[1], "--print" in sys.argv))
