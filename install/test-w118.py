"""Checks for the W-118's path, from its first network installs (2026-11-15).
No AP and no network.

    python test-w118.py

What the W-118 needed that no other model had:
  - an SSH server that only speaks an old key exchange and host-key type;
  - a CLI whose version command is 'get version', and an upgrade wizard that
    asks for the server straight after Continue?, with no version questions;
  - a boot loader that starts its image with a bare 'bootm';
  - an install that goes into flash Arista does not use, so going back to
    Arista is one boot-command change and nothing is restored.
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
    print("%s  %s%s" % ("ok  " if ok else "FAIL", name, ("   (%s)" % detail) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


M = af.MODELS["w118"]

# --- the one-time network boot ---------------------------------------------------
env = dict(af.netboot_env("192.168.1.20", "192.168.1.230", "w118.itb", None, M["load_addr"],
                          M.get("stock_bootcmd", "run ubootscript"), M.get("fdt_high", "0x4A3E0000"),
                          5, 2, 3, "no", False))
nb = env["owrt_nb"]
check("the image is fetched to 0x84000000 and started with a bare bootm",
      "tftpboot 0x84000000 w118.itb && bootm 0x84000000;" in nb and "#" not in nb, nb)
check("fdt_high is left alone on this boot loader", "fdt_high" not in nb, nb)
check("Arista's own boot command is saved before the fetch, and the fetch ends in a reset",
      env["owrt_clear"] == 'setenv bootcmd "run ubootscript"; saveenv' and nb.rstrip().endswith("reset")
      and env["bootcmd"] == "run owrt_clear; run owrt_nb", env)
c360 = dict(af.netboot_env("192.168.1.20", "192.168.1.230", "c360.itb", "config@hk09", "0xA0000000"))
check("a model with a named configuration still gets it", "bootm 0xA0000000#config@hk09" in c360["owrt_nb"])

# --- what the install writes -------------------------------------------------------
ie = dict(M["install_env"])
check("the install's boot command tries OpenWrt, then Arista with its own settings put back",
      ie["bootcmd"] == "setenv bootargs ${owrt_args}; run owrt_boot; run arista_boot"
      and "setenv mtdids; setenv mtdparts" in ie["arista_boot"] and "${ba0}" in ie["arista_boot"]
      and ie["arista_boot"].rstrip().endswith("run ubootscript"), ie)
check("OpenWrt is read from the upper half of the NAND",
      "mtdparts=nand0:0x8000000@0x8000000(ubi)" in ie["owrt_boot"] and "bootm 0x84000000" in ie["owrt_boot"])
check("the install does not change pri", "pri" not in ie)
check("it is installed by this tool, by sysupgrade, and Arista is kept",
      af.field_install("w118") and M["image"] == "sysupgrade" and M["keeps_arista"]
      and M["root_order"] == ("upgrade",))

# --- its flash, as its own firmware lists it ---------------------------------------
STOCK = """dev:    size   erasesize  name
mtd0: 04000000 00020000 "rootfs"
mtd1: 04000000 00020000 "rootfs2"
mtd2: 00040000 00010000 "0:SBL1"
mtd3: 00020000 00010000 "0:MIBIB"
mtd4: 00060000 00010000 "0:QSEE"
mtd5: 00010000 00010000 "0:CDT"
mtd6: 00010000 00010000 "0:DDRPARAMS"
mtd7: 00080000 00010000 "0:APPSBL"
mtd8: 00010000 00010000 "0:APPSBLENV"
mtd9: 00010000 00010000 "0:ART"
mtd10: 00500000 00010000 "0:HLOS"
mtd11: 00500000 00010000 "0:HLOS1"
mtd12: 00400000 00010000 "0:UCONFIG"
mtd13: 0139e000 0001f000 "ubi_rootfs"
mtd14: 0250e000 0001f000 "ubi_rootfs_data"
"""
rows = af.mtd_table(STOCK)
real = [r for r in rows if not af.is_ubi_volume(r)]
nor = sum(r["size"] for r in real if r["erase"] < af.NAND_ERASE)
check("thirteen real partitions; the two UBI volumes are told by block size", len(real) == 13,
      [r["name"] for r in real])
check("its NOR partitions add up to what the model entry expects", nor == M["nor_total"], hex(nor))
s0, s1 = af.find_part(rows, M["slot0"]), af.find_part(rows, M["slot1"])
check("both Arista slots are found, 64 MiB each",
      s0 and s1 and s0["size"] == s1["size"] == M["slot_size"])
check("its boot variables and fallback kernel are found by name",
      af.find_part(rows, M["appsblenv"]) and af.find_part(rows, M["need_nor"]))


# --- the upgrade wizard on 8.7.1 ---------------------------------------------------

class Wizard:
    """Stands in for the CLI: answers each line the way 8.7.1 or 11.x does."""

    def __init__(self, script):
        self.script = list(script)      # (expected line or None, what the AP prints next)
        self.buf = ""
        self.sent = []

    def mark(self):
        return len(self.buf)

    def since(self, mark):
        return self.buf[mark:]

    def send(self, text):
        self.sent.append(text)
        self.last = len(self.buf)       # the AP's answer arrives after the line is sent
        if self.script:
            self.buf += self.script.pop(0)

    def wait(self, pattern, timeout, mark=None, fail=None):
        import re
        return re.search(pattern, self.buf[(self.last if mark is None else mark):], re.M | re.S)


PAYLOAD = "http://192.0.2.9/;sh</dev/tty>/dev/tty;"
w = Wizard(["...\nContinue? ([y]/n): ",
            "\nConfigure Server with [AP] Flashing Files\nServer IP Address or Upgrade path []: ",
            "\nConfirm? ([y]/n): ",
            "\nBusyBox v1.20.2 built-in shell (ash)\n~ # "])
af._upgrade_wizard(w, "192.0.2.9", PAYLOAD)
check("8.7.1: Continue?, then the server prompt at once, then Confirm?",
      w.sent == ["upgrade", "y", PAYLOAD, "y"], w.sent)
w = Wizard(["...\nContinue? ([y]/n): ",
            "\nEnter upgrade build version string []: ",
            "\nEnter upgrade build vendor version []: ",
            "\nServer IP Address or Upgrade path []: ",
            "\nConfirm? ([y]/n): ",
            "\n~ # "])
af._upgrade_wizard(w, "192.0.2.9", PAYLOAD)
check("8.9 / 11.x: the two version questions are still answered first",
      w.sent == ["upgrade", "y", "1", "1", PAYLOAD, "y"], w.sent)

# --- the old SSH server --------------------------------------------------------------
try:
    import paramiko

    class T:                            # the four tables _old_ssh extends, nothing else
        _kex_info = dict(paramiko.Transport._kex_info)
        _key_info = dict(paramiko.Transport._key_info)
        _preferred_kex = tuple(paramiko.Transport._preferred_kex)
        _preferred_keys = tuple(paramiko.Transport._preferred_keys)
        _preferred_ciphers = tuple(paramiko.Transport._preferred_ciphers)
        _preferred_macs = tuple(paramiko.Transport._preferred_macs)

    t = T()
    af._old_ssh(t)
    k = t._kex_info["diffie-hellman-group1-sha1"]
    check("the old key exchange and host-key type are added to one connection only",
          "diffie-hellman-group1-sha1" in t._preferred_kex and "ssh-rsa" in t._preferred_keys
          and "diffie-hellman-group1-sha1" not in paramiko.Transport._kex_info
          and "ssh-rsa" not in paramiko.Transport._key_info)
    check("they come after everything current, so a modern server never picks them",
          t._preferred_kex[-1] == "diffie-hellman-group1-sha1" and t._preferred_keys[-1] == "ssh-rsa")
    check("the exchange is group 1: a 1024-bit prime, generator 2, SHA-1",
          k.P.bit_length() == 1024 and k.G == 2 and k.hash_algo().name == "sha1")
except ImportError:
    print("skip  paramiko is not installed here: the old-SSH checks were not run")

check("a W-118's MAC prefix is one discovery looks at", "88:b1:e1" in af.AP_OUIS)

print("")
print("%d failed" % len(FAILED) if FAILED else "all W-118 checks pass")
sys.exit(1 if FAILED else 0)
