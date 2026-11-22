#!/usr/bin/env python3
"""
Automated Switch ABI and compatibility checker for MooSwitch builds and upstream updates.

Verifies:
  1. HOS 21.0.0–23.5.0 / Atmosphere 1.10.0+ libnx >= 4.10.0 TLS ABI (`tpidrro_el0 + 0x180`,
     no legacy pre-HOS-21 `0x108` ThreadVars) across all NROs, overlays, and libnx sysmodules.
  2. Sysmodule `exefs.nsp` headers (`PFS0`, 64-bit `META`, Title ID parity), zero TID collisions,
     and auto-started steady-state memory headroom inside Atmosphere's 16 MiB pool.
  3. Overlay `.ovl` static memory span within `nx-ovlloader`'s 6 MiB HOS 23.0.0+ heap.
  4. `package3` (`FSS0`) Exosphere / KIP1 compatibility with Horizon-OC and `ams_mitm.kip`.
  5. Hekate payload IRAM size limits (`<= 126,296` bytes).
"""

from __future__ import annotations

import argparse
import io
import os
import struct
import zipfile
from pathlib import Path

IRAM_PAYLOAD_MAX = 126296
OVERLAY_MEM_MAX = int(4.5 * 1024 * 1024)
STEADY_SYSMODULE_MEM_MAX = int(4.5 * 1024 * 1024)
BOOT_EXIT_SYSMODULE_TIDS = frozenset({"420000000000000B"})  # sys-patch exits immediately after boot

EXPECTED_KIP_TIDS = {
    "ams_mitm.kip": 0x010041544D530000,
    "hoc.kip": 0x0100000000000001,
}


def lz4_block_decompress(src: bytes, expected_len: int) -> bytes:
    out = bytearray()
    i, n = 0, len(src)
    while i < n and len(out) < expected_len:
        token = src[i]
        i += 1
        lit_len = token >> 4
        if lit_len == 15:
            while i < n:
                b = src[i]
                i += 1
                lit_len += b
                if b != 255:
                    break
        out.extend(src[i : i + lit_len])
        i += lit_len
        if i >= n or len(out) >= expected_len:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset <= 0 or offset > len(out):
            raise ValueError(f"Invalid LZ4 backreference offset {offset}")
        match_len = (token & 0xF) + 4
        if (token & 0xF) == 15:
            while i < n:
                b = src[i]
                i += 1
                match_len += b
                if b != 255:
                    break
        start = len(out) - offset
        for k in range(match_len):
            out.append(out[start + k])
    return bytes(out)


def scan_arm64_tls_offsets(text_bytes: bytes) -> set[int]:
    """Return immediate byte offsets in 0x080..0x1FF accessed from `tpidrro_el0`."""
    offs: set[int] = set()
    usable = len(text_bytes) - (len(text_bytes) % 4)
    if usable <= 0:
        return offs
    insns = struct.unpack_from(f"<{usable // 4}I", text_bytes, 0)
    for i, insn in enumerate(insns):
        if (insn & 0xFFFFFFE0) != 0xD53BD060:  # mrs xRd, tpidrro_el0
            continue
        rd = insn & 0x1F
        for nxt in insns[i + 1 : i + 8]:
            if (nxt & 0xFFC003E0) == (0x91000000 | (rd << 5)):  # add xD, xRd, #imm12
                imm = (nxt >> 10) & 0xFFF
                if 0x080 <= imm <= 0x1FF:
                    offs.add(imm)
            if (nxt & 0x3FC003E0) in ((0x39400000 | (rd << 5)), (0x39000000 | (rd << 5))):
                size = (nxt >> 30) & 3
                imm = ((nxt >> 10) & 0xFFF) << size
                if 0x080 <= imm <= 0x1FF:
                    offs.add(imm)
    return offs


