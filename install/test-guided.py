"""Checks for the guided run of arista-field 2.0, with no AP and no network.

Every function that would touch an AP or GitHub is replaced by a recorder, so
what is tested is the order of things: what is asked, what is shown, and that
nothing reaches an AP before the one question is answered yes.

    python test-guided.py
"""
import contextlib
import importlib.util
import io
import os
import socket
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("af", os.path.join(HERE, "arista-field.py"))
af = importlib.util.module_from_spec(spec)
spec.loader.exec_module(af)

RESULTS = []
T = "v2026.10.17.1-%s"


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print("%s  %s%s" % ("ok  " if cond else "FAIL", name, ("\n        " + str(detail)) if detail and not cond else ""))


# ------------------------------------------------------------- pure functions --

WIN_ARP = """
Interface: 192.168.1.20 --- 0x12
  Internet Address      Physical Address      Type
  192.168.1.1           d4-01-c3-11-22-33     dynamic
  192.168.1.50          e0-1c-a7-00-00-02     dynamic
  192.168.1.255         ff-ff-ff-ff-ff-ff     static
"""
LINUX_NEIGH = """192.168.1.61 dev eth0 lladdr 30:86:2d:00:00:01 REACHABLE
192.168.1.9 dev eth0  FAILED
fe80::1 dev eth0 lladdr d4:01:c3:11:22:33 router STALE
"""
MAC_ARP = """? (192.168.1.67) at e4:d1:24:d:0:5 on en0 ifscope [ethernet]
? (192.168.1.5) at (incomplete) on en0 ifscope [ethernet]
"""
nb = af.parse_neighbours(WIN_ARP)
check("neighbours: Windows arp -a", nb == {"192.168.1.1": "d4:01:c3:11:22:33",
                                           "192.168.1.50": "e0:1c:a7:00:00:02"}, nb)
nb = af.parse_neighbours(LINUX_NEIGH)
check("neighbours: Linux ip neigh (no IPv6, no FAILED)", nb == {"192.168.1.61": "30:86:2d:00:00:01"}, nb)
nb = af.parse_neighbours(MAC_ARP)
check("neighbours: macOS arp -a (short octets padded)", nb == {"192.168.1.67": "e4:d1:24:0d:00:05"}, nb)

V20 = ("show device info\r\nDevice model: C-360\r\nDevice MAC address: 30:86:2D:00:00:04\r\n"
       "Serial number: 30862D000004\r\nDevice version: 20.0.0\r\nDevice build: 20.0.0-179\r\n")
V11 = "version\r\nThe device model is C-260.\r\nDevice Version: [11.0.1]\r\nDevice Build: [11.0.1-49]\r\n"
V16 = "Device Model   : C-260\r\nDevice Build   : 16.1.0-51.6\r\n"
i = af.parse_device_info(V20)
check("device info: firmware 20 (model, build, MAC)",
      i == {"name": "C-360", "firmware": "20.0.0-179", "mac": "30:86:2d:00:00:04"}, i)
i = af.parse_device_info(V11)
check("device info: firmware 11 ('The device model is', bracketed build)",
      i["name"] == "C-260" and i["firmware"] == "11.0.1-49", i)
i = af.parse_device_info(V16)
check("device info: firmware 16 (aligned colons)", i["name"] == "C-260" and i["firmware"] == "16.1.0-51.6", i)
check("device info: nothing recognisable gives no model", af.parse_device_info("Invalid command")["name"] is None)

check("build number and short tag", af.build_num(T % "157-g0500cfe") == 157
      and af.short_tag(T % "157-g0500cfe") == "build 157" and af.build_num("v2026.10.17.1") is None)
check("the port's tag is read out of DISTRIB_DESCRIPTION",
      af.build_of("OpenWrt SNAPSHOT r38195-abc (arista %s)" % (T % "159-gdeadbee")) == T % "159-gdeadbee"
      and af.build_of("OpenWrt SNAPSHOT r1-abc") is None)
check("model key", af.model_key("arista,c-260") == "c260" and af.model_key("C-460") == "c460")
check("an update needs only the sysupgrade image; an install does not ask for it",
      list(af.files_for("c260", "update")) == ["sysupgrade"]
      and "sysupgrade" not in af.files_for("c260", "install") and "ubi" in af.files_for("c260", "install"))
