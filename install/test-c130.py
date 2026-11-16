"""Checks for the C-130's path.  No AP and no network.

    python test-c130.py

What the C-130 has that no other model had: a device that keeps its community
name (mojo_c-130, board mojo,c-130) in the image names and on the running
system; two NAND partitions per slot; and a boot loader with no 'sleep'.  Its
install is the W-118's shape (one fw_setenv, then OpenWrt's own sysupgrade),
but into Arista's slot 0, with slot 1 kept as the fallback.
"""
import importlib.util
import os
import sys

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


M = af.MODELS["c130"]

# --- names -------------------------------------------------------------------------
f = af.files_for("c130", "install")
check("its release files carry the device's own name",
      f == {"itb": "openwrt-ipq806x-generic-mojo_c-130-initramfs-fit-uImage.itb",
            "sysupgrade": "openwrt-ipq806x-generic-mojo_c-130-squashfs-sysupgrade.bin"}, f)
check("a unit that answers as mojo,c-130 is one of this port's, and is the C-130",
      af.port_board("mojo,c-130") and af.model_key("mojo,c-130") == "c130"
      and af.port_board("arista,c-260") and not af.port_board("netgear,r7800"))
check("discovery looks at its MAC prefix", "00:11:74" in af.AP_OUIS)

# --- its flash, as its own firmware lists it (Arista 13.0.2-28.402) ------------------
STOCK = """dev:    size   erasesize  name
mtd0: 02000000 00020000 "rootfs"
mtd1: 02000000 00020000 "opt"
mtd2: 02000000 00020000 "rootfs2"
mtd3: 02000000 00020000 "opt2"
mtd4: 00020000 00010000 "0:SBL1"
mtd5: 00020000 00010000 "0:MIBIB"
mtd6: 00040000 00010000 "0:SBL2"
mtd7: 00080000 00010000 "0:SBL3"
mtd8: 00010000 00010000 "0:DDRCONFIG"
mtd9: 00010000 00010000 "0:SSD"
mtd10: 00080000 00010000 "0:TZ"
mtd11: 00080000 00010000 "0:RPM"
mtd12: 00100000 00010000 "0:APPSBL"
mtd13: 00040000 00010000 "0:APPSBLENV"
mtd14: 00100000 00010000 "0:ART"
mtd15: 00020000 00010000 "0:VENDORDATA"
mtd16: 00010000 00010000 "0:BOOTCONFIG"
mtd17: 00500000 00010000 "0:HLOS"
mtd18: 00500000 00010000 "0:HLOS1"
mtd19: 00400000 00010000 "0:UCONFIG"
mtd20: 00d00000 00010000 "0:RECOVERY"
mtd21: 01078000 0001f000 "ubi_rootfs"
mtd22: 00ba0000 0001f000 "ubi_rootfs_data"
mtd23: 01c18000 0001f000 "ubi_opt"
"""
rows = af.mtd_table(STOCK)
real = [r for r in rows if not af.is_ubi_volume(r)]
s0, s1 = af.find_part(rows, M["slot0"]), af.find_part(rows, M["slot1"])
nor = sum(r["size"] for r in real if r["erase"] < af.NAND_ERASE)
check("21 real partitions; the three UBI volumes are not among them", len(real) == 21, len(real))
check("its NOR partitions add up to what the model entry expects", nor == M["nor_total"], hex(nor))
check("slot 0 and slot 1 are found (rootfs, rootfs2), 32 MiB each; opt and opt2 are saved with the rest",
      s0["dev"] == "mtd0" and s1["dev"] == "mtd2" and s0["size"] == M["slot_size"]
      and {"opt", "opt2"} <= {r["name"] for r in real})


class Shell:
    def __init__(self, answers):
        self.answers = answers

    def run(self, cmd, timeout=60):
        for key, val in self.answers.items():
            if key in cmd:
                return val, 0
        return "", 1


live = af.live_partitions(Shell({"mtd_num": "0\n1", "cmdline": "ubi.mtd=rootfs root=mtd:ubi_rootfs",
                                 "/proc/mounts": "ubi1:ubi_opt /opt ubifs rw,sync 0 0\n"
                                                 "/dev/mtdblock19 /config_info jffs2 rw,sync 0 0\n"}),
                         {"mtd": rows, "pri": "0", "slot0": s0, "slot1": s1})