def check_nro_abi(data: bytes, label: str, is_overlay: bool = False) -> dict:
    if len(data) < 0x50 or data[0x10:0x14] != b"NRO0":
        raise AssertionError(f"{label}: invalid or truncated NRO0 header")
    mod_off = struct.unpack_from("<I", data, 4)[0]
    if mod_off + 4 > len(data) or data[mod_off : mod_off + 4] != b"MOD0":
        raise AssertionError(f"{label}: missing MOD0 header")

    t_off, t_sz, _r_off, _r_sz, d_off, d_sz, bss_sz = struct.unpack_from("<7I", data, 0x20)
    if t_off + t_sz > len(data):
        raise AssertionError(f"{label}: truncated .text section")
    mem_span = d_off + d_sz + bss_sz
    if is_overlay and mem_span > OVERLAY_MEM_MAX:
        raise AssertionError(
            f"{label}: overlay memory span {mem_span / 1024:.1f} KiB exceeds {OVERLAY_MEM_MAX / 1024:.1f} KiB limit"
        )

    offs = scan_arm64_tls_offsets(data[t_off : t_off + t_sz])
    if 0x180 not in offs or 0x108 in offs:
        fmt_offs = [hex(x) for x in sorted(offs)]
        raise AssertionError(
            f"{label}: fails HOS 21+ libnx>=4.10.0 TLS ABI check (tpidrro_el0 offsets={fmt_offs}; "
            "requires 0x180 and no legacy 0x108)"
        )
    return {"label": label, "mem_span": mem_span, "tls_offsets": sorted(offs)}


def check_exefs_nsp_abi(data: bytes, expected_tid: int | None, label: str) -> dict:
    if len(data) < 16 or data[:4] != b"PFS0":
        raise AssertionError(f"{label}: missing PFS0 header")
    n_files, str_sz = struct.unpack_from("<II", data, 4)
    str_base = 16 + n_files * 24
    data_base = str_base + str_sz
    if data_base > len(data):
        raise AssertionError(f"{label}: truncated PFS0 directory")

    entries: dict[str, bytes] = {}
    for i in range(n_files):
        off, sz, soff, _ = struct.unpack_from("<QQII", data, 16 + i * 24)
        end = data.find(b"\x00", str_base + soff, data_base)
        if end < 0:
            raise AssertionError(f"{label}: malformed PFS0 string table")
        fname = data[str_base + soff : end].decode("utf-8", errors="ignore")
        entries[fname] = data[data_base + off : data_base + off + sz]

    npdm = entries.get("main.npdm", b"")
    main_nso = entries.get("main", b"")
    if len(npdm) < 0x80 or npdm[:4] != b"META":
        raise AssertionError(f"{label}: missing or truncated main.npdm (META)")
    meta_flags = npdm[0x0C]
    if not (meta_flags & 1):
        raise AssertionError(f"{label}: main.npdm must be 64-bit")

    pname = npdm[0x20:0x30].split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
    aci0_off = struct.unpack_from("<I", npdm, 0x70)[0]
    if aci0_off + 0x18 > len(npdm):
        raise AssertionError(f"{label}: invalid ACI0 offset in main.npdm")
    aci_tid = struct.unpack_from("<Q", npdm, aci0_off + 0x10)[0]
    if expected_tid is not None and aci_tid != expected_tid:
        raise AssertionError(f"{label}: ACI0 TID {aci_tid:016X} != folder TID {expected_tid:016X}")

    if len(main_nso) < 0x100 or main_nso[:4] != b"NSO0":
        raise AssertionError(f"{label}: missing or truncated main NSO0")
    nso_flags = struct.unpack_from("<I", main_nso, 0x0C)[0]
    t_off, _t_loc, t_sz = struct.unpack_from("<III", main_nso, 0x10)
    d_off_mem, d_sz, bss_sz = (
        struct.unpack_from("<I", main_nso, 0x34)[0],
        struct.unpack_from("<I", main_nso, 0x38)[0],
        struct.unpack_from("<I", main_nso, 0x3C)[0],
    )
    nso_mem = d_off_mem + d_sz + bss_sz
    t_comp_sz = struct.unpack_from("<I", main_nso, 0x60)[0]
    raw_text = main_nso[t_off : t_off + t_comp_sz]
    text_bytes = lz4_block_decompress(raw_text, t_sz) if (nso_flags & 1) else raw_text

    offs = scan_arm64_tls_offsets(text_bytes)
    has_threadvars = any(0x1E0 <= x <= 0x1F8 for x in offs)
    if has_threadvars and (0x180 not in offs or 0x108 in offs):
        fmt_offs = [hex(x) for x in sorted(offs)]
        raise AssertionError(
            f"{label}: sysmodule fails HOS 21+ libnx TLS ABI check (tpidrro_el0 offsets={fmt_offs})"
        )

    return {
        "label": label,
        "name": pname,
        "tid": aci_tid,
        "nso_mem": nso_mem,
        "tls_offsets": sorted(offs),
    }


