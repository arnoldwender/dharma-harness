#!/usr/bin/env python3
"""The Dharma Harness gate: the effect a function does not declare.

    python3 gate/side_effects.py                       # the diff against origin/main
    python3 gate/side_effects.py --base HEAD~3         # against another ref
    python3 gate/side_effects.py --files a.py b.py     # exactly these files
    python3 gate/side_effects.py --sarif out.json      # machine-readable too

Exit codes are the contract shared by the conduct-harness family:

    0   no findings
    1   findings — the diff leaves a trace its signatures do not admit to
    2   the gate itself failed

The third is not decoration. A checker that returns 1 when it crashed reads as
"I found something"; one that returns 0 reads as "clean" and fails OPEN. This
gate keeps its own failure separate from its verdict.

WHAT THIS GATE IS FOR
---------------------
शौच (*śauca*) is the discipline of what you leave behind, and karma is the older
observation underneath it: every action leaves a consequence. The consequence
that bites is never the declared one. `def normalise(rows)` that quietly sorts
the caller's list in place has done its job *and* something else, and the
something else surfaces three call sites away, in a test that passes alone and
fails in a suite.

`scripts/check.py` already verifies that this repo keeps the promises it prints.
It says nothing about the promises a *function signature* makes. That is this
file's job, and it is done with `ast`, not with grep: `xs.append(1)` is a defect
when `xs` is a parameter, ordinary work when `xs` is a local, and the right thing
when `xs` is `self`. A regex cannot tell those apart. A parse can.

WHAT IT DELIBERATELY DOES NOT CATCH
-----------------------------------
Named here rather than left for a reader to discover, because a gate that hides
its blind spots is worse than one that has none:

  * `obj.attr = value` on a received parameter. Real effect, but the configure
    -an-object idiom is common enough that flagging it costs more than it earns.
  * `xs += [1]` on a parameter. In-place for a list, a rebinding for an int, and
    the AST cannot say which. Treated as a rebinding, which is the quiet choice.
  * A closure mutating the enclosing function's parameter. Each function is
    analysed against its own parameters only.
  * Any language but Python. Files with another suffix are counted and skipped,
    never reported clean by omission.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The root is overridable so the tests can point the gate at a scratch repo. A
# checker that can only ever run on itself cannot be shown to work: the only way
# to prove a check has teeth is to hand it a repo with the defect planted and
# watch it go red.
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).resolve().parent.parent)
ALLOWLIST = ROOT / ".conduct" / "side-effects-allow.txt"

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

# Methods that change the receiver rather than returning a new value. Calling one
# of these on a parameter moves an object the caller still holds.
MUTATING_METHODS = frozenset({
    "append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse",
    "update", "setdefault", "popitem", "add", "discard",
    "difference_update", "intersection_update", "symmetric_difference_update",
})

# Defaults evaluated once, at definition time, and then shared by every call.
MUTABLE_FACTORIES = frozenset({
    "list", "dict", "set", "bytearray", "defaultdict", "OrderedDict", "Counter", "deque",
})

# A function that reaches these has left the process.
EFFECT_MODULES = frozenset({
    "requests", "httpx", "urllib", "urllib2", "http", "subprocess", "socket",
    "shutil", "smtplib", "ftplib", "boto3", "psycopg2", "psycopg", "sqlite3",
    "pymysql", "MySQLdb", "sqlalchemy", "redis", "pymongo",
})
OS_MUTATORS = frozenset({
    "makedirs", "mkdir", "remove", "removedirs", "rename", "renames", "replace",
    "rmdir", "unlink", "system", "chdir", "putenv", "unsetenv", "truncate",
    "symlink", "link", "chmod", "chown", "utime", "popen", "execv", "fork", "kill",
})
EFFECT_FUNCS = frozenset({
    "load_dotenv", "create_engine", "connect", "urlopen", "check_call", "check_output",
})
WRITE_METHODS = frozenset({"write", "writelines", "write_text", "write_bytes",
                           "mkdir", "unlink", "touch", "rmtree"})
BUILTIN_EFFECTS = frozenset({"open", "print", "input"})

# Decorators that assert the function is a value, not an act. `cache` / `lru_cache`
# are the loudest: memoising a function that writes a file means the write happens
# on the first call and silently never again.
PURITY_DECORATORS = frozenset({"pure", "cache", "lru_cache", "cached", "memoize"})
PURITY_DOCSTRING = re.compile(r"\bpure\b|\bno side[- ]effects?\b|\bside[- ]effect[- ]free\b",
                              re.IGNORECASE)


class GateFailure(Exception):
    """The gate could not do its job. Never a verdict — always exit 2."""


@dataclass
class Finding:
    """One undeclared effect, with the place a reader can go and look at it."""
    check: str
    message: str
    path: str = ""
    line: int = 0


@dataclass
class ModuleInfo:
    """What the module level binds, so a function's writes can be placed."""
    imported: set[str] = field(default_factory=set)
    module_names: set[str] = field(default_factory=set)


