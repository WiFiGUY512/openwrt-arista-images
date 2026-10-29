# Status

Generated 2026-10-29 22:01 UTC from the releases, the field updater, the OpenWrt pull
requests and the port's bug ledger. Every build here is OpenWrt main
(Linux 6.18) with this port's patches (40 of them in build 62).

## At a glance

| | Progress | |
|---|---|---|
| Models with published images | `██████████` 100% | 5 of 5: C-230, C-260, C-360, W-318, W-118 |
| Models with a stable (non-test) build | `████████░░` 80% | 4 of 5; the W-118 has testing builds only |
| Models the field updater can install with no console (code-ready) | `██████░░░░` 60% | 3 of 5: C-230, C-260, C-360 |
| Field installs proven on real units | `████░░░░░░` 40% | 2 of 5: C-230, C-260 |
| Device support submitted to OpenWrt | `██████████` 100% | 5 of 5 pull requests |
| Device support merged into OpenWrt | `░░░░░░░░░░` 0% | 0 of 5; all are waiting for review |
| Known issues resolved | `███░░░░░░░` 32% | 35 of 110 in the port's bug ledger ([below](#known-issues)) |

## Published builds

The build number is the middle of the tag: `v2026.10.17.1-62-ge144ddb` is build **62**, the
62nd port commit after release v2026.10.17.1, from port commit `e144ddb`.
From build 57 on, a unit names its build: `grep DISTRIB_DESCRIPTION
/etc/openwrt_release` ends in `(arista <tag>)`, as does LuCI's firmware version.

| Build | Released (UTC) | Channel | Models | What it brought |
|---|---|---|---|---|
| **[62](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-62-ge144ddb)** | 2026-10-29 | testing | C-230, C-260, C-360, W-318, W-118 | Client signal levels read true on ath11k radios (C-360, C-260, C-230, W-318): they read up to 7 dB low on noisy channels. |
| [61](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-61-gefd854b) | 2026-10-29 | testing, replaced by 62 | C-230, C-260, C-360, W-318, W-118 | 5 GHz keeps its speed after a channel change (C-360, C-260: it fell to a tenth after automatic channel moves). The C-360's QCN9072 offers both its streams. Images name their build. `dfs-look` works again. |
| [55](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-55-g876a7de) | 2026-10-28 | testing, replaced by 62 | C-230, C-260, C-360, W-318, W-118 | Faster client joins on networks without PMF. 5 GHz starts outside DFS channels; `dfs-look` moves it to DFS later only where that is clearly better. A disabled radio no longer changes the country. RADIUS accounting sent once. No fake noise floor from the C-360's QCN9074/QCN9072 cards. HT/VHT on those cards' 5 GHz. |
| [46](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-46-g704bd6a) | 2026-10-28 | testing, replaced by 62 | C-230, C-260, C-360, W-318, W-118 | Daemons survive a ubusd restart. airscan probes far less. A sysupgrade's final reboot is watched. First W-118 images. |
| **[42](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-42-g4949f0e)** | 2026-10-26 | **stable** (latest) | C-230, C-260, C-360, W-318 | First C-360 and W-318 images. ath11k noise-floor, beacon-stats and monitor-mode fixes. |
| [41](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-41-gb4ecbba) | 2026-10-25 | stable, older | C-230, C-260 | Installer files (RAM installer, factory image, checksums) for both. |
| [40](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-40-g2a240ea) | 2026-10-25 | stable, older | C-230, C-260 | First build on OpenWrt main (sysupgrade images only). |

## By model

| Model | OpenWrt target | Stable build | Testing build | Field updater | OpenWrt pull request |
|---|---|---|---|---|---|
| C-230 | qualcommax/ipq807x | 42 | 62 | install and revert, proven | [#26136](https://github.com/openwrt/openwrt/pull/26136) (open) |
| C-260 | qualcommax/ipq807x | 42 | 62 | install and revert, proven | [#25937](https://github.com/openwrt/openwrt/pull/25937) (open) |
| C-360 | qualcommax/ipq807x | 42 | 62 | install and revert, code-ready, not yet run on a unit | [#25941](https://github.com/openwrt/openwrt/pull/25941) (open) |
| W-318 | qualcommax/ipq60xx | 42 | 62 | images only; first install by console | [#25947](https://github.com/openwrt/openwrt/pull/25947) (open) |
| W-118 | ipq40xx/generic | none yet | 62 | images only; first install by console | [#26028](https://github.com/openwrt/openwrt/pull/26028) (open) |

## The field updater: `arista-field.py`

[`install/arista-field.py`](install/README.md), last changed 2026-10-27 (commit
`72ba036`), needs no console cable: it checks the AP, saves its whole flash,
then installs OpenWrt or puts Arista's firmware back.

- **Stable channel** (default): the newest non-test release that carries the
  AP's model. Today that is build 42.
- **Testing channel** (`--channel testing`): pre-releases too. Today that is
  build 62.
- It boots the FIT configuration it finds in the image rather than a fixed
  name, so a model whose configuration changed between OpenWrt trees (the
  C-360: `config@1` on 25.12, `config@hk09` on main) boots either.

| Model | Fetch and verify images | Back up the whole flash | Install OpenWrt, no console | Put Arista's firmware back | Run on real units |
|---|:---:|:---:|:---:|:---:|---|
| C-230 | ✅ | ✅ | ✅ | ✅ | install and revert, end to end, twice (2026-10-25) |
| C-260 | ✅ | ✅ | ✅ | ✅ | backup, install and revert (2026-10-25); install again (2026-10-27) |
| C-360 | ✅ | ✅ | ✅ | ✅ | not yet; code-ready |
| W-318 | ✅ | — | — | — | the release notes give the console install |
| W-118 | ✅ | — | — | — | the release notes give the console install |

For the W-318 and W-118 the updater downloads and verifies the images
(`--fetch w318`), then stops and prints the console steps.

## Upstream

- **Device support:** one OpenWrt pull request per model (table above).
- **Linux wireless fixes sent from this work:** ath11k monitor-mode buffer handling, 2 patches (2026-10-24); mac80211 key tailroom, the faster-joins fix in build 55 (2026-10-28); ath11k HT, VHT and every spatial stream for a QCN9074's 5 GHz band, 2 patches (2026-10-28); ath11k: stop every vdev before a channel switch starts them, the 5 GHz speed fix in build 61 (2026-10-29).

## Known issues

The port keeps a ledger of every problem these APs have had, on Arista's
firmware or on OpenWrt: drivers, radio firmware, hardware quirks, and
anything Arista worked around instead of fixing.

| | Count |
|---|---:|
| Fixed in published images | 26 |
| Closed: a hardware or firmware limit, by design, or not this port's to fix | 9 |
| Open: seen on OpenWrt, or being worked on | 52 |
| Open: suspected only, from Arista's own workarounds and public release notes, not yet seen on OpenWrt | 23 |
| **Total** | **110** |

Limits worth knowing before you install:

- **6 GHz power** (C-360, W-318): OpenWrt runs 6 GHz as Low Power Indoor,
  about 19 dBm. Arista's firmware went higher with Standard Power through AFC,
  which OpenWrt does not have.
- **QCN9074/QCN9072 radio firmware** (C-360, W-318): every public Qualcomm
  firmware stalls at start-up on these cards, so the port runs Arista's own.
  Reported upstream.
- **W-318 and W-118 first install** needs a console cable for now.