check("only private networks are swept", af._private("192.168.1.9") and af._private("10.1.0.2")
      and af._private("172.19.160.1") and not af._private("8.8.8.8") and not af._private("172.32.0.1"))
msg = af.port_trouble("udp", 69, OSError("in use"))
check("a port in use is explained", "UDP 69 (TFTP)" in msg and "Another program" in msg, msg)
if os.name != "nt":
    msg = af.port_trouble("udp", 69, PermissionError("denied"))
    check("a privileged port says sudo", "sudo" in msg, msg)

# ----------------------------------------------------------- release choice ---


def rel(n, pre, models=("c260",)):
    assets = [{"name": "SHA256SUMS"}]
    for x in models:
        for purpose in ("install", "update"):
            assets += [{"name": f} for f in af.files_for(x, purpose).values()]
    return {"tag_name": T % ("%d-gabcdef0" % n), "prerelease": pre, "draft": False,
            "published_at": "2026-11-%02dT00:00:00Z" % (n % 28 + 1), "assets": assets}


def with_releases(rels, fn, *a):
    saved = af.list_releases, af.release_index
    af.list_releases = lambda channel="stable": sorted(
        rels, key=lambda r: af.build_num(r["tag_name"]), reverse=True)
    af.release_index = lambda r: None
    try:
        return fn(*a)
    finally:
        af.list_releases, af.release_index = saved


R = [rel(42, False), rel(112, True), rel(157, True, ("c260", "c200"))]
got, newer = with_releases(R, af.pick_release, ["c260"], None)
check("default: the newest accepted release, and it knows a newer test build exists",
      af.build_num(got["tag_name"]) == 42 and af.build_num(newer["tag_name"]) == 157)
got, newer = with_releases(R, af.pick_release, ["c200"], None)
check("default, a model with no accepted release: the newest test build",
      af.build_num(got["tag_name"]) == 157 and newer is None)
got, newer = with_releases(R, af.pick_release, ["c200"], "stable")
check("--channel stable never takes a test build", got is None)
got, newer = with_releases(R, af.pick_release, ["c260"], "testing")
check("--channel testing: the newest of either", af.build_num(got["tag_name"]) == 157)
got, newer = with_releases(R, af.pick_release, ["c460"], None)
check("a model no release has: nothing", got is None and newer is None)

# ------------------------------------------------------------ the guided run --