# --- AST helpers -------------------------------------------------------------

def _targets(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return [node.target]
    return []


def _bound_names(target: ast.AST) -> list[str]:
    """Names this target REBINDS. A subscript or attribute target mutates an
    object instead of rebinding a name, so it deliberately yields nothing."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for e in target.elts for n in _bound_names(e)]
    return []


def _base_name(node: ast.AST) -> str | None:
    """The leftmost Name of an attribute/subscript chain: `a.b[0].c` -> `a`."""
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _dotted(node: ast.AST) -> str | None:
    """`os.path.join` for an Attribute chain rooted in a Name, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _decorator_names(fn: ast.AST) -> set[str]:
    """The last dotted component of each decorator: `@functools.cache` -> `cache`."""
    out: set[str] = set()
    for dec in getattr(fn, "decorator_list", []):
        expr = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(expr, ast.Attribute):
            out.add(expr.attr)
        elif isinstance(expr, ast.Name):
            out.add(expr.id)
    return out


def body_of(fn: ast.AST) -> Iterator[ast.AST]:
    """Every node in this function's OWN scope.

    A nested def is yielded — it binds a name here — but never entered: it owns
    its own parameters and is visited separately by `iter_functions`. Without
    that boundary a nested `def g(xs)` would be checked against the outer
    function's parameter names and invent findings out of shadowing.
    """
    def walk(node: ast.AST) -> Iterator[ast.AST]:
        for child in ast.iter_child_nodes(node):
            yield child
            if not isinstance(child, SCOPES):
                yield from walk(child)

    for stmt in fn.body:                                   # type: ignore[attr-defined]
        yield stmt
        if not isinstance(stmt, SCOPES):
            yield from walk(stmt)


def iter_functions(tree: ast.Module) -> Iterator[tuple[ast.AST, bool]]:
    """(function, is_method) for every def at any nesting depth."""
    methods = {id(s) for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
               for s in n.body if isinstance(s, FUNCS)}
    for node in ast.walk(tree):
        if isinstance(node, FUNCS):
            yield node, id(node) in methods


def _all_args(fn: ast.AST) -> list[ast.arg]:
    a = fn.args                                            # type: ignore[attr-defined]
    out = list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
    if a.vararg:
        out.append(a.vararg)
    if a.kwarg:
        out.append(a.kwarg)
    return out


def parameters(fn: ast.AST, is_method: bool) -> set[str]:
    """The names whose objects belong to the CALLER.

    `self` and `cls` are excluded: mutating the receiver is what a method is for.
    On a method the first positional parameter is the receiver whatever it is
    called, unless the function is a staticmethod and has none.
    """
    a = fn.args                                            # type: ignore[attr-defined]
    positional = list(a.posonlyargs) + list(a.args)
    drop = {"self", "cls"}
    if is_method and positional and "staticmethod" not in _decorator_names(fn):
        drop.add(positional[0].arg)
    return {p.arg for p in _all_args(fn)} - drop


def local_names(fn: ast.AST) -> set[str]:
    """Every name bound inside this function, so module state can be told from a
    local that merely shares its name."""
    names = {p.arg for p in _all_args(fn)}
    for node in body_of(fn):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for t in _targets(node):
                names.update(_bound_names(t))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            names.update(_bound_names(node.target))
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            names.update(_bound_names(node.optional_vars))
        elif isinstance(node, ast.NamedExpr):
            names.update(_bound_names(node.target))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, SCOPES) and not isinstance(node, ast.Lambda):
            names.add(node.name)                           # type: ignore[attr-defined]
    for node in body_of(fn):
        if isinstance(node, ast.Global):
            names -= set(node.names)                       # a global is not a local
    return names


