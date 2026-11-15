"""Checks for the firmware backup and the stopped-run ending, from the first
live run on a C-360 (2026-11-15).  No AP and no network.

    python test-backup.py

Each check is one of the faults that run found:
  - a partition the running firmware writes to cannot be compared between two
    reads (slot 1 and LOGGING on a C-360 running from slot 1);
  - a C-360 lists four UBI volumes as if they were partitions, and one of them
    cannot be opened at all while Arista has it mounted;
  - a stopped run said the AP was restarting when the restart had never been
    sent (the SSH library had already closed the session).
"""
import importlib.util
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("af", os.path.join(HERE, "arista-field.py"))
af = importlib.util.module_from_spec(spec)
sys.modules["af"] = af
spec.loader.exec_module(af)

FAILED = []


def check(name, ok, detail=""):
    print("%s  %s%s" % ("ok  " if ok else "FAIL", name, ("   (%s)" % detail) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


# /proc/mtd of a C-360 on Arista 20.0.0-179, running from slot 1 (the unit's own).
C360_STOCK = """dev:    size   erasesize  name
mtd0: 00050000 00010000 "0:SBL1"
mtd14: 00010000 00010000 "0:APPSBLENV"
mtd19: 00800000 00010000 "0:HLOS"
mtd20: 00800000 00010000 "0:HLOS_1"
mtd24: 003b0000 00010000 "RESERVED2"
mtd25: 18000000 00040000 "rootfs"
mtd26: 18000000 00040000 "rootfs_1"
mtd27: 10000000 00040000 "LOGGING"
mtd28: 05a94000 0003e000 "ubi_rootfs"
mtd29: 104d6000 0003e000 "rootfs_data"
mtd30: 0000192a 0003e000 "rootfs_sig"
mtd31: 0e3a8000 0003e000 "ubi_logging"
"""
# A C-260's volumes have a 124 KiB block; a volume under a name not seen yet.
OTHER = """mtd3: 02000000 0001f000 "some_new_volume"
mtd4: 10000000 00020000 "rootfs"
"""

rows = af.mtd_table(C360_STOCK)
vols = [r["name"] for r in rows if af.is_ubi_volume(r)]
check("the C-360's four UBI volumes are not taken for partitions",
      vols == ["ubi_rootfs", "rootfs_data", "rootfs_sig", "ubi_logging"], vols)
real = [r["name"] for r in rows if not af.is_ubi_volume(r)]
check("its real partitions are all kept", real == ["0:SBL1", "0:APPSBLENV", "0:HLOS", "0:HLOS_1",
                                                   "RESERVED2", "rootfs", "rootfs_1", "LOGGING"], real)
o = af.mtd_table(OTHER)
check("a volume under an unknown name is told by its block size",
      af.is_ubi_volume(o[0]) and not af.is_ubi_volume(o[1]))
check("an eMMC partition (512-byte blocks) is not a UBI volume",
      not af.is_ubi_volume({"name": "rootfs", "erase": 512, "kind": "emmc"}))


class Shell:
    """Stands in for the root shell: answers by command."""

    def __init__(self, answers):
        self.answers = answers
        self.sent = []

    def run(self, cmd, timeout=60):
        for key, val in self.answers.items():
            if key in cmd:
                return val, 0
        return "", 1

    def send(self, text):
        self.sent.append(text)


card = {"mtd": rows, "pri": "1",
        "slot0": af.find_part(rows, ("rootfs",)), "slot1": af.find_part(rows, ("rootfs_1",))}
live = af.live_partitions(Shell({"mtd_num": "26\n27", "cmdline": "console=ttyMSM0 ubi.mtd=rootfs_1 root=mtd:ubi_rootfs"}), card)
check("the slot Arista runs from and its log partition are found in use", live == {"mtd26", "mtd27"}, live)
live = af.live_partitions(Shell({"cmdline": "console=ttyMSM0 ubi.mtd=rootfs_1"}), card)
check("without the kernel's UBI list, the command line still names the slot", "mtd26" in live, live)
card0 = dict(card, pri="0")
live = af.live_partitions(Shell({}), card0)
check("with neither, pri names it (pri=0: slot 0, the one the install replaces)", live == {"mtd25"}, live)
check("the slot that is not running is compared in full", "mtd25" not in af.live_partitions(
    Shell({"mtd_num": "26\n27", "cmdline": "ubi.mtd=rootfs_1"}), card))


# --- the ending of a stopped run ------------------------------------------------

class Chan:
    def __init__(self, closed):
        self.closed = closed

    def get_transport(self):
        return self

    def is_active(self):
        return not self.closed


def stopped_run(closed):
    """What a stopped run says, and whether the restart was sent."""
    sh = Shell({})
    sh.chan = Chan(closed)
    af._STOCK, af._REBOOTED, af._STOCK_HOST = sh, False, "192.0.2.9"
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    real_sleep = af.time.sleep
    af.time.sleep = lambda s: setattr(sh.chan, "closed", True)   # the AP drops the session
    try:
        af._cleanup_stock()
    finally:
        sys.stdout = old
        af.time.sleep = real_sleep
    return sh.sent, out.getvalue()


sent, said = stopped_run(closed=False)
check("a stopped run sends the restart while the session is open",
      sent == ["sync; /sbin/reboot"] and "restarts on its own firmware" in said, (sent, said))
sent, said = stopped_run(closed=True)
check("with the session already gone it sends nothing and does not claim a restart",
      sent == [] and "restarts on its own" not in said and "unplug" in said, (sent, said))
check("the ending runs once, not again at exit", af._STOCK is None)

print("")
print("%d failed" % len(FAILED) if FAILED else "all backup and ending checks pass")
sys.exit(1 if FAILED else 0)
