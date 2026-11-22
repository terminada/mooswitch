#!/usr/bin/env python3
"""
Check upstream GitHub releases/commits for components.json entries and safely
bump compatible components (excluding ABI-coupled core unless --include-core is given).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BUILDER_DIR = Path(__file__).resolve().parent
PACK_ROOT = BUILDER_DIR.parent
if str(BUILDER_DIR) not in sys.path:
    sys.path.insert(0, str(BUILDER_DIR))

from abi_check import check_asset_abi  # noqa: E402

CORE_COMPONENT_IDS = frozenset({"atmosphere-base", "hekate-base", "horizon-oc"})


def _gh_json(url: str) -> dict | list:
    headers = {
        "User-Agent": "mooswitch-upstream-sync/1.0",
        "Accept": "application/vnd.github+json",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "mooswitch-upstream-sync/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _url_exists(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "mooswitch-upstream-sync/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError:
        return False


def _latest_release_for(comp: dict) -> tuple[str | None, str, dict | None]:
    repo = comp["repo"]
    rel = _gh_json(f"https://api.github.com/repos/{repo}/releases/latest")
    tag = rel.get("tag_name")
    if not tag or tag == comp["tag"]:
        return None, "up-to-date", None
    glob = comp["asset_glob"]
    matches = [a for a in rel.get("assets", []) if fnmatch.fnmatch(a["name"], glob)]
    if len(matches) != 1:
        names = [a["name"] for a in matches]
        return None, f"tag {tag} asset_glob {glob!r} matched {names}", None
    asset = matches[0]
    return tag, f"matched asset {asset['name']}", asset


def _latest_commit_for(comp: dict) -> tuple[str | None, str, dict | None]:
    repo = comp["repo"]
    commit = _gh_json(f"https://api.github.com/repos/{repo}/commits/HEAD")
    sha = commit.get("sha")
    if not sha or sha == comp["tag"]:
        return None, "up-to-date", None
    raw_url = f"https://raw.githubusercontent.com/{repo}/{sha}/{comp['raw_path']}"
    if not _url_exists(raw_url):
        return None, f"commit {sha[:12]} missing {comp['raw_path']}", None
    return sha, f"HEAD commit {sha[:12]}", {"name": Path(comp["raw_path"]).name, "browser_download_url": raw_url}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check/update components.json from upstream GitHub releases")
    parser.add_argument("--dry-run", action="store_true", help="Do not modify components.json or pack.json")
    parser.add_argument("--include-core", action="store_true", help="Also bump atmosphere-base, hekate-base, horizon-oc")
    parser.add_argument("--verify-abi", action="store_true", help="Download candidate assets and verify HOS 21+ ABI")
    parser.add_argument("--summary-out", default=None, help="Optional markdown summary output path")
    args = parser.parse_args()

    comps_path = PACK_ROOT / "components.json"
    pack_path = PACK_ROOT / "pack.json"
    comps_cfg = json.loads(comps_path.read_text(encoding="utf-8"))
    pack_cfg = json.loads(pack_path.read_text(encoding="utf-8"))

    third_party_updates: list[dict[str, str]] = []
    core_updates: list[dict[str, str]] = []
    notes: list[str] = []

    for comp in comps_cfg.get("components", []):
        cid = comp["id"]
        kind = comp.get("kind")
        if kind not in ("github_release", "github_raw_zip"):
            continue
        try:
            new_tag, detail, asset = (
                _latest_release_for(comp) if kind == "github_release" else _latest_commit_for(comp)
            )
        except Exception as exc:
            notes.append(f"- `{cid}` (`{comp['repo']}`): error `{exc}`")
            continue

        if new_tag is None:
            if detail != "up-to-date":
                notes.append(f"- `{cid}` (`{comp['repo']}`): skipped ({detail})")
            continue

        if args.verify_abi and asset and (args.include_core or cid not in CORE_COMPONENT_IDS):
            try:
                blob = _download_bytes(asset["browser_download_url"])
                violations = check_asset_abi(blob, asset["name"])
            except Exception as exc:
                notes.append(f"- `{cid}` `{new_tag}`: skipped ABI check error `{exc}`")
                continue
            if violations:
                notes.append(f"- `{cid}` `{new_tag}`: **blocked by ABI check** (`{'; '.join(violations)}`)")
                continue

        item = {"id": cid, "repo": comp["repo"], "old": comp["tag"], "new": new_tag, "detail": detail}
        if cid in CORE_COMPONENT_IDS:
            core_updates.append(item)
            if args.include_core and not args.dry_run:
                comp["tag"] = new_tag
                if cid == "atmosphere-base":
                    pack_cfg["atmosphere_version"] = new_tag.lstrip("v")
                elif cid == "hekate-base":
                    pack_cfg["hekate_version"] = new_tag.lstrip("v")
        else:
            third_party_updates.append(item)
            if not args.dry_run:
                comp["tag"] = new_tag

    if not args.dry_run and (third_party_updates or (args.include_core and core_updates)):
        comps_path.write_text(json.dumps(comps_cfg, indent=2) + "\n", encoding="utf-8")
        if args.include_core and core_updates:
            pack_path.write_text(json.dumps(pack_cfg, indent=2) + "\n", encoding="utf-8")

    lines = ["## MooSwitch Upstream Component Check", ""]
    if third_party_updates:
        lines += ["### Third-Party Updates", "| Component | Repo | Old | New |", "| :--- | :--- | :--- | :--- |"]
        for u in third_party_updates:
            lines.append(f"| `{u['id']}` | `{u['repo']}` | `{u['old'][:12]}` | `{u['new'][:12]}` |")
        lines.append("")
    else:
        lines += ["- All third-party components are up to date.", ""]

    if core_updates:
        action_word = "Updated (--include-core)" if args.include_core and not args.dry_run else "Available (requires HOS_COW fork rebase)"
        lines += [f"### Core Updates ({action_word})", "| Component | Repo | Pinned | Upstream |", "| :--- | :--- | :--- | :--- |"]
        for u in core_updates:
            lines.append(f"| `{u['id']}` | `{u['repo']}` | `{u['old']}` | `{u['new']}` |")
        lines.append("")

    if notes:
        lines += ["### Warnings", *notes, ""]

    summary = "\n".join(lines)
    print(summary)
    if args.summary_out:
        out_p = Path(args.summary_out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(summary, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
