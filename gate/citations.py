#!/usr/bin/env python3
"""The conduct-harness citation gate: every attributed quotation must trace to a source.

    python3 gate/citations.py              # offline checks
    python3 gate/citations.py --online     # also resolve every source URL
    python3 gate/citations.py --sarif out.json

Exit codes are part of the contract shared by the conduct-harness family:

    0   no findings
    1   findings — the repo makes a claim it cannot back
    2   the gate itself failed

The third one is not decoration. A checker that returns 1 when it crashed reads
as "I found something"; one that returns 0 reads as "clean" and fails OPEN —
which is the exact sin every codex in this family names as never tradeable.
This gate distinguishes its own failure from its verdict.

WHAT THIS GATE IS FOR
---------------------
`scripts/check.py` already verifies that a quotation printed in the README
exists somewhere in this repo. That catches a README quoting a line the emitter
never emits. It does NOT catch the failure that actually shipped: a quotation
that exists in the pool, is beautifully formatted, and is **not real** — wrong
work, wrong year, wrong author, wrong translator, or invented outright.

Four of the ten public harnesses shipped fabricated citations. Two dated
metadata errors got past every human reader:

  * "Sir Edwin Arnold, The Song Celestial (1885)" — Arnold was knighted in 1888,
    so the honorific is three years early.
  * Newton, "Principia (1687)", Rule I — in the 1687 edition that passage is
    *Hypothesis I*. It only becomes Regula I in the second edition of 1713.

Neither is catchable by grep. Both are catchable by arithmetic against the
author's dates and the edition history, which is what CHECK 3 does.

THIS FILE IS SHARED, ON PURPOSE
-------------------------------
It is the one piece of gate logic the conduct-harness repos hold in common.
Every other gate in the family is specific to the discipline its repo
automates; provenance is not — a fabricated citation is the same defect in
every tradition, so it gets one implementation rather than N drifting ones.
Nothing here names a particular repo: `CITED_FILES` lists the document names
used across the family and silently skips the ones a given repo does not have,
and the SARIF tool name is derived from the directory. Copy it verbatim.

A repo whose codex says "invent nothing" cannot ship a citation nobody can test.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The root is overridable so the mutation tests can point the gate at a scratch
# repo. A checker that can only ever run on itself cannot be shown to work: the
# only way to prove a check has teeth is to hand it a repo with the defect
# planted and watch it go red.
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).resolve().parent.parent)
SOURCES = ROOT / "sources"

# Files whose attributed quotations must resolve. The pool document is the
# source of the rotation, so it is checked too — an unsourced line there reaches
# the user on startup. The list spans the family (PRECEPTS.md in most repos,
# PROVERBS.md in the Ubuntu edition); a name that does not exist here is skipped,
# which is what lets one file serve every repo.
CITED_FILES = ("README.md", "PRECEPTS.md", "PROVERBS.md", "codex-block.md",
               "CODEX.md", "EXAMPLE.md")

# Every field a source must carry before any quotation in it counts as sourced.
# `translator` and `edition` are deliberately NOT here: an English original has
# no translator, and demanding one would push an author toward inventing a value.
REQUIRED_FIELDS = ("work", "author", "author_born", "author_died", "year",
                   "pd_status_us", "pd_status_eu", "source_url", "quotes")


@dataclass
class Finding:
    check: str
    message: str
    path: str = ""
    line: int = 0


@dataclass
class Source:
    path: Path
    data: dict[str, Any]
    quotes: list[str] = field(default_factory=list)


def norm(s: str) -> str:
    """Normalise a quotation for comparison.

    Unicode NFKC first: a curly apostrophe and a straight one are the same
    character to a reader and different bytes to `==`. Then strip the markup a
    quotation picks up on its way into a markdown file, collapse whitespace, and
    lowercase. What survives is the sentence itself.

    Collapsing whitespace is load-bearing for verse. Arnold's Gita 2.47 sits on
    two lines in the original ("Let right deeds be / Thy motive, not the fruit
    which comes from them"); the repo prints it as one. Those are the same
    quotation and only compare equal after the collapse.
    """
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"')
    s = s.replace("—", "-").replace("–", "-")
    s = re.sub(r"[*_`>\[\]()\"']", "", s)
    return " ".join(s.split()).lower()


# --- loading -----------------------------------------------------------------

def load_sources() -> tuple[list[Source], list[Finding]]:
    """Read sources/*.yml. Falls back to a minimal parser when PyYAML is absent.

    The fallback exists so the gate runs in a bare CI container without a pip
    install. It handles exactly the shape this repo's source files use — scalars
    and a `quotes:` list — and refuses anything else rather than guessing.

    Every function here RETURNS its findings rather than appending to a list the
    caller handed in. The out-parameter version reads fine and is a genuine
    undeclared side effect: the signature promises a value and the body quietly
    rewrites the caller's object. The Dharma edition's own gate
    (`gate/side_effects.py`) flags exactly that under `mutated-argument`, and it
    flagged this file — 18 findings — when it was first added. Silencing it in
    `.conduct/side-effects-allow.txt` was available and is the cheap rescue
    DHRITI 3 refuses; the accumulator was fixed instead, so the allowlist still
    holds one entry and `mutated-argument` stays live over this file.
    """
    findings: list[Finding] = []
    if not SOURCES.is_dir():
        findings.append(Finding("sources", f"no sources/ directory at {SOURCES}"))
        return [], findings

    try:
        import yaml  # type: ignore[import-untyped]

        def parse(text: str) -> Any:
            return yaml.safe_load(text)
    except ImportError:
        parse = _parse_minimal_yaml

    out: list[Source] = []
    for p in sorted(SOURCES.glob("*.yml")):
        try:
            data = parse(p.read_text(encoding="utf-8"))
        except Exception as exc:                      # noqa: BLE001 - reported, not swallowed
            findings.append(Finding("sources", f"{p.name}: unparseable ({exc})"))
            continue
        if not isinstance(data, dict):
            findings.append(Finding("sources", f"{p.name}: top level is not a mapping"))
            continue
        quotes = data.get("quotes") or []
        if not isinstance(quotes, list):
            findings.append(Finding("sources", f"{p.name}: `quotes` is not a list"))
            quotes = []
        out.append(Source(p, data, [str(q) for q in quotes]))
    if not out:
        findings.append(Finding("sources", "sources/ holds no readable *.yml"))
    return out, findings


def _parse_minimal_yaml(text: str) -> dict[str, Any]:
    """Parse the subset of YAML these source files use. Refuses anything else."""
    data: dict[str, Any] = {}
    key: str | None = None
    for raw in text.splitlines():
        line = raw.split(" #")[0].rstrip() if not raw.strip().startswith("#") else ""
        if not line.strip():
            continue
        if line.startswith("  - ") and key:
            data.setdefault(key, []).append(_scalar(line[4:].strip()))
            continue
        if line.startswith(" "):
            raise ValueError(f"unsupported indentation: {raw!r}")
        if ":" not in line:
            raise ValueError(f"not a key: {raw!r}")
        k, _, v = line.partition(":")
        key = k.strip()
        v = v.strip()
        data[key] = [] if not v else _scalar(v)
    return data


def _scalar(v: str) -> Any:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return {"true": True, "false": False, "null": None, "~": None}.get(v.lower(), v)


def _delimited(quote: str) -> bool:
    """True when the quotation is wrapped end to end in quotes or emphasis.

    A partially italicised sentence is prose with emphasis in it, not a
    quotation, so the wrapper has to close as well as open.
    """
    return (
        (quote.startswith('"') and quote.endswith('"'))
        or (quote.startswith("“") and quote.endswith("”"))
        or (quote.startswith("*") and quote.endswith("*"))
        or (quote.startswith("_") and quote.endswith("_"))
    )


def extract_quotations(path: Path) -> list[tuple[int, str, str]]:
    """Return (line number, quotation, attribution) for attributed quotes.

    Three shapes carry an attribution in these repos, and only those three count.

    SHAPE A — a flowing blockquote, delimiter REQUIRED:

        > "…quotation…" — Author, Work (year)

    THE DELIMITER IS THE DISCRIMINANT HERE, and it was learned the hard way. The
    first version of this function treated "blockquote containing an em dash" as
    a citation, and fired on two lines of a repo's own prose:

        > The first utterance … from the empiricist canon — Bacon, Newton, Huxley.
        > Four pillars … — and every rule earns its place by carrying a way to
        > prove it broken.

    Both are prose that happens to contain an em dash. Neither is a quotation.
    A real citation in flowing text is always *delimited* — wrapped in quotation
    marks or in italics — because that is how a quotation is set off from the
    sentence around it. A gate with a 12% false-positive rate on its own README
    gets deleted in a week, and deserves to be.

    SHAPE B — a blockquote LIST ITEM, delimiter OPTIONAL:

        > - …quotation… — Author, Work (year)

    A bullet inside a blockquote is a list entry, not flowing prose, so the
    delimiter is not what tells the two apart and requiring it only creates
    blind spots. That is not a guess: measured 2026-09-10 across the four repos
    that carry this gate, EVERY `> - ` bullet containing an em dash is a
    citation and NONE is prose — 49 bullets, 49 citations, zero false positives.

    Requiring the delimiter here would have made the gate structurally blind to
    two entire pools, and blind in the quietest possible way: it would have
    reported "0 attributed quotations, clean, exit 0" over a repo whose eight
    haiku and eleven proverbs had never been checked at all. A gate that passes
    because it looked at nothing is worse than no gate, because it also issues
    a green badge. The Zen edition lists its haiku as undelimited bullets and
    the Ubuntu edition lists its proverbs the same way.

    SHAPE C — an attribution on its own line, attaching to the line above:

        > …quotation…
        > — Author (dates)

    This is the ordinary way a pull-quote is set in markdown, and all three
    non-empirical repos use it in the README. The attribution attaches to the
    immediately preceding non-empty blockquote line, which is exactly the line a
    reader's eye attaches it to. Measured: 5 occurrences across the family, 5
    genuine citations, no prose.

    A blockquote with no attribution at all is prose, not a citation, and
    demanding a source for it would make the gate fire on the repo's own
    writing. That distinction is the difference between a gate people keep and
    one they rip out.
    """
    out: list[tuple[int, str, str]] = []
    if not path.exists():
        return out
    # The last blockquote line that carried no attribution of its own: the
    # candidate a bare "— Author" line on the next row would belong to.
    pending: tuple[int, str] | None = None
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line.startswith(">"):
            pending = None                             # the blockquote ended
            continue
        body = line.lstrip("> ").strip()
        if not body:
            continue                                   # a spacer row inside one quote
        bullet = body.startswith("- ")
        if bullet:
            body = body[2:].strip()

        # SHAPE C: the whole line is an attribution for the row above it.
        if not bullet and re.match(r"^[—–]\s*\S", body):
            if pending is not None:
                lineno, quote = pending
                if len(norm(quote)) >= 18:
                    out.append((lineno, quote, body.lstrip("—– ").strip()))
            pending = None
            continue

        m = re.search(r"\s[—–]\s*(.+)$", body)
        if not m:
            # No attribution on this line. It may be the quotation half of a
            # Shape C pair, so remember it — but only the last one, so an
            # attribution binds to the line directly above and nothing further.
            pending = (n, body)
            continue

        quote = body[:m.start()].strip()
        attribution = m.group(1).strip()
        pending = None
        if len(norm(quote)) < 18:
            continue                                   # too short to be a claim
        if not bullet and not _delimited(quote):
            continue                                   # flowing prose with an em dash
        out.append((n, quote, attribution))
    return out


# --- checks ------------------------------------------------------------------

def check_quotes_resolve(sources: list[Source]) -> tuple[int, list[Finding]]:
    """CHECK 1 — every attributed quotation resolves verbatim to a source."""
    findings: list[Finding] = []
    haystack = [(s, norm(q)) for s in sources for q in s.quotes]
    checked = 0
    for name in CITED_FILES:
        path = ROOT / name
        for lineno, quote, attribution in extract_quotations(path):
            checked += 1
            needle = norm(quote).rstrip(".,;:!?…")
            if not any(needle in hay or hay in needle for _, hay in haystack):
                findings.append(Finding(
                    "unsourced-quote",
                    f'quotation attributed to "{attribution}" resolves to no file in '
                    f"sources/: {quote[:72]}",
                    name, lineno))
    return checked, findings


def check_provenance_complete(sources: list[Source]) -> list[Finding]:
    """CHECK 2 — a source with missing provenance does not make a quote sourced.

    Without this, CHECK 1 is circular: anyone can silence it by pasting the
    quotation into a source file. The provenance is what makes the claim
    auditable by someone who is not us.
    """
    findings: list[Finding] = []
    for s in sources:
        for fld in REQUIRED_FIELDS:
            if fld not in s.data or s.data[fld] in ("", None, []):
                findings.append(Finding(
                    "incomplete-provenance",
                    f"{s.path.name}: missing or empty `{fld}` — a quotation is not "
                    f"sourced until someone else can check it", f"sources/{s.path.name}"))
        if s.data.get("provenance") == "unverified" and not s.data.get("provenance_note"):
            findings.append(Finding(
                "unverified-without-note",
                f"{s.path.name}: marked `provenance: unverified` with no "
                f"`provenance_note` saying what could not be confirmed",
                f"sources/{s.path.name}"))
    return findings


def check_anachronism(sources: list[Source]) -> list[Finding]:
    """CHECK 3 — the arithmetic that catches what a reader does not.

    A work cannot be published before its author was born, and an author cannot
    write after they die. Posthumous publication is real, so a `posthumous: true`
    flag opts out of the upper bound — but it has to be declared, which is the
    point: an undeclared posthumous date is indistinguishable from a wrong one.

    Ancient authors carry negative years (Aristotle: author_born: -384), which is
    why the comparisons are plain integer arithmetic and not date parsing.
    """
    findings: list[Finding] = []
    for s in sources:
        born, died, year = (s.data.get(k) for k in ("author_born", "author_died", "year"))
        if not all(isinstance(v, int) for v in (born, died, year)):
            continue                                   # CHECK 2 already reported it
        assert isinstance(born, int) and isinstance(died, int) and isinstance(year, int)
        name = s.path.name
        if year < born:
            findings.append(Finding(
                "anachronism", f"{name}: work dated {year}, author born {born}",
                f"sources/{name}"))
        elif year > died and not s.data.get("posthumous"):
            findings.append(Finding(
                "anachronism",
                f"{name}: work dated {year}, author died {died} — if this is a "
                f"posthumous edition, declare `posthumous: true`", f"sources/{name}"))
        if died < born:
            findings.append(Finding(
                "anachronism", f"{name}: author died {died}, born {born}",
                f"sources/{name}"))
        # A translation cannot predate the translator's death by more than a
        # lifetime, and it certainly cannot postdate it. This is the check that
        # would have caught a 19th-century text credited to a 20th-century hand.
        tdied = s.data.get("translator_died")
        if isinstance(tdied, int) and isinstance(year, int) and year > tdied:
            findings.append(Finding(
                "anachronism",
                f"{name}: edition dated {year} but the translator died {tdied}",
                f"sources/{name}"))
    return findings


def check_pd_status(sources: list[Source]) -> list[Finding]:
    """CHECK 4 — public-domain status is claimed per jurisdiction, not in general.

    "Public domain" is not one fact. The US rule is publication-based (pre-1930
    is out of copyright); the EU rule is life-of-the-author-plus-70. A 1928
    translation is public domain in the US and NOT in the EU if the translator
    died after 1955. This repo is published from Germany, so an unqualified "PD"
    claim is the one that gets a repo in trouble.

    The translator carries a copyright term of their own, independent of the
    author's. Aristotle died in -322 and the Ross translation of him is still
    under EU copyright until 2042; a source file that reasons only about the
    author would call that public domain and be wrong by two millennia.
    """
    findings: list[Finding] = []
    for s in sources:
        died = s.data.get("author_died")
        eu = str(s.data.get("pd_status_eu", ""))
        if isinstance(died, int) and died > 1955 and "public domain" in eu.lower():
            if str(died) not in eu:
                findings.append(Finding(
                    "pd-claim",
                    f"{s.path.name}: claims EU public domain but the author died "
                    f"{died} — life+70 needs the arithmetic spelled out",
                    f"sources/{s.path.name}"))
        translator_died = s.data.get("translator_died")
        if isinstance(translator_died, int) and translator_died > 1955:
            if str(translator_died) not in eu:
                findings.append(Finding(
                    "pd-claim",
                    f"{s.path.name}: the TRANSLATION carries its own copyright and "
                    f"the translator died {translator_died} — EU status must account "
                    f"for the translator, not only the author",
                    f"sources/{s.path.name}"))
        # A source that names a translator but never says when they died cannot
        # have had its EU term computed at all.
        if s.data.get("translator") and "translator_died" not in s.data:
            findings.append(Finding(
                "pd-claim",
                f"{s.path.name}: names a translator but carries no "
                f"`translator_died` — the translation's own EU term is unknown",
                f"sources/{s.path.name}"))
    return findings


def check_urls_online(sources: list[Source]) -> list[Finding]:
    """CHECK 5 (--online) — every source URL still resolves.

    Kept behind a flag on purpose: a gate that needs the network fails open the
    day the network is down, and a link-rot finding is not a reason to block a
    commit that did not touch the link.
    """
    findings: list[Finding] = []
    import urllib.error
    import urllib.request

    for s in sources:
        url = str(s.data.get("source_url", ""))
        if not url.startswith("http"):
            continue
        req = urllib.request.Request(url, method="HEAD", headers={
            "User-Agent": "conduct-harness-citation-gate/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
                if resp.status >= 400:
                    findings.append(Finding(
                        "dead-source", f"{s.path.name}: {url} returned {resp.status}",
                        f"sources/{s.path.name}"))
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 405, 429):
                continue          # WAF or HEAD not allowed: not evidence of rot
            findings.append(Finding(
                "dead-source", f"{s.path.name}: {url} returned {exc.code}",
                f"sources/{s.path.name}"))
        except Exception as exc:                       # noqa: BLE001
            findings.append(Finding(
                "dead-source", f"{s.path.name}: {url} unreachable ({exc})",
                f"sources/{s.path.name}"))
    return findings


# --- output ------------------------------------------------------------------

def to_sarif(findings: list[Finding]) -> dict[str, Any]:
    repo = ROOT.name
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": f"{repo}-citations",
                "informationUri": f"https://github.com/arnoldwender/{repo}",
                "rules": [{"id": r} for r in sorted({f.check for f in findings})],
            }},
            "results": [{
                "ruleId": f.check,
                "level": "error",
                "message": {"text": f.message},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": f.path or "sources/"},
                    "region": {"startLine": max(f.line, 1)},
                }}],
            } for f in findings],
        }],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--online", action="store_true", help="also resolve source URLs")
    ap.add_argument("--sarif", metavar="PATH", help="write SARIF 2.1.0 to PATH")
    args = ap.parse_args(argv)

    findings: list[Finding] = []
    try:
        sources, findings = load_sources()
        checked, quote_findings = check_quotes_resolve(sources)
        findings += quote_findings
        findings += check_provenance_complete(sources)
        findings += check_anachronism(sources)
        findings += check_pd_status(sources)
        if args.online:
            findings += check_urls_online(sources)
    except Exception as exc:                           # noqa: BLE001
        # Exit 2, never 1 and never 0: the gate broke, it did not judge.
        print(f"gate failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.sarif:
        Path(args.sarif).write_text(json.dumps(to_sarif(findings), indent=2), encoding="utf-8")

    unverified = sum(1 for s in sources if s.data.get("provenance") == "unverified")
    print(f"citations: {len(sources)} source file(s), {checked} attributed quotation(s)")
    # Printed on every run, clean or not. An unverified source is not a failure —
    # marking one is the honest outcome the codex asks for — but a count that only
    # appeared on red runs would let the pile grow unwatched.
    if unverified:
        print(f"  {unverified} source(s) marked `provenance: unverified`, each with a note")
    for f in findings:
        where = f"{f.path}:{f.line}" if f.line else (f.path or "sources/")
        print(f"  FAIL [{f.check}] {where}: {f.message}")
    if findings:
        print(f"\n{len(findings)} finding(s)")
        return 1
    print("  every attributed quotation traces to a source with checkable provenance")
    return 0


if __name__ == "__main__":
    sys.exit(main())
