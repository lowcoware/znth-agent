#!/usr/bin/env python3
"""Per-area file/line inventory of a tree: cut by size, and count progress.

Prints one row per area (a path prefix under the root), largest by lines first,
so "what is left" and "what did that pass remove" are numbers, not impressions.

Usage:
  python inventory_tree.py /path/to/fork                        # top-level areas
  python inventory_tree.py /path/to/fork --depth 2 --match '*.py'
  python inventory_tree.py /path/to/fork --group providers      # areas under every <segment> dir
"""
from __future__ import annotations

import argparse
import fnmatch
import os
from collections import defaultdict

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", "dist", "build",
    ".next", ".nuxt", "target", ".tox", ".idea", ".gradle",
}


def count_lines(path: str) -> int:
    try:
        with open(path, "rb") as handle:
            return handle.read().count(b"\n")
    except OSError:
        return 0


def area_of(rel_parts: list[str], depth: int, group: str | None) -> str:
    if group and group in rel_parts:
        index = rel_parts.index(group)
        head = rel_parts[: index + 2]  # <...>/<group>/<entry>
        return os.sep.join(head)
    if len(rel_parts) > depth:
        return os.sep.join(rel_parts[:depth])
    return "(files in the root)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("root")
    parser.add_argument("--depth", type=int, default=1,
                        help="path components under the root that form an area (default 1)")
    parser.add_argument("--group", default=None,
                        help="form areas under every directory with this name, e.g. 'providers'")
    parser.add_argument("--match", default="*", help="glob for file names, e.g. '*.py'")
    parser.add_argument("--top", type=int, default=40, help="how many areas to print (default 40)")
    args = parser.parse_args()

    root = os.path.realpath(args.root)
    if not os.path.isdir(root):
        print(f"FAIL  not a directory: {root}")
        return 2

    files: dict[str, int] = defaultdict(int)
    lines: dict[str, int] = defaultdict(int)
    total_files = total_lines = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in filenames:
            if not fnmatch.fnmatch(name, args.match):
                continue
            full = os.path.join(dirpath, name)
            parts = os.path.relpath(full, root).split(os.sep)
            area = area_of(parts, args.depth, args.group)
            count = count_lines(full)
            files[area] += 1
            lines[area] += count
            total_files += 1
            total_lines += count

    print(f"root:   {root}")
    print(f"filter: {args.match}" + (f"   group: {args.group}" if args.group else f"   depth: {args.depth}"))
    print(f"total:  {total_files} files, {total_lines} lines, {len(lines)} areas")
    print(f"skipped dirs: {', '.join(sorted(SKIP_DIRS))}")
    print()
    print(f"{'lines':>10}  {'files':>6}  {'share':>6}  area")
    for area, count in sorted(lines.items(), key=lambda item: -item[1])[: args.top]:
        share = (100.0 * count / total_lines) if total_lines else 0.0
        print(f"{count:>10}  {files[area]:>6}  {share:>5.1f}%  {area}")
    if len(lines) > args.top:
        print(f"... {len(lines) - args.top} more area(s) below the cut")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
