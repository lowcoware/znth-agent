#!/usr/bin/env python3
"""Verify that a fork's modules import FROM THE FORK, not from an installed twin.

Why this exists: when any other copy of the same package sits on sys.path (an
editable install, a site-packages install, a pristine clone), `import <mod>`
succeeds even for modules that were deleted from the fork -- the import silently
resolves into the other tree, so the check proves nothing about the fork.

What it does:
  1. puts the fork root FIRST on sys.path;
  2. drops other sys.path entries that look like a second copy of the same
     top-level package (any entry holding a directory named after the requested
     modules' top-level packages), except site-packages/dist-packages -- those
     are never dropped, because the dependency tree usually lives there;
  3. imports every requested module and asserts its __file__ is under --root.

A module that cannot be imported, or that resolves into a site-packages twin,
FAILS loudly: that is the finding, not a reason to relax the check.

Usage:
  python strict_import_check.py --root /path/to/fork hermes_cli.main model_tools tools.browser_tool
  python strict_import_check.py --root /path/to/fork --drop /usr/local/lib/hermes-agent hermes_cli.main

Exit status 0 only when every module imported and every __file__ is under --root.
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys


def real(path: str) -> str:
    return os.path.realpath(path or ".").rstrip("/")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", required=True, help="fork root; every import must land inside it")
    parser.add_argument("--drop", action="append", default=[],
                        help="extra source root to remove from sys.path (repeatable)")
    parser.add_argument("modules", nargs="+", help="dotted module names to import")
    args = parser.parse_args()

    root = real(args.root)
    if not os.path.isdir(root):
        print(f"FAIL  not a directory: {root}")
        return 2

    tops = sorted({m.split(".")[0] for m in args.modules})
    explicit = {real(d) for d in args.drop}

    kept: list[str] = []
    dropped: list[str] = []
    for entry in sys.path:
        here = real(entry)
        if here == root:
            continue  # the fork is re-added at position 0 below
        is_site = "site-packages" in here or "dist-packages" in here
        twin = any(
            here and not here.startswith(root + os.sep) and os.path.isdir(os.path.join(here, top))
            for top in tops
        )
        if here in explicit or (twin and not is_site):
            dropped.append(here)
            continue
        kept.append(entry)
    sys.path[:] = [root] + kept

    print(f"fork root:              {root}")
    print(f"dropped from sys.path:  {', '.join(dropped) if dropped else '(no second copy of these packages found)'}")
    print(f"kept (still searching):  {len(kept)} path entries")
    print()

    failed: list[str] = []
    for name in args.modules:
        try:
            module = importlib.import_module(name)
        except Exception as exc:
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            failed.append(name)
            continue
        where = real(getattr(module, "__file__", "") or "")
        inside = where.startswith(root + os.sep)
        print(f"{'ok  ' if inside else 'FAIL'}  {name} -> {where or '(no __file__)'}")
        if not inside:
            failed.append(name)

    print()
    if failed:
        print(f"{len(failed)} of {len(args.modules)} module(s) did NOT come from the fork: {', '.join(failed)}")
        return 1
    print(f"all {len(args.modules)} module(s) imported from the fork")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
