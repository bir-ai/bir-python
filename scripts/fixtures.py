#!/usr/bin/env python3
"""Drift guard for the cross-repo shared contract fixtures.

The ``bir`` product repo (FastAPI server + Next.js dashboard) and the
``bir-python`` SDK (``bir-sdk``) each keep their own copy of the wire-contract
fixtures under ``tests/fixtures/``. They are meant to be byte-for-byte
identical; nothing else enforced that, so they drifted. This script is the
enforcement.

Modes
-----
check
    Verify that this repo's ``tests/fixtures/<f>`` match the checksums recorded
    in the committed manifest (``tests/fixtures/CHECKSUMS.sha256``). Needs only
    this one repo checked out -- this is what each repo's CI runs. Exits
    non-zero on any checksum mismatch, missing fixture, or fixture file that is
    not registered as shared.

sync
    Local developer command. Copy each fixture FROM its canonical repo TO the
    other repo, then regenerate the (identical) manifest in BOTH repos. Requires
    the sibling repo checked out alongside this one; if it is not auto-detected,
    point at it with ``--sibling`` or ``$BIR_SIBLING``.

Canonical source per fixture (whose copy wins on ``sync``)
----------------------------------------------------------
* ``event-schema-v1.json``  -> bir-python (SDK): the ``1.0`` event schema the
  SDK produces and the server/dashboard consume.
* ``valid-events.jsonl``    -> bir-python (SDK): a representative trace.
* ``redaction-cases.json``  -> bir-python (SDK): secret-redaction parity cases
  for the SDK's and server's independent redactors.
* ``valid-experiment.json`` -> bir (product): the canonical ``/v1/experiments``
  upload shape the server locks.

Tradeoff
--------
``check`` verifies a repo against *its own* committed manifest and deliberately
does not check out the sibling (CI cannot assume both repos are present). So the
guarantee is "fixtures match this repo's manifest"; cross-repo identity holds
because ``sync`` writes the *same* manifest into both repos at once. Editing a
fixture without re-running ``sync`` is caught (its checksum stops matching, CI
goes red). Hand-editing *both* a fixture and its manifest entry in one repo only
is not caught by CI alone -- but that bypasses ``sync``, leaves the sibling
untouched, and surfaces as an unpaired manifest diff in review. Cloning the
sibling in CI to diff directly was rejected: it couples each repo's CI to the
other's location and breaks the "no assumption both are checked out" constraint.

Zero third-party dependencies (stdlib only): the SDK ships dependency-free and
its CI installs nothing extra, and the product server CI already has Python.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

# Fixtures shared by both repos and required to stay byte-for-byte identical.
SHARED_FIXTURES = (
    "event-schema-v1.json",
    "redaction-cases.json",
    "valid-events.jsonl",
    "valid-experiment.json",
)

# Canonical owner of each fixture -- the repo whose copy wins when re-syncing.
# "sdk" == bir-python, "product" == bir. See the module docstring for rationale.
CANONICAL = {
    "event-schema-v1.json": "sdk",
    "valid-events.jsonl": "sdk",
    "redaction-cases.json": "sdk",
    "valid-experiment.json": "product",
}

MANIFEST_NAME = "CHECKSUMS.sha256"
# Files that legitimately live in tests/fixtures/ but are not themselves shared
# fixtures, so they are excluded from the "unregistered fixture" check.
NON_FIXTURE_FILES = {MANIFEST_NAME, "README.md"}

REPO_ROOT = Path(__file__).resolve().parents[1]


def fixtures_dir(repo_root: Path) -> Path:
    return repo_root / "tests" / "fixtures"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rel(path: Path) -> str:
    """Path relative to this repo root, for friendly messages."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def repo_kind(repo_root: Path) -> "str | None":
    """Best-effort identification of a checkout as the product or SDK repo."""
    if (repo_root / "apps" / "web").is_dir() and (repo_root / "apps" / "server").is_dir():
        return "product"
    if (repo_root / "src" / "bir").is_dir():
        return "sdk"
    return None


