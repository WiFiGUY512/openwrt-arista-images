"""Checks for the W-318's path, from its first network installs (2026-11-15).
No AP and no network.

    python test-w318.py

What the W-318 needed that no other model had:
  - a configuration name with a hyphen in it (config@cp01-c3), which was read
    short and sent to the boot loader wrong;
  - a stock boot command that holds double quotes and ${...} of its own;
  - its kernels in NOR and its systems on the eMMC, checked together;
  - an install done by the image's own script, run dry first;
  - a way back to Arista by the boot command, to the slot the install kept.
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


M = af.MODELS["w318"]
STOCK_BOOTCMD = ('env default default_env_version; if test -n "${default_env_version}"; '
                 'then run ubootscriptv2; else run ubootscript; fi;')

# --- the configuration name ----------------------------------------------------------
d = tempfile.mkdtemp()
one = os.path.join(d, "one.itb")
open(one, "wb").write(b"\xd0\x0d\xfe\xed" + b"\0" * 64 + b"config@cp01-c3\0" + b"\0" * 32 + b"config@cp01-c3\0")
check("a configuration name with a hyphen is read whole",
      af.fit_config_for(one, "config@cp01-c3") == ("config@cp01-c3", ["config@cp01-c3"]),
      af.fit_config_for(one, "config@cp01-c3"))
two = os.path.join(d, "two.itb")
open(two, "wb").write(b"config@hk09\0....config@hk09.1\0")
check("and one with a dot, beside a shorter one that it starts with",
      af.fit_config_for(two, "config@hk09")[1] == ["config@hk09", "config@hk09.1"],
      af.fit_config_for(two, "config@hk09"))

# --- the one-time network boot -----------------------------------------------------
env = dict(af.netboot_env("192.168.1.20", "192.168.1.230", "w318.itb", M["fit_cfg"],
                          M.get("load_addr", "0x60000000"), M["stock_bootcmd"],
                          M.get("fdt_high", "0x4A3E0000"), M.get("net_settle", 5),
                          M.get("net_tries", 2), M.get("retry_sleep", 3), M.get("netretry", "no"),
                          M.get("net_helper", False)))
check("the model's boot command is Arista's own, exactly", M["stock_bootcmd"] == STOCK_BOOTCMD)
check("it is put back inside single quotes, so U-Boot stores it unexpanded",
      env["owrt_clear"] == "setenv bootcmd '%s'; saveenv" % STOCK_BOOTCMD, env["owrt_clear"])
check("the image is started with its full configuration name",
      env["owrt_try"] == "tftpboot 0x60000000 w318.itb && bootm 0x60000000#config@cp01-c3; sleep 3",
      env["owrt_try"])
check("four short attempts, fdt_high left alone, and a reset at the end",
      env["owrt_nb"].count("run owrt_try;") == 4 and "fdt_high" not in env["owrt_nb"]
      and env["owrt_nb"].rstrip().endswith("reset"), env["owrt_nb"])
check("no line pasted into Arista's shell is long", max(len(v) for v in env.values()) < 260,
      max(len(v) for v in env.values()))

# --- its flash ---------------------------------------------------------------------
GPT = """mmcblk0p1 rootfs 34 262144
mmcblk0p2 rootfs_1 262178 262144
mmcblk0p3 rootfs_data 524322 524288
mmcblk0p4 rootfs_data_1 1048610 524288
mmcblk0p5 panicdump 1572898 10240
mmcblk0p6 ramdump 1583138 6148096
"""
rows = af.emmc_table(GPT)
check("its eMMC layout is the one the model entry expects", af.emmc_problems(rows, M) == [],
      af.emmc_problems(rows, M))
moved = af.emmc_table(GPT.replace("rootfs 34 ", "rootfs 2048 "))
check("a system partition that starts elsewhere is refused",
      any("starts at LBA" in p for p in af.emmc_problems(moved, M)), af.emmc_problems(moved, M))
check("the slots are the two systems and their settings, kernels apart in NOR",
      M["slot0_parts"] == ("rootfs", "rootfs_data") and M["slot1_parts"] == ("rootfs_1", "rootfs_data_1")
      and M["need_nor"][0] == "0:HLOS_1" and M["storage"] == "emmc")
check("it is installed by this tool, by the image's own script",
      af.field_install("w318") and M["install_by"] == "script" and M["installer"] == "arista-w318-install")


# --- the install: the script, dry first --------------------------------------------

class Run:
    installer = object()
    yes = yes_install = True

    def __init__(self):
        self.env_writes, self.flash_writes = [], []


def install(dry_ok=True, real_ok=True):
    """Drive do_install_script against a stand-in installer; return what it ran."""
    ran = []
    bootcmd = ["bootcmd=stock"]

    def ssh_exec(con, cmd, timeout=0):
        ran.append(cmd)
        if cmd.startswith("command -v"):
            return "ok\n", 0
        if cmd.startswith("sha256sum"):
            return af.sha256_file(img) + "\n", 0
        if "--dry-run" in cmd:
            return ("w318-install: checks\nW318-INSTALL-RESULT: OK (dry run, nothing written)\n" if dry_ok
                    else "W318-INSTALL-RESULT: FAILED: slot 1 holds no Arista system\n"), 0
        if "--yes" in cmd:
            if real_ok:
                bootcmd[0] = "bootcmd=run owrt_boot; run arista_boot"
                return "W318-INSTALL-RESULT: OK: OpenWrt is in slot 0; reboot to start it\n", 0
            return "W318-INSTALL-RESULT: FAILED: rootfs did not read back\n", 1
        if cmd.startswith("fw_printenv bootcmd"):
            return bootcmd[0] + "\n", 0
        return "", 0

    img = os.path.join(d, "sysupgrade.bin")
    open(img, "wb").write(b"image")
    saved = (af.ssh_exec, af.ssh_must, af._send_file, af.die)
    af.ssh_exec = ssh_exec
    af.ssh_must = lambda con, cmd, timeout=0: ""
    af._send_file = lambda con, local, remote, what: None

    class Died(Exception):
        pass

    def die(msg, hint=None):
        raise Died(msg)
    af.die = die
    run = Run()
    died = None
    try:
        af.do_install_script(run, {"model": "w318"}, "192.0.2.9", img, None)
    except Died as e:
        died = str(e)
    finally:
        af.ssh_exec, af.ssh_must, af._send_file, af.die = saved
    return ran, run, died


ran, run, died = install()
order = [("dry" if "--dry-run" in c else "real" if "--yes" in c else "reboot" if "reboot" in c else "")
         for c in ran]
order = [o for o in order if o]
check("the installer is run dry, then for real, then the AP is restarted",
      died is None and order == ["dry", "real", "reboot"], (order, died))
check("its one boot-command write and its flash write are counted",
      run.env_writes == ["boot command (installer)"] and len(run.flash_writes) == 1,
      (run.env_writes, run.flash_writes))
ran, run, died = install(dry_ok=False)
check("a failed dry run stops before anything is written",
      died and "nothing was written" in died and not any("--yes" in c for c in ran)
      and not run.env_writes and not run.flash_writes, died)
ran, run, died = install(real_ok=False)
check("a stopped install says so, and that Arista still boots", died and "slot 1" in died, died)


# --- back to Arista ----------------------------------------------------------------

def revert():
    sent = []
    env = ("bootcmd=run owrt_boot; run arista_boot\npri=1\nsr=0\nubootscript=x\nubootscriptv2=y\n"
           "owrt_boot=sf probe\narista_boot=setenv bootargs\nbootargs=console=ttyMSM0,115200n8 root=/dev/mmcblk0p1\n")
    state = {"written": False}

    def ssh_exec(con, cmd, timeout=0):
        if cmd.startswith("printf"):
            sent.append(cmd)
            state["written"] = True
            return "", 0
        if cmd.startswith("fw_printenv bootcmd"):
            return ("bootcmd=%s\n" % STOCK_BOOTCMD) if state["written"] else "bootcmd=x\n", 0
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
check("back to Arista: its own boot command, and the slot the install kept",
      "bootcmd " + STOCK_BOOTCMD in payload and "root=/dev/mmcblk0p2" in payload
      and "\npri 1\n" in payload and "\nsr 0\n" in payload, payload)
check("the install's two variables are removed",
      "\nowrt_boot\n" in payload and "\narista_boot\n" in payload, payload)
check("it is one environment write, counted", run.env_writes == ["boot command back to Arista's"],
      run.env_writes)

print("")
print("%d failed" % len(FAILED) if FAILED else "all W-318 checks pass")
sys.exit(1 if FAILED else 0)