def rebound_at(fn: ast.AST, params: set[str]) -> dict[str, int]:
    """First line on which each parameter name is rebound.

    `def f(xs): xs = list(xs); xs.append(1)` builds a copy and mutates the copy —
    the caller's object never moves, and flagging it would be a false positive.
    Recording the LINE rather than the bare fact keeps the reverse order —
    mutate first, rebind after — catchable, which is the case that actually ships.
    """
    first: dict[str, int] = {}

    def note(name: str, lineno: int) -> None:
        if name in params and name not in first:
            first[name] = lineno

    for node in body_of(fn):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for t in _targets(node):
                for name in _bound_names(t):
                    note(name, node.lineno)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for name in _bound_names(node.target):
                note(name, node.lineno)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            for name in _bound_names(node.optional_vars):
                note(name, node.optional_vars.lineno)
        elif isinstance(node, ast.NamedExpr):
            for name in _bound_names(node.target):
                note(name, node.lineno)
    return first


def _top_level_statements(tree: ast.Module) -> Iterator[ast.stmt]:
    """Statements that run at IMPORT time.

    Recurses into top-level `if` / `try` / `with` / loops, because an effect
    wrapped in a `try:` still fires on import. Skips defs and classes — their
    bodies run when called — and skips the `if __name__ == "__main__":` guard,
    which is precisely the declared place for effects.
    """
    def walk(stmts: list[ast.stmt]) -> Iterator[ast.stmt]:
        for stmt in stmts:
            if isinstance(stmt, (*FUNCS, ast.ClassDef)):
                continue
            if isinstance(stmt, ast.If) and _is_main_guard(stmt):
                continue
            yield stmt
            for attr in ("body", "orelse", "finalbody"):
                nested = getattr(stmt, attr, None)
                if isinstance(nested, list):
                    yield from walk(nested)
            for handler in getattr(stmt, "handlers", []):
                yield from walk(handler.body)

    yield from walk(tree.body)


def _is_main_guard(node: ast.If) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "__name__" for n in ast.walk(node.test))


def _effectful_call(call: ast.Call) -> str | None:
    """The reason this call reaches outside the process, or None."""
    if isinstance(call.func, ast.Name):
        if call.func.id in EFFECT_FUNCS or call.func.id == "open":
            return f"{call.func.id}()"
        return None
    if not isinstance(call.func, ast.Attribute):
        return None
    dotted = _dotted(call.func) or call.func.attr
    root = dotted.split(".")[0]
    if root in EFFECT_MODULES:
        return f"{dotted}()"
    if root == "os" and call.func.attr in OS_MUTATORS:
        return f"{dotted}()"
    if call.func.attr in EFFECT_FUNCS:
        return f"{dotted}()"
    return None