def render_manifest(fixture_bytes: "dict[str, bytes]") -> str:
    """Render the committed manifest. Standard sha256sum(1) format with a
    comment header, so `shasum -a 256 -c` / `sha256sum -c` can verify it too."""
    header = [
        "# Shared cross-repo contract fixtures -- DO NOT EDIT BY HAND.",
        "#",
        "# Committed identically in BOTH repos (bir and bir-python). Regenerate",
        "# it (and re-sync the fixtures) from a checkout with both repos side by",
        "# side:",
        "#",
        "#     python scripts/fixtures.py sync",
        "#",
        "# CI verifies each repo's fixtures against this manifest:",
        "#",
        "#     python scripts/fixtures.py check",
        "#",
        "# Canonical source per fixture (whose copy wins on sync):",
        "#     event-schema-v1.json   -> bir-python (SDK)",
        "#     valid-events.jsonl     -> bir-python (SDK)",
        "#     redaction-cases.json   -> bir-python (SDK)",
        "#     valid-experiment.json  -> bir        (product/server)",
        "#",
        "# Fallback verification without this script:",
        "#     (cd tests/fixtures && shasum -a 256 -c CHECKSUMS.sha256)",
    ]
    body = [f"{sha256_hex(fixture_bytes[name])}  {name}" for name in sorted(fixture_bytes)]
    return "\n".join(header + body) + "\n"


def parse_manifest(text: str) -> "dict[str, str]":
    recorded: "dict[str, str]" = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)  # "<hex>  <name>" (sha256sum format)
        if len(parts) != 2:
            raise ValueError(f"malformed manifest line: {raw!r}")
        digest, name = parts[0].lower(), parts[1].lstrip("*").strip()
        recorded[name] = digest
    return recorded


