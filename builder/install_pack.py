#!/usr/bin/env python3
"""
Safe manifest-aware installer and updater for MooSwitch (mooswitch) Switch SD distributions.

Preserves user data (/emuMMC, /Nintendo, and existing curated configs unless
--overwrite-configs is given), quarantines active custom Home Menu themes, cleans
stale crash/error reports, removes obsolete tracked files from previous MooSwitch
manifests, and verifies SHA-256 checksums on every written file.

Inspired by rashevskyv/kefir (KEFIR / Kefirosphere) safe theme-quarantine and
cleanup workflows and sthetix's HATS manifest-driven pack structure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

PACK_ROOT = Path(__file__).resolve().parent.parent

THEME_TIDS = (
    "0100000000001000",  # qlaunch (Home Menu)
    "0100000000001007",  # playerSelect
    "0100000000001013",  # myPage
    "010000000000100B",  # shopN
    "0100000000000811",  # miiEdit
)

PRESERVE_CONFIG_PATHS = frozenset({
    "config/cowmmc/guard.ini",
    "bootloader/hekate_ipl.ini",
    "bootloader/nyx.ini",
    "atmosphere/config/system_settings.ini",
})

SEED_ONLY_PATHS = frozenset({
    "emuMMC/COW0/cow_autoinit",
})

NEVER_DELETE_PREFIXES = (
    "emuMMC/",
    "Nintendo/",
    "config/cowmmc/quarantined_themes/",
)

STALE_REPORT_DIRS = (
    "atmosphere/erpt_reports",
    "atmosphere/fatal_reports",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_rel_posix(path_str: str) -> str:
    norm = path_str.replace("\\", "/").lstrip("/")
    parts = [p for p in norm.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"Unsafe relative path: {path_str!r}")
    return "/".join(parts)


class SourceReader:
    """Unified verified reader over a built sd_root directory or MooSwitch ZIP."""

    def __init__(self, sd_root: Path | None, zip_path: Path | None) -> None:
        self._sd_root = sd_root
        self._zf = zipfile.ZipFile(zip_path, "r") if zip_path is not None else None

    def __enter__(self) -> "SourceReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._zf is not None:
            self._zf.close()

    def read_bytes(self, rel_path: str) -> bytes:
        rel = safe_rel_posix(rel_path)
        if self._zf is not None:
            return self._zf.read(rel)
        assert self._sd_root is not None
        p = self._sd_root / rel
        if not p.is_file():
            raise FileNotFoundError(f"Missing pack source file: {p}")
        return p.read_bytes()


def resolve_source(sd_root_arg: str | None, zip_arg: str | None) -> tuple[Path | None, Path | None]:
    if sd_root_arg and zip_arg:
        raise SystemExit("Pass at most one of --sd-root or --zip")
    if sd_root_arg:
        p = Path(sd_root_arg).resolve()
        if not p.is_dir():
            raise SystemExit(f"--sd-root directory not found: {p}")
        return p, None
    if zip_arg:
        p = Path(zip_arg).resolve()
        if not p.is_file():
            raise SystemExit(f"--zip archive not found: {p}")
        return None, p

    default_sd = PACK_ROOT / "dist" / "sd_root"
    if default_sd.is_dir():
        return default_sd, None
    zips = sorted((PACK_ROOT / "dist").glob("mooswitch-*.zip"))
    if zips:
        return None, zips[-1]
    raise SystemExit("No pack source found; specify --sd-root or --zip")


def quarantine_themes(target: Path) -> list[str]:
    contents_dir = target / "atmosphere" / "contents"
    if not contents_dir.is_dir():
        return []

    tid_map = {t.lower(): t for t in THEME_TIDS}
    quarantine_root = target / "config" / "cowmmc" / "quarantined_themes"
    moved: list[str] = []

    for child in sorted(contents_dir.iterdir(), key=lambda p: p.name.lower()):
        canon_tid = tid_map.get(child.name.lower())
        if canon_tid is None or not child.is_dir():
            continue
        quarantine_root.mkdir(parents=True, exist_ok=True)
        dst = quarantine_root / canon_tid
        if dst.exists():
            shutil.rmtree(dst)
        shutil.move(str(child), str(dst))
        moved.append(canon_tid)
    return moved


def clean_stale_reports(target: Path) -> list[str]:
    cleaned: list[str] = []
    for rel in STALE_REPORT_DIRS:
        p = target / rel
        if p.is_dir():
            shutil.rmtree(p)
            cleaned.append(rel)
    return cleaned


def remove_obsolete_files(target: Path, old_manifest_path: Path, new_paths: set[str]) -> list[str]:
    if not old_manifest_path.is_file():
        return []
    try:
        old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    old_paths = set()
    for entry in old_manifest.get("files", []):
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            try:
                old_paths.add(safe_rel_posix(entry["path"]))
            except ValueError:
                continue

    removed: list[str] = []
    for rel in sorted(old_paths - new_paths):
        if rel in PRESERVE_CONFIG_PATHS or rel.startswith(NEVER_DELETE_PREFIXES):
            continue
        p = target / rel
        if p.is_file():
            p.unlink()
            removed.append(rel)
            parent = p.parent
            while parent != target:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
    return removed


def install_pack(
    target: Path,
    sd_root: Path | None,
    zip_path: Path | None,
    overwrite_configs: bool = False,
    keep_themes: bool = False,
) -> dict[str, object]:
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)

    with SourceReader(sd_root, zip_path) as src:
        manifest_bytes = src.read_bytes("pack-manifest.json")
        new_manifest = json.loads(manifest_bytes.decode("utf-8"))
        entries = new_manifest["files"]

        # Pre-validate every payload from source before mutating target
        staged_payloads: list[tuple[str, bytes, str]] = []
        new_paths: set[str] = set()
        for entry in entries:
            rel = safe_rel_posix(entry["path"])
            if rel.startswith(("emuMMC/", "Nintendo/")) and rel not in SEED_ONLY_PATHS:
                raise RuntimeError(f"Refusing to install protected user-tree path: {rel}")
            data = src.read_bytes(rel)
            if len(data) != entry["size"] or sha256_bytes(data) != entry["sha256"]:
                raise RuntimeError(f"Source integrity mismatch for {rel}")
            new_paths.add(rel)
            staged_payloads.append((rel, data, entry["sha256"]))

    quarantined = [] if keep_themes else quarantine_themes(target)
    cleaned_reports = clean_stale_reports(target)
    removed_obsolete = remove_obsolete_files(target, target / "pack-manifest.json", new_paths)

    written: list[str] = []
    preserved: list[str] = []
    skipped_seeds: list[str] = []
    for rel, data, expected_sha in staged_payloads:
        dest = target / rel
        if rel in SEED_ONLY_PATHS:
            if (
                dest.is_file()
                or (target / "emuMMC" / "emummc.ini").is_file()
                or (target / "emuMMC" / "COW0" / "eMMC" / "00").is_file()
            ):
                skipped_seeds.append(rel)
                continue
        elif rel in PRESERVE_CONFIG_PATHS and dest.is_file() and not overwrite_configs:
            preserved.append(rel)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        actual_sha = sha256_bytes(dest.read_bytes())
        if actual_sha != expected_sha:
            raise RuntimeError(f"Post-write SHA-256 verification failed on {dest}")
        written.append(rel)

    manifest_dest = target / "pack-manifest.json"
    manifest_dest.write_bytes(manifest_bytes)
    if sha256_bytes(manifest_dest.read_bytes()) != sha256_bytes(manifest_bytes):
        raise RuntimeError(f"Post-write SHA-256 verification failed on {manifest_dest}")

    return {
        "target": str(target),
        "version": new_manifest.get("version"),
        "written": written,
        "preserved": preserved,
        "skipped_seeds": skipped_seeds,
        "removed_obsolete": removed_obsolete,
        "quarantined_themes": quarantined,
        "cleaned_reports": cleaned_reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe manifest-aware MooSwitch (mooswitch) SD installer / updater")
    parser.add_argument("--target", required=True, help="Target Switch microSD root directory")
    parser.add_argument("--sd-root", default=None, help="Path to staged MooSwitch dist/sd_root directory")
    parser.add_argument("--zip", default=None, help="Path to mooswitch-<version>.zip archive")
    parser.add_argument(
        "--overwrite-configs",
        action="store_true",
        help="Overwrite existing user-customized config files (guard.ini, hekate_ipl.ini, nyx.ini, system_settings.ini)",
    )
    parser.add_argument(
        "--keep-themes",
        action="store_true",
        help="Do not quarantine custom Home Menu theme directories under atmosphere/contents/",
    )
    args = parser.parse_args()

    sd_root, zip_path = resolve_source(args.sd_root, args.zip)
    summary = install_pack(
        target=Path(args.target),
        sd_root=sd_root,
        zip_path=zip_path,
        overwrite_configs=args.overwrite_configs,
        keep_themes=args.keep_themes,
    )

    print(f"[+] Installed MooSwitch {summary['version']} -> {summary['target']}")
    print(f"    Written (SHA-256 verified) : {len(summary['written'])} files")
    print(f"    Preserved user configs     : {len(summary['preserved'])} ({summary['preserved']})")
    print(f"    Skipped seed files         : {len(summary['skipped_seeds'])} ({summary['skipped_seeds']})")
    print(f"    Removed obsolete files     : {len(summary['removed_obsolete'])} ({summary['removed_obsolete']})")
    print(f"    Quarantined themes         : {len(summary['quarantined_themes'])} ({summary['quarantined_themes']})")
    print(f"    Cleaned report dirs        : {len(summary['cleaned_reports'])} ({summary['cleaned_reports']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
