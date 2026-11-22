# MooSwitch

[![CI](https://github.com/terminada/mooswitch/actions/workflows/ci.yml/badge.svg)](https://github.com/terminada/mooswitch/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/terminada/mooswitch?include_prereleases&label=Release)](https://github.com/terminada/mooswitch/releases)
![HOS](https://img.shields.io/badge/HOS-23.5.0-black)
![Atmosphere](https://img.shields.io/badge/Atmosph%C3%A8re-1.12.2-111111)
![Hekate](https://img.shields.io/badge/Hekate-6.5.5-222222)

Opinionated, ready-to-boot Nintendo Switch CFW pack powered by **HOS_COW (`cowMMC`)**, **Sphaira**, **Ultrahand**, and **Horizon OC**.

Instead of cloning your entire 32 GB / 64 GB eMMC, **cowMMC** starts with a ~512 MiB thin copy-on-write virtual NAND on SD—keeping `sysMMC` untouched while freeing gigabytes of storage for games.

---

## Install & Upgrade

1. **Download** the latest `mooswitch-<version>.zip` from [**Releases**](https://github.com/terminada/mooswitch/releases).
2. **Copy to a FAT32 microSD card:**
   - **Fresh or manual install:** Extract the ZIP directly to the root of your microSD card (merge/overwrite existing folders; `/emuMMC` and `/Nintendo` are untouched).
   - **Safe upgrade via CLI** *(preserves user configs, quarantines stale Home Menu themes, removes obsolete files)*:
     ```bash
     python3 -B builder/install_pack.py --target /path/to/sd_card --zip dist/mooswitch-<version>.zip
     ```
3. **Boot your Switch:** Insert the microSD card and power on. First boot automatically initializes `emuMMC/COW0` and launches **CFW (cowMMC)**. Hold `VOL-` at the splash screen to open **Hekate / Nyx** (go to `cowMMC (Exp)` to manage overlays).

> [!IMPORTANT]
> **Updating Switch Firmware:** Do **not** install HOS updates inside `cowMMC` (`daybreak.nro` is intentionally excluded). Update firmware normally in **Full Stock (OFW)** or **Semi-Stock (OFW)**—on your next CFW boot, `cowMMC` automatically re-baselines to match `sysMMC` and safely quarantines incompatible Home Menu themes while keeping your SD games and saves intact.

---

## Boot Menu & Controls

| Entry / Combo | Action |
| :--- | :--- |
| **`CFW (cowMMC)`** | Auto-boots after 3s into `cowMMC` (`emuMMC` forced, `ams_mitm` + `Horizon OC` active, `[CFW (sysMMC)]` omitted for safety) |
| **`Semi-Stock (OFW)`** | Clean `sysMMC` boot via `package3` without CFW sysmodules or fuse burn |
| **`Full Stock (OFW)`** | Pure hardware Nintendo bootloader |
| **`VOL-`** *(at boot)* | Enter Hekate / Nyx (`cowMMC (Exp)`, payloads, tools) |
| **`Album`** | Launch **Sphaira** (built-in game installer, MTP/FTP server, App Store, Themezer browser, file manager) |
| **`Hold R` + Title** | Launch **Sphaira** in full-RAM mode *(recommended for heavy homebrew)* |
| **`Hold R` + Album** | Open the stock Switch Album viewer |
| **`L + D-Pad Down + Right Stick`** | Open **Ultrahand** overlay menu (sysmodule toggles, Horizon OC, FPSLocker, cowMMC status, recovery actions) |

---

<details>
<summary><strong>What's Included</strong></summary>

| Category | Components |
| :--- | :--- |
| **Core & Boot** | Atmosphère `1.12.2`, Hekate `6.5.5` + Nyx `1.9.5`, `cowmmc-core` (`libsys_cow.bso`, `emummc.kipm`, `ams_mitm.kip`, `sys-cowguard`, `cowmmc.ovl`, `cow_manager.nro`), `sys-patch` `v1.6.3.0`, modchip root chain (`boot.dat`, `boot.ini`, `payload.bin`), 90DNS `emummc.txt`, telemetry/sleep-drain tuning, WebApplet CA & remap dialog patches |
| **Performance & OC** | Horizon OC `2.6.1` (`hoc.kip`, patched `exosphere.bin`, `hoc-clk`, `Benchmark-Toolbox.nro`), SaltyNX `2.1.4`, FPSLocker `3.5.1`, ReverseNX-RT `2.2.1`, Status Monitor `v1.4.1+r5` |
| **Homebrew & Overlays** | Sphaira `1.1.1` (replaces `hbmenu.nro`), Ultrahand `v2.5.4`, `ovlSysmodules` `v1.5.3`, QuickNTP `1.6.0+r6`, EdiZon Overlay `v1.0.15`, Alchemist `v1.0.6`, MooSwitch Tools package, JKSV, Linkalho (`impeeza/linkalho` `v2.0.2`), Switch 90DNS Tester `v1.1.0`, NXThemesInstaller `nxt-3.0.2`, `cow_manager.nro` |
| **Sysmodules** | **Enabled at boot:** `sys-cowguard`, `nx-ovlloader`, `sys-patch`, `hoc-clk`, `SaltyNX`, `MissionControl`, `sys-con`<br>**Installed disabled (toggle in `ovlSysmodules`):** `emuiibo`, `ldn_mitm` |
| **Payloads & Scripts** | `Lockpick_RCM.bin`, `TegraExplorer.bin`, `CommonProblemResolver.bin`, `picofly_toolbox_0.2.bin`, `hwfly_toolbox.bin`, `Instinct-NX_toolbox.bin`, `scripts/mooswitch_recovery.te` |

</details>

<details>
<summary><strong>Building from Source & Customizing</strong></summary>

The open-source `cowMMC` bootloader and `emummc`/`ams_mitm` forks live at [`terminada/hekatomb`](https://github.com/terminada/hekatomb) and [`terminada/AtmosphereCOW`](https://github.com/terminada/AtmosphereCOW). Releases and nightlies are date-tagged (`YYYY.MM.DD` / `nightly-YYYY.MM.DD`).

1. Install Python dependencies and build + verify `dist/mooswitch-<version>.zip` against an exported `cowmmc-core` directory or `cowmmc-core.tar.gz`:
   ```bash
   pip install -r requirements.txt
   python3 -B builder/build_pack.py --cowmmc-core /path/to/cowmmc-core.tar.gz
   python3 -B builder/verify_pack.py --sd-root dist/sd_root --zip dist/mooswitch-<version>.zip
   ```
   *(Subsequent offline rebuilds reuse `.cache/` with `--offline`; pass `--version <tag>` to stamp a release or nightly tag.)*

- **Custom bootlogo:** Drop a `1280x720` or `720x1280` PNG at `branding/custom/bootlogo.png` or edit [`pack.json`](./pack.json) (`brand_title`, `brand_subtitle`).
- **Pin or swap components:** Edit [`components.json`](./components.json) or run `python3 -B builder/update_components.py --dry-run`. All staged files are SHA-256 tracked in `pack-manifest.json`.

</details>

---

## Credits

Built with upstream work by **SciresM** (Atmosphère), **CTCaer** (Hekate/Nyx), **ITotalJustice** (Sphaira), **ppkantorski** & **WerWolv** (Ultrahand/ovl-sysmodules/QuickNTP/Alchemist), **Horizon-OC**, **masagrator** (SaltyNX/FPSLocker/ReverseNX-RT/Status Monitor), **impeeza** (`sys-patch` / Linkalho), **ndeadly** (MissionControl), **o0Zz** (`sys-con`), **XorTroll** (emuiibo), **spacemeowx2** (`ldn_mitm`), **J-D-K** (JKSV), **exelix11** (NXThemesInstaller), **meganukebmp** (Switch 90DNS Tester), **proferabg** (EdiZon-Overlay), **suchmememanyskill** (TegraExplorer), **THZoria** (Lockpick_RCM), and **Team-Neptune** (CommonProblemResolver).

Pack architecture and safety workflows draw inspiration from **rashevskyv** (KEFIR / Kefirosphere), **lemon07r** (StarterNX), and **sthetix** (HATS).
