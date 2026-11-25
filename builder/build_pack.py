#!/usr/bin/env python3
"""
MooSwitch (mooswitch) pack builder: imports verified HOS_COW cowmmc-core binaries, downloads
(or reads cached) pinned GitHub releases, replaces hbmenu.nro with Sphaira,
applies Switch branding & curated configurations, emits HATS-style pack-manifest.json,
and packages dist/<pack_name>-<version>.zip.
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

PACK_ROOT = Path(__file__).resolve().parent.parent
if str(PACK_ROOT) not in sys.path:
    sys.path.insert(0, str(PACK_ROOT))

from branding.generate_branding import generate_branding  # noqa: E402

UPDATE_PACK_EXCLUDED_PATHS = frozenset({
    "bootloader/hekate_ipl.ini",
    "bootloader/nyx.ini",
    "atmosphere/config/system_settings.ini",
    "atmosphere/config/override_config.ini",
    "atmosphere/hosts/emummc.txt",
    "exosphere.ini",
    "config/cowmmc/guard.ini",
    "config/sphaira/config.ini",
    "config/ultrahand/config.ini",
    "config/sys-con/config.ini",
    "switch/JC-color-swapper/profiles.json",
    "SaltySD/exceptions.txt",
    "emuMMC/COW0/cow_autoinit",
})

UPDATE_PACK_EXCLUDED_GLOBS = (
    "atmosphere/contents/*/flags/boot2.flag",
)


def is_excluded_from_update_pack(rel_path: str) -> bool:
    rel = safe_rel_posix(rel_path)
    if rel in UPDATE_PACK_EXCLUDED_PATHS:
        return True
    return any(fnmatch.fnmatch(rel, pat) for pat in UPDATE_PACK_EXCLUDED_GLOBS)


def _write_sd_zip(
    zip_path: Path,
    sd_dir: Path,
    exclude_fn=None,
    manifest_override: dict | None = None,
) -> None:
    """Write a deterministic DEFLATED ZIP archive from sd_dir atomically."""
    zip_tmp = zip_path.with_name(f"{zip_path.name}.tmp.{os.getpid()}")
    fixed_dt = (2026, 1, 1, 0, 0, 0)

    def _add_entry(zf: zipfile.ZipFile, rel: str, payload: bytes) -> None:
        zi = zipfile.ZipInfo(rel, date_time=fixed_dt)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.external_attr = 0o644 << 16
        zf.writestr(zi, payload)

    try:
        with zipfile.ZipFile(zip_tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p in sorted(sd_dir.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(sd_dir).as_posix()
                if rel == "pack-manifest.json":
                    continue
                if exclude_fn is not None and exclude_fn(rel):
                    continue
                _add_entry(zf, rel, p.read_bytes())
            if manifest_override is not None:
                _add_entry(
                    zf,
                    "pack-manifest.json",
                    (json.dumps(manifest_override, indent=2) + "\n").encode("utf-8"),
                )
            elif (sd_dir / "pack-manifest.json").is_file():
                _add_entry(
                    zf,
                    "pack-manifest.json",
                    (sd_dir / "pack-manifest.json").read_bytes(),
                )
        zip_tmp.replace(zip_path)
    finally:
        if zip_tmp.exists():
            zip_tmp.unlink()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def safe_rel_posix(path_str: str) -> str:
    norm = path_str.replace("\\", "/").lstrip("/")
    parts = [p for p in norm.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"Unsafe archive/overlay relative path: {path_str!r}")
    return "/".join(parts)


def stage_bytes(
    sd_root: Path,
    owners: dict[str, dict],
    rel_path: str,
    data: bytes,
    component_id: str,
    protected: bool,
) -> None:
    rel = safe_rel_posix(rel_path)
    dest = sd_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    owners[rel] = {
        "path": rel,
        "component": component_id,
        "size": len(data),
        "sha256": sha256_bytes(data),
        "protected": protected,
    }


def _cache_tag_slug(tag: str) -> str:
    return tag.replace("/", "_")


def fetch_github_release(repo: str, tag: str, cache_dir: Path, offline: bool) -> dict:
    slug = repo.replace("/", "__")
    rel_cache = cache_dir / "releases" / f"{slug}__{_cache_tag_slug(tag)}.json"
    if rel_cache.is_file():
        return json.loads(rel_cache.read_text(encoding="utf-8"))
    if offline:
        raise RuntimeError(f"Offline mode enabled and cached release metadata missing: {rel_cache}")

    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    headers = {"User-Agent": "mooswitch-builder/0.2", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    rel_cache.parent.mkdir(parents=True, exist_ok=True)
    rel_cache.write_text(raw, encoding="utf-8")
    return json.loads(raw)


def fetch_github_asset(
    repo: str,
    tag: str,
    asset_glob: str,
    cache_dir: Path,
    offline: bool,
) -> tuple[Path, dict]:
    slug = repo.replace("/", "__")
    tag_slug = _cache_tag_slug(tag)
    rel_cache = cache_dir / "releases" / f"{slug}__{tag_slug}.json"
    is_exact_asset = not any(ch in asset_glob for ch in "*?[")

    asset: dict | None = None
    try:
        meta = fetch_github_release(repo, tag, cache_dir, offline)
        matches = [a for a in meta.get("assets", []) if fnmatch.fnmatch(a["name"], asset_glob)]
        if len(matches) != 1:
            names = [a["name"] for a in meta.get("assets", [])]
            raise RuntimeError(f"Expected 1 asset matching {asset_glob!r} in {repo}@{tag}, found {matches} (all={names})")
        asset = matches[0]
    except urllib.error.HTTPError as exc:
        if exc.code not in (403, 429) or not is_exact_asset:
            raise
        dl_url = f"https://github.com/{repo}/releases/download/{urllib.parse.quote(tag, safe='')}/{urllib.parse.quote(asset_glob, safe='')}"
        asset = {"name": asset_glob, "size": None, "browser_download_url": dl_url}

    asset_path = cache_dir / "assets" / f"{slug}__{tag_slug}__{asset['name']}"
    if not asset_path.is_file():
        if offline:
            raise RuntimeError(f"Offline mode enabled and cached asset missing: {asset_path}")
        size_note = f" ({asset['size'] / 1024:.1f} KiB)" if asset["size"] is not None else ""
        print(f"[*] Downloading {repo}@{tag} -> {asset['name']}{size_note}...")
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_dl = asset_path.with_suffix(asset_path.suffix + f".tmp.{os.getpid()}")
        try:
            req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "mooswitch-builder/0.2"})
            with urllib.request.urlopen(req, timeout=180) as resp, open(tmp_dl, "wb") as fp:
                content_len = resp.getheader("Content-Length")
                if asset["size"] is None and content_len and content_len.isdigit():
                    asset["size"] = int(content_len)
                shutil.copyfileobj(resp, fp)
            tmp_dl.replace(asset_path)
        finally:
            if tmp_dl.exists():
                tmp_dl.unlink()
    else:
        print(f"[*] Using cached asset: {asset_path.name}")

    actual_size = asset_path.stat().st_size
    if asset["size"] is not None and actual_size != asset["size"]:
        raise RuntimeError(f"Asset size mismatch for {asset_path}: {actual_size} != {asset['size']}")
    if not rel_cache.is_file():
        asset["size"] = actual_size
        rel_cache.parent.mkdir(parents=True, exist_ok=True)
        rel_cache.write_text(json.dumps({"tag_name": tag, "assets": [asset]}, indent=2), encoding="utf-8")

    lock_info = {
        "repo": repo,
        "tag": tag,
        "asset_name": asset["name"],
        "size": actual_size,
        "sha256": sha256_file(asset_path),
        "browser_download_url": asset["browser_download_url"],
    }
    return asset_path, lock_info


def fetch_github_raw_asset(
    repo: str,
    tag: str,
    raw_path: str,
    cache_dir: Path,
    offline: bool,
) -> tuple[Path, dict]:
    slug = repo.replace("/", "__")
    rel_raw = safe_rel_posix(raw_path)
    asset_name = Path(rel_raw).name
    asset_path = cache_dir / "assets" / f"{slug}__{tag[:12]}__{asset_name}"
    url = f"https://raw.githubusercontent.com/{repo}/{tag}/{rel_raw}"

    if not asset_path.is_file():
        if offline:
            raise RuntimeError(f"Offline mode enabled and cached raw asset missing: {asset_path}")
        print(f"[*] Downloading {repo}@{tag[:12]}:{rel_raw}...")
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_dl = asset_path.with_suffix(asset_path.suffix + f".tmp.{os.getpid()}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mooswitch-builder/0.2"})
            with urllib.request.urlopen(req, timeout=180) as resp, open(tmp_dl, "wb") as fp:
                shutil.copyfileobj(resp, fp)
            tmp_dl.replace(asset_path)
        finally:
            if tmp_dl.exists():
                tmp_dl.unlink()
    else:
        print(f"[*] Using cached raw asset: {asset_path.name}")

    actual_size = asset_path.stat().st_size
    lock_info = {
        "repo": repo,
        "tag": tag,
        "asset_name": asset_name,
        "size": actual_size,
        "sha256": sha256_file(asset_path),
        "browser_download_url": url,
    }
    return asset_path, lock_info


def import_cowmmc_core(core_src: Path, sd_root: Path, owners: dict[str, dict], protected: bool) -> dict:
    if not core_src.exists():
        raise RuntimeError(f"cowmmc-core path not found: {core_src}")

    with tempfile.TemporaryDirectory(prefix="mooswitch_core_") as tmp:
        if core_src.is_file() and core_src.name.endswith((".tar.gz", ".tgz")):
            extract_dir = Path(tmp)
            with tarfile.open(core_src, "r:gz") as tf:
                try:
                    tf.extractall(extract_dir, filter="data")
                except TypeError:
                    tf.extractall(extract_dir)
            base_dir = extract_dir
        elif core_src.is_dir():
            base_dir = core_src
        else:
            raise RuntimeError(f"Unsupported cowmmc-core source type: {core_src}")

        manifest_path = base_dir / "cowmmc-core-manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError(f"Missing cowmmc-core-manifest.json inside {base_dir}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        for entry in manifest["files"]:
            rel = safe_rel_posix(entry["path"])
            src_file = base_dir / rel
            if not src_file.is_file():
                raise RuntimeError(f"Missing cowmmc-core file: {src_file}")
            data = src_file.read_bytes()
            if len(data) != entry["size"] or sha256_bytes(data) != entry["sha256"]:
                raise RuntimeError(f"Integrity mismatch importing cowmmc-core file: {rel}")
            stage_bytes(sd_root, owners, rel, data, "cowmmc-core", protected)

        return manifest


def should_exclude(rel: str, exclude_patterns: list[str]) -> bool:
    for pat in exclude_patterns:
        if fnmatch.fnmatch(rel, pat) or rel == pat:
            return True
    return False


def _normalize_zip_rel(raw_name: str, strip_prefix: str) -> str | None:
    rel = safe_rel_posix(raw_name)
    if strip_prefix:
        prefix = safe_rel_posix(strip_prefix) + "/"
        if not rel.startswith(prefix):
            return None
        rel = rel[len(prefix):]
        if not rel:
            return None
    return rel


def apply_github_component(comp: dict, asset_path: Path, sd_root: Path, owners: dict[str, dict]) -> None:
    cid = comp["id"]
    protected = bool(comp["protected"])
    mode = comp["extract_mode"]

    if mode == "single_file":
        stage_bytes(sd_root, owners, comp["dest_path"], asset_path.read_bytes(), cid, protected)
        return

    strip_prefix = comp.get("strip_prefix", "")
    rename_map = {safe_rel_posix(k): safe_rel_posix(v) for k, v in comp.get("rename_map", {}).items()}

    with zipfile.ZipFile(asset_path, "r") as zf:
        infos = [zi for zi in zf.infolist() if not zi.is_dir()]

        if mode == "zip_tree":
            excludes = comp.get("exclude_paths", [])
            for zi in infos:
                rel = _normalize_zip_rel(zi.filename, strip_prefix)
                if rel is None or should_exclude(rel, excludes):
                    continue
                dest_rel = rename_map.get(rel, rel)
                if should_exclude(dest_rel, excludes):
                    continue
                stage_bytes(sd_root, owners, dest_rel, zf.read(zi), cid, protected)
            return

        if mode == "zip_selected":
            wanted = {safe_rel_posix(p) for p in comp["include_paths"]}
            found: set[str] = set()
            for zi in infos:
                rel = _normalize_zip_rel(zi.filename, strip_prefix)
                if rel is not None and rel in wanted:
                    dest_rel = rename_map.get(rel, rel)
                    stage_bytes(sd_root, owners, dest_rel, zf.read(zi), cid, protected)
                    found.add(rel)
            missing = sorted(wanted - found)
            if missing:
                raise RuntimeError(f"Component {cid} missing expected archive entries: {missing}")
            return

        if mode == "replace_hbmenu":
            canon_rel = safe_rel_posix(comp["canonical_path"])
            exact_matches = [zi for zi in infos if safe_rel_posix(zi.filename) == canon_rel]
            if len(exact_matches) == 1:
                nro_info = exact_matches[0]
            else:
                nro_matches = [
                    zi for zi in infos
                    if not safe_rel_posix(zi.filename).startswith("nx2/")
                    and os.path.basename(safe_rel_posix(zi.filename)).lower() == "sphaira.nro"
                ]
                if len(nro_matches) != 1:
                    names = [zi.filename for zi in infos]
                    raise RuntimeError(f"Expected 1 sphaira.nro in {asset_path}, found {nro_matches} (all={names})")
                nro_info = nro_matches[0]

            sphaira_bytes = zf.read(nro_info)
            stage_bytes(sd_root, owners, canon_rel, sphaira_bytes, cid, protected)
            stage_bytes(sd_root, owners, comp["hbmenu_path"], sphaira_bytes, cid, protected)

            # Stage companion sphaira subdirectories (e.g. switch/ or config/) if present
            for zi in infos:
                if zi.filename == nro_info.filename:
                    continue
                rel = safe_rel_posix(zi.filename)
                if rel.startswith(("switch/", "config/", "atmosphere/")):
                    stage_bytes(sd_root, owners, rel, zf.read(zi), cid, protected)
            return

        raise RuntimeError(f"Unknown extract_mode {mode!r} in component {cid}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build HATS-style MooSwitch (mooswitch) Switch SD distribution")
    parser.add_argument(
        "--cowmmc-core",
        default=str(PACK_ROOT.parent / "HOS_COW" / "dist" / "cowmmc-core"),
        help="Path to HOS_COW dist/cowmmc-core directory or cowmmc-core.tar.gz",
    )
    parser.add_argument("--out-dir", default=str(PACK_ROOT / "dist"))
    parser.add_argument("--cache-dir", default=str(PACK_ROOT / ".cache"))
    parser.add_argument("--version", default=None, help="Override pack.json version (e.g. 2026.11.22 or nightly-2026.11.22)")
    parser.add_argument("--offline", action="store_true", help="Build strictly from local .cache/ without network access")
    args = parser.parse_args()

    pack_cfg = json.loads((PACK_ROOT / "pack.json").read_text(encoding="utf-8"))
    if args.version:
        pack_cfg["version"] = args.version
    comps_cfg = json.loads((PACK_ROOT / "components.json").read_text(encoding="utf-8"))

    out_dir = Path(args.out_dir).resolve()
    cache_dir = Path(args.cache_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    sd_stage = out_dir / f"sd_root.tmp.{os.getpid()}"
    sd_final = out_dir / "sd_root"
    if sd_stage.exists():
        shutil.rmtree(sd_stage)
    sd_stage.mkdir(parents=True, exist_ok=True)

    owners: dict[str, dict] = {}
    lock_checksums: list[dict] = []
    comp_meta: dict[str, dict] = {}
    core_manifest: dict = {}

    try:
        for comp in comps_cfg["components"]:
            cid = comp["id"]
            kind = comp["kind"]
            if kind == "github_release":
                asset_path, lock_info = fetch_github_asset(
                    comp["repo"], comp["tag"], comp["asset_glob"], cache_dir, args.offline
                )
                lock_checksums.append({"id": cid, **lock_info})
                apply_github_component(comp, asset_path, sd_stage, owners)
                comp_meta[cid] = {
                    "name": comp["name"],
                    "version": comp["tag"],
                    "source": f"github:{comp['repo']}@{comp['tag']}",
                    "asset": lock_info["asset_name"],
                    "asset_sha256": lock_info["sha256"],
                    "protected": bool(comp["protected"]),
                }
            elif kind == "github_raw_zip":
                asset_path, lock_info = fetch_github_raw_asset(
                    comp["repo"], comp["tag"], comp["raw_path"], cache_dir, args.offline
                )
                lock_checksums.append({"id": cid, **lock_info})
                apply_github_component(comp, asset_path, sd_stage, owners)
                comp_meta[cid] = {
                    "name": comp["name"],
                    "version": comp["tag"][:12],
                    "source": f"github-raw:{comp['repo']}@{comp['tag'][:12]}:{comp['raw_path']}",
                    "asset": lock_info["asset_name"],
                    "asset_sha256": lock_info["sha256"],
                    "protected": bool(comp["protected"]),
                }
            elif kind == "local_core":
                core_path = Path(args.cowmmc_core).resolve()
                core_manifest = import_cowmmc_core(core_path, sd_stage, owners, bool(comp["protected"]))
                comp_meta[cid] = {
                    "name": comp["name"],
                    "version": core_manifest.get("hos_cow_commit", "unknown")[:12],
                    "source": "local:HOS_COW/dist/cowmmc-core",
                    "hekate_commit": core_manifest.get("hekate_commit"),
                    "atmosphere_commit": core_manifest.get("atmosphere_commit"),
                    "protected": bool(comp["protected"]),
                }
            else:
                raise RuntimeError(f"Unsupported component kind {kind!r} for {cid}")

        # Generate Switch BMP branding assets
        brand_files = generate_branding(pack_cfg, PACK_ROOT / "branding" / "custom", sd_stage)
        for rel in brand_files:
            data = (sd_stage / rel).read_bytes()
            owners[rel] = {
                "path": rel,
                "component": "mooswitch-branding",
                "size": len(data),
                "sha256": sha256_bytes(data),
                "protected": False,
            }
        comp_meta["mooswitch-branding"] = {
            "name": "MooSwitch Branding Assets",
            "version": pack_cfg["version"],
            "source": "local:branding/",
            "protected": False,
        }

        # Apply curated SD configuration overlay last
        overlay_dir = PACK_ROOT / "sd_overlay"
        for src in sorted(overlay_dir.rglob("*")):
            if not src.is_file():
                continue
            rel = safe_rel_posix(src.relative_to(overlay_dir).as_posix())
            stage_bytes(sd_stage, owners, rel, src.read_bytes(), "mooswitch-config", False)
        comp_meta["mooswitch-config"] = {
            "name": "MooSwitch Curated Configuration",
            "version": pack_cfg["version"],
            "source": "local:sd_overlay/",
            "protected": False,
        }

        # Verify bootloader/update.bin == payload.bin == atmosphere/reboot_payload.bin
        upd = (sd_stage / "bootloader/update.bin").read_bytes()
        if (sd_stage / "payload.bin").read_bytes() != upd or (sd_stage / "atmosphere/reboot_payload.bin").read_bytes() != upd:
            raise RuntimeError("Payload parity violation between update.bin, payload.bin, and reboot_payload.bin")

        # Write HATS-style pack-manifest.json
        files_list = sorted(owners.values(), key=lambda x: x["path"])
        pack_manifest = {
            "format": "mooswitch-manifest-v1",
            "variant": "full",
            "pack_name": pack_cfg["pack_name"],
            "brand_title": pack_cfg["brand_title"],
            "version": pack_cfg["version"],
            "supported_hos": pack_cfg["supported_hos"],
            "atmosphere_version": pack_cfg["atmosphere_version"],
            "hekate_version": pack_cfg["hekate_version"],
            "built_at_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cowmmc_core": {
                "hos_cow_commit": core_manifest.get("hos_cow_commit"),
                "hekate_commit": core_manifest.get("hekate_commit"),
                "atmosphere_commit": core_manifest.get("atmosphere_commit"),
            },
            "components": comp_meta,
            "file_count": len(files_list),
            "files": files_list,
        }
        (sd_stage / "pack-manifest.json").write_text(
            json.dumps(pack_manifest, indent=2) + "\n", encoding="utf-8"
        )

        # Write dist/lock-checksums.json
        (out_dir / "lock-checksums.json").write_text(
            json.dumps({"format": "mooswitch-locks-v1", "assets": lock_checksums}, indent=2) + "\n",
            encoding="utf-8",
        )

        if sd_final.exists():
            shutil.rmtree(sd_final)
        sd_stage.rename(sd_final)

        # Build distributable Full ZIP and config-preserving Update ZIP from sd_root
        zip_name = f"{pack_cfg['pack_name']}-{pack_cfg['version']}.zip"
        zip_path = out_dir / zip_name
        _write_sd_zip(zip_path, sd_final)

        update_files_list = [entry for entry in files_list if not is_excluded_from_update_pack(entry["path"])]
        excluded_paths = sorted(entry["path"] for entry in files_list if is_excluded_from_update_pack(entry["path"]))
        update_manifest = dict(pack_manifest)
        update_manifest.update({
            "variant": "update",
            "file_count": len(update_files_list),
            "excluded_count": len(excluded_paths),
            "excluded_paths": excluded_paths,
            "files": update_files_list,
        })
        update_zip_name = f"{pack_cfg['pack_name']}-{pack_cfg['version']}-update.zip"
        update_zip_path = out_dir / update_zip_name
        _write_sd_zip(
            update_zip_path,
            sd_final,
            exclude_fn=is_excluded_from_update_pack,
            manifest_override=update_manifest,
        )

        print(f"[+] Built {zip_path} ({zip_path.stat().st_size / (1024 * 1024):.2f} MiB, {len(files_list)} tracked files)")
        print(
            f"[+] Built {update_zip_path} ({update_zip_path.stat().st_size / (1024 * 1024):.2f} MiB, "
            f"{len(update_files_list)} tracked files, {len(excluded_paths)} user config/flag/seed paths excluded)"
        )
        return 0
    finally:
        if sd_stage.exists():
            shutil.rmtree(sd_stage)


if __name__ == "__main__":
    raise SystemExit(main())
