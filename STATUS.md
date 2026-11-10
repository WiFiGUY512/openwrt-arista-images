# Status

Generated 2026-11-10 06:46 UTC from the releases, the field updater, the OpenWrt pull
requests and the port's bug ledger. Every build here is OpenWrt main
(Linux 6.18) with this port's patches (100 of them in build 157).

## At a glance

| | Progress | |
|---|---|---|
| Models with published images | `██████████` 100% | 6 of 6: C-230, C-260, C-360, W-318, W-118, C-200 |
| Models with a stable (non-test) build | `███████░░░` 67% | 4 of 6; the W-118 and C-200 have testing builds only |
| Models the field updater can install with no console (code-ready) | `█████░░░░░` 50% | 3 of 6: C-230, C-260, C-360 |
| Field installs proven on real units | `███░░░░░░░` 33% | 2 of 6: C-230, C-260 |
| Device support submitted to OpenWrt | `██████████` 100% | 6 of 6 pull requests |
| Device support merged into OpenWrt | `░░░░░░░░░░` 0% | 0 of 6; all are waiting for review |
| Known issues resolved | `████████░░` 81% | 140 of 173 in the port's bug ledger ([below](#known-issues)) |

## Published builds

The build number is the middle of the tag: `v2026.10.17.1-157-g0500cfe` is build **157**, the
157th port commit after release v2026.10.17.1, from port commit `0500cfe`.
From build 57 on, a unit names its build: `grep DISTRIB_DESCRIPTION
/etc/openwrt_release` ends in `(arista <tag>)`, as does LuCI's firmware version.