check("in use: rootfs and opt of the running slot, and UCONFIG", live == {"mtd0", "mtd1", "mtd19"}, live)

# --- the one-time network boot -----------------------------------------------------
env = dict(af.netboot_env("192.168.1.20", "192.168.1.230", "c130.itb", M["fit_cfg"], M["load_addr"],
                          M.get("stock_bootcmd", "run ubootscript"), M.get("fdt_high", "0x4A3E0000"),
                          M.get("net_settle", 5), M.get("net_tries", 2), M.get("retry_sleep", 3),
                          M.get("netretry", "no"), M.get("net_helper", False)))
check("the image is fetched to 0x44000000 and started with a bare bootm",
      env["owrt_try"] == "tftpboot 0x44000000 c130.itb && bootm 0x44000000"
      and "#" not in env["owrt_try"] and "fdt_high" not in env["owrt_nb"], env)
check("its boot loader has no 'sleep', so none is written; three tries, then a reset",
      "sleep" not in env["owrt_try"] + env["owrt_nb"]
      and env["owrt_nb"].endswith("run owrt_try; run owrt_try; run owrt_try; reset"), env)
w = af.MODELS["w118"]
other = dict(af.netboot_env("192.168.1.20", "192.168.1.230", "w118.itb", w["fit_cfg"],
                            w.get("load_addr", "0x60000000"), w.get("stock_bootcmd", "run ubootscript"),
                            w.get("fdt_high", "0x4A3E0000"), w.get("net_settle", 5), w.get("net_tries", 2),
                            w.get("retry_sleep", 3), w.get("netretry", "no"), w.get("net_helper", False)))
check("a model whose boot loader has 'sleep' still gets both waits",
      "sleep" in other["owrt_nb"], other)
check("Arista's own boot command is saved first, and the attempts end in a reset",
      env["owrt_clear"] == 'setenv bootcmd "run ubootscript"; saveenv'
      and env["owrt_nb"].rstrip().endswith("reset") and env["bootcmd"] == "run owrt_clear; run owrt_nb", env)

# --- what the install writes -------------------------------------------------------
ie = dict(M["install_env"])
check("OpenWrt is read from the first 64 MiB of the NAND",
      "mtdparts=nand0:64m(ubi)" in ie["owrt_boot"] and "ubi read 0x44000000 kernel" in ie["owrt_boot"]
      and ie["owrt_boot"].rstrip().endswith("bootm 0x44000000") and ";" not in ie["owrt_boot"], ie["owrt_boot"])
check("the fallback clears OpenWrt's partition table, restores Arista's arguments, and runs its boot",
      ie["arista_boot"].startswith("setenv mtdids; setenv mtdparts;") and "${ba1}" in ie["arista_boot"]
      and ie["arista_boot"].rstrip().endswith("run ubootscript"), ie["arista_boot"])
check("the boot command tries OpenWrt, then Arista; pri is set to the kept slot",
      ie["bootcmd"] == "run owrt_boot; run arista_boot" and ie["pri"] == "1")
check("it is installed by this tool, by sysupgrade, from the sysupgrade image",
      af.field_install("c130") and not M.get("no_image") and M["image"] == "sysupgrade"
      and M["root_order"] == ("radartool",))


# --- back to Arista ----------------------------------------------------------------

class Run:
    def __init__(self):
        self.env_writes, self.flash_writes = [], []


def revert():
    sent = []
    env = ("bootcmd=run owrt_boot; run arista_boot\npri=1\nubootscript=x\nba0=A0\nba1=A1\n"
           "owrt_boot=a\narista_boot=b\nmtdids=nand0=nand0\nmtdparts=mtdparts=nand0:64m(ubi)\n")
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
check("back to Arista: its own boot command, slot 1, and slot 1's arguments",
      "bootcmd run ubootscript" in payload and "\npri 1\n" in payload and "\nbootargs A1\n" in payload, payload)
check("pri is not removed again, and the install's variables and OpenWrt's partition table are",
      payload.count("\npri") == 1 and all(("\n%s\n" % n) in payload
                                          for n in ("owrt_boot", "arista_boot", "mtdids", "mtdparts")), payload)

print("")
print("%d failed" % len(FAILED) if FAILED else "all C-130 checks pass")
sys.exit(1 if FAILED else 0)
