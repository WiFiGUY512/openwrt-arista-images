# Installing by hand

**Most people do not need this page.** The [network installer](README.md) does
the whole install from a PC, with no cable, for every model here. The
console-cable procedure is for:

- an AP the network installer cannot reach or log in to;
- anyone who wants to see every step.

It needs a console cable and a TFTP server. The idea is the same on every
model: stop the boot loader, start OpenWrt from memory over TFTP, back up the
flash, write OpenWrt, and set the boot command once. The commands differ per
model, and each model has its own page:

| Model | Its page | Console |
|---|---|---|
| C-200 | [c200/BY-HAND.md](https://github.com/WiFiGUY512/openwrt-arista/blob/main/c200/BY-HAND.md) | RJ-45 port, RS-232 |
| C-230 | [c230/BY-HAND.md](https://github.com/WiFiGUY512/openwrt-arista/blob/main/c230/BY-HAND.md) | RJ-45 port, RS-232 |
| C-260 | [c260/BY-HAND.md](https://github.com/WiFiGUY512/openwrt-arista/blob/main/c260/BY-HAND.md) | RJ-45 port, RS-232 |
| C-360 | [c360/BY-HAND.md](https://github.com/WiFiGUY512/openwrt-arista/blob/main/c360/BY-HAND.md) | RJ-45 port, RS-232 |
| C-460 | [c460/BY-HAND.md](https://github.com/WiFiGUY512/openwrt-arista/blob/main/c460/BY-HAND.md) | RJ-45 port, RS-232 |
| W-118 | [w118/BY-HAND.md](https://github.com/WiFiGUY512/openwrt-arista/blob/main/w118/BY-HAND.md) | a 3.3 V header inside the case |
| W-318 | [w318/BY-HAND.md](https://github.com/WiFiGUY512/openwrt-arista/blob/main/w318/BY-HAND.md) | RJ-45 port, RS-232 |
| C-130 | no page yet | |

A halfway route on the C-230, C-260 and C-360: start OpenWrt from memory by
hand (the first two steps of the model's page), then let the network installer
do the rest from your PC with
`python arista-field.py --installer-at <the AP's address>`.
