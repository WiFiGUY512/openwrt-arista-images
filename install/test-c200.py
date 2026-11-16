"""Checks for the C-200's path, from its first network installs (2026-11-15).
No AP and no network.

    python test-c200.py

What the C-200 needed that no other model had: its image carries no installer
script, so the tool makes the install's steps itself.  The order is the thing
to hold in place: the unit is checked, the boot command (with Arista as the
fallback) is written first, and only then is slot 0 formatted and read back.
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("af", os.path.join(HERE, "arista-field.py"))
af = importlib.util.module_from_spec(spec)
sys.modules["af"] = af
spec.loader.exec_module(af)

FAILED = []


def check(name, ok, detail=""):
    print("%s  %s%s" % ("ok  " if ok else "FAIL", name, ("   (%s)" % (detail,)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


M = af.MODELS["c200"]

# --- its flash, as its own firmware lists it (Arista 11.0.0-36.19) -------------------
STOCK = """dev:    size   erasesize  name
mtd0: 000c0000 00010000 "0:SBL1"
mtd1: 00010000 00010000 "0:MIBIB"
mtd2: 00020000 00010000 "0:BOOTCONFIG"
mtd3: 00020000 00010000 "0:BOOTCONFIG1"
mtd4: 001a0000 00010000 "0:QSEE"
mtd5: 001a0000 00010000 "0:QSEE_1"
mtd6: 00010000 00010000 "0:DEVCFG"
mtd7: 00010000 00010000 "0:DEVCFG_1"
mtd8: 00040000 00010000 "0:RPM"
mtd9: 00040000 00010000 "0:RPM_1"
mtd10: 00010000 00010000 "0:CDT"
mtd11: 00010000 00010000 "0:CDT_1"
mtd12: 00010000 00010000 "0:APPSBLENV"
mtd13: 00100000 00010000 "0:APPSBL"
mtd14: 00100000 00010000 "0:APPSBL_1"
mtd15: 00040000 00010000 "0:ART"
mtd16: 00800000 00010000 "HL_backup0"
mtd17: 00800000 00010000 "HL_backup1"
mtd18: 00400000 00010000 "CONFIG"
mtd19: 00100000 00010000 "LOGGING"
mtd20: 00020000 00010000 "DEVCERT"
mtd21: 00380000 00010000 "RESERVED"
mtd22: 10000000 00020000 "rootfs"
mtd23: 10000000 00020000 "rootfs_1"
mtd24: 01cf1000 0001f000 "ubi_rootfs"
mtd25: 0d0c4000 0001f000 "rootfs_data"
mtd26: 00003000 0001f000 "rootfs_sig"
"""
rows = af.mtd_table(STOCK)
real = [r for r in rows if not af.is_ubi_volume(r)]
s0, s1 = af.find_part(rows, M["slot0"]), af.find_part(rows, M["slot1"])
nor = sum(r["size"] for r in real if r is not s0 and r is not s1 and r["erase"] < af.NAND_ERASE)
check("24 real partitions; the three UBI volumes are not among them", len(real) == 24, len(real))
check("its NOR partitions add up to 32 MiB", nor == M["nor_total"], hex(nor))
check("both slots are found, 256 MiB each", s0 and s1 and s0["size"] == s1["size"] == M["slot_size"])
check("its fallback kernel is found under its own name (HL_backup1)",
      af.find_part(rows, M["need_nor"])["dev"] == "mtd17")


class Shell:
    def __init__(self, answers):
        self.answers = answers

    def run(self, cmd, timeout=60):
        for key, val in self.answers.items():
            if key in cmd:
                return val, 0
        return "", 1


card = {"mtd": rows, "pri": "1", "slot0": s0, "slot1": s1}
live = af.live_partitions(Shell({"mtd_num": "23", "cmdline": "ubi.mtd=rootfs_1",
                                 "/proc/mounts": "ubi0:rootfs_data /overlay ubifs rw,sync 0 0\n"
                                                 "/dev/mtdblock18 /config_info jffs2 rw,relatime 0 0\n"
                                                 "/dev/mtdblock15 /art squashfs ro 0 0\n"}), card)
check("in use: the slot Arista runs from, and CONFIG (mounted for writing); not a read-only mount",
      live == {"mtd23", "mtd18"}, live)

# --- the one-time network boot -----------------------------------------------------
env = dict(af.netboot_env("192.168.1.20", "192.168.1.230", "c200.itb", M["fit_cfg"],
                          M.get("load_addr", "0x60000000"), M.get("stock_bootcmd", "run ubootscript"),
                          M.get("fdt_high", "0x4A3E0000"), M.get("net_settle", 5), M.get("net_tries", 2),
                          M.get("retry_sleep", 3), M.get("netretry", "no"), M.get("net_helper", False)))
check("the image is started with its full configuration name, fdt_high left alone",
      env["owrt_try"] == "tftpboot 0x60000000 c200.itb && bootm 0x60000000#config@cp01-c3; sleep 3"
      and "fdt_high" not in env["owrt_nb"], env)
check("the wizard is tried first on this model (11.0.0 has no radartool)", M["root_order"][0] == "upgrade")

# --- the install, made by the tool -------------------------------------------------
d = tempfile.mkdtemp()
img = os.path.join(d, "factory.ubi")
open(img, "wb").write(b"UBI#" + b"\0" * 60)
KERNEL, ROOTFS = "k" * 64, "r" * 64
sums = os.path.join(d, "c200-install-SHA256SUMS")
open(sums, "w").write("%s  factory.ubi\n%s volume:kernel 100\n%s volume:rootfs 200\n"
                      % (af.sha256_file(img), KERNEL, ROOTFS))
RAM = """mtd0: 10000000 00020000 "rootfs"
mtd1: 10000000 00020000 "rootfs_1"
mtd14: 00010000 00010000 "0:APPSBLENV"
mtd19: 00800000 00010000 "HL_backup1"
"""


class Run:
    installer = object()
    yes = yes_install = True

    def __init__(self):
        self.env_writes, self.flash_writes = [], []


def install(slot1_magic="55424923", kernel_magic="7f454c46", rootfs_sha=ROOTFS, format_rc=0):
    ran = []
    wrote = {"env": False}

    def ssh_exec(con, cmd, timeout=0):
        ran.append(cmd)
        if cmd.startswith("command -v"):
            return "ok\n", 0
        if cmd.startswith("hexdump"):
            return "%s\n%s\n" % (slot1_magic, kernel_magic), 0
        if cmd.startswith("sha256sum"):
            return "%s\n55424923" % af.sha256_file(img), 0
        if cmd.startswith("printf"):
            wrote["env"] = True
            return "", 0
        if cmd.startswith("fw_printenv bootcmd pri"):
            return "bootcmd=setenv bootargs ${owrt_args}; run owrt_boot; run arista_boot\npri=1\n", 0
        if cmd.startswith("fw_printenv"):
            return "bootcmd=run ubootscript\nubootscript=x\nba0=a\nba1=b\npri=0\n", 0
        if "ubiformat /dev/" in cmd:
            return "UBIFORMAT_RC=%d\nUBI device number 0, total 2048 LEBs\n" % format_rc, 0
        if cmd.startswith("for v in /sys/class/ubi"):
            return "kernel ubi0_0\nrootfs ubi0_1\nrootfs_data ubi0_2\n", 0
        if cmd.startswith("head -c 100 "):
            return KERNEL + "\n", 0
        if cmd.startswith("head -c 200 "):
            return rootfs_sha + "\n", 0
        return "", 0

    class Died(Exception):
        pass

    def die(msg, hint=None):
        raise Died(msg)

    saved = (af.ssh_exec, af.ssh_must, af._send_file, af.die)
    af.ssh_exec = ssh_exec
    af.ssh_must = lambda con, cmd, timeout=0: RAM if "proc/mtd" in cmd else ""
    af._send_file = lambda con, local, remote, what: None
    af.die = die
    run = Run()
    died = None
    try:
        af.do_install_ubiformat(run, {"model": "c200"}, "192.0.2.9", img, sums)
    except Died as e:
        died = str(e)
    finally:
        af.ssh_exec, af.ssh_must, af._send_file, af.die = saved
    return ran, run, died


ran, run, died = install()
i_env = next(i for i, c in enumerate(ran) if c.startswith("printf"))
i_fmt = next(i for i, c in enumerate(ran) if "ubiformat /dev/" in c)
check("a good unit: the boot command is written before slot 0 is formatted, then it restarts",
      died is None and i_env < i_fmt and ran[-1] == "reboot", (died, i_env, i_fmt, ran[-1]))
check("slot 0 is formatted on the installer's own name for it, and detached first",
      "ubidetach -p /dev/mtd0" in ran[i_fmt] and "ubiformat /dev/mtd0 -y -q -f /tmp/c200/factory.ubi" in ran[i_fmt],
      ran[i_fmt])
check("one environment write and one flash write are counted",
      len(run.env_writes) == 1 and run.flash_writes == ["NAND slot 0 (ubiformat)"],
      (run.env_writes, run.flash_writes))
payload = ran[i_env]
check("the boot command tries OpenWrt, falls back to Arista, and makes slot 1 Arista's",
      "bootcmd setenv bootargs ${owrt_args}; run owrt_boot; run arista_boot" in payload
      and "\npri 1\n" in payload and "arista_boot run arista_unset arista_args ubootscript" in payload, payload)

ran, run, died = install(slot1_magic="ffffffff")
check("slot 1 without a UBI: refused before anything is written",
      died and "nothing was written" in died and not run.env_writes and not run.flash_writes
      and not any(c.startswith("printf") or "ubiformat /dev/" in c for c in ran), died)
ran, run, died = install(kernel_magic="ffffffff")
check("slot 1 without a kernel: refused before anything is written",
      died and "nothing was written" in died and not any("ubiformat /dev/" in c for c in ran), died)
ran, run, died = install(rootfs_sha="0" * 64)
check("a volume that does not read back stops, and says Arista still boots",
      died and "rootfs volume" in died and "slot 1" in died and ran[-1] != "reboot", died)
ran, run, died = install(format_rc=1)
check("a failed format stops, and says Arista still boots", died and "slot 1" in died, died)


# --- back to Arista ----------------------------------------------------------------

def revert():
    sent = []
    env = ("bootcmd=setenv bootargs ${owrt_args}; run owrt_boot; run arista_boot\npri=1\nubootscript=x\n"
           "ba0=A0\nba1=A1\nowrt_args=a\nowrt_parts=b\nowrt_load=c\nowrt_boot=d\narista_unset=e\n"
           "arista_args=f\narista_boot=g\n")
    state = {"written": False}

    def ssh_exec(con, cmd, timeout=0):
        if cmd.startswith("printf"):
            sent.append(cmd)
            state["written"] = True
            return "", 0
        if cmd.startswith("fw_printenv bootcmd"):
            return "bootcmd=run ubootscript\n" if state["written"] else "bootcmd=x\n", 0
        if cmd.startswith("fw_printenv"):
            return env, 0
        return "", 0

    class Con:
        def close(self):
            pass

    saved = (af.ssh_open, af.ssh_exec)
    af.ssh_open = lambda *a, **k: Con()
    af.ssh_exec = ssh_exec
    run = Run()
    try:
        af.do_restore_bootcmd(run, {"ip": "192.0.2.9"}, M)
    finally:
        af.ssh_open, af.ssh_exec = saved
    return sent[0] if sent else "", run


payload, run = revert()
check("back to Arista: its own boot command and slot 1",
      "bootcmd run ubootscript" in payload and "\npri 1\n" in payload, payload)
check("pri is set to the kept slot and not removed again in the same write, and Arista's bootargs follow it",
      payload.count("\npri") == 1 and "\npri 1\n" in payload and "\nbootargs A1\n" in payload, payload)
check("every variable the install added is removed",
      all(("\n%s\n" % n) in payload for n in ("owrt_args", "owrt_parts", "owrt_load", "owrt_boot",
                                              "arista_unset", "arista_args", "arista_boot")), payload)

print("")
print("%d failed" % len(FAILED) if FAILED else "all C-200 checks pass")
sys.exit(1 if FAILED else 0)