def scan_module(tree: ast.Module) -> ModuleInfo:
    info = ModuleInfo()
    for node in _top_level_statements(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                info.imported.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    info.imported.add(alias.asname or alias.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for t in _targets(node):
                info.module_names.update(_bound_names(t))
    for node in tree.body:
        if isinstance(node, (*FUNCS, ast.ClassDef)):
            info.module_names.add(node.name)
    return info


# --- the six checks ----------------------------------------------------------

def check_mutated_arguments(tree: ast.Module, rel: str, info: ModuleInfo) -> list[Finding]:
    """CHECK 1 — a function that moves an object its caller still holds.

    `def f(xs): xs.append(1)` has two results: the one it returns and the one it
    leaves in the caller's list. Only the first is in the signature.
    """
    out: list[Finding] = []
    for fn, is_method in iter_functions(tree):
        params = parameters(fn, is_method)
        if not params:
            continue
        rebound = rebound_at(fn, params)
        name = fn.name                                     # type: ignore[attr-defined]

        def live(param: str, lineno: int) -> bool:
            at = rebound.get(param)
            return at is None or lineno <= at

        for node in body_of(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in params
                    and node.func.attr in MUTATING_METHODS
                    and live(node.func.value.id, node.lineno)):
                out.append(Finding(
                    "mutated-argument",
                    f"{name}(): `{node.func.value.id}.{node.func.attr}(...)` mutates a "
                    f"received argument — the caller keeps that object and its "
                    f"signature promises nothing of the sort; return a new value instead",
                    rel, node.lineno))
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                for target in _targets(node):
                    base = target.value if isinstance(target, ast.Subscript) else None
                    if (isinstance(base, ast.Name) and base.id in params
                            and live(base.id, node.lineno)):
                        out.append(Finding(
                            "mutated-argument",
                            f"{name}(): assigns into `{base.id}[...]`, which writes "
                            f"through to the caller's object",
                            rel, node.lineno))
            elif isinstance(node, ast.Delete):
                for target in node.targets:
                    base = target.value if isinstance(target, ast.Subscript) else None
                    if (isinstance(base, ast.Name) and base.id in params
                            and live(base.id, node.lineno)):
                        out.append(Finding(
                            "mutated-argument",
                            f"{name}(): `del {base.id}[...]` removes an item from the "
                            f"caller's object",
                            rel, node.lineno))
    return out


def check_mutable_defaults(tree: ast.Module, rel: str, info: ModuleInfo) -> list[Finding]:
    """CHECK 2 — the default that is evaluated once and shared forever.

    `def f(xs=[])` binds ONE list at definition time. Every call that does not
    pass `xs` gets the same object, carrying whatever the previous call left in
    it. The effect is invisible in the signature and permanent in the process.
    """
    out: list[Finding] = []
    for fn, _ in iter_functions(tree):
        a = fn.args                                        # type: ignore[attr-defined]
        positional = list(a.posonlyargs) + list(a.args)
        pairs: list[tuple[ast.arg, ast.expr | None]] = []
        if a.defaults:
            pairs += list(zip(positional[len(positional) - len(a.defaults):], a.defaults))
        pairs += list(zip(a.kwonlyargs, a.kw_defaults))
        for arg, default in pairs:
            if default is None:
                continue
            shape = _mutable_default_shape(default)
            if shape:
                out.append(Finding(
                    "mutable-default",
                    f"{fn.name}(): default `{arg.arg}={shape}` is built once at "  # type: ignore[attr-defined]
                    f"definition time and shared by every call that omits it — use "
                    f"`None` and build it inside",
                    rel, default.lineno))
    return out


def _mutable_default_shape(node: ast.expr) -> str | None:
    if isinstance(node, ast.List):
        return "[]"
    if isinstance(node, ast.Dict):
        return "{}"
    if isinstance(node, ast.Set):
        return "{...}"
    if isinstance(node, ast.Call):
        called = None
        if isinstance(node.func, ast.Name):
            called = node.func.id
        elif isinstance(node.func, ast.Attribute):
            called = node.func.attr
        if called in MUTABLE_FACTORIES:
            return f"{called}()"
    return None


def check_module_state_writes(tree: ast.Module, rel: str, info: ModuleInfo) -> list[Finding]:
    """CHECK 3 — a function that writes to state outliving the call.

    Three shapes, all invisible from the call site: rebinding a name declared
    `global`, writing to an attribute or key of a module-level object, and
    calling a mutator on a module-level container.
    """
    out: list[Finding] = []
    for fn, _ in iter_functions(tree):
        declared_global = {n for node in body_of(fn)
                           if isinstance(node, ast.Global) for n in node.names}
        locals_ = local_names(fn)
        name = fn.name                                     # type: ignore[attr-defined]

        def owned(candidate: str | None) -> bool:
            """True when the name resolves to module state rather than a local."""
            if candidate is None:
                return False
            if candidate in declared_global:
                return True
            return candidate in info.module_names and candidate not in locals_

        for node in body_of(fn):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                for target in _targets(node):
                    for bound in _bound_names(target):
                        if bound in declared_global:
                            out.append(Finding(
                                "module-state-write",
                                f"{name}(): `{bound}` is declared `global` and "
                                f"reassigned — the next caller inherits this write",
                                rel, node.lineno))
                    if isinstance(target, (ast.Attribute, ast.Subscript)):
                        base = _base_name(target)
                        if base not in info.imported and owned(base):
                            out.append(Finding(
                                "module-state-write",
                                f"{name}(): writes into module-level `{base}` — state "
                                f"that outlives the call and no signature mentions",
                                rel, node.lineno))
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.attr in MUTATING_METHODS):
                base = node.func.value.id
                if base not in info.imported and owned(base):
                    out.append(Finding(
                        "module-state-write",
                        f"{name}(): `{base}.{node.func.attr}(...)` mutates a "
                        f"module-level object — the effect accumulates across calls",
                        rel, node.lineno))
    return out


def check_impure_declared_pure(tree: ast.Module, rel: str, info: ModuleInfo) -> list[Finding]:
    """CHECK 4 — the function that says it is a value and then acts.

    A `@cache`d function that writes a file writes it on the first call and never
    again; a docstring promising no side effects is read by the next author as a
    licence to call it anywhere. Both make the declaration the dangerous part.
    """
    out: list[Finding] = []
    for fn, _ in iter_functions(tree):
        marker = _purity_marker(fn)
        if not marker:
            continue
        name = fn.name                                     # type: ignore[attr-defined]
        for node in body_of(fn):
            reason = None
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in BUILTIN_EFFECTS:
                    reason = f"{node.func.id}()"
                elif isinstance(node.func, ast.Attribute):
                    reason = _effectful_call(node)
                    if reason is None and node.func.attr in WRITE_METHODS:
                        reason = f".{node.func.attr}()"
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                for target in _targets(node):
                    if isinstance(target, ast.Subscript) and _is_environ(target.value):
                        reason = "os.environ[...] ="
            if reason:
                out.append(Finding(
                    "impure-declared-pure",
                    f"{name}() is declared pure ({marker}) and calls `{reason}` — "
                    f"either the declaration is wrong or the call is",
                    rel, node.lineno))
    return out


def _purity_marker(fn: ast.AST) -> str | None:
    decorators = _decorator_names(fn) & PURITY_DECORATORS
    if decorators:
        return "@" + sorted(decorators)[0]
    doc = ast.get_docstring(fn)                            # type: ignore[arg-type]
    if doc and PURITY_DOCSTRING.search(doc):
        return "docstring"
    return None


def _is_environ(node: ast.AST) -> bool:
    return (_dotted(node) == "os.environ"
            or (isinstance(node, ast.Name) and node.id == "environ"))


def check_import_time_effects(tree: ast.Module, rel: str, info: ModuleInfo) -> list[Finding]:
    """CHECK 5 — the module that acts merely because someone imported it.

    An import is supposed to be a definition, and a reader treats it as one. A
    network call, a `makedirs`, a `load_dotenv()` or a database connection at
    module level turns `import x` into an action with no call site to blame.
    """
    out: list[Finding] = []
    for node in _top_level_statements(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            called = _dotted(node.value.func) or getattr(node.value.func, "attr", "a call")
            out.append(Finding(
                "import-time-effect",
                f"`{called}(...)` runs at import time and its result is discarded — "
                f"an import should define, not act; move it behind a function or the "
                f"`if __name__ == \"__main__\":` guard",
                rel, node.lineno))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
            reason = _effectful_call(node.value)
            if reason:
                out.append(Finding(
                    "import-time-effect",
                    f"`{reason}` runs at import time — importing this module opens a "
                    f"connection, touches the filesystem or reaches the network",
                    rel, node.lineno))
    return out


def check_monkey_patching(tree: ast.Module, rel: str, info: ModuleInfo) -> list[Finding]:
    """CHECK 6 — rewriting somebody else's module.

    Assigning to an attribute of an imported name changes that object for every
    other importer in the process. The effect is global, permanent for the run,
    and attributed to the module that got patched rather than the one that
    patched it.
    """
    out: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for target in _targets(node):
                if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                        and target.value.id in info.imported):
                    out.append(Finding(
                        "monkey-patch",
                        f"assigns `{target.value.id}.{target.attr}` — patching an "
                        f"imported module changes it for every other importer in the "
                        f"process, and the traceback will name them, not this file",
                        rel, node.lineno))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "setattr" and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in info.imported):
            out.append(Finding(
                "monkey-patch",
                f"`setattr({node.args[0].id}, ...)` patches an imported module at "
                f"runtime — the same effect as an assignment, one indirection deeper",
                rel, node.lineno))
    return out