def check_kip1_header(data: bytes, label: str) -> dict:
    if len(data) < 0x100 or data[:4] != b"KIP1":
        raise AssertionError(f"{label}: invalid KIP1 header")
    name_raw, tid = struct.unpack_from("<12sQ", data, 4)
    name = name_raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore")
    bss_off, bss_sz = struct.unpack_from("<II", data, 0x50)
    return {"label": label, "name": name, "tid": tid, "img_span": bss_off + bss_sz}


def parse_package3(pk3_bytes: bytes) -> dict:
    if len(pk3_bytes) < 0x100 or pk3_bytes[:4] != b"PK31":
        raise AssertionError("atmosphere/package3 missing PK31 magic")
    meta_off = struct.unpack_from("<I", pk3_bytes, 4)[0]
    if meta_off + 0x20 > len(pk3_bytes):
        raise AssertionError("atmosphere/package3 invalid meta header offset")
    magic, size, _crt0, cnt_off, cnt_count, hos_ver, ams_ver, git_rev = struct.unpack_from("<8I", pk3_bytes, meta_off)
    if magic != 0x30535346:
        raise AssertionError(f"atmosphere/package3 invalid FSS0 meta magic: {hex(magic)}")

    exo_bytes = b""
    kips: dict[int, dict] = {}
    for i in range(cnt_count):
        off, sz, ctype, flags0, _f1, _f2, _rsvd, raw_name = struct.unpack_from(
            "<IIBBBBI16s", pk3_bytes, cnt_off + i * 0x20
        )
        if flags0 & 1:  # CNT_FLAG0_EXPERIMENTAL
            continue
        if off + sz > size or off + sz > len(pk3_bytes):
            continue
        blob = pk3_bytes[off : off + sz]
        if ctype == 1:
            exo_bytes = blob
        elif ctype == 6 and blob[:4] == b"KIP1":
            info = check_kip1_header(blob, f"package3[{i}]")
            kips[info["tid"]] = info
    return {"hos_ver": hos_ver, "ams_ver": ams_ver, "git_rev": git_rev, "exo_bytes": exo_bytes, "kips": kips}


def _expected_tid_from_rel(rel_posix: str) -> int | None:
    parts = Path(rel_posix).parts
    if len(parts) >= 2:
        parent = parts[-2]
        if len(parent) == 16:
            try:
                return int(parent, 16)
            except ValueError:
                return None
    return None


def check_asset_abi(data: bytes, filename: str) -> list[str]:
    """Inspect a candidate release file (.nro/.ovl/.kip/.bin/.zip) and return ABI violations."""
    errors: list[str] = []
    low = filename.lower()

    def _inspect_member(rel: str, blob: bytes) -> None:
        rlow = rel.lower()
        try:
            if rlow.endswith(".nro"):
                check_nro_abi(blob, rel, is_overlay=False)
            elif rlow.endswith(".ovl"):
                check_nro_abi(blob, rel, is_overlay=True)
            elif os.path.basename(rlow) == "exefs.nsp":
                check_exefs_nsp_abi(blob, _expected_tid_from_rel(rel), rel)
            elif rlow.endswith(".kip"):
                check_kip1_header(blob, rel)
            elif rlow.endswith(".bin") and (rel == filename or "bootloader/payloads/" in rlow):
                if len(blob) == 0 or len(blob) > IRAM_PAYLOAD_MAX:
                    raise AssertionError(f"{rel}: payload size {len(blob)} outside 1..{IRAM_PAYLOAD_MAX}")
        except AssertionError as exc:
            errors.append(str(exc))

    if low.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
                for zi in zf.infolist():
                    if zi.is_dir():
                        continue
                    rel = zi.filename.replace("\\", "/").lstrip("/")
                    if rel.lower().endswith((".nro", ".ovl", ".kip", ".bin")) or os.path.basename(rel).lower() == "exefs.nsp":
                        _inspect_member(rel, zf.read(zi))
        except zipfile.BadZipFile as exc:
            errors.append(f"{filename}: corrupt zip ({exc})")
    else:
        _inspect_member(filename, data)
    return errors