| Build | Released (UTC) | Channel | Models | What it brought |
|---|---|---|---|---|
| **[157](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-157-g0500cfe)** | 2026-11-10 | testing | C-230, C-260, C-360, W-318, W-118, C-200 | uploads use all the CPU cores; fast roams log no false error |
| [155](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-155-g9248ea3) | 2026-11-09 | testing, replaced by 157 | C-230, C-260, C-360, W-318, W-118, C-200 | radios recover from firmware crashes; 6 GHz on all four chains; DHCP behind duplicating switches; OpenWrt main of 2026-11-08 |
| [112](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-112-g9bc295a) | 2026-11-04 | testing, replaced by 157 | C-230, C-260, C-360, W-318, W-118, C-200 | Release candidate. netifd keeps an address two interfaces share (a down/up no longer strips the other's address or routes); W-318 5 GHz stops at channel 165, as its FCC grant; no IPQ60xx crypto-clock warning at boot. |
| [108](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-108-g6cc3c2f) | 2026-11-02 | testing, replaced by 157 | C-230, C-260, C-360, W-318, W-118, C-200 | A new model, the Arista C-200 (IPQ6010; first install by console); W-318 and C-200 Ethernet no longer drops about 0.5% of sent frames (a port's TX clock ran from its UNIPHY's RX clock): W-318 uplink TCP 1.07 to 2.35 Gbit/s. |
| [105](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-105-g1f899dd) | 2026-11-02 | testing, replaced by 157 | C-230, C-260, C-360, W-318, W-118 | New OpenWrt base (main of 2026-11-01, Linux 6.18.60); a VXLAN tunnel no longer vanishes when the bridge under it is rebuilt; FT roams to APs with sae_pwe set but no SAE work; 6 GHz switch_chan at 80 MHz; btmon. |
| [95](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-95-g9c8b99c) | 2026-11-01 | testing, replaced by 157 | C-230, C-260, C-360, W-318, W-118 | Fast (802.11r) roams back to an AP no longer fail about one time in twenty (hostapd answered a repeated request with new keys); C-360 Bluetooth carries its public address. |
| [93](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-93-g68fb6e8) | 2026-11-01 | testing, replaced by 157 | C-230, C-260, C-360, W-318, W-118 | Clients' TX retries are counted on the 802.11ax radios; W-118 iw shows the TX power actually sent; a protocol package installed after boot works after a network reload; uci batch is fast; four hostapd and wireless fixes. |
| [84](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-84-gc3bc51f) | 2026-11-01 | testing, replaced by 157 | C-230, C-260, C-360, W-318, W-118 | With BSS colour off, spatial reuse is forbidden outright (he_spr_sr_control = 3, as the OpenWrt pull request now does); a hand-set SRG or non-SRG bit no longer makes hostapd advertise zeroed fields. Defaults unchanged. |
| [83](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-83-g439336f) | 2026-11-01 | testing, replaced by 157 | C-230, C-260, C-360, W-318, W-118 | ucode moves to OpenWrt main's: fired uloop timers are freed and finished tasks close their pipes (long-running ucode daemons stop growing); W-118 socket receive timestamps keep their fractions. |
| [79](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-79-g1aba8ac) | 2026-10-31 | testing, replaced by 157 | C-230, C-260, C-360, W-318, W-118 | An automatic 5 GHz channel starts outside DFS with a channel list of one channel per block (as Aeolus writes), so 5 GHz serves at once instead of after a 60 s radar check; dfs-look rates such lists and can move to a better DFS block. |
| [77](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-77-g0f4b8de) | 2026-10-31 | testing, replaced by 157 | C-230, C-260, C-360, W-318, W-118 | ath11k monitor interfaces hand up clean frames (C-360 scan radio): no stray FCS, signal level back, bad-checksum frames only where asked for. |
| [76](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-76-g858405e) | 2026-10-31 | testing, replaced by 157 | C-230, C-260, C-360, W-318, W-118 | 5 GHz comes back after a configuration change on a DFS channel (hostapd skipped the radar check and the radio stayed off); hostapd and wpa_supplicant no longer crash on every stop; a forced channel switch goes where it was sent; no more "Unexpected Link measurement report" log flood; RADIUS, ucode and uclient fixes. |
| [63](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-63-g1f746ea) | 2026-10-31 | testing, replaced by 157 | C-230, C-260, C-360, W-318, W-118 | Clients keep their uplink with BSS colour off (OpenWrt sent no Spatial Reuse element then, and phones ignored the AP's weaker frames); sent upstream as openwrt/openwrt#26326. |
| [62](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-62-ge144ddb) | 2026-10-29 | testing, replaced by 157 | C-230, C-260, C-360, W-318, W-118 | Client signal levels read true on ath11k radios (C-360, C-260, C-230, W-318): they read up to 7 dB low on noisy channels. |
| [61](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-61-gefd854b) | 2026-10-29 | testing, replaced by 157 | C-230, C-260, C-360, W-318, W-118 | 5 GHz keeps its speed after a channel change (C-360, C-260: it fell to a tenth after automatic channel moves). The C-360's QCN9072 offers both its streams. Images name their build. `dfs-look` works again. |
| [55](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-55-g876a7de) | 2026-10-28 | testing, replaced by 157 | C-230, C-260, C-360, W-318, W-118 | Faster client joins on networks without PMF. 5 GHz starts outside DFS channels; `dfs-look` moves it to DFS later only where that is clearly better. A disabled radio no longer changes the country. RADIUS accounting sent once. No fake noise floor from the C-360's QCN9074/QCN9072 cards. HT/VHT on those cards' 5 GHz. |
| [46](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-46-g704bd6a) | 2026-10-28 | testing, replaced by 157 | C-230, C-260, C-360, W-318, W-118 | Daemons survive a ubusd restart. airscan probes far less. A sysupgrade's final reboot is watched. First W-118 images. |
| **[42](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-42-g4949f0e)** | 2026-10-26 | **stable** (latest) | C-230, C-260, C-360, W-318 | First C-360 and W-318 images. ath11k noise-floor, beacon-stats and monitor-mode fixes. |
| [41](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-41-gb4ecbba) | 2026-10-25 | stable, older | C-230, C-260 | Installer files (RAM installer, factory image, checksums) for both. |
| [40](https://github.com/WiFiGUY512/openwrt-arista-images/releases/tag/v2026.10.17.1-40-g2a240ea) | 2026-10-25 | stable, older | C-230, C-260 | First build on OpenWrt main (sysupgrade images only). |

## By model

| Model | OpenWrt target | Stable build | Testing build | Field updater | OpenWrt pull request |
|---|---|---|---|---|---|
| C-230 | qualcommax/ipq807x | 42 | 157 | install and revert, proven | [#26136](https://github.com/openwrt/openwrt/pull/26136) (open) |
| C-260 | qualcommax/ipq807x | 42 | 157 | install and revert, proven | [#25937](https://github.com/openwrt/openwrt/pull/25937) (open) |
| C-360 | qualcommax/ipq807x | 42 | 157 | install and revert, code-ready, not yet run on a unit | [#25941](https://github.com/openwrt/openwrt/pull/25941) (open) |
| W-318 | qualcommax/ipq60xx | 42 | 157 | images only; first install by console | [#25947](https://github.com/openwrt/openwrt/pull/25947) (open) |
| W-118 | ipq40xx/generic | none yet | 157 | images only; first install by console | [#26028](https://github.com/openwrt/openwrt/pull/26028) (open) |
| C-200 | qualcommax/ipq60xx | none yet | 157 | images only; first install by console | [#26410](https://github.com/openwrt/openwrt/pull/26410) (open) |

## The field updater: `arista-field.py`

[`install/arista-field.py`](install/README.md), last changed 2026-11-05 (commit
`a437702`), needs no console cable: it checks the AP, saves its whole flash,
then installs OpenWrt or puts Arista's firmware back.

- **Stable channel** (default): the newest non-test release that carries the
  AP's model. Today that is build 42.
- **Testing channel** (`--channel testing`): pre-releases too. Today that is
  build 157.
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
| C-200 | ✅ | — | — | — | the release notes give the console install |

For the W-318, W-118 and C-200 the updater downloads and verifies the images
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
| Fixed in published images | 100 |
| Closed: a hardware or firmware limit, by design, or not this port's to fix | 40 |
| Open: seen on OpenWrt, or being worked on | 26 |
| Open: suspected only, from Arista's own workarounds and public release notes, not yet seen on OpenWrt | 7 |
| **Total** | **173** |

Limits worth knowing before you install:

- **6 GHz power** (C-360, W-318): OpenWrt runs 6 GHz as Low Power Indoor,
  about 19 dBm. Arista's firmware went higher with Standard Power through AFC,
  which OpenWrt does not have.
- **QCN9074/QCN9072 radio firmware** (C-360, W-318): every public Qualcomm
  firmware stalls at start-up on these cards, so the port runs Arista's own.
  Reported upstream.
- **W-318, W-118 and C-200 first install** needs a console cable for now.