# --- driver ------------------------------------------------------------------

def analyse_tree(tree: ast.Module, rel: str) -> list[Finding]:
    info = scan_module(tree)
    findings: list[Finding] = []
    findings.extend(check_mutated_arguments(tree, rel, info))
    findings.extend(check_mutable_defaults(tree, rel, info))
    findings.extend(check_module_state_writes(tree, rel, info))
    findings.extend(check_impure_declared_pure(tree, rel, info))
    findings.extend(check_import_time_effects(tree, rel, info))
    findings.extend(check_monkey_patching(tree, rel, info))
    return findings


def relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def analyse(paths: list[Path]) -> tuple[list[Finding], int, int]:
    """Returns (findings, python files analysed, other files skipped)."""
    findings: list[Finding] = []
    analysed = skipped = 0
    for path in paths:
        rel = relative(path)
        if path.suffix != ".py":
            skipped += 1
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            findings.append(Finding(
                "missing-file",
                f"{rel}: named for analysis but not on disk — a file the gate could "
                f"not read is never reported clean", rel))
            continue
        except OSError as exc:
            findings.append(Finding("unreadable", f"{rel}: {exc}", rel))
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            # Reported, not raised: one unparseable file must not stop the gate
            # from judging the rest of the diff, and must not read as clean either.
            findings.append(Finding(
                "unparseable",
                f"{rel}: will not parse ({exc.msg}) — nothing can be cleared in a "
                f"file the gate could not read", rel, exc.lineno or 1))
            continue
        analysed += 1
        findings.extend(analyse_tree(tree, rel))
    return findings, analysed, skipped