class Script:
    """Everything main() would do to an AP or GitHub, recorded instead."""

    def __init__(self, who, answers, releases=R, found=None, argv=()):
        self.who, self.answers, self.releases = who, list(answers), releases
        self.found = found
        self.argv = list(argv)
        self.calls, self.asked = [], []
        self.arp = {}

    def rec(self, name, ret=None):
        def f(*a, **k):
            self.calls.append(name)
            return ret(*a, **k) if callable(ret) else ret
        return f

    def ask(self, prompt, hint):
        self.asked.append(prompt.strip())
        if not self.answers:
            raise AssertionError("unexpected question: " + prompt)
        return self.answers.pop(0)

    def run(self):
        who = self.who
        card = {"model": who.get("model"), "mac": who.get("mac"), "firmware": who.get("firmware")}
        manifest = {"partitions": [{"file": "x"}] * 26, "mac": who.get("mac"), "name": who.get("name")}
        tmp = tempfile.mkdtemp(prefix="af-guided-")

        def verify_stock(shell, c):
            self.calls.append("verify_stock")
            c.update(version=who.get("firmware"), mac=who.get("mac"), pri="1")

        patches = dict(
            ask=self.ask,
            ensure_paramiko=lambda interactive: None,
            new_run_dir=lambda: tmp,
            discover=self.rec("discover", (self.found or [], ["192.168.1"])),
            probe_host=self.rec("probe_host", lambda ip, *a, **k: dict(who, ip=ip)),
            pc_address=lambda towards: "192.168.1.20",
            port_open=lambda ip, port=22, timeout=3: True,
            neighbours=lambda: dict(self.arp),
            release_files=self.rec("release_files", lambda a, model: ("i.itb", "f.ubi", "sums", a.tag or "local")),
            fetch_release=self.rec("fetch_release", lambda tag, models, cache, channel: (tmp, tag)),
            ensure_kexec=self.rec("ensure_kexec"),
            check_ports=self.rec("check_ports"),
            connect=self.rec("connect", (who["kind"], object(), card)),
            verify_stock=verify_stock,
            download_firmware=self.rec("download_firmware", manifest),
            pty_clean=self.rec("pty_clean"),
            boot_installer=self.rec("boot_installer"),
            wait_installer=self.rec("wait_installer", (object(), "192.168.1.53", "arista," + (who.get("model") or ""), "tmpfs")),
            do_install=self.rec("do_install"),
            do_restore=self.rec("do_restore"),
            do_update=self.rec("do_update", True),
            wait_gone=self.rec("wait_gone", True),
            wait_openwrt=self.rec("wait_openwrt", ("192.168.1.54", "arista,x")),
            _probe_openwrt=lambda ip, u, p, t: dict(who, ip=ip, build=self.releases[-1]["tag_name"]),
            find_stock=self.rec("find_stock", ("192.168.1.50", "Device build: 18.2.0-32\n")),
            load_manifest=lambda d: manifest,
            saved_backup=lambda model, mac: (None, None),
            list_releases=lambda channel="stable": sorted(
                self.releases, key=lambda r: af.build_num(r["tag_name"]), reverse=True),
            release_index=lambda r: None,
            http_get=lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used")),
        )
        patches.update(getattr(self, "extra", {}))
        saved = {k: getattr(af, k) for k in patches}
        for k, v in patches.items():
            setattr(af, k, v)
        old_argv, old_stdin = sys.argv, sys.stdin
        sys.argv = ["arista-field.py"] + self.argv

        class Tty(io.StringIO):
            def isatty(self):
                return True

        sys.stdin = Tty()
        out = io.StringIO()
        died = False
        try:
            with contextlib.redirect_stdout(out):
                try:
                    af.main()
                except SystemExit as e:
                    died = bool(e.code)
        finally:
            sys.argv, sys.stdin = old_argv, old_stdin
            for k, v in saved.items():
                setattr(af, k, v)
            if af.LOG.f:
                af.LOG.f.close()
                af.LOG.f = None
            af.THIS_RUN = None
            af.STEP.update(n=0, title="", hint="")
            af.PURPOSE = "install"
            af.VERBOSE = False
        self.out, self.died = out.getvalue(), died
        return self


STOCK_C260 = {"kind": "stock", "model": "c260", "name": "C-260", "mac": "30:86:2d:00:00:01",
              "firmware": "11.0.1-49.8"}
STOCK_C460 = dict(STOCK_C260, model="c460", name="C-460", mac="e0:1c:a7:00:00:02", firmware="18.2.0-32")
STOCK_W318 = dict(STOCK_C260, model="w318", name="W-318", mac="e4:d1:24:00:00:03")
# The one model with no confirmed way in (2026-11-15): the C-200.
STOCK_C200 = dict(STOCK_C260, model="c200", name="C-200", mac="e4:d1:24:00:00:06")
OWRT = lambda n: {"kind": "openwrt", "model": "c260", "name": "C-260", "mac": "30:86:2d:00:00:01",  # noqa: E731
                  "board": "arista,c-260", "series": "SNAPSHOT", "ram": False, "user": "root",
                  "password": "", "release": "x", "build": T % ("%d-gabcdef0" % n)}
WRITERS = {"boot_installer", "do_install", "do_restore", "do_update", "download_firmware", "connect"}

# 1. no options at all: found on the network, picked, installed
s = Script(STOCK_C260, ["1", "1", "y"], found=[dict(STOCK_C260, ip="192.168.1.191"),
                                             dict(OWRT(159), ip="192.168.1.61")]).run()
want = ["discover", "release_files", "check_ports", "connect", "verify_stock", "download_firmware",
        "boot_installer", "wait_installer", "do_install", "wait_openwrt"]
check("no options: find, pick, one menu, one question, then straight through to a running OpenWrt",
      not s.died and s.calls == want and len(s.asked) == 3, (s.calls, s.asked, s.out[-400:]))
check("  the question names the build and says the firmware is saved first",
      "Ready to install OpenWrt, build 42 (2026-" in s.out and "saved to this PC first" in s.out, s.out)