def verify_sd_abi_and_compatibility(sd_root: Path) -> dict:
    # 1. package3 vs Horizon-OC exosphere.bin & external KIPs
    pk3 = parse_package3((sd_root / "atmosphere/package3").read_bytes())
    exo = (sd_root / "atmosphere/exosphere.bin").read_bytes()
    pk3_exo = pk3["exo_bytes"]
    assert pk3_exo, "atmosphere/package3 missing Exosphere (CNT_TYPE_EXO)"
    assert exo[:256] == pk3_exo[:256], "atmosphere/exosphere.bin head differs from package3 Exosphere"
    assert abs(len(exo) - len(pk3_exo)) <= 4096, (
        f"atmosphere/exosphere.bin size {len(exo)} diverges from package3 Exosphere {len(pk3_exo)}"
    )

    seen_tids: dict[int, str] = {}
    kip_count = 0
    for kip in sorted((sd_root / "atmosphere/kips").glob("*.kip")):
        info = check_kip1_header(kip.read_bytes(), kip.relative_to(sd_root).as_posix())
        kip_count += 1
        tid = info["tid"]
        expected = EXPECTED_KIP_TIDS.get(kip.name)
        if expected is not None:
            assert tid == expected, f"{kip.name}: TID {tid:016X} != {expected:016X}"
        assert tid in pk3["kips"], f"{kip.name}: TID {tid:016X} not found in atmosphere/package3 KIPs"
        assert tid not in seen_tids, f"Duplicate KIP TID {tid:016X}: {seen_tids[tid]} vs {info['label']}"
        seen_tids[tid] = info["label"]

    # 2. All sysmodules (TID uniqueness, 64-bit META, HOS 21+ TLS ABI, steady-state RAM)
    steady_mem = 0
    enabled_count = 0
    sysmodule_count = 0
    for nsp in sorted((sd_root / "atmosphere/contents").glob("*/exefs.nsp")):
        tid_dir = nsp.parent.name
        expected_tid = int(tid_dir, 16)
        rel = nsp.relative_to(sd_root).as_posix()
        info = check_exefs_nsp_abi(nsp.read_bytes(), expected_tid, rel)
        sysmodule_count += 1
        assert expected_tid not in seen_tids, f"Duplicate Program ID {expected_tid:016X}: {seen_tids[expected_tid]} vs {rel}"
        seen_tids[expected_tid] = rel
        if (nsp.parent / "flags/boot2.flag").is_file():
            enabled_count += 1
            if tid_dir.upper() not in BOOT_EXIT_SYSMODULE_TIDS:
                steady_mem += info["nso_mem"]

    assert steady_mem <= STEADY_SYSMODULE_MEM_MAX, (
        f"Auto-started steady-state sysmodules static NSO RAM {steady_mem / 1024:.1f} KiB "
        f"exceeds {STEADY_SYSMODULE_MEM_MAX / 1024:.1f} KiB safe ceiling for 16 MiB pool"
    )

    # 3. All Overlays & Homebrew NROs
    ovl_count = 0
    for ovl in sorted((sd_root / "switch/.overlays").glob("*.ovl")):
        check_nro_abi(ovl.read_bytes(), ovl.relative_to(sd_root).as_posix(), is_overlay=True)
        ovl_count += 1

    nro_paths = sorted(list((sd_root / "switch").rglob("*.nro")) + [sd_root / "hbmenu.nro"])
    nro_count = 0
    for nro in nro_paths:
        if nro.is_file():
            check_nro_abi(nro.read_bytes(), nro.relative_to(sd_root).as_posix(), is_overlay=False)
            nro_count += 1

    # 4. Pre-boot payload IRAM size limit
    payload_count = 0
    for pl in sorted((sd_root / "bootloader/payloads").glob("*.bin")):
        sz = pl.stat().st_size
        assert 0 < sz <= IRAM_PAYLOAD_MAX, f"{pl.name}: size {sz} outside 1..{IRAM_PAYLOAD_MAX}"
        payload_count += 1

    return {
        "kips": kip_count,
        "sysmodules": sysmodule_count,
        "enabled_sysmodules": enabled_count,
        "steady_sysmodule_kib": round(steady_mem / 1024.0, 1),
        "overlays": ovl_count,
        "nros": nro_count,
        "payloads": payload_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify HOS 23.5 / AMS 1.12.2 / HOS 21+ TLS ABI compatibility on SD root")
    parser.add_argument("--sd-root", default=str(Path(__file__).resolve().parent.parent / "dist" / "sd_root"))
    args = parser.parse_args()

    res = verify_sd_abi_and_compatibility(Path(args.sd_root).resolve())
    print(
        f"[PASS] ABI/compat verified: {res['kips']} KIPs + exosphere, "
        f"{res['sysmodules']} sysmodules ({res['enabled_sysmodules']} enabled, {res['steady_sysmodule_kib']} KiB steady RAM), "
        f"{res['overlays']} overlays, {res['nros']} NROs, {res['payloads']} payloads (HOS 21+ TLS 0x180)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