def git(*args: str) -> str:
    result = subprocess.run(["git", "-C", str(ROOT), *args],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise GateFailure(f"git {' '.join(args)}: {result.stderr.strip()[:200]}")
    return result.stdout


def changed_files(base: str) -> list[Path]:
    """Files touched since `base`, including work not yet committed.

    Compared against the merge base rather than the tip, so a branch is judged on
    what IT changed and not on what main moved on to. Untracked files are added
    on purpose: a brand-new file is exactly where a new side effect arrives, and
    `git diff` alone would let it through.
    """
    merge_base = git("merge-base", base, "HEAD").strip()
    names = git("diff", "--name-only", "--diff-filter=ACMR", merge_base).splitlines()
    names += git("ls-files", "--others", "--exclude-standard").splitlines()
    seen: dict[str, None] = {}
    for name in names:
        if name.strip():
            seen.setdefault(name.strip(), None)
    return [ROOT / name for name in seen]


def load_allowlist() -> tuple[list[re.Pattern[str]], list[Finding]]:
    """Patterns from .conduct/side-effects-allow.txt, plus findings about itself.

    A whole line starting with `#` is a comment; `#` does NOT start a trailing
    comment, because a regex may legitimately contain one.
    """
    patterns: list[re.Pattern[str]] = []
    problems: list[Finding] = []
    if not ALLOWLIST.exists():
        return patterns, problems
    rel = relative(ALLOWLIST)
    for number, raw in enumerate(ALLOWLIST.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line))
        except re.error as exc:
            problems.append(Finding(
                "bad-allow-pattern",
                f"line {number} is neither a path nor a valid regex ({exc}) — an "
                f"allowlist entry nobody can read suppresses nothing on purpose",
                rel, number))
    return patterns, problems


