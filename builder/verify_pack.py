#!/usr/bin/env python3
"""
Strict verifier for built MooSwitch (mooswitch) SD trees and distributable ZIP archives.
Validates BMP pixel headers, Sphaira hbmenu replacement (NRO0 + ASET/NACP),
payload parity, INI target resolution, binary magics, manifest SHA-256 coverage,
and ZIP integrity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

BUILDER_DIR = Path(__file__).resolve().parent
if str(BUILDER_DIR) not in sys.path:
    sys.path.insert(0, str(BUILDER_DIR))

from abi_check import verify_sd_abi_and_compatibility  # noqa: E402
from build_pack import UPDATE_PACK_EXCLUDED_PATHS, is_excluded_from_update_pack  # noqa: E402
from install_pack import install_pack  # noqa: E402


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_bmp32(
    path: Path,
    expected_w: int,
    expected_h: int,
    require_opaque: bool = False,
    require_grayscale: bool = False,
) -> None:
    assert path.is_file(), f"Missing BMP asset: {path}"
    data = path.read_bytes()
    assert len(data) >= 54, f"Truncated BMP: {path}"
    sig, fsz, r1, r2, off, dib, w, h, planes, bpp, comp, imgsz = struct.unpack("<2sIHHIIiiHHII", data[:38])
    assert sig == b"BM", f"Invalid BMP signature in {path}: {sig!r}"
    assert r1 == 0 and r2 == 0 and off == 54 and dib == 40, f"Unexpected BMP header layout in {path}"
    assert planes == 1 and bpp == 32 and comp == 0, f"BMP must be 32-bit BI_RGB in {path}: bpp={bpp} comp={comp}"
    assert (w, h) == (expected_w, expected_h), f"BMP {path.name} dims {(w, h)} != {(expected_w, expected_h)}"
    expected_px = expected_w * expected_h * 4
    assert len(data) == 54 + expected_px and fsz == len(data), f"BMP byte size mismatch in {path}"
    if require_opaque:
        alphas = set(data[57::4])
        assert alphas == {0xFF}, f"{path.name} must have opaque alpha=0xFF, got {alphas}"
    if require_grayscale:
        b, g, r = data[54::4], data[55::4], data[56::4]
        assert b == g == r, f"{path.name} must be strictly monochrome B&W grayscale (R == G == B)"


def verify_sphaira_hbmenu(sd_root: Path) -> str:
    hbmenu = sd_root / "hbmenu.nro"
    canon = sd_root / "switch/sphaira/sphaira.nro"
    assert hbmenu.is_file(), "Missing SD-root hbmenu.nro"
    assert canon.is_file(), "Missing switch/sphaira/sphaira.nro"

    hb_bytes = hbmenu.read_bytes()
    sp_bytes = canon.read_bytes()
    assert hb_bytes == sp_bytes, "hbmenu.nro is not byte-identical to switch/sphaira/sphaira.nro"

    assert len(hb_bytes) >= 0x20, "Truncated hbmenu.nro"
    magic, _, nro_size = struct.unpack_from("<4sII", hb_bytes, 0x10)
    assert magic == b"NRO0", f"hbmenu.nro bad magic: {magic!r}"
    assert len(hb_bytes) > nro_size + 0x38, "Missing NRO asset header on hbmenu.nro"
    aset_magic, _ = struct.unpack_from("<4sI", hb_bytes, nro_size)
    assert aset_magic == b"ASET", f"Missing ASET asset header in hbmenu.nro: {aset_magic!r}"
    nacp_off, nacp_sz = struct.unpack_from("<QQ", hb_bytes, nro_size + 0x18)
    assert nacp_sz >= 0x300, f"Invalid NACP size in hbmenu.nro: {nacp_sz}"

    nacp = hb_bytes[nro_size + nacp_off : nro_size + nacp_off + nacp_sz]
    titles = []
    for lang_idx in range(16):
        off = lang_idx * 0x300
        if off + 0x200 <= len(nacp):
            raw_title = nacp[off : off + 0x200].split(b"\x00", 1)[0].decode("utf-8", errors="ignore").strip()
            if raw_title:
                titles.append(raw_title)
    assert any("sphaira" in t.lower() for t in titles), f"hbmenu.nro NACP titles do not identify Sphaira: {titles}"
    assert all("hbmenu" not in t.lower() for t in titles), f"hbmenu.nro still identifies as stock hbmenu: {titles}"
    return titles[0]


def verify_hekate_ini(sd_root: Path) -> None:
    ini_path = sd_root / "bootloader/hekate_ipl.ini"
    assert ini_path.is_file(), "Missing bootloader/hekate_ipl.ini"
    sections: dict[str, dict[str, str]] = {}
    cur = None
    for raw_line in ini_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";", "{")):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1]
            sections.setdefault(cur, {})
            continue
        if "=" in line and cur is not None:
            k, v = [x.strip() for x in line.split("=", 1)]
            sections[cur][k] = v
            if k in ("pkg3", "fss0", "secmon", "logopath", "icon"):
                target = sd_root / v
                assert target.is_file(), f"hekate_ipl.ini [{cur}] {k}={v} points to missing file"
            elif k == "kip1":
                if v == "atmosphere/kips/*":
                    for kip_name in ("ams_mitm.kip", "hoc.kip"):
                        assert (sd_root / "atmosphere/kips" / kip_name).is_file(), (
                            f"hekate_ipl.ini [{cur}] kip1={v} missing {kip_name}"
                        )
                else:
                    assert (sd_root / v).is_file(), f"hekate_ipl.ini [{cur}] {k}={v} points to missing file"

    assert "CFW (sysMMC)" not in sections, "hekate_ipl.ini must not expose [CFW (sysMMC)]"
    assert "CFW (cowMMC)" in sections, "Missing [CFW (cowMMC)] section in hekate_ipl.ini"
    cow_sec = sections["CFW (cowMMC)"]
    assert cow_sec.get("emummcforce") == "1", "[CFW (cowMMC)] must enforce emummcforce=1"
    assert cow_sec.get("kip1") == "atmosphere/kips/*", "[CFW (cowMMC)] must load atmosphere/kips/*"
    assert cow_sec.get("secmon") == "atmosphere/exosphere.bin", "[CFW (cowMMC)] must load Horizon-OC secmon"
    assert cow_sec.get("logopath") == "bootloader/bootlogo.bmp", "[CFW (cowMMC)] must load bootloader/bootlogo.bmp"

    for sec_name, sec_vals in sections.items():
        assert "icon" not in sec_vals, f"hekate_ipl.ini [{sec_name}] must use default Hekate icons without icon= override"

    assert "Semi-Stock (OFW)" in sections, "Missing [Semi-Stock (OFW)] section in hekate_ipl.ini"
    semi_sec = sections["Semi-Stock (OFW)"]
    assert semi_sec.get("stock") == "1" and semi_sec.get("emummc_force_disable") == "1", (
        "[Semi-Stock (OFW)] must set stock=1 and emummc_force_disable=1"
    )

    assert "Full Stock (OFW)" in sections, "Missing [Full Stock (OFW)] section in hekate_ipl.ini"
    assert sections["Full Stock (OFW)"].get("ofw") == "1", "[Full Stock (OFW)] must set ofw=1"


def _assert_nro0(path: Path, rel: str) -> None:
    assert path.is_file(), f"Missing NRO/OVL file: {rel}"
    data = path.read_bytes()
    assert len(data) >= 0x14 and data[0x10:0x14] == b"NRO0", f"{rel} missing NRO0 magic"


def verify_binary_magics(sd_root: Path) -> None:
    assert (sd_root / "boot.dat").is_file() and (sd_root / "boot.dat").stat().st_size > 0, "Missing boot.dat"
    assert (sd_root / "boot.ini").is_file() and (sd_root / "boot.ini").stat().st_size > 0, "Missing boot.ini"

    upd = (sd_root / "bootloader/update.bin").read_bytes()
    pay = (sd_root / "payload.bin").read_bytes()
    reb = (sd_root / "atmosphere/reboot_payload.bin").read_bytes()
    assert upd == pay == reb, "update.bin, payload.bin, and reboot_payload.bin must be byte-identical"
    assert len(upd) <= 126296, f"update.bin exceeds Hekate IRAM limit: {len(upd)}"

    assert (sd_root / "bootloader/sys/libsys_cow.bso").read_bytes()[:4] == b"\x7fELF"
    assert (sd_root / "bootloader/sys/nyx.bin").is_file()
    assert (sd_root / "bootloader/sys/emummc.kipm").stat().st_size > 0x40000
    assert (sd_root / "atmosphere/kips/ams_mitm.kip").read_bytes()[:4] == b"KIP1"
    assert (sd_root / "atmosphere/kips/hoc.kip").read_bytes()[:4] == b"KIP1"
    assert (sd_root / "atmosphere/exosphere.bin").is_file() and (sd_root / "atmosphere/exosphere.bin").stat().st_size > 0
    assert (sd_root / "atmosphere/package3").read_bytes()[:4] == b"PK31"

    enabled_sysmodules = (
        "420000000000C001",  # sys-cowguard
        "420000000007E51A",  # nx-ovlloader
        "420000000000000B",  # sys-patch
        "00FF0000636C6BFF",  # hoc-clk
        "0000000000534C56",  # SaltyNX
        "010000000000bd00",  # MissionControl
        "690000000000000D",  # sys-con
    )
    for tid in enabled_sysmodules:
        base = sd_root / "atmosphere/contents" / tid
        assert (base / "exefs.nsp").read_bytes()[:4] == b"PFS0", f"{tid}/exefs.nsp missing PFS0 magic"
        json.loads((base / "toolbox.json").read_text(encoding="utf-8"))
        assert (base / "flags/boot2.flag").is_file(), f"Missing boot2.flag for {tid}"

    disabled_sysmodules = (
        "0100000000000352",  # emuiibo
        "4200000000000010",  # ldn_mitm
    )
    for tid in disabled_sysmodules:
        base = sd_root / "atmosphere/contents" / tid
        assert (base / "exefs.nsp").read_bytes()[:4] == b"PFS0", f"{tid}/exefs.nsp missing PFS0 magic"
        json.loads((base / "toolbox.json").read_text(encoding="utf-8"))
        assert not (base / "flags/boot2.flag").exists(), f"Disabled sysmodule {tid} must not have boot2.flag"

    overlays = (
        "switch/.overlays/ovlmenu.ovl",
        "switch/.overlays/cowmmc.ovl",
        "switch/.overlays/sys-patch-overlay.ovl",
        "switch/.overlays/horizon-oc-overlay.ovl",
        "switch/.overlays/Status-Monitor-Overlay.ovl",
        "switch/.overlays/FPSLocker.ovl",
        "switch/.overlays/ReverseNX-RT-ovl.ovl",
        "switch/.overlays/emuiibo.ovl",
        "switch/.overlays/ldnmitm_config.ovl",
        "switch/.overlays/ovlSysmodules.ovl",
        "switch/.overlays/QuickNTP.ovl",
        "switch/.overlays/ovlEdiZon.ovl",
    )
    for rel in overlays:
        _assert_nro0(sd_root / rel, rel)

    nros = (
        "hbmenu.nro",
        "switch/sphaira/sphaira.nro",
        "switch/cow_manager.nro",
        "switch/Benchmark-Toolbox/Benchmark-Toolbox.nro",
        "switch/sys-con/sys-con.nro",
        "switch/ldnmitm_config/ldnmitm_config.nro",
        "switch/JKSV/JKSV.nro",
        "switch/linkalho/linkalho.nro",
        "switch/Switch_90DNS_tester/Switch_90DNS_tester.nro",
        "switch/NXThemesInstaller/NXThemesInstaller.nro",
        "switch/JC-color-swapper/JC-color-swapper.nro",
    )
    for rel in nros:
        _assert_nro0(sd_root / rel, rel)

    assert not (sd_root / "switch/daybreak.nro").exists(), "daybreak.nro must remain excluded on cowMMC"

    payloads = (
        "Lockpick_RCM.bin",
        "TegraExplorer.bin",
        "CommonProblemResolver.bin",
        "picofly_toolbox_0.2.bin",
        "hwfly_toolbox.bin",
        "Instinct-NX_toolbox.bin",
    )
    for pname in payloads:
        p = sd_root / "bootloader/payloads" / pname
        assert p.is_file() and p.stat().st_size > 0, f"Missing or empty payload: bootloader/payloads/{pname}"


def verify_configs_and_packages(sd_root: Path) -> None:
    sys_ini = sd_root / "atmosphere/config/system_settings.ini"
    assert sys_ini.is_file(), "Missing atmosphere/config/system_settings.ini"
    sys_txt = sys_ini.read_text(encoding="utf-8")
    for req in (
        "[eupld]",
        "upload_enabled = u8!0x0",
        "[erpt]",
        "disable_automatic_report_cleanup = u8!0x0",
        "[olsc]",
        "default_auto_upload_global_setting = u8!0x0",
        "default_auto_download_global_setting = u8!0x0",
    ):
        assert req in sys_txt, f"system_settings.ini missing {req!r}"

    pkg_ini = sd_root / "switch/.packages/mooswitch/package.ini"
    assert pkg_ini.is_file(), "Missing Ultrahand package switch/.packages/mooswitch/package.ini"
    pkg_txt = pkg_ini.read_text(encoding="utf-8")
    for sec in (
        "[Allow SysMem Installs]",
        "[Disable SD Save Redirect]",
        "[Quarantine Custom Themes]",
        "[Restore Quarantined Themes]",
        "[Clean Error & Crash Reports]",
        "[Dot Clean SD]",
        "[Reboot to Hekate / Nyx]",
    ):
        assert sec in pkg_txt, f"mooswitch/package.ini missing section {sec!r}"

    for rel in (
        "switch/.packages/Alchemist/package.ini",
        "scripts/mooswitch_recovery.te",
        "config/sphaira/config.ini",
        "config/ultrahand/config.ini",
        "bootloader/nyx.ini",
    ):
        p = sd_root / rel
        assert p.is_file() and p.stat().st_size > 0, f"Missing or empty config/script: {rel}"

    sphaira_txt = (sd_root / "config/sphaira/config.ini").read_text(encoding="utf-8")
    assert not any(line.strip().startswith("theme=") for line in sphaira_txt.splitlines()), (
        "config/sphaira/config.ini must not override default Sphaira theme"
    )

    nyx_txt = (sd_root / "bootloader/nyx.ini").read_text(encoding="utf-8")
    assert "themebg=2d2d2d" in nyx_txt and "themecolor=167" in nyx_txt, (
        "bootloader/nyx.ini must use Hekate/Nyx default theme values (themebg=2d2d2d, themecolor=167)"
    )

    jc_profiles_p = sd_root / "switch/JC-color-swapper/profiles.json"
    assert jc_profiles_p.is_file() and jc_profiles_p.stat().st_size > 0, (
        "Missing or empty JC-color-swapper profiles.json"
    )
    jc_profiles = json.loads(jc_profiles_p.read_text(encoding="utf-8"))
    assert isinstance(jc_profiles, list) and len(jc_profiles) > 0, (
        "switch/JC-color-swapper/profiles.json must be a non-empty JSON list"
    )

    autoinit_p = sd_root / "emuMMC/COW0/cow_autoinit"
    assert autoinit_p.is_file(), "Missing first-boot seed emuMMC/COW0/cow_autoinit"
    auto_txt = autoinit_p.read_text(encoding="utf-8")
    for kv in ("version=2", "thin=1", "size_mb=auto", "max_size_mb=auto", "sd_reserve_mb=512"):
        assert kv in auto_txt, f"emuMMC/COW0/cow_autoinit missing {kv!r}"
    assert not (sd_root / "emuMMC/emummc.ini").exists(), "Pack must never ship emuMMC/emummc.ini"

    gh_json_path = sd_root / "config/sphaira/github/mooswitch.json"
    assert gh_json_path.is_file(), "Missing Sphaira GitHub updater config config/sphaira/github/mooswitch.json"
    gh_cfg = json.loads(gh_json_path.read_text(encoding="utf-8"))
    assert gh_cfg.get("name") == "MooSwitch", f"Unexpected Sphaira GitHub entry name: {gh_cfg.get('name')!r}"
    assert gh_cfg.get("url") == "https://github.com/terminada/mooswitch", f"Unexpected Sphaira GitHub URL: {gh_cfg.get('url')!r}"
    assert gh_cfg.get("tag") == "latest", f"Sphaira GitHub updater must track stable releases ('latest'), got {gh_cfg.get('tag')!r}"
    assets = gh_cfg.get("assets", [])
    assert len(assets) == 2, f"Expected 2 asset filters in mooswitch.json, found {len(assets)}"
    assert assets[0].get("name") == "-update.zip" and assets[0].get("path") == "/"
    assert assets[1].get("name") == ".zip" and assets[1].get("path") == "/"
    for idx, asset in enumerate(assets):
        assert asset.get("pre_install_message", "").strip(), f"mooswitch.json assets[{idx}] missing pre_install_message"
        assert asset.get("post_install_message", "").strip(), f"mooswitch.json assets[{idx}] missing post_install_message"

    # Simulate Sphaira ghdl.cpp substring asset filter order (`find` + first-match break)
    def _match_sphaira_asset(fname: str) -> str | None:
        matched = next((a for a in assets if a["name"] in fname), None)
        return matched["name"] if matched else None

    assert _match_sphaira_asset("mooswitch-2026.11.22-update.zip") == "-update.zip"
    assert _match_sphaira_asset("mooswitch-2026.11.22.zip") == ".zip"
    for non_zip in ("SHA256SUMS.txt", "pack-manifest.json", "lock-checksums.json"):
        assert _match_sphaira_asset(non_zip) is None, f"{non_zip} must not match Sphaira GitHub asset filters"


def verify_install_pack_self_test(sd_root: Path, zip_path: Path | None) -> None:
    src_sd = None if zip_path is not None else sd_root
    with tempfile.TemporaryDirectory(prefix="mooswitch_install_test_") as tmp:
        sim_sd = Path(tmp)

        # 1. Seed protected user data, customized guard.ini, stale custom theme, stale reports & obsolete file
        cow_arch = sim_sd / "emuMMC/COW0/eMMC/00"
        cow_arch.parent.mkdir(parents=True, exist_ok=True)
        cow_arch.write_bytes(b"COW_USER_DATA")

        nint_file = sim_sd / "Nintendo/Contents/dummy.nca"
        nint_file.parent.mkdir(parents=True, exist_ok=True)
        nint_file.write_bytes(b"USER_NINTENDO_DATA")

        custom_guard = "[guard]\nenabled=0\nled_alerts=0\npoll_interval_sec=60\n"
        guard_p = sim_sd / "config/cowmmc/guard.ini"
        guard_p.parent.mkdir(parents=True, exist_ok=True)
        guard_p.write_text(custom_guard, encoding="utf-8")

        theme_file = sim_sd / "atmosphere/contents/0100000000001000/romfs/lyt/common.szs"
        theme_file.parent.mkdir(parents=True, exist_ok=True)
        theme_file.write_bytes(b"STALE_THEME_SZS")

        erpt_file = sim_sd / "atmosphere/erpt_reports/old.erpt"
        erpt_file.parent.mkdir(parents=True, exist_ok=True)
        erpt_file.write_bytes(b"ERPT")
        fatal_file = sim_sd / "atmosphere/fatal_reports/err.bin"
        fatal_file.parent.mkdir(parents=True, exist_ok=True)
        fatal_file.write_bytes(b"FATAL")

        obsolete_file = sim_sd / "switch/obsolete_tool/obsolete_tool.nro"
        obsolete_file.parent.mkdir(parents=True, exist_ok=True)
        obsolete_file.write_bytes(b"OLD_TOOL")
        old_manifest = {
            "format": "mooswitch-manifest-v1",
            "version": "0.0.9",
            "files": [
                {"path": "switch/obsolete_tool/obsolete_tool.nro", "size": 8, "sha256": sha256_bytes(b"OLD_TOOL")},
            ],
        }
        (sim_sd / "pack-manifest.json").write_text(json.dumps(old_manifest), encoding="utf-8")

        # 2. Run manifest-aware installer
        res = install_pack(sim_sd, sd_root=src_sd, zip_path=zip_path, overwrite_configs=False, keep_themes=False)

        assert cow_arch.read_bytes() == b"COW_USER_DATA", "Installer modified emuMMC user data!"
        assert nint_file.read_bytes() == b"USER_NINTENDO_DATA", "Installer modified Nintendo user data!"
        assert guard_p.read_text(encoding="utf-8") == custom_guard, "User guard.ini was not preserved!"
        assert "config/cowmmc/guard.ini" in res["preserved"]
        assert not (sim_sd / "atmosphere/contents/0100000000001000").exists(), "Stale Home Menu theme not quarantined"
        q_theme = sim_sd / "config/cowmmc/quarantined_themes/0100000000001000/romfs/lyt/common.szs"
        assert q_theme.is_file() and q_theme.read_bytes() == b"STALE_THEME_SZS", "Quarantined theme copy missing"
        assert not (sim_sd / "atmosphere/erpt_reports").exists(), "erpt_reports not purged"
        assert not (sim_sd / "atmosphere/fatal_reports").exists(), "fatal_reports not purged"
        assert not obsolete_file.exists(), "Obsolete file from old manifest was not removed"
        assert "switch/obsolete_tool/obsolete_tool.nro" in res["removed_obsolete"]
        assert (sim_sd / "switch/.overlays/ovlSysmodules.ovl").is_file()
        assert (sim_sd / "switch/.packages/mooswitch/package.ini").is_file()
        assert (sim_sd / "pack-manifest.json").is_file()
        assert "emuMMC/COW0/cow_autoinit" in res["skipped_seeds"]
        assert not (sim_sd / "emuMMC/COW0/cow_autoinit").exists(), "Existing COW0 SD must skip cow_autoinit seed"

        # 3. Verify --overwrite-configs updates guard.ini cleanly without clobbering existing COW0 seed skip
        res_ov = install_pack(sim_sd, sd_root=src_sd, zip_path=zip_path, overwrite_configs=True, keep_themes=True)
        assert not res_ov["preserved"]
        assert guard_p.read_bytes() == (sd_root / "config/cowmmc/guard.ini").read_bytes()
        assert "emuMMC/COW0/cow_autoinit" in res_ov["skipped_seeds"]
        assert not (sim_sd / "emuMMC/COW0/cow_autoinit").exists()

    # 4. Verify fresh SD install seeds emuMMC/COW0/cow_autoinit for zero-touch first-boot CFW
    with tempfile.TemporaryDirectory(prefix="mooswitch_fresh_install_test_") as fresh_tmp:
        fresh_sd = Path(fresh_tmp)
        fresh_res = install_pack(fresh_sd, sd_root=src_sd, zip_path=zip_path, overwrite_configs=False, keep_themes=False)
        assert not fresh_res["skipped_seeds"], f"Fresh SD should install seed files, skipped: {fresh_res['skipped_seeds']}"
        assert (fresh_sd / "emuMMC/COW0/cow_autoinit").read_bytes() == (sd_root / "emuMMC/COW0/cow_autoinit").read_bytes()


def verify_manifest_and_zip(sd_root: Path, zip_path: Path | None) -> int:
    manifest_path = sd_root / "pack-manifest.json"
    assert manifest_path.is_file(), "Missing pack-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    recorded: dict[str, dict] = {}
    for entry in manifest["files"]:
        rel = entry["path"]
        assert rel not in recorded, f"Duplicate path in pack-manifest.json: {rel}"
        p = sd_root / rel
        assert p.is_file(), f"Manifest references missing file: {rel}"
        data = p.read_bytes()
        assert len(data) == entry["size"], f"Size mismatch in manifest for {rel}"
        assert sha256_bytes(data) == entry["sha256"], f"SHA-256 mismatch in manifest for {rel}"
        recorded[rel] = entry

    disk_files = {
        p.relative_to(sd_root).as_posix()
        for p in sd_root.rglob("*")
        if p.is_file() and p != manifest_path
    }
    orphans = sorted(disk_files - set(recorded))
    assert not orphans, f"Orphan SD files missing from pack-manifest.json: {orphans}"
    assert manifest["file_count"] == len(recorded)

    if zip_path is not None:
        assert zip_path.is_file(), f"Missing ZIP archive: {zip_path}"
        with zipfile.ZipFile(zip_path, "r") as zf:
            bad = zf.testzip()
            assert bad is None, f"Corrupt member in ZIP archive: {bad}"
            zip_members = set(zf.namelist())
            expected_all = disk_files | {"pack-manifest.json"}
            assert zip_members == expected_all, (
                f"ZIP/SD mismatch: extra={sorted(zip_members - expected_all)}, "
                f"missing={sorted(expected_all - zip_members)}"
            )
            for rel in expected_all:
                assert zf.read(rel) == (sd_root / rel).read_bytes(), f"ZIP payload mismatch for {rel}"

    return len(recorded)


def verify_update_zip(sd_root: Path, update_zip_path: Path) -> tuple[int, int]:
    assert update_zip_path.is_file(), f"Missing Update ZIP archive: {update_zip_path}"
    manifest_path = sd_root / "pack-manifest.json"
    disk_files = {
        p.relative_to(sd_root).as_posix()
        for p in sd_root.rglob("*")
        if p.is_file() and p != manifest_path
    }
    expected_included = {p for p in disk_files if not is_excluded_from_update_pack(p)}
    expected_excluded = disk_files - expected_included

    missing_excl = sorted(UPDATE_PACK_EXCLUDED_PATHS - expected_excluded)
    assert not missing_excl, f"UPDATE_PACK_EXCLUDED_PATHS missing from sd_root: {missing_excl}"
    assert any(p.endswith("/flags/boot2.flag") and p.startswith("atmosphere/contents/") for p in expected_excluded), (
        "Expected at least one sysmodule boot2.flag to be excluded from update ZIP"
    )

    with zipfile.ZipFile(update_zip_path, "r") as zf:
        bad = zf.testzip()
        assert bad is None, f"Corrupt member in Update ZIP archive: {bad}"
        zip_members = set(zf.namelist())
        expected_members = expected_included | {"pack-manifest.json"}
        assert zip_members == expected_members, (
            f"Update ZIP mismatch: extra={sorted(zip_members - expected_members)}, "
            f"missing={sorted(expected_members - zip_members)}"
        )
        for rel in expected_included:
            assert zf.read(rel) == (sd_root / rel).read_bytes(), f"Update ZIP payload mismatch for {rel}"

        update_manifest = json.loads(zf.read("pack-manifest.json").decode("utf-8"))
        assert update_manifest.get("variant") == "update", (
            f"Update ZIP manifest variant must be 'update', got {update_manifest.get('variant')!r}"
        )
        assert update_manifest.get("file_count") == len(expected_included), (
            f"Update manifest file_count {update_manifest.get('file_count')} != {len(expected_included)}"
        )
        assert set(update_manifest.get("excluded_paths", [])) == expected_excluded, (
            "Update manifest excluded_paths mismatch"
        )

        recorded: dict[str, dict] = {}
        for entry in update_manifest.get("files", []):
            rel = entry["path"]
            assert rel not in recorded, f"Duplicate path in update pack-manifest.json: {rel}"
            assert rel in expected_included, f"Unexpected path in update pack-manifest.json: {rel}"
            data = (sd_root / rel).read_bytes()
            assert len(data) == entry["size"], f"Size mismatch in update manifest for {rel}"
            assert sha256_bytes(data) == entry["sha256"], f"SHA-256 mismatch in update manifest for {rel}"
            recorded[rel] = entry
        assert set(recorded) == expected_included

    with tempfile.TemporaryDirectory(prefix="mooswitch_update_guard_test_") as tmp:
        try:
            install_pack(Path(tmp), sd_root=None, zip_path=update_zip_path)
        except ValueError:
            pass
        else:
            raise AssertionError("install_pack() must reject *-update.zip archives")

    return len(expected_included), len(expected_excluded)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify staged MooSwitch (mooswitch) SD tree and release ZIPs")
    parser.add_argument("--sd-root", required=True)
    parser.add_argument("--zip", default=None)
    parser.add_argument("--update-zip", default=None)
    args = parser.parse_args()

    sd_root = Path(args.sd_root).resolve()
    zip_path = Path(args.zip).resolve() if args.zip else None
    update_zip_path = Path(args.update_zip).resolve() if args.update_zip else None

    print("=== MooSwitch Distribution Verifier ===")
    verify_bmp32(sd_root / "bootloader/bootlogo.bmp", 720, 1280, require_opaque=True, require_grayscale=True)
    for icon_name in ("icon_switch.bmp", "icon_payload.bmp"):
        assert (sd_root / "bootloader/res" / icon_name).is_file(), f"Missing default Hekate icon: {icon_name}"
    for absent_theme_rel in (
        "bootloader/res/background.bmp",
        "bootloader/res/icon_cowmmc.bmp",
        "bootloader/res/icon_semistock.bmp",
        "bootloader/res/icon_stock.bmp",
        "config/ultrahand/theme.ini",
        "config/sphaira/themes/mooswitch_theme.ini",
    ):
        assert not (sd_root / absent_theme_rel).exists(), f"Custom UI theme asset must be absent: {absent_theme_rel}"
    print("  [PASS] Minimal B&W bootlogo BMP verified (720x1280 32-bit BI_RGB) + default Hekate/Sphaira/Ultrahand themes")

    hb_title = verify_sphaira_hbmenu(sd_root)
    print(f"  [PASS] Sphaira hbmenu replacement verified (NACP={hb_title!r}, SHA-256 == switch/sphaira/sphaira.nro)")

    verify_binary_magics(sd_root)
    print("  [PASS] Bootloader parity, Horizon-OC/SaltyNX, sysmodules, 12 overlays, 11 NROs, 6 payloads, and Daybreak exclusion verified")

    abi = verify_sd_abi_and_compatibility(sd_root)
    print(
        f"  [PASS] HOS 21+ TLS 0x180 & KIP/package3 ABI verified "
        f"({abi['kips']} KIPs, {abi['sysmodules']} sysmodules/{abi['steady_sysmodule_kib']} KiB steady RAM, "
        f"{abi['overlays']} OVLs, {abi['nros']} NROs, {abi['payloads']} payloads)"
    )

    verify_configs_and_packages(sd_root)
    print("  [PASS] system_settings.ini, default UI configs, Alchemist, MooSwitch package, and recovery script verified")

    verify_hekate_ini(sd_root)
    print("  [PASS] hekate_ipl.ini targets & cowMMC safety flags verified")

    count = verify_manifest_and_zip(sd_root, zip_path)
    print(f"  [PASS] pack-manifest.json 100% SHA-256 parity ({count} files, 0 orphans)" + (" + ZIP verified" if zip_path else ""))

    verify_install_pack_self_test(sd_root, zip_path)
    print("  [PASS] install_pack.py self-test (user data + config preservation + theme quarantine + cleanup) verified")

    if update_zip_path is not None:
        upd_count, excl_count = verify_update_zip(sd_root, update_zip_path)
        print(
            f"  [PASS] Update ZIP verified ({upd_count} files, {excl_count} user config/flag/seed paths excluded, "
            "install_pack guard verified)"
        )

    print("\nALL MOOSWITCH VERIFICATION CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