def cmd_check(_args: argparse.Namespace) -> int:
    fdir = fixtures_dir(REPO_ROOT)
    manifest_path = fdir / MANIFEST_NAME
    if not manifest_path.is_file():
        print(f"error: manifest not found: {rel(manifest_path)}", file=sys.stderr)
        print("       run `python scripts/fixtures.py sync` to create it.", file=sys.stderr)
        return 1

    recorded = parse_manifest(manifest_path.read_text(encoding="utf-8"))
    expected = set(SHARED_FIXTURES)
    errors: "list[str]" = []

    # 1. The manifest must register exactly the known shared fixtures.
    for name in sorted(expected - set(recorded)):
        errors.append(f"{name}: declared shared but absent from the manifest")
    for name in sorted(set(recorded) - expected):
        errors.append(f"{name}: present in the manifest but not a known shared fixture")

    # 2. tests/fixtures/ must contain exactly the known shared fixtures, so a
    #    newly-added shared fixture cannot silently escape the guard.
    on_disk = {
        p.name
        for p in fdir.iterdir()
        if p.is_file() and p.name not in NON_FIXTURE_FILES and not p.name.startswith(".")
    }
    for name in sorted(on_disk - expected):
        errors.append(
            f"{name}: file in tests/fixtures/ is not a registered shared fixture; "
            "add it to SHARED_FIXTURES in scripts/fixtures.py and re-run sync, "
            "or move it out of tests/fixtures/"
        )

    # 3. Each known fixture must exist and match its recorded checksum.
    for name in SHARED_FIXTURES:
        path = fdir / name
        if not path.is_file():
            errors.append(f"{name}: missing from tests/fixtures/")
            continue
        if name not in recorded:
            continue  # already reported in step 1
        actual = sha256_hex(path.read_bytes())
        if actual != recorded[name]:
            errors.append(
                f"{name}: checksum mismatch\n"
                f"      manifest: {recorded[name]}\n"
                f"      on disk:  {actual}"
            )

    if errors:
        print("Fixture drift detected:\n", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print(
            "\nThe shared contract fixtures no longer match the committed manifest.\n"
            "If the change is intentional, re-sync both repos and regenerate the\n"
            "manifest with:  python scripts/fixtures.py sync",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {len(SHARED_FIXTURES)} shared fixtures match {rel(manifest_path)}")
    return 0


def resolve_sibling(this_root: Path, this_kind: str, override: "str | None") -> "Path | None":
    want = "sdk" if this_kind == "product" else "product"
    env_override = os.environ.get("BIR_SIBLING")
    explicit = override or env_override
    if explicit:
        candidates = [Path(explicit).expanduser()]
    else:
        candidates = [
            p
            for p in sorted(this_root.parent.iterdir())
            if p.is_dir() and p.resolve() != this_root.resolve()
        ]
    for cand in candidates:
        if fixtures_dir(cand).is_dir() and repo_kind(cand) == want:
            return cand.resolve()

    print(f"error: could not locate the sibling '{want}' repo.", file=sys.stderr)
    if explicit:
        print(
            f"       the path given via --sibling/$BIR_SIBLING ({explicit}) is not a "
            f"'{want}' checkout with tests/fixtures/.",
            file=sys.stderr,
        )
    else:
        print(
            "       check out both repos side by side, or pass "
            "--sibling /path/to/repo (or set $BIR_SIBLING).",
            file=sys.stderr,
        )
    return None


def cmd_sync(args: argparse.Namespace) -> int:
    this_kind = repo_kind(REPO_ROOT)
    if this_kind is None:
        print("error: cannot identify this repo as 'bir' or 'bir-python'.", file=sys.stderr)
        return 1

    sibling_root = resolve_sibling(REPO_ROOT, this_kind, args.sibling)
    if sibling_root is None:
        return 1
    sibling_kind = "sdk" if this_kind == "product" else "product"
    roots = {this_kind: REPO_ROOT, sibling_kind: sibling_root}

    # Copy each fixture from its canonical repo into the other, collecting the
    # canonical bytes so the manifest reflects the source of truth.
    fixture_bytes: "dict[str, bytes]" = {}
    changes: "list[str]" = []
    for name in SHARED_FIXTURES:
        owner = CANONICAL[name]
        other = "sdk" if owner == "product" else "product"
        src = fixtures_dir(roots[owner]) / name
        if not src.is_file():
            print(f"error: canonical source missing: {src}", file=sys.stderr)
            return 1
        data = src.read_bytes()
        fixture_bytes[name] = data
        dst = fixtures_dir(roots[other]) / name
        if not dst.exists() or dst.read_bytes() != data:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            changes.append(f"{name}: copied {owner} -> {other} ({dst})")

    # Write the identical manifest into both repos.
    manifest = render_manifest(fixture_bytes)
    for root in roots.values():
        mpath = fixtures_dir(root) / MANIFEST_NAME
        if not mpath.exists() or mpath.read_text(encoding="utf-8") != manifest:
            mpath.write_text(manifest, encoding="utf-8")
            changes.append(f"manifest written: {mpath}")

    print("Synced shared contract fixtures between:")
    print(f"  product: {roots['product']}")
    print(f"  sdk:     {roots['sdk']}")
    if changes:
        print("\nChanges:")
        for change in changes:
            print(f"  - {change}")
    else:
        print("\nAlready in sync; nothing changed.")
    print("\nReview and commit the result in BOTH repos (ideally in paired PRs).")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fixtures.py",
        description="Cross-repo shared contract fixture drift guard.",
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser(
        "check",
        help="verify this repo's fixtures against the committed manifest (used by CI)",
    )
    p_sync = sub.add_parser(
        "sync",
        help="re-sync fixtures between both repos and regenerate the manifest",
    )
    p_sync.add_argument(
        "--sibling",
        metavar="PATH",
        help="path to the sibling repo checkout (default: auto-detect, or $BIR_SIBLING)",
    )
    args = parser.parse_args(argv)
    if args.mode == "check":
        return cmd_check(args)
    if args.mode == "sync":
        return cmd_sync(args)
    parser.error(f"unknown mode: {args.mode}")  # unreachable
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