check("  it mentions the newer test build once", s.out.count("--channel testing") == 1)
check("  it ends with where the AP is and what to do next",
      "Done.  OpenWrt is running on this C-260, at 192.168.1.54." in s.out and "http://192.168.1.54" in s.out)
lines = [l for l in s.out.splitlines() if l.strip()]
check("  the whole run is under 45 lines on screen, with no clock and no shouting",
      len(lines) < 45 and not any(l.startswith("[") for l in lines)
      and not any(w in s.out for w in ("WARNING", "ERROR", "DANGER", "!!")), len(lines))
check("  six numbered steps", [l[:3] for l in lines if l[1:3] == "/6"] == ["%d/6" % i for i in range(1, 7)],
      [l for l in lines if "/6" in l])

# 2. saying no changes nothing
s = Script(STOCK_C260, ["1", "n"], argv=["--host", "192.168.1.191"]).run()
check("answering no: nothing is fetched, nothing reaches the AP",
      not s.died and not (set(s.calls) & (WRITERS | {"release_files"})) and "Nothing was changed." in s.out,
      s.calls)

# 3. the menu's 'nothing'
s = Script(STOCK_C260, ["3"], argv=["--host", "192.168.1.191"]).run()
check("choosing nothing: nothing reaches the AP", not s.died and not (set(s.calls) & WRITERS), s.calls)

# 4. only save the firmware
s = Script(STOCK_C260, ["2"], argv=["--host", "192.168.1.191"]).run()
check("only save the firmware: no question, no release, no installer, and the AP is left to restart",
      not s.died and s.calls == ["probe_host", "check_ports", "connect", "verify_stock", "download_firmware",
                                 "pty_clean"]
      and "nothing on the AP was changed" in s.out, (s.calls, s.out[-300:]))

# 5. a model with no confirmed way in is refused before anything.  Every model
#    in the table installs now (the C-200 was the last, 2026-11-15), so one is
#    marked as not installable for this check: the refusal is still what a new
#    model gets until a unit has proven its path.
af.MODELS["c200"]["field_install"] = False
s = Script(STOCK_C200, [], argv=["--host", "192.168.1.63"]).run()
check("a model the tool cannot install: refused before any login for a shell, with the by-hand page",
      s.died and not (set(s.calls) & WRITERS) and "c200/INSTALL.md" in s.out
      and "Nothing was changed" in s.out, s.out[-400:])
del af.MODELS["c200"]["field_install"]

# 6. C-460: the helper files are fetched before the AP is touched, and no TFTP port is asked for
ports = []
s = Script(STOCK_C460, ["1", "y"], argv=["--host", "192.168.1.50"],
           releases=[rel(200, True, ("c460",))])
s.extra = {"check_ports": lambda tftp, sink: ports.append((tftp, sink))}
s.run()
check("C-460: helper files before the connect; TFTP not needed (kexec), the firmware sink is",
      not s.died and s.calls.index("ensure_kexec") < s.calls.index("connect") and ports == [(False, True)],
      (s.calls, ports, s.out[-300:]))
check("  a model with only test builds says so in the question", "build 200 (testing, " in s.out, s.out)

# 7. no published build for the model
s = Script(STOCK_C460, [], argv=["--host", "192.168.1.50"]).run()
check("a model no release carries: stops before the menu, nothing touched",
      s.died and "no published build has the C-460" in s.out and not (set(s.calls) & WRITERS), s.out[-300:])

# 8. an AP already on OpenWrt, behind the newest test build: update offered and run
s = Script(OWRT(112), ["1", "y"], argv=["--host", "192.168.1.61"]).run()
check("OpenWrt on build 112 (a test build): offered build 157, not the older accepted 42",
      not s.died and "Update OpenWrt to build 157 (testing" in s.out and "build 42" not in s.out
      and s.calls == ["probe_host", "fetch_release", "do_update", "wait_gone"], (s.calls, s.out[-500:]))
check("  it ends by naming what the AP runs now", "now runs OpenWrt build 157" in s.out, s.out[-300:])

