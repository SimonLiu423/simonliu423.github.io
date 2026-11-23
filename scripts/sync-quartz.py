#!/usr/bin/env python3
"""Sync publish: true notes and referenced assets from the standalone Obsidian vault into Quartz content/.

Usage:
  python3 scripts/sync-quartz.py --dry-run
  python3 scripts/sync-quartz.py
  python3 scripts/sync-quartz.py --build
  python3 scripts/sync-quartz.py --vault /path/to/vault --dest /path/to/content
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys

DEFAULT_VAULT = pathlib.Path(os.environ.get("OBSIDIAN_VAULT", "~/vault")).expanduser()
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_DEST = REPO_ROOT / "content"

# Folders and files that must NEVER be exported to the public Quartz repo.
BLOCKED_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("00 Inbox",),
    ("10 Projects",),
    ("20 Meetings",),
    ("30 Research", "Reports"),
    ("30 Research", "Papers"),
    ("_system",),
)
BLOCKED_ROOT_FILES = {"Dashboard.md", "Published.base", "Memory.base"}

PUBLISH_KEY_RE = re.compile(r"^publish\s*:\s*(.*)$", re.IGNORECASE)
WIKILINK_RE = re.compile(r"!\[\[([^\]]+)\]\]|\[\[([^\]]+)\]\]")
MD_LINK_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
ASSET_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif",
    ".pdf", ".mp4", ".webm", ".mp3", ".wav", ".ogg",
}


def has_publish_true(text: str) -> bool:
    """Return True iff top-level YAML frontmatter explicitly sets `publish: true`."""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].rstrip() != "---":
        return False
    end_idx = next((i for i in range(1, len(lines)) if lines[i].rstrip() in ("---", "...")), None)
    if end_idx is None:
        return False
    for line in lines[1:end_idx]:
        if line.startswith((" ", "\t")):
            continue
        m = PUBLISH_KEY_RE.match(line)
        if m:
            val = m.group(1).split("#", 1)[0].strip().strip("'\"").lower()
            return val == "true"
    return False


def is_blocked(rel_parts: tuple[str, ...]) -> bool:
    if any(p.startswith(".") for p in rel_parts):
        return True
    if len(rel_parts) == 1 and rel_parts[0] in BLOCKED_ROOT_FILES:
        return True
    return any(rel_parts[: len(pref)] == pref for pref in BLOCKED_PREFIXES)


def extract_asset_refs(text: str) -> set[str]:
    refs: set[str] = set()
    for m in WIKILINK_RE.finditer(text):
        raw = (m.group(1) or m.group(2) or "").split("|", 1)[0].split("#", 1)[0].strip()
        if raw and pathlib.Path(raw).suffix.lower() in ASSET_EXTS:
            refs.add(raw)
    for m in MD_LINK_RE.finditer(text):
        raw = m.group(1).strip().split("#", 1)[0].strip()
        if raw and not re.match(r"^[a-z]+://", raw, re.IGNORECASE):
            if pathlib.Path(raw).suffix.lower() in ASSET_EXTS:
                refs.add(raw)
    return refs


def resolve_asset(vault: pathlib.Path, ref: str, by_name: dict[str, list[pathlib.Path]]) -> pathlib.Path | None:
    clean = ref.lstrip("./")
    direct = vault / clean
    if direct.is_file():
        return direct
    matches = by_name.get(pathlib.Path(clean).name, [])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Prefer _system/assets/ matches
        for m in matches:
            if m.relative_to(vault).parts[:2] == ("_system", "assets"):
                return m
        return matches[0]
    return None


def collect_exports(vault: pathlib.Path, include_non_public_assets: bool) -> tuple[dict[pathlib.Path, pathlib.Path], list[str]]:
    """Return (map of dest_rel -> src_abs, warnings)."""
    exports: dict[pathlib.Path, pathlib.Path] = {}
    warnings: list[str] = []

    # Index all vault non-dot asset files by basename for Obsidian shortest-path resolution
    assets_by_name: dict[str, list[pathlib.Path]] = {}
    for p in sorted(vault.rglob("*")):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(vault).parts
        if any(seg.startswith(".") for seg in rel_parts):
            continue
        if p.suffix.lower() in ASSET_EXTS:
            assets_by_name.setdefault(p.name, []).append(p)

    # 1. Collect publish: true markdown notes outside blocked folders
    published_mds: list[tuple[pathlib.Path, str]] = []
    for p in sorted(vault.rglob("*.md")):
        rel = p.relative_to(vault)
        if is_blocked(rel.parts):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if has_publish_true(text):
            exports[rel] = p
            published_mds.append((rel, text))

    # 2. Collect assets referenced by published notes
    for md_rel, text in published_mds:
        for ref in sorted(extract_asset_refs(text)):
            found = resolve_asset(vault, ref, assets_by_name)
            if found is None:
                warnings.append(f"Missing asset referenced by {md_rel}: {ref}")
                continue
            asset_rel = found.relative_to(vault)
            if not include_non_public_assets and not found.name.startswith("public-"):
                warnings.append(
                    f"Skipped non-public asset {asset_rel} referenced by {md_rel} "
                    "(rename with 'public-' prefix or pass --all-assets to export)"
                )
                continue
            exports[asset_rel] = found

    return exports, warnings


def sync(vault: pathlib.Path, dest: pathlib.Path, *, dry_run: bool, include_non_public_assets: bool) -> int:
    if not vault.is_dir():
        print(f"error: vault not found: {vault}", file=sys.stderr)
        return 1

    exports, warnings = collect_exports(vault, include_non_public_assets)
    dest.mkdir(parents=True, exist_ok=True)

    # Existing files in dest (excluding .gitkeep)
    existing: set[pathlib.Path] = set()
    for p in dest.rglob("*"):
        if p.is_file() or p.is_symlink():
            rel = p.relative_to(dest)
            if rel.name == ".gitkeep" and len(rel.parts) == 1:
                continue
            existing.add(rel)

    desired = set(exports.keys())
    to_remove = sorted(existing - desired)
    to_add: list[pathlib.Path] = []
    to_update: list[pathlib.Path] = []
    unchanged = 0

    for rel in sorted(desired):
        src = exports[rel]
        dst = dest / rel
        if not dst.exists():
            to_add.append(rel)
        elif src.read_bytes() != dst.read_bytes():
            to_update.append(rel)
        else:
            unchanged += 1

    mode = "[DRY RUN] " if dry_run else ""
    print(f"{mode}Syncing {vault} -> {dest}")
    print(f"  Publishable files: {len(desired)} (add={len(to_add)}, update={len(to_update)}, unchanged={unchanged}, remove={len(to_remove)})")

    for rel in to_add:
        print(f"  + {rel}")
        if not dry_run:
            dst = dest / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(exports[rel], dst)

    for rel in to_update:
        print(f"  ~ {rel}")
        if not dry_run:
            shutil.copy2(exports[rel], dest / rel)

    for rel in to_remove:
        print(f"  - {rel}")
        if not dry_run:
            target = dest / rel
            if target.is_symlink() or target.is_file():
                target.unlink()

    if not dry_run:
        # Prune empty directories left behind (including old .obsidian/.pandoc if removed)
        for p in sorted(dest.rglob("*"), reverse=True):
            if p.is_dir() and not any(p.iterdir()):
                p.rmdir()

    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(f"  ! {w}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync publish: true notes from standalone Obsidian vault to Quartz content/.")
    parser.add_argument("--vault", type=pathlib.Path, default=DEFAULT_VAULT, help=f"Source Obsidian vault (default: {DEFAULT_VAULT})")
    parser.add_argument("--dest", type=pathlib.Path, default=DEFAULT_DEST, help=f"Quartz content directory (default: {DEFAULT_DEST})")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without modifying files")
    parser.add_argument("--all-assets", action="store_true", help="Export all assets referenced by published notes (not just public-*)")
    parser.add_argument("--build", action="store_true", help="Run `npx quartz build` after syncing")
    args = parser.parse_args()

    rc = sync(args.vault.expanduser(), args.dest.expanduser(), dry_run=args.dry_run, include_non_public_assets=args.all_assets)
    if rc != 0:
        return rc
    if args.build and not args.dry_run:
        print("\nRunning `npx quartz build`...")
        res = subprocess.run(["npx", "quartz", "build"], cwd=REPO_ROOT)
        return res.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