def apply_allowlist(findings: list[Finding],
                    patterns: list[re.Pattern[str]]) -> tuple[list[Finding], int]:
    kept: list[Finding] = []
    suppressed = 0
    for finding in findings:
        key = f"{finding.path}::{finding.check}"
        if finding.check != "bad-allow-pattern" and any(
                p.search(finding.path) or p.search(key) for p in patterns):
            suppressed += 1
        else:
            kept.append(finding)
    return kept, suppressed


def to_sarif(findings: list[Finding]) -> dict[str, Any]:
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "dharma-harness-side-effects",
                "informationUri": "https://github.com/arnoldwender/dharma-harness",
                "rules": [{"id": r} for r in sorted({f.check for f in findings})],
            }},
            "results": [{
                "ruleId": f.check,
                "level": "error",
                "message": {"text": f.message},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": f.path or "."},
                    "region": {"startLine": max(f.line, 1)},
                }}],
            } for f in findings],
        }],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Find the effect a function does not declare.")
    parser.add_argument("--base", default="origin/main",
                        help="ref to diff against (default: origin/main)")
    parser.add_argument("--files", nargs="+", metavar="PATH",
                        help="analyse exactly these files instead of a diff")
    parser.add_argument("--sarif", metavar="PATH", help="write SARIF 2.1.0 to PATH")
    args = parser.parse_args(argv)

    try:
        if args.files:
            paths = [Path(f) if Path(f).exists() or Path(f).is_absolute() else ROOT / f
                     for f in args.files]
        else:
            paths = changed_files(args.base)
        findings, analysed, skipped = analyse(paths)
        patterns, problems = load_allowlist()
        findings, suppressed = apply_allowlist(findings, patterns)
        findings = problems + findings
    except GateFailure as exc:
        print(f"gate failure: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:                               # noqa: BLE001
        # Exit 2, never 1 and never 0: the gate broke, it did not judge.
        print(f"gate failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.sarif:
        Path(args.sarif).write_text(json.dumps(to_sarif(findings), indent=2),
                                    encoding="utf-8")

    print(f"side-effects: {analysed} python file(s) analysed, "
          f"{skipped} other file(s) skipped")
    if suppressed:
        # Printed even when the run is clean. An allowlist that quietly grows
        # until it covers everything is the cheap rescue the codex names.
        print(f"  {suppressed} finding(s) suppressed by {relative(ALLOWLIST)}")
    for finding in findings:
        where = f"{finding.path}:{finding.line}" if finding.line else (finding.path or ".")
        print(f"  FAIL [{finding.check}] {where}: {finding.message}")
    if findings:
        print(f"\n{len(findings)} finding(s)")
        return 1
    print("  every analysed function leaves only what its signature admits to")
    return 0


if __name__ == "__main__":
    sys.exit(main())