# 9. an AP on the accepted build: stays on accepted, told about the test build
s = Script(OWRT(42), [], argv=["--host", "192.168.1.61"]).run()
check("OpenWrt on the accepted build: nothing to do, no question at all",
      not s.died and "already runs the newest accepted build" in s.out and not s.asked
      and not (set(s.calls) & WRITERS), (s.asked, s.out[-300:]))

# 10. an AP ahead of everything published is never offered a downgrade
s = Script(OWRT(198), [], argv=["--host", "192.168.1.57"]).run()
check("OpenWrt on a build newer than any published: no update offered",
      not s.died and "newer build than the newest published" in s.out and not s.asked, s.out[-300:])
s = Script(OWRT(198), [], argv=["--host", "192.168.1.57", "--update", "--yes"]).run()
check("  and --update refuses to go backwards", s.died and "--tag" in s.out and "do_update" not in s.calls)

# 11. the flags still work without a question
s = Script(STOCK_C260, [], argv=["--host", "192.168.1.191", "--install", "--yes", "--no-wait"]).run()
check("--install --yes --no-wait: no questions, five steps, no waiting",
      not s.died and not s.asked and "wait_openwrt" not in s.calls and "do_install" in s.calls
      and "5/5" in s.out and "6/" not in s.out, (s.calls, s.out[-300:]))

# 12. a changed password is asked for, not guessed
seen = []


def locked_then_ok(ip, user=None, password=None, mac=None, timeout=6):
    seen.append((user, password))
    return dict(STOCK_C260, ip=ip) if password == "s3cret" else {"ip": ip, "kind": "locked"}


import getpass  # noqa: E402
real_getpass = getpass.getpass
getpass.getpass = lambda prompt="": "s3cret"
try:
    s = Script(STOCK_C260, ["", "3"], argv=["--host", "192.168.1.191"])
    s.extra = {"probe_host": locked_then_ok}
    s.run()
finally:
    getpass.getpass = real_getpass
check("a refused default login: the password is asked for once, then the menu",
      not s.died and seen == [(None, None), ("config", "s3cret")] and "s3cret" not in s.out, (seen, s.out[-300:]))

# 12b. an address whose MAC is not an Arista one is questioned before any login
s = Script(STOCK_C260, ["n"], argv=["--host", "192.168.1.1"])
s.arp = {"192.168.1.1": "d4:01:c3:11:22:33"}
s.run()
check("a typed address with a foreign MAC: asked first, and 'no' means no login is tried",
      s.died and "probe_host" not in s.calls and "does not look like an Arista access point" in s.out,
      (s.calls, s.out[-300:]))
s = Script(STOCK_C260, ["3"], argv=["--host", "192.168.1.191"])
s.arp = {"192.168.1.191": "30:86:2d:00:00:01"}
s.run()
check("  an Arista MAC is not questioned", not s.died and len(s.asked) == 1 and "probe_host" in s.calls, s.asked)

# 13. without a terminal and without --host it says what to give
old_stdin, old_argv = sys.stdin, sys.argv
sys.stdin = io.StringIO()               # not a terminal
sys.argv = ["arista-field.py"]
out = io.StringIO()
try:
    with contextlib.redirect_stdout(out):
        try:
            af.main()
            died = False
        except SystemExit as e:
            died = bool(e.code)
finally:
    sys.stdin, sys.argv = old_stdin, old_argv
check("no terminal and no --host: stops and says so", died and "--host" in out.getvalue(), out.getvalue())

# 14. the ports check finds a busy port
busy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
busy.bind(("0.0.0.0", 0))
port = busy.getsockname()[1]
s2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s2.bind(("0.0.0.0", port))
    clash = False
except OSError as e:
    clash = "cannot open" in af.port_trouble("udp", port, e)
finally:
    s2.close()
    busy.close()
check("a busy port is caught as a sentence", clash)

# 15. progress lines never reach a pipe
out = io.StringIO()
with contextlib.redirect_stdout(out):
    with af.Heartbeat("working"):
        pass
    af.status("half way")
    af.say("detail")
    af.tell("headline")
check("piped output: headlines only, no progress lines, no detail", out.getvalue() == "headline\n", repr(out.getvalue()))

print("\n%d checks, %d failed" % (len(RESULTS), RESULTS.count(False)))
sys.exit(1 if RESULTS.count(False) else 0)
