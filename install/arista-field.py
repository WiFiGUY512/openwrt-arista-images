#!/usr/bin/env python3
"""Put OpenWrt on an Arista access point in the field -- or put that AP's own
firmware back -- with no serial cable.

One script for every model: what differs (FIT configuration, partition names,
installer, NOR size) is in MODELS, and nothing else is per-model.

The rule this tool is built around: an AP in the wild may be the only copy of
its own firmware.  So it always downloads the AP's flash first, verifies every
byte of it, and only then offers to change anything.

    1. Connect to the AP on its factory firmware (SSH as config, then
       prep_upgrade.sh's eval gives a root shell; nothing is written by this).
    2. Verify the AP: model, firmware version, flash layout, U-Boot variables
       the fallback needs.  Refuses to go on if anything is unexpected.
    3. Download all of its flash over SSH (gzip, streamed to this PC, never
       through the AP's tmpfs), hashing both ends and comparing.
    4. Ask: install OpenWrt here, or put this AP's original firmware back.
    5. Do it, verify it, and say exactly what was written.

    python arista-field.py --host 192.168.1.191              # menu
    python arista-field.py --host 192.168.1.191 --install
    python arista-field.py --host 192.168.1.191 --backup-only
    python arista-field.py --restore backups/c230-30862d73b66f   # from OpenWrt or Arista
"""

import argparse
import atexit
import concurrent.futures
import contextlib
import gzip
import hashlib
import importlib.util
import json
import os
import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = "WiFiGUY512/openwrt-arista-images"

# ---------------------------------------------------------------------------
# Helpers carried in this file on purpose: a field installer has to be ONE
# file somebody can download and run.  These are the ones proven on the bench
# unit (SSH, the pty, the release fetch); they lived in arista-net-install.py
# (Griff's, same project) and are reproduced here unchanged.
# ---------------------------------------------------------------------------

class Log:
    def __init__(self):
        self.f = None
        self.lock = threading.Lock()

    def open(self, path):
        self.f = open(path, "a", encoding="utf-8", errors="replace")

    def write(self, text):
        with self.lock:
            if self.f:
                self.f.write(text)
                self.f.flush()


LOG = Log()


class Pty:
    """An interactive SSH shell (a pty), read into a buffer so we can wait
    for prompts.  Both the config shell and the injected root shell are
    interactive, so this is how they are driven."""

    def __init__(self, chan):
        self.chan = chan
        self.buf = ""
        self.lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            try:
                d = self.chan.recv(4096)
            except Exception:
                break
            if not d:
                break
            t = d.decode("utf-8", "replace")
            LOG.write(t)
            with self.lock:
                self.buf += t

    def mark(self):
        with self.lock:
            return len(self.buf)

    def since(self, mark):
        with self.lock:
            return self.buf[mark:]

    def send(self, text):
        self.chan.send(text + "\r")

    def wait(self, pattern, timeout, mark=None, fail=None):
        rx = re.compile(pattern, re.M | re.S) if isinstance(pattern, str) else pattern
        fx = re.compile(fail, re.M | re.S) if fail else None
        start = self.mark() if mark is None else mark
        end = time.time() + timeout
        while time.time() < end:
            text = self.since(start)
            if fx and fx.search(text):
                return None
            m = rx.search(text)
            if m:
                return m
            time.sleep(0.05)
        return None

    def run(self, cmd, timeout=60):
        """A command's output between two markers, and its exit code."""
        tag = "Q" + secrets.token_hex(4)
        mk = self.mark()
        self.send("echo %sS; %s; echo %s=$?" % (tag, cmd, tag))
        m = self.wait(r"(?:^|\n)%sS\r?\n(.*?)(?:\r?\n)?%s=(\d+)" % (tag, tag), timeout, mk)
        if not m:
            die("no answer from the AP's root shell within %d s to: %s" % (timeout, cmd))
        return m.group(1).replace("\r", "").strip(), int(m.group(2))

    def must(self, cmd, timeout=60, what=None):
        out, rc = self.run(cmd, timeout)
        if rc != 0:
            die("%s failed (exit %d): %s\n%s" % (what or "a command on the AP", rc, cmd, out[-800:]))
        return out


def ssh_open(host, user, password, timeout=15, tries=1, delay=2):
    """An SSH client.  OpenWrt's root has no password, and dropbear accepts a
    blank one only through the "none" method -- so that is asked for by name
    rather than left to paramiko's password path."""
    import paramiko
    last = None
    for _ in range(tries):
        t = None
        try:
            sock = socket.create_connection((host, 22), timeout)
            t = paramiko.Transport(sock)
            t.banner_timeout = timeout
            t.start_client(timeout=timeout)
            if password == "":
                t.auth_none(user)
            else:
                t.auth_password(user, password)
            c = paramiko.SSHClient()
            c._transport = t
            return c
        except Exception as e:
            last = e
            if t is not None:
                try:
                    t.close()
                except Exception:
                    pass
            time.sleep(delay)
    raise last


def ssh_exec(con, cmd, timeout=120):
    """One command on the AP's installer or OpenWrt, no pty."""
    chan = con.get_transport().open_session()
    chan.settimeout(30)
    out = b""
    err = b""
    end = time.time() + timeout
    rc = -1
    try:
        chan.exec_command(cmd)
        while True:
            if chan.recv_ready():
                out += chan.recv(65536)
            elif chan.recv_stderr_ready():
                err += chan.recv_stderr(65536)
            elif chan.exit_status_ready():
                break
            elif time.time() > end:
                err += b"\n(timed out after %d s)\n" % timeout
                break
            else:
                time.sleep(0.05)
        while chan.recv_ready():
            out += chan.recv(65536)
        while chan.recv_stderr_ready():
            err += chan.recv_stderr(65536)
        if chan.exit_status_ready():
            rc = chan.recv_exit_status()
    except Exception as e:
        err += ("\n(%s)\n" % e).encode()
    finally:
        try:
            chan.close()
        except Exception:
            pass
    text = (out + err).decode("utf-8", "replace")
    LOG.write(text)
    return text, rc


def ssh_must(con, cmd, timeout=120, what=None):
    out, rc = ssh_exec(con, cmd, timeout)
    if rc != 0:
        die("%s failed (exit %d): %s\n%s" % (what or "a command on the AP", rc, cmd, out[-800:]))
    return out


def files_for(model):
    d = "c-" + model[1:]
    return {
        "itb": "openwrt-qualcommax-ipq807x-arista_%s-initramfs-uImage.itb" % d,
        "ubi": "openwrt-qualcommax-ipq807x-arista_%s-squashfs-factory.ubi" % d,
        "sums": "%s-install-SHA256SUMS" % model,
    }


def http_get(url, accept=None):
    req = urllib.request.Request(url, headers={"User-Agent": "arista-net-install"})
    if accept:
        req.add_header("Accept", accept)
    return urllib.request.urlopen(req, timeout=60)


def fetch_release(tag, models, cache):
    api = "https://api.github.com/repos/%s/releases/%s" % (REPO, "tags/" + tag if tag else "latest")
    rel = json.load(http_get(api, "application/vnd.github+json"))
    tag = rel["tag_name"]
    assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
    d = os.path.join(cache, tag)
    os.makedirs(d, exist_ok=True)
    say("Release %s" % tag)
    want = ["SHA256SUMS"] + [f for m in models for f in files_for(m).values()]
    for name in want:
        if name not in assets:
            die("the release has no %s" % name)
        path = os.path.join(d, name)
        if not os.path.exists(path):
            say("  downloading %s" % name)
            with http_get(assets[name]) as r, open(path + ".part", "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
            os.replace(path + ".part", path)
    check_sums(d, want)
    return d, tag


def check_sums(d, names):
    sums = {}
    with open(os.path.join(d, "SHA256SUMS")) as f:
        for line in f:
            parts = line.split()
            if len(parts) == 2:
                sums[parts[1].lstrip("*")] = parts[0]
    for name in names:
        if name == "SHA256SUMS":
            continue
        if sums.get(name) != sha256_file(os.path.join(d, name)):
            die("%s does not match the release's SHA256SUMS; delete %s and run again" % (name, d))
    say("  files checked against SHA256SUMS")


def pc_address(towards):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((towards, 9))
        return s.getsockname()[0]
    finally:
        s.close()



class _Helpers:
    """Attribute view of this module, so the rest of the file can say m.say(...)
    whether it is run as __main__ or loaded by path from a test harness."""

    def __getattr__(self, name):
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(name)


m = _Helpers()


def _up(rel, default):
    """The first of these relative paths that exists, else the last one's parent.

    The script runs from three places: this project (tools/arista-field), a
    checkout of the published repo (install/), and anywhere somebody copied it
    to.  So nothing may assume a fixed depth.
    """
    for r in rel:
        d = os.path.abspath(os.path.join(HERE, *r))
        if os.path.isdir(d):
            return d
    return os.path.abspath(os.path.join(HERE, *default))


def _find_tools():
    """The aarch64 mtd tools the revert path needs, wherever they live."""
    return _up((("tools", "aarch64"), ("..", "tools", "aarch64"),
                ("..", "..", "build", "field-tools", "aarch64")),
               ("..", "tools", "aarch64"))


def _find_backups():
    """Where AP firmware is kept: the project's backups/, not next to a copy."""
    return _up((("..", "..", "backups"), ("..", "backups"), ("backups",)),
               ("..", "backups"))


def _find_releases():
    """The release cache: the project's firmware/releases, or an older home one."""
    d = _up((("..", "..", "firmware", "releases"), ("..", "firmware", "releases")),
            ("..", "firmware", "releases"))
    if os.path.isdir(d):
        return d
    legacy = os.path.join(os.path.expanduser("~"), "arista-openwrt-files")
    return legacy if os.path.isdir(legacy) else d


BACKUPS = _find_backups()
RELEASES = _find_releases()
TOOLS = _find_tools()

# Every line goes to the screen with a clock, and every long wait says so out
# loud: an installer the operator cannot see working is one they will kill.
T0 = time.time()


def ask(prompt, hint):
    """A question needs a terminal.  With piped input `input()` blocks for ever
    and the run looks alive but is doing nothing (2026-10-25)."""
    if not sys.stdin.isatty():
        die("this run needs an answer to\n    %s\nand there is no terminal to ask on. "
            "Add %s, or run it from a terminal." % (prompt.strip(), hint))
    return input(prompt)


def say(msg=""):
    """Every line carries the clock, on screen and in the log."""
    line = "" if msg is None else str(msg)
    if getattr(LOG, "f", None):
        LOG.write("\n### %s %s\n" % (time.strftime("%H:%M:%S"), line))
    print(("[%s] %s" % (time.strftime("%H:%M:%S"), line)) if line else "", flush=True)


def phase(text):
    say("")
    say("=" * 72)
    say("%s   (+%ds)" % (text, int(time.time() - T0)))
    say("=" * 72)


TOTAL_STEPS = 7
STEP = {"n": 0, "title": "", "hint": ""}
THIS_RUN = None


def step(title, why, hint):
    """Announce a step: what it does, why, and what to try if it fails."""
    STEP["n"] += 1
    STEP["title"] = title
    STEP["hint"] = hint
    say("")
    say("--- step %d/%d  %s" % (STEP["n"], TOTAL_STEPS, title))
    for line in str(why).splitlines():
        say("      why:  " + line.strip())
    for line in str(hint).splitlines():
        say("      fail: " + line.strip())


def die(msg):
    """Stop, and say which step failed and what to do about it."""
    say("")
    say("STOPPED: %s" % msg)
    if STEP["n"]:
        say("  step %d/%d:   %s" % (STEP["n"], TOTAL_STEPS, STEP["title"]))
        say("  what to try: %s" % STEP["hint"])
    if THIS_RUN is not None:
        say("  env writes so far:   %s" % (", ".join(THIS_RUN.env_writes) or "none"))
        say("  flash writes so far: %s" % (", ".join(THIS_RUN.flash_writes) or "none"))
    say("  full log: %s" % (os.path.join(os.getcwd(), "field-run-*") + "\\run.log"))
    sys.exit(1)


class Heartbeat:
    """Prints 'still working' every few seconds during a slow step, so a long
    operation never looks like a hang."""

    def __init__(self, what, every=15):
        self.what = what
        self.every = every
        self.stop = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        start = time.time()
        while not self.stop.wait(self.every):
            say("    ... %s: still working (%d s)" % (self.what, int(time.time() - start)))

    def __enter__(self):
        say("    %s..." % self.what)
        self.t.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        return False

NETMASK = "255.255.255.0"

MODELS = {
    "c230": {
        "name": "C-230",
        "fit_cfg": "config@ac02",
        "installer": "arista-c230-install",
        "slot0": ("rootfs", "ubi"),
        "slot1": ("rootfs_1",),
        "appsblenv": ("0:APPSBLENV", "appsblenv"),
        "need_nor": ("0:HLOS_1",),
        "need_env": ("bootcmd", "ba0", "ba1", "ubootscript"),
        "nor_total": 32 * 1024 * 1024,
        "slot_size": 0x10000000,
    },
    "c260": {
        "name": "C-260",
        # OpenWrt main's FIT for this board has one configuration, config@hk01;
        # the 25.12 port used config@1.  The images in the release are main.
        "fit_cfg": "config@hk01",
        "installer": "arista-c260-install",
        # Slot 0 is "ubi" in the C-260's device tree (25.12's name) and "rootfs"
        # in main's; Arista's own firmware calls it "rootfs".  Both are accepted.
        "slot0": ("rootfs", "ubi"),
        "slot1": ("rootfs_1",),
        "appsblenv": ("0:APPSBLENV", "appsblenv"),
        "need_nor": ("0:HLOS_1",),
        "need_env": ("bootcmd", "ba0", "ba1", "ubootscript"),
        # Measured from a C-260's own /proc/mtd (backups/c260-30862db33c8f):
        # the same partition set as the C-230's minus DEVCERT, and it adds up
        # to exactly the same 32 MiB.
        "nor_total": 32 * 1024 * 1024,
        "slot_size": 0x10000000,
        # Arista normally runs the C-260 from slot 1 (pri=1), so slot 0 is the
        # spare.  pri=0 means slot 0 holds the running Arista: the installer
        # warns, and the backup taken first is what makes that recoverable.
        "pri_note": "Arista usually runs a C-260 from slot 1 (pri=1).",
    },
}


def sha256_file(path, limit=None):
    h = hashlib.sha256()
    left = limit
    with open(path, "rb") as f:
        while True:
            n = 1 << 20 if left is None else min(1 << 20, left)
            if n <= 0:
                break
            b = f.read(n)
            if not b:
                break
            h.update(b)
            if left is not None:
                left -= len(b)
    return h.hexdigest()


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def gunzip_sha_md5(gz_path, raw_path):
    """Decompress a backup .gz and return (sha256, md5) of the raw bytes."""
    import gzip
    hs, hm = hashlib.sha256(), hashlib.md5()
    with gzip.open(gz_path, "rb") as g, open(raw_path, "wb") as f:
        for b in iter(lambda: g.read(1 << 20), b""):
            f.write(b)
            hs.update(b)
            hm.update(b)
    return hs.hexdigest(), hm.hexdigest()


# --- TFTP, with the option negotiation U-Boot asks for -----------------------

class Tftp:
    """Read-only TFTP that answers blksize/tsize, so 23 MB takes seconds rather
    than 46,000 512-byte round trips."""

    def __init__(self, files, port=69):
        self.files = files
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", port))
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        while True:
            try:
                pkt, peer = self.sock.recvfrom(1500)
            except OSError:
                return
            if len(pkt) < 2 or struct.unpack("!H", pkt[:2])[0] != 1:
                continue
            parts = pkt[2:].split(b"\0")
            name = parts[0].decode(errors="replace").lstrip("/")
            opts = {}
            for i in range(2, len(parts) - 1, 2):
                if parts[i]:
                    opts[parts[i].decode(errors="replace").lower()] = parts[i + 1].decode(errors="replace")
            threading.Thread(target=self._session, args=(peer, name, opts), daemon=True).start()

    def _session(self, peer, name, opts):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", 0))
        s.settimeout(3)
        path = self.files.get(name)
        if not path:
            s.sendto(struct.pack("!HH", 5, 1) + b"no such file\0", peer)
            m.say("  TFTP: %s asked for %r (not served)" % (peer[0], name))
            s.close()
            return
        data = open(path, "rb").read()
        blk = 512
        if "blksize" in opts:
            try:
                blk = max(8, min(int(opts["blksize"]), 1468))
            except ValueError:
                blk = 512
        oack = b""
        if blk != 512:
            oack += b"blksize\0%d\0" % blk
        if "tsize" in opts:
            oack += b"tsize\0%d\0" % len(data)
        m.say("  TFTP: %s wants %s (%.1f MiB)" % (peer[0], name, len(data) / 1048576.0))
        try:
            if oack:
                s.sendto(struct.pack("!H", 6) + oack, peer)
                pkt, _ = s.recvfrom(1500)
                if len(pkt) < 4 or struct.unpack("!HH", pkt[:4]) != (4, 0):
                    return
            t0 = time.time()
            wire, off = 1, 0
            while True:
                chunk = data[off:off + blk]
                last = len(chunk) < blk
                pkt = struct.pack("!HH", 3, wire) + chunk
                tries = 0
                while True:
                    s.sendto(pkt, peer)
                    try:
                        ack, _ = s.recvfrom(1500)
                    except socket.timeout:
                        tries += 1
                        if tries > 8:
                            m.say("  TFTP: no answer at block %d" % wire)
                            return
                        continue
                    if len(ack) >= 4 and struct.unpack("!HH", ack[:4])[0] == 4 \
                            and struct.unpack("!H", ack[2:4])[0] == wire:
                        break
                off += len(chunk)
                if last:
                    break
                wire = (wire + 1) & 0xFFFF
            m.say("  TFTP: sent in %.1f s (%.1f MiB/s, %d-byte blocks)" %
                  (time.time() - t0, len(data) / 1048576.0 / max(time.time() - t0, 0.001), blk))
        except Exception as e:                        # noqa: BLE001
            m.say("  TFTP: %s" % e)
        finally:
            s.close()


# --- 1. connect --------------------------------------------------------------

def shell_alive(pty, timeout=12):
    """Is the injected shell still there?  Non-fatal: Arista's upgrade helper
    kills it after about a minute, and a long backup or a slow answer at the
    menu can easily outlive it."""
    if pty is None:
        return False
    try:
        tag = "P" + secrets.token_hex(3)
        pty.send("echo %s" % tag)
        return bool(pty.wait(tag, timeout))
    except Exception:
        return False


def ensure_shell(run, host, user, password, why):
    """Return a live factory shell, reconnecting if the old one expired."""
    if shell_alive(run.shell):
        return run.shell
    m.say("")
    m.say("  The AP's shell has gone (Arista's upgrade helper times out after about")
    m.say("  a minute; %s took longer). Reconnecting." % why)
    try:
        run.shell.chan.get_transport().close()
    except Exception:
        pass
    wait_reboot(host, 240)
    kind, shell, card = connect(host, user, password)
    if kind != "stock":
        m.die("the AP came back on %s, not its factory firmware" % kind)
    run.shell = shell
    m.say("  reconnected.")
    return shell


class ShellNotReady(Exception):
    """The factory shell accepted the upgrade but never handed back a shell.

    Seen when the AP's previous upgrade helper still holds the terminal: the
    typed commands then go to the config shell ("Invalid command"), because the
    eval'd shell never became the foreground process.  Closing the session makes
    Arista's own helper time out and reboot the AP, so the answer is to wait for
    that reboot and try again, not to give up."""


def wait_reboot(host, up_to=300):
    """Wait for the AP to go down and come back (its own helper reboots it)."""
    m.say("  waiting for the AP to restart itself (up to %d s)" % up_to)
    start = time.time()
    went_down = False
    while time.time() - start < up_to:
        try:
            socket.create_connection((host, 22), 2).close()
            if went_down:
                m.say("  back at %s after %.0f s" % (host, time.time() - start))
                return True
        except Exception:
            if not went_down:
                went_down = True
                m.say("  [%ds] the AP is down; waiting for it" % int(time.time() - start))
        time.sleep(4)
    return went_down


def connect(host, user=None, password=None):
    """Connect, retrying once through the AP's own restart when its shell is in
    the leftover-helper state."""
    for attempt in (1, 2):
        try:
            return _connect_once(host, user, password)
        except ShellNotReady as e:
            m.say("")
            m.say("  The upgrade was accepted but the shell never came back.")
            m.say("  Detail: %s" % str(e).strip()[-200:])
            if attempt == 2:
                m.die("the AP keeps answering with its config shell instead of the "
                      "injected one.  Power-cycle it, wait for it to come up, and run "
                      "again; nothing has been written to it.")
            wait_reboot(host)
            m.say("  trying again.")


def _connect_once(host, user=None, password=None):
    """Returns (kind, pty_or_conn, card).  kind is 'stock' or 'openwrt'.

    Credentials: OpenWrt as installed here has root with no password; Arista's
    factory firmware has `config`, whose factory password is `config` (checked
    against md5crypt in the C-260's own shadow), and a *locked* root, so root
    can never log in there.  A unit whose password was changed needs
    --user/--password."""
    m.say("")
    m.say("Connecting to %s." % host)
    tried = []
    if user is not None:
        tried.append("%s/%s" % (user, "*" * len(password or "")))
    # An AP already on our OpenWrt has root and no password.
    try:
        con = m.ssh_open(host, user or "root", "" if user else "", timeout=10)
        board = m.ssh_exec(con, "cat /tmp/sysinfo/board_name 2>/dev/null", 20)[0].strip()
        if board.startswith("arista,"):
            rel = m.ssh_exec(con, "cat /etc/openwrt_release 2>/dev/null | tr '\\n' ' '", 20)[0]
            mac = m.ssh_exec(con, "cat /sys/class/net/br-lan/address 2>/dev/null", 20)[0].strip()
            model = board.split(",")[-1].replace("-", "")
            if model not in MODELS:
                m.die("this is an Arista %s, which this tool has no entry for" % board)
            m.say("  This AP is on OpenWrt already: %s" % rel.strip()[:110])
            return "openwrt", con, {"board": board, "model": model, "mac": mac.lower(),
                                    "release": rel.strip()}
    except Exception:
        pass

    # Factory firmware: SSH as config, then the upgrade eval.
    fuser = user or "config"
    fpass = password if user else "config"
    tried.append("%s/%s" % (fuser, "*" * len(fpass or "")))
    try:
        client = m.ssh_open(host, fuser, fpass, timeout=10, tries=2, delay=2)
    except Exception as e:
        m.die("cannot log in to %s (tried %s): %s\n"
              "Is the AP up, and at that address?  On Arista's firmware root is\n"
              "locked and the account is 'config' (factory password 'config'); if\n"
              "this unit's was changed, give it with --user config --password ..."
              % (host, " and ".join(tried), e))
    pty = m.Pty(client.invoke_shell(width=200, height=60))
    if not pty.wait(r"\[config\]\$", 30):
        m.die("%s does not answer with Arista's config shell; not touching it." % host)
    mk = pty.mark()
    pty.send("version")
    time.sleep(3)
    text = pty.since(mk)
    model = None
    for key, d in MODELS.items():
        if d["name"] in text:
            model = key
    if not model:
        m.die("the AP does not say which model it is:\n%s" % text.strip()[-500:])
    fw = None
    for pat in (r"Device Build:\s*\[([^\]]+)\]", r"Device Version:\s*\[([^\]]+)\]"):
        mm = re.search(pat, text)
        if mm:
            fw = mm.group(1).strip()
            break
    m.say("  This AP is an Arista %s, firmware %s." % (MODELS[model]["name"], fw or "?"))
    pty.send("upgrade")
    if not pty.wait(r"Continue\? \(\[y\]/n\)", 30):
        m.die("'upgrade' did not offer to continue: offline mode, or another build.")
    pty.send("y")
    pty.wait(r"Enter upgrade build version string", 30)
    pty.send("1")
    pty.wait(r"Enter upgrade build vendor version", 30)
    pty.send("1")
    if not pty.wait(r"Server IP Address or Upgrade path", 30):
        m.die("the upgrade flow did not ask for a server address")
    pty.send("http://%s/;sh</dev/tty>/dev/tty;" % host)
    if not pty.wait(r"Confirm\? \(\[y\]/n\)", 90):
        m.die("no confirm prompt:\n%s" % pty.since(0).strip()[-800:])
    pty.send("y")
    time.sleep(10)
    out, rc = pty.run("id", 120)
    if "uid=0" not in out:
        # Drop the session: that is what makes Arista's helper give up and
        # restart the AP, which is the only way out of this state without hands.
        try:
            client.close()
        except Exception:
            pass
        raise ShellNotReady(out[-300:] or "(no output)")
    global _STOCK, _STOCK_HOST
    _STOCK = pty
    _STOCK_HOST = host
    return "stock", pty, {"model": model, "firmware": fw}


def pty_clean(pty, host, wait=True):
    """A session on the factory firmware ends with a restart: Arista's own
    prep_upgrade.sh reboots the AP the moment this shell closes, so we do it
    ourselves and wait for it to answer again."""
    m.say("")
    m.say("The AP restarts now. Arista's prep_upgrade.sh does that by itself when")
    m.say("the injected shell closes; doing it here just makes it predictable.")
    try:
        pty.send("sync; /sbin/reboot")
    except Exception:
        return
    global _REBOOTED
    _REBOOTED = True
    if not wait:
        return
    time.sleep(25)
    end = time.time() + 300
    while time.time() < end:
        try:
            socket.create_connection((host, 22), 3).close()
            m.say("  back at %s." % host)
            return
        except Exception:
            time.sleep(5)
    m.say("  it has not answered yet; it will come back on its own.")


# Whatever way this program ends -- including a refused check in the middle --
# the stock AP must not be left with sensord stopped.
_STOCK = None
_REBOOTED = False
_STOCK_HOST = None


def _cleanup_stock():
    """If we stop early (a refused check, Ctrl-C), still leave the AP in a
    defined state rather than with the upgrade service half-run."""
    if _STOCK is not None and not _REBOOTED:
        try:
            pty_clean(_STOCK, _STOCK_HOST)
        except Exception:
            pass


atexit.register(_cleanup_stock)


# --- 2. verify ---------------------------------------------------------------

NAND_ERASE = 0x20000          # 128 KiB PEBs; NOR partitions are 64 KiB


def is_ubi_volume(part):
    """The pseudo-MTD devices a running system creates for UBI volumes."""
    return part["name"] in ("ubi_rootfs", "rootfs_data")


def mtd_table(text):
    rows = []
    for line in text.splitlines():
        mm = re.match(r"(mtd\d+):\s+([0-9a-f]+)\s+([0-9a-f]+)\s+\"(.*)\"", line.strip())
        if mm:
            rows.append({"dev": mm.group(1), "size": int(mm.group(2), 16),
                         "erase": int(mm.group(3), 16), "name": mm.group(4)})
    return rows


def find_part(rows, names):
    for name in names:
        for r in rows:
            if r["name"].lower() == name.lower():
                return r
    return None


def verify_stock(pty, card):
    """Everything that must be true before we change anything."""
    m.say("")
    m.say("Verifying the AP.")
    hard = pty.must("uname -srm", 30, "uname")
    card["version"] = card.get("firmware") or "?"
    m.say("  firmware   %s" % card["version"])
    m.say("  kernel     %s" % hard.strip())
    card["mac"] = pty.must("cat /sys/class/net/br0/address 2>/dev/null", 30, "MAC").strip().lower()
    m.say("  MAC        %s" % card["mac"])
    mtd = pty.must("cat /proc/mtd", 30, "/proc/mtd")
    card["mtd"] = mtd_table(mtd)
    card["mtd_raw"] = mtd
    if not card["mtd"]:
        m.die("could not read the flash layout")
    d = MODELS[card["model"]]
    s0 = find_part(card["mtd"], d["slot0"])
    s1 = find_part(card["mtd"], d["slot1"])
    env = find_part(card["mtd"], d["appsblenv"])
    # Which partitions are NOR is not a matter of position: the C-230 lists its
    # NOR before the NAND slots, the C-260 lists them after.  Erase size tells
    # them apart (64 KiB NOR, 128 KiB NAND), and the UBI volume pseudo-devices
    # that a running system exposes are not partitions at all.
    nor = [r for r in card["mtd"]
           if r is not s0 and r is not s1
           and not is_ubi_volume(r) and r["erase"] < NAND_ERASE]
    nand = [r for r in card["mtd"] if r is s0 or r is s1]
    problems = []
    if not s0:
        problems.append("no slot 0 partition (%s)" % "/".join(d["slot0"]))
    elif s0["size"] != d["slot_size"]:
        problems.append("slot 0 is %d bytes, expected %d" % (s0["size"], d["slot_size"]))
    if not s1:
        problems.append("no fallback slot (%s)" % "/".join(d["slot1"]))
    if not env:
        problems.append("no U-Boot environment partition (%s)" % "/".join(d["appsblenv"]))
    if not find_part(card["mtd"], d["need_nor"]):
        problems.append("no %s (the fallback kernel)" % d["need_nor"][0])
    nor_bytes = sum(r["size"] for r in nor)
    if d["nor_total"] is None:
        m.say("  NOR        %d partitions, %d bytes (no expected total for the %s "
              "yet; recording it)" % (len(nor), nor_bytes, d["name"]))
        card["nor_total_seen"] = nor_bytes
    elif nor_bytes != d["nor_total"]:
        problems.append("NOR partitions add up to %d bytes, expected %d"
                        % (nor_bytes, d["nor_total"]))
    env_text = pty.must("fw_printenv 2>&1", 30, "fw_printenv")
    card["env"] = env_text
    for v in d["need_env"]:
        if not re.search(r"^%s=" % re.escape(v), env_text, re.M):
            problems.append("the U-Boot environment has no %s" % v)
    card["pri"] = (re.search(r"^pri=(\S+)", env_text, re.M) or [None, "?"])[1] \
        if re.search(r"^pri=(\S+)", env_text, re.M) else "?"
    m.say("  Arista's primary slot (pri): %s" % card["pri"])
    if card["pri"] == "0":
        m.say("  NOTE: %s" % (d.get("pri_note") or
                              "Arista calls slot 0 its primary, so the system that runs "
                              "is the one the install replaces."))
        m.say("        Slot 1 stays as the fallback, and the backup taken next is what")
        m.say("        puts slot 0 back if you want it.")
    root = pty.must("awk '$2 == \"/\" {print $3; exit}' /proc/mounts", 20, "root fs").strip()
    m.say("  root fs    %s" % root)
    if root not in ("overlay", "rootfs", "tmpfs", "ubifs"):
        problems.append("root filesystem is %r" % root)
    free_kb = pty.must("df -k /tmp | tail -1 | awk '{print $4}'", 20, "df").strip()
    m.say("  /tmp       %s KiB free" % free_kb)
    m.say("  flash      %d partitions, slot 0 = %s, slot 1 = %s, env = %s"
          % (len(card["mtd"]), s0["dev"] if s0 else "?", s1["dev"] if s1 else "?",
             env["dev"] if env else "?"))
    if problems:
        m.say("")
        for p in problems:
            m.say("  PROBLEM: " + p)
        m.die("this AP is not the expected layout; nothing was written.")
    card["slot0"], card["slot1"], card["env_dev"] = s0, s1, env
    m.say("  layout ok  (slot 0 %s %d MiB, slot 1 %s, env %s, %s)"
          % (s0["dev"], s0["size"] // 1048576, s1["dev"], env["dev"],
             card["version"]))


# --- 3. download the firmware ------------------------------------------------

class Receiver:
    """An HTTP sink on this PC.  Arista's sshd gives us a shell but no exec
    channels, so the AP has to push: gzip | curl, one partition at a time."""

    def __init__(self, outdir, port=8069):
        import http.server
        import socketserver
        self.outdir = outdir
        self.port = port
        self.got = {}                       # name -> dict(size, md5, sha256)
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _sink(self):
                name = re.sub(r"[^A-Za-z0-9._-]", "_", self.path.rsplit("/", 1)[-1])
                if not name:
                    self.send_error(400)
                    return
                path = os.path.join(outer.outdir, name)
                hm, hs = hashlib.md5(), hashlib.sha256()
                n = 0
                with open(path + ".part", "wb") as f:
                    if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                        while True:
                            size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                            if size == 0:
                                self.rfile.readline()
                                break
                            left = size
                            while left > 0:
                                b = self.rfile.read(min(left, 1 << 20))
                                if not b:
                                    break
                                f.write(b)
                                hm.update(b)
                                hs.update(b)
                                n += len(b)
                                left -= len(b)
                            self.rfile.readline()
                    else:
                        left = int(self.headers.get("Content-Length", "0"))
                        while left > 0:
                            b = self.rfile.read(min(left, 1 << 20))
                            if not b:
                                break
                            f.write(b)
                            hm.update(b)
                            hs.update(b)
                            n += len(b)
                            left -= len(b)
                if n == 0:
                    os.remove(path + ".part")
                    self.send_error(500)
                    return
                os.replace(path + ".part", path)
                outer.got[name] = {"size": n, "md5": hm.hexdigest(), "sha256": hs.hexdigest()}
                body = b"ok\n"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self._sink()

            def do_PUT(self):
                self._sink()

        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.httpd = S(("0.0.0.0", port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def _tr(target):
    """An SSHClient or a bare Transport -- the root shell gives us a Transport."""
    return target if not hasattr(target, "get_transport") else target.get_transport()


def exec_t(target, cmd, timeout=120):
    """m.ssh_exec, but also on a Transport."""
    chan = _tr(target).open_session()
    chan.settimeout(30)
    out = b""
    end = time.time() + timeout
    chan.exec_command(cmd)
    while True:
        if chan.recv_ready():
            out += chan.recv(65536)
        elif chan.exit_status_ready():
            break
        elif time.time() > end:
            break
        else:
            time.sleep(0.05)
    while chan.recv_ready():
        out += chan.recv(65536)
    rc = chan.recv_exit_status() if chan.exit_status_ready() else -1
    chan.close()
    text = out.decode("utf-8", "replace")
    m.LOG.write(text)
    return text, rc


def stream(target, cmd, path, timeout=3600):
    """Run cmd on the AP and write its stdout to path, without a pty."""
    chan = _tr(target).open_session()
    chan.settimeout(None)
    chan.exec_command(cmd)
    h = hashlib.md5()
    n = 0
    with open(path, "wb") as f:
        while True:
            b = chan.recv(262144)
            if not b:
                break
            f.write(b)
            h.update(b)
            n += len(b)
    rc = chan.recv_exit_status()
    chan.close()
    return n, h.hexdigest(), rc


def download_firmware(pty, card, outdir, include_slot1=True, pc_ip=None, port=8069):
    """Every partition off the AP: gzip on the AP, POSTed to this PC.  For the
    small partitions the AP also hashes the raw flash first, and the two are
    compared, so a bad read cannot pass unnoticed."""
    d = MODELS[card["model"]]
    os.makedirs(outdir, exist_ok=True)
    pty = ensure_shell(run, card.get("host"), run.user, run.password, "the menu")
    recv = Receiver(outdir, port)
    m.say("")
    m.say("Downloading this AP's firmware to %s" % outdir)
    m.say("(the AP gzips each partition and posts it to this PC at %s:%d)"
          % (pc_ip or "this PC", port))
    manifest = {"model": card["model"], "name": d["name"], "mac": card["mac"],
                "version": card.get("version"), "host": card.get("host"),
                "when": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()),
                "partitions": []}
    total = 0
    for r in card["mtd"]:
        # Every real partition, in either model's order.  A UBI volume that a
        # running system exposes (ubi_rootfs, rootfs_data) is not a partition:
        # its bytes are inside the raw slot already.
        if is_ubi_volume(r):
            continue
        if r is card["slot1"] and not include_slot1:
            m.say("  %-6s %-14s skipped (--fast)" % (r["dev"], r["name"]))
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", r["name"])
        fname = "%s_%s.bin.gz" % (r["dev"], safe)
        gz = os.path.join(outdir, fname)
        out = pty.must("md5sum /dev/%s | cut -d' ' -f1" % r["dev"], 900, "md5 on the AP")
        ap_md5 = out.strip().splitlines()[-1] if out.strip() else None
        if not ap_md5:
            m.die("%s (%s) could not be hashed on the AP" % (r["dev"], r["name"]))
        t0 = time.time()
        cmd = ("gzip -1 -c /dev/%s | curl -s -S --data-binary @- "
               "http://%s:%d/put/%s" % (r["dev"], pc_ip, port, fname))
        with Heartbeat("%s %s (%.0f MiB)" % (r["dev"], r["name"], r["size"] / 1048576.0), 12):
            pty.run(cmd, 1800)
        if fname not in recv.got:
            m.die("%s (%s) did not arrive; the AP could not reach this PC "
                  "(firewall on TCP %d?)" % (r["dev"], r["name"], port))
        got = recv.got[fname]
        raw = gz + ".raw"
        sha, md5 = gunzip_sha_md5(gz, raw)
        os.remove(raw)
        if md5 != ap_md5:
            m.die("%s (%s): this copy does not match the AP's own md5 "
                  "(AP %s, here %s)" % (r["dev"], r["name"], ap_md5, md5))
        m.say("  %-6s %-14s %6.1f MiB -> %5.1f MiB gz, verified (%.1f s)"
              % (r["dev"], r["name"], r["size"] / 1048576.0, got["size"] / 1048576.0,
                 time.time() - t0))
        manifest["partitions"].append({"dev": r["dev"], "name": r["name"], "size": r["size"],
                                       "erase": r["erase"], "file": fname,
                                       "sha256": sha, "md5": md5})
        total += got["size"]
    for name, text in (("u-boot-env.txt", card.get("env")), ("proc-mtd.txt", card.get("mtd_raw"))):
        if text:
            with open(os.path.join(outdir, name), "w", encoding="utf-8") as f:
                f.write(text)
    with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    with open(os.path.join(outdir, "SHA256SUMS"), "w", encoding="utf-8") as f:
        for p in manifest["partitions"]:
            f.write("%s  %s\n" % (p["sha256"], p["file"]))
    m.say("")
    m.say("Saved %d partitions, %.1f MiB compressed, in %s"
          % (len(manifest["partitions"]), total / 1048576.0, outdir))
    if not manifest["partitions"]:
        m.die("nothing was saved; refusing to go on")
    return manifest


# --- 4. netboot the RAM installer -------------------------------------------

def ap_address(a_host, pc_ip):
    return a_host.rsplit(".", 1)[0] + ".230"


def netboot_env(pc_ip, ap_ip, itb_name, cfg):
    """The two variables the netboot needs, as (name, value) pairs.

    They are kept apart on purpose.  The injected shell on Arista's firmware
    lives about a minute (prep_upgrade gives up and reboots), and a single
    several-hundred-character fw_setenv is both slow to paste and easy to get
    wrong, so each write stays short:

      owrt_nb  -- the RAM-image fetch, with two attempts: from a bootcmd U-Boot
                  runs ~2 s after power-on and the AQR114C still reports the
                  link Down, which is why the first try can fail.
      bootcmd  -- run it; if the image never arrives, put Arista's own boot back
                  (its bootargs too) and reset.  A failed tftpboot leaves
                  U-Boot's load address set, and Arista's ubootscript would then
                  call its own kernel bad, flip slots and loop (seen on the
                  bench unit, 2026-10-25)."""
    fetch = ("setenv ipaddr %s; setenv gatewayip %s; setenv serverip %s; "
             "setenv fdt_high 0x4A3E0000; "
             "setenv bootargs console=ttyMSM0,115200n8 rootwait panic=0; "
             "sleep 5; tftpboot 0x60000000 %s && bootm 0x60000000#%s; "
             "sleep 3; tftpboot 0x60000000 %s && bootm 0x60000000#%s"
             % (ap_ip, ap_ip.rsplit(".", 1)[0] + ".1", pc_ip, itb_name, cfg, itb_name, cfg))
    fallback = ('run owrt_nb; setenv mtdids; setenv mtdparts; setenv fdt_high 0x4A400000; '
                'if test ${pri} = 0; then setenv bootargs ${ba0}; else setenv bootargs ${ba1}; fi; '
                'setenv bootcmd "run ubootscript"; saveenv; reset')
    return [("owrt_nb", fetch), ("bootcmd", fallback)]


def reboot_and_confirm(pty, host, wait=45):
    """Reboot from the injected shell and confirm the AP actually went down."""
    mk = pty.mark()
    pty.send("sync; /sbin/reboot")
    time.sleep(2)
    said = pty.since(mk)
    if re.search(r"not found|cannot execute|Read-only", said):
        m.die("the AP's shell could not reboot itself:\n%s" % said.strip()[-300:])
    end = time.time() + wait
    while time.time() < end:
        try:
            socket.create_connection((host, 22), 2).close()
            time.sleep(1)
        except Exception:
            return True
    return False


def boot_installer(run, card, pc_ip, ap_ip, itb, kind):
    """Two short environment writes, then reboot into the RAM installer."""
    global _REBOOTED
    itb_name = os.path.basename(itb)
    short = "%s.itb" % card["model"]      # short on purpose: it is pasted into a line
    writes = netboot_env(pc_ip, ap_ip, short, MODELS[card["model"]]["fit_cfg"])
    run.tftp = Tftp({short: itb, itb_name: itb})     # serve before the AP asks
    m.say("TFTP serving %s as %s on UDP 69." % (itb_name, short))
    m.say("")
    m.say("The U-Boot environment changes in two places:")
    for name, v in writes:
        m.say("  %s = %s" % (name, v))
    m.say("")
    m.say("If the fetch fails, bootcmd puts Arista's own boot back and resets, so")
    m.say("the AP ends up on its own firmware; running this again costs nothing.")
    if not run.yes:
        m.say("(sensord is stopped while this shell is held: answer now.)")
        if ask("Type 'env' to write it: ", "--yes").strip().lower() != "env":
            m.die("not confirmed; nothing was written")

    if kind == "stock":
        pty = ensure_shell(run, card["host"], run.user, run.password,
                           "the download and the menu")
        # One short command at a time: a single line carrying everything was
        # truncated by the AP's tty, which leaves the shell waiting for the rest
        # of a quoted value while nothing runs (bench unit, 2026-10-25).
        for n, v in writes:
            out, rc = pty.run("fw_setenv %s '%s'" % (n, v.replace("'", "'\\''")), 60)
            m.LOG.write("\nENV WRITE %s: rc=%s %s\n" % (n, rc, out))
            if rc != 0:
                m.die("writing %s failed (exit %d): %s" % (n, rc, out[-300:]))
            run.env_writes.append(n)
            m.say("  %s written (%d of %d)." % (n, len(run.env_writes), len(writes)))
        out, rc = pty.run("fw_printenv bootcmd | cut -c1-34", 60)
        m.say("  bootcmd now: %s" % (out.strip()[:40] or "(no answer)"))
        m.say("  Rebooting: the AP will fetch the installer now.")
        _REBOOTED = True
        try:
            pty.send("sync; /sbin/reboot")
        except Exception:
            pass
        time.sleep(20)
    else:
        m.say("")
        m.say("Writing the environment on OpenWrt, then rebooting.")
        for n, v in writes:
            m.ssh_exec(run.shell, "fw_setenv %s '%s'" % (n, v.replace("'", "'\\''")), 30)
            run.env_writes.append(n)
        out, _ = m.ssh_exec(run.shell, "fw_printenv owrt_nb | cut -c1-50", 30)
        m.say("  owrt_nb reads back: %s" % (out.strip()[:50] or "(nothing)"))
        _REBOOTED = True
        m.ssh_exec(run.shell, "sync; reboot", 20)
    time.sleep(30)


# --- 5a. install OpenWrt -----------------------------------------------------

def do_install(run, card, ip, ubi, sums):
    con = run.installer
    m.say("")
    m.say("  Checking the installer has the tools it needs.")
    out, _ = m.ssh_exec(con, "command -v %s unsquashfs ubiformat >/dev/null && echo ok"
                        % MODELS[card["model"]]["installer"], 60)
    if "ok" not in out:
        m.die("the RAM installer lacks the tools the install needs:\n%s" % out[-300:])
    stage = "/tmp/" + card["model"]
    m.ssh_must(con, "rm -rf %s; mkdir -p %s" % (stage, stage), 60)
    for local, remote, what in ((sums, stage + "/SHA256SUMS", "SHA256SUMS"),
                                (ubi, stage + "/factory.ubi", "factory.ubi")):
        m.say("  Sending %s (%.1f MiB) to %s over SSH."
              % (what, os.path.getsize(local) / 1048576.0, remote))
        with Heartbeat("sending %s" % what, 10):
            try:
                sftp = con.open_sftp()
                sftp.put(local, remote)
                sftp.close()
            except Exception as e:                       # noqa: BLE001
                m.say("    (no sftp-server: %s; using a plain SSH channel)" % e)
                chan = con.get_transport().open_session()
                chan.exec_command("cat > %s" % remote)
                with open(local, "rb") as f:
                    for b in iter(lambda: f.read(1 << 20), b""):
                        chan.sendall(b)
                chan.shutdown_write()
                chan.recv_exit_status()
                chan.close()
        out, _ = m.ssh_exec(con, "sha256sum %s | cut -d' ' -f1" % remote, 600)
        if out.strip() != sha256_file(local):
            m.die("%s arrived wrong on the AP" % what)
        m.say("    on the AP and sha256-verified.")
    # What the shipped installer accepts differs by image: the C-230 one has
    # --no-backup, the C-260 one (11.0.1-era) does not, and that one also insists
    # on >512 MiB free wherever its backup goes while this RAM installer's /tmp
    # reports ~432 MiB.  So ask the script what it supports, and if it must take
    # a backup, give it a tmpfs sized to its own check -- the backup is only
    # ~65 MiB; 512 MiB is a conservative bound and tmpfs uses RAM per page
    # written.
    inst = MODELS[card["model"]]["installer"]
    out, _ = m.ssh_exec(con, "grep -q -- '--no-backup' $(command -v %s) && echo yes || echo no"
                        % inst, 30)
    no_backup = "yes" in out
    out, _ = m.ssh_exec(con, "df -k %s | tail -1 | awk '{print $4}'" % stage, 30)
    try:
        free_mb = int(out.strip().split()[0]) // 1024
    except Exception:
        free_mb = 0
    m.say("  installer %s: --no-backup %s, %s has %d MiB free"
          % (inst, "supported" if no_backup else "NOT supported", stage, free_mb))
    if not no_backup and free_mb < 600:
        big = "/mnt/" + card["model"] + "stage"
        m.say("  it wants >512 MiB for its own backup: staging on a tmpfs")
        m.ssh_must(con, "mkdir -p %s; mountpoint -q %s || mount -t tmpfs -o size=768M none %s"
                   % (big, big, big), 60)
        m.ssh_must(con, "cp %s/SHA256SUMS %s/factory.ubi %s/" % (stage, stage, big), 300)
        stage = big
        m.say("    staging on %s" % stage)
    flags = "--dry-run --stick %s" if not no_backup else "--dry-run --no-backup --stick %s"
    flags = flags % stage

    m.say("")
    m.say("  Running the installer's own checks (this writes nothing):")
    with Heartbeat("installer dry run", 20):
        out, _ = m.ssh_exec(con, "%s %s 2>&1" % (inst, flags), 1800)
    for line in out.splitlines():
        m.say("    " + line)
    if "INSTALL-RESULT: OK" not in out:
        m.die("the installer's checks did not pass; nothing written")
    m.say("")
    m.say("  Checks passed.  About to write:")
    m.say("    1 U-Boot environment write (bootcmd, pri=1)")
    m.say("    NAND slot 0, erased and written with factory.ubi")
    if not run.yes_install and not run.yes:
        mac = board_mac(con) or "?"   # br-lan/lan1/eth0: whichever the model has
        tail = mac.replace(":", "")[-4:]
        if ask("  Type the last four characters of the MAC on the label (%s): " % mac, "--yes-install").strip().lower() != tail:
            m.die("no match; nothing was written")
    m.say("  Writing slot 0 (erasing 256 MiB and writing back; about a minute):")
    with Heartbeat("writing NAND slot 0", 20):
        out, _ = m.ssh_exec(con, "%s %s 2>&1"
                            % (inst, flags.replace("--dry-run", "--yes")), 2400)
    for line in out.splitlines():
        m.say("    " + line)
    if "INSTALL-RESULT: OK installed" not in out:
        m.die("the install did not finish (above); the backup is safe")
    run.env_writes.append("bootcmd+pri (installer)")
    run.flash_writes.append("NAND slot 0 (installer)")
    m.say("  Installer reports success.  Rebooting into OpenWrt from NAND.")
    m.ssh_exec(con, "reboot", 20)


# --- 5b. put the AP's own firmware back -------------------------------------

RAW_TOOLS = "https://raw.githubusercontent.com/%s/main/install/tools/aarch64/" % REPO


def fetch_tool(name, cache):
    """Get one mtd binary next to this script if it is not there, and verify it
    against the published SHA256SUMS.  A single-file download still has to be
    able to run the revert path."""
    dest = os.path.join(cache, name)
    if os.path.exists(dest):
        return dest
    os.makedirs(cache, exist_ok=True)
    m.say("    %s is not on this PC; fetching it from the project's repository" % name)
    try:
        with http_get(RAW_TOOLS + "SHA256SUMS") as r:
            sums = dict(reversed(line.split()) for line in r.read().decode().splitlines() if line.strip())
        want = sums.get(name)
        if not want:
            m.die("%s is not in the published SHA256SUMS" % name)
        with http_get(RAW_TOOLS + name) as r, open(dest + ".part", "wb") as f:
            for b in iter(lambda: r.read(1 << 20), b""):
                f.write(b)
        got = hashlib.sha256(open(dest + ".part", "rb").read()).hexdigest()
        if got != want:
            os.remove(dest + ".part")
            m.die("%s from the repository does not match its SHA256 (%s)" % (name, got))
        os.replace(dest + ".part", dest)
        os.chmod(dest, 0o755)
        m.say("    %s fetched and verified" % name)
        return dest
    except Exception as e:                            # noqa: BLE001
        m.die("no %s in %s and it could not be fetched (%s).  The revert path needs "
              "it; the install path does not." % (name, cache, e))


def push_tools(con, need=("flash_erase", "nandwrite")):
    """Put the mtd tools on the RAM installer if its image has none.  They are
    aarch64/musl builds from this project's own mtd-utils 2.3.1, published with
    the installer (install/tools/aarch64/ plus a SHA256SUMS)."""
    have = m.ssh_exec(con, "for t in %s; do command -v $t >/dev/null && echo HAVE-$t || echo MISSING-$t; done"
                      % " ".join(need), 60)[0]
    for t in need:
        if "MISSING-%s" % t in have:
            local = os.path.join(TOOLS, t)
            if not os.path.exists(local):
                local = fetch_tool(t, TOOLS)
            m.say("    sending %s (%.0f KiB)" % (t, os.path.getsize(local) / 1024.0))
            chan = con.get_transport().open_session()
            chan.exec_command("cat > /tmp/%s; chmod +x /tmp/%s" % (t, t))
            with open(local, "rb") as f:
                for blk in iter(lambda: f.read(1 << 20), b""):
                    chan.sendall(blk)
            chan.shutdown_write()
            chan.recv_exit_status()
            chan.close()
            out, _ = m.ssh_exec(con, "/tmp/%s --help 2>&1 | head -2" % t, 30)
            if "flash_erase" not in out and "nandwrite" not in out and "usage" not in out.lower():
                m.die("/tmp/%s does not run on the installer:\n%s" % (t, out[-300:]))
    return "/tmp/"


def _send_gz(con, gz, remote, what):
    """Stream a local .gz to the AP and gunzip it there, hashing in between."""
    m.say("    sending %s (%.1f MiB gzipped) ..." % (what, os.path.getsize(gz) / 1048576.0))
    chan = con.get_transport().open_session()
    chan.settimeout(None)
    with Heartbeat("sending %s" % what, 10):
        chan.exec_command("gunzip -c > %s" % remote)
        with open(gz, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                chan.sendall(blk)
        chan.shutdown_write()
        rc = chan.recv_exit_status()
        chan.close()
    if rc != 0:
        m.die("%s did not decompress on the AP" % what)


def do_restore(run, card, manifest, backup_dir):
    con = run.installer
    d = MODELS[card["model"]] if card and card.get("model") else MODELS[manifest["model"]]
    m.say("")
    m.say("  Reading the installer's partition table.")
    rows = mtd_table(m.ssh_must(con, "cat /proc/mtd", 30))
    s0 = find_part(rows, d["slot0"])
    env = find_part(rows, d["appsblenv"])
    if not s0 or not env:
        m.die("the installer does not see the partitions to write (%s, %s)"
              % (d["slot0"], d["appsblenv"]))
    part = next((p for p in manifest["partitions"]
                 if any(p["name"].lower() == n.lower() for n in d["slot0"])), None)
    if not part:
        m.die("this backup has no slot 0 image")
    envp = next((p for p in manifest["partitions"]
                 if any(p["name"].lower() == n.lower() for n in d["appsblenv"])), None)
    m.say("  slot 0 image: %s, %.1f MiB raw, md5 %s"
          % (part["file"], part["size"] / 1048576.0, part["md5"][:12]))
    m.say("  environment:  %s" % (envp["file"] if envp else "(not in the backup)"))
    m.say("  This erases NAND slot 0 (%s) and writes the AP's own image back." % s0["dev"])
    m.say("  Slot 1 and every NOR partition except the environment stay as found.")
    if not run.yes:
        if ask("  Type 'restore' to write it: ", "--yes").strip().lower() != "restore":
            m.die("not confirmed; nothing was written")

    prefix = push_tools(con, ("flash_erase", "nandwrite"))
    m.ssh_exec(con, "mkdir -p /tmp/restore", 30)
    _send_gz(con, os.path.join(backup_dir, part["file"]), "/tmp/restore/slot0.bin", "slot 0")
    out = m.ssh_must(con, "md5sum /tmp/restore/slot0.bin | cut -d' ' -f1", 600)
    if out.strip() != part["md5"]:
        m.die("the image on the AP does not match the backup (md5 %s, here %s)"
              % (part["md5"], out.strip()))
    m.say("    on the AP and verified against the backup (md5).")
    m.say("  Erasing %s and writing %.1f MiB back (this takes about a minute):"
          % (s0["dev"], part["size"] / 1048576.0))
    cmd = ("ubidetach -p /dev/%s >/dev/null 2>&1; "
           "%sflash_erase /dev/%s 0 0 >/dev/null 2>&1; "
           "%snandwrite -q -p -k /dev/%s /tmp/restore/slot0.bin >/tmp/nandwrite.log 2>&1; "
           "tail -2 /tmp/nandwrite.log; "
           "md5sum /dev/%s | cut -d' ' -f1"
           % (s0["dev"], prefix, s0["dev"], prefix, s0["dev"], s0["dev"]))
    with Heartbeat("writing slot 0", 12):
        out, _ = m.ssh_exec(con, cmd, 1800)
    got = [l for l in out.splitlines() if re.fullmatch(r"[0-9a-f]{32}", l.strip())]
    for line in out.splitlines():
        if "bad block" in line.lower() or "written" in line.lower():
            m.say("    " + line.strip())
    if not got or got[-1].strip() != part["md5"]:
        m.die("slot 0 does not read back as the backup (got %s, want %s)"
              % (got[-1].strip() if got else "?", part["md5"]))
    m.say("    slot 0 reads back identical to the backup.")
    run.flash_writes.append("NAND slot 0 (restore)")

    if envp:
        _send_gz(con, os.path.join(backup_dir, envp["file"]), "/tmp/restore/env.bin", "environment")
        m.say("  Writing the U-Boot environment back (%s):" % env["dev"])
        cmd = ("%sflash_erase /dev/%s 0 0 >/dev/null 2>&1; "
               "dd if=/tmp/restore/env.bin of=/dev/%s 2>/dev/null; "
               "md5sum /dev/%s | cut -d' ' -f1"
               % (prefix, env["dev"], env["dev"], env["dev"]))
        out, _ = m.ssh_exec(con, cmd, 300)
        ok = [l for l in out.splitlines() if re.fullmatch(r"[0-9a-f]{32}", l.strip())]
        if ok and ok[-1].strip() == envp["md5"]:
            m.say("    environment restored and reads back identical.")
        else:
            m.die("the environment does not read back as the backup (got %s, want %s)"
                  % (ok[-1].strip() if ok else "?", envp["md5"]))
        run.env_writes.append("0:APPSBLENV (restore)")
        run.flash_writes.append("NOR 0:APPSBLENV (restore)")
    m.say("  Rebooting into the AP's own firmware.")
    m.ssh_exec(con, "sync; reboot", 20)


def card_slot_names(d):
    return d["slot0"], d["slot1"]


# --- finding the AP again ----------------------------------------------------

def _first_answer(live, prefix, probe, workers=32):
    """Ask every live host at once and take the first real answer.

    Sequential identification was the actual cost of a scan: 35 s for 19 hosts,
    against 2.4 s in parallel (measured 2026-10-25)."""
    if not live:
        return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(live))) as ex:
        futs = {ex.submit(probe, "%s.%d" % (prefix, i)): i for i in live}
        for f in concurrent.futures.as_completed(futs):
            try:
                r = f.result()
            except Exception:
                continue
            if r:
                return r
    return None


def find_stock(prefix, timeout):
    """The AP back on its own firmware: Arista's config shell answers."""
    m.say("")
    m.say("Waiting for the AP to come back on Arista's firmware.")
    end = time.time() + timeout
    while time.time() < end:
        with concurrent.futures.ThreadPoolExecutor(max_workers=128) as ex:
            futs = [ex.submit(lambda i=i: (socket.create_connection(("%s.%d" % (prefix, i), 22), 0.4).close(), i)[1],)
                    for i in range(1, 255)]
            live = []
            for f in concurrent.futures.as_completed(futs):
                try:
                    live.append(f.result())
                except Exception:
                    pass
        def probe_stock(ip):
            try:
                client = m.ssh_open(ip, "config", "config", timeout=4)
            except Exception:
                return None
            pty = m.Pty(client.invoke_shell(width=120, height=40))
            if not pty.wait(r"\[config\]\$", 10):
                return None
            pty.send("version")
            time.sleep(1.5)
            return ip, pty.since(0)

        got = _first_answer(live, prefix, probe_stock)
        if got:
            return got
        time.sleep(5)
    return None, None

def find_ap(prefix, timeout, want_ram=None, skip=(), mac=None):
    def by_dns():
        """The AP's own DHCP name, if the server publishes leases in DNS and the
        MAC matches.  Tried before every scan round: after a reboot the lease is
        new, so a single attempt at the start arrives too early (seen on the
        C-260 revert, 2026-10-25)."""
        if not mac:
            return None
        for name in ("OpenWrt", "openwrt"):
            try:
                ip = socket.gethostbyname(name)
            except Exception:
                continue
            r = check_installer(ip, timeout=4, mac=mac)
            if r:
                ram = r[2] in ("tmpfs", "rootfs", "ramfs")
                if want_ram is None or ram == want_ram:
                    m.say("")
                    m.say("  DNS: %r -> %s, and the MAC matches" % (name, ip))
                    return r[0], ip, r[1], r[2]
            break
        return None

    got = by_dns()
    if got:
        return got
    m.say("")
    m.say("Scanning %s.0/24 for %s (a few seconds per round)."
          % (prefix, ("the AP with MAC %s" % mac) if mac else "the AP"))
    end = time.time() + timeout
    start = time.time()
    window = 0
    while time.time() < end:
        got = by_dns()
        if got:
            return got
        with concurrent.futures.ThreadPoolExecutor(max_workers=128) as ex:
            futs = [ex.submit(lambda i=i: (socket.create_connection(("%s.%d" % (prefix, i), 22), 0.4).close(), i)[1],)
                    for i in range(1, 255) if "%s.%d" % (prefix, i) not in skip]
            live = []
            for f in concurrent.futures.as_completed(futs):
                try:
                    live.append(f.result())
                except Exception:
                    pass
        m.say("  [+%ds] %d host(s) with SSH; asking each what it is."
              % (int(time.time() - start), len(live)))
        def probe_ap(ip):
            r = check_installer(ip, timeout=4, mac=mac)
            if not r:
                return None
            con, board, root, got_mac = r
            ram = root in ("tmpfs", "rootfs", "ramfs")
            m.say("  %s: %s, root on %s, MAC %s%s"
                  % (ip, board, root, got_mac or "?", " (RAM)" if ram else ""))
            if want_ram is not None and ram != want_ram:
                con.close()
                return None
            return con, ip, board, root

        got = _first_answer(live, prefix, probe_ap)
        if got:
            return got
        time.sleep(5)
    return None, None, None, None


# --- the run book ------------------------------------------------------------

class Run:
    def __init__(self, yes=False, yes_install=False, user=None, password=None):
        self.user = user
        self.password = password
        self.yes = yes
        self.yes_install = yes_install
        self.env_writes = []
        self.flash_writes = []
        self.shell = None
        self.installer = None
        self.tftp = None


def _cache_ok(cache, tag, model):
    """Everything this model needs for `tag`, already on this PC?"""
    d = os.path.join(cache, tag)
    names = m.files_for(model)
    want = ["SHA256SUMS"] + list(names.values())
    if all(os.path.exists(os.path.join(d, n)) for n in want):
        return d, want
    return None, want


def release_files(a, model):
    """The release files, from GitHub or from the copy already on this PC.

    A field laptop often has no uplink at the site, so a cached release is used
    when it is there and verifies, and `--offline` skips the API entirely."""
    cache = os.path.abspath(a.files)
    if not a.offline:
        try:
            d, tag = m.fetch_release(a.tag, [model], cache)
            n = m.files_for(model)
            return (os.path.join(d, n["itb"]), os.path.join(d, n["ubi"]),
                    os.path.join(d, n["sums"]), tag)
        except SystemExit:
            raise
        except Exception as e:
            m.say("  cannot reach GitHub (%s)" % e)
            m.say("  looking for the release already saved on this PC.")
    tag = a.tag
    if not tag:
        cands = [x for x in os.listdir(cache)
                 if _cache_ok(cache, x, model)[0]] if os.path.isdir(cache) else []
        if not cands:
            m.die("no release on this PC and GitHub is unreachable.  Run once with "
                  "a connection, or copy %s\\ into %s" % ("<tag>", cache))
        tag = sorted(cands)[-1]
    d, want = _cache_ok(cache, tag, model)
    if not d:
        m.die("the cached release %s is incomplete in %s" % (tag, os.path.join(cache, tag)))
    m.check_sums(d, want)                      # never trust a cached file blindly
    m.say("  using %s from this PC (%s), SHA256SUMS verified" % (tag, d))
    n = m.files_for(model)
    return (os.path.join(d, n["itb"]), os.path.join(d, n["ubi"]),
            os.path.join(d, n["sums"]), tag)


def verify_openwrt(prefix, mac=None, timeout=420):
    m.say("")
    m.say("Waiting for OpenWrt to come up from NAND%s.  It is identified by its"
          % (" on MAC %s" % mac if mac else ""))
    m.say("MAC, so another AP on the same network cannot answer for it.")
    con2, ip2, board2, root2 = find_ap(prefix, timeout, want_ram=False, mac=mac)
    m.say("")
    if con2 is None:
        m.say("OpenWrt is not answering on %s.0/24 yet." % prefix)
        m.say("It falls back to this AP's own firmware if it will not start;")
        m.say("power-cycle the AP and check its console.")
        return
    m.say("OpenWrt is up at %s (%s, root on %s)." % (ip2, board2, root2))
    info, _ = m.ssh_exec(con2, "grep -E 'DESCRIPTION|VERSION_ID' /etc/openwrt_release; "
                               "uname -r; uptime", 60)
    for line in info.splitlines():
        m.say("  " + line.strip())


def board_mac(con):
    text, _ = m.ssh_exec(con, "for i in br-lan lan1 eth0; do cat /sys/class/net/$i/address 2>/dev/null; done", 20)
    for line in text.splitlines():
        line = line.strip().lower()
        if re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", line) and line != "00:00:00:00:00:00":
            return line
    return ""


def check_installer(ip, timeout=8, mac=None):
    """Is there an Arista OpenWrt/RAM installer at ip?  Returns (conn, board,
    root, mac) or None.  With mac set, only that unit answers."""
    con = None
    try:
        con = m.ssh_open(ip, "root", "", timeout=timeout)
        text, _ = m.ssh_exec(con, "cat /tmp/sysinfo/board_name 2>/dev/null; "
                                  "awk '$2 == \"/\" {print $3; exit}' /proc/mounts", 20)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines or not lines[0].startswith("arista,"):
            con.close()
            return None
        got_mac = board_mac(con)
        if mac and got_mac and got_mac != mac.lower():
            con.close()
            return None
        return con, lines[0], (lines[1] if len(lines) > 1 else "?"), got_mac
    except Exception:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    return None


def wait_installer(a, prefix, timeout=420, mac=None):
    """Where the RAM installer came up.  The operator is asked, because in the
    field nobody knows our subnets and DHCP may hand out anything; a scan of the
    AP's own /24 is only the fallback."""
    cand = a.installer_ip or a.installer_at
    if not cand:
        # The installer's own hostname is "OpenWrt" (from its image), and a DHCP
        # server that publishes lease names in DNS answers this in a second.
        # The MAC check is what makes it safe: another OpenWrt box on the network
        # answers to the same name.
        for name in ("OpenWrt", "openwrt"):
            try:
                ip = socket.gethostbyname(name)
            except Exception:
                continue
            r = check_installer(ip, timeout=4, mac=mac)
            if r:
                m.say("")
                m.say("  the DHCP server publishes the installer as '%s' -> %s" % (name, ip))
                return r[0], ip, r[1], r[2]
            break
    if not cand and not a.yes:
        m.say("")
        cand = ask("The installer is booting. What address did it take "
                     "(blank = scan %s.0/24)? " % prefix, "--installer-ip").strip() or None
    if cand:
        m.say("")
        m.say("Waiting for the installer at %s (it may take ~1-2 min to boot)." % cand)
        end = time.time() + timeout
        start = time.time()
        while time.time() < end:
            r = check_installer(cand, mac=mac)
            if r:
                return r[0], cand, r[1], r[2]
            m.say("  [+%ds] not answering yet." % int(time.time() - start))
            time.sleep(5)
        m.die("nothing answered as a RAM installer at %s.  Is that the address it"
              " took from DHCP?  Arista's own firmware answers 22 there too, so a"
              " wrong address looks like a hang." % cand)
    return find_ap(prefix, timeout, want_ram=True, mac=mac)


def newest_backup(model, mac):
    """The newest saved firmware for this AP, re-verified from disk."""
    base = os.path.join(BACKUPS, "%s-%s" % (model, (mac or "").replace(":", "")))
    if not os.path.isdir(base):
        return None, None
    for name in sorted(os.listdir(base), reverse=True):
        d = os.path.join(base, name)
        man = os.path.join(d, "manifest.json")
        if not os.path.exists(man):
            continue
        with open(man, encoding="utf-8") as f:
            manifest = json.load(f)
        if manifest.get("mac") and mac and manifest["mac"] != mac:
            continue
        raw = os.path.join(d, ".verify.tmp")
        try:
            for p in manifest["partitions"]:
                sha, md5 = gunzip_sha_md5(os.path.join(d, p["file"]), raw)
                if sha != p["sha256"] or md5 != p["md5"]:
                    break
            else:
                return d, manifest
        except Exception:
            pass
        finally:
            if os.path.exists(raw):
                os.remove(raw)
        m.say("  (the backup in %s does not verify; ignoring it)" % name)
    return None, None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", help="the AP's address")
    ap.add_argument("--pc-ip", help="this PC as the AP sees it")
    ap.add_argument("--user", help="log in as this user (default: root on OpenWrt, "
                                  "config on Arista's firmware)")
    ap.add_argument("--password", help="that user's password (default: none on "
                                      "OpenWrt, 'config' on Arista's firmware)")
    ap.add_argument("--ap-ip", help="the address U-Boot uses (default host/24 .230)")
    ap.add_argument("--tag", help="release tag (default: newest)")
    ap.add_argument("--files", default=RELEASES, help="release cache (default: %(default)s)")
    ap.add_argument("--backup-dir", help="where to save the firmware (default backups/<model>-<mac>)")
    ap.add_argument("--backup-only", action="store_true", help="download the firmware and stop")
    ap.add_argument("--verify-only", action="store_true", help="check the AP and stop")
    ap.add_argument("--install", action="store_true", help="install OpenWrt without asking")
    ap.add_argument("--restore", metavar="DIR", help="put an AP's saved firmware back")
    ap.add_argument("--installer-at", metavar="IP",
                    help="the RAM installer is already running at this address "
                         "(resume after an interruption)")
    ap.add_argument("--verify", action="store_true",
                    help="afterwards, wait for the AP and check it (default: leave it "
                         "to the operator and the DHCP lease list)")
    ap.add_argument("--installer-ip", metavar="IP",
                    help="the address the installer took from DHCP, for the netboot "
                         "that follows (blank: scan the AP's /24)")
    ap.add_argument("--fast", action="store_true", help="skip the 256 MiB fallback slot")
    ap.add_argument("--offline", action="store_true",
                    help="use the release already on this PC; do not ask GitHub")
    ap.add_argument("--skip-download", action="store_true",
                    help="reuse this AP's firmware if it is already saved here and verifies")
    ap.add_argument("--yes", action="store_true", help="no prompts (still refuses on bad checks)")
    ap.add_argument("--yes-install", action="store_true", help="no MAC prompt at the install")
    a = ap.parse_args()

    if not a.host and not a.restore and not a.installer_at:
        m.die("give --host (an AP on its factory firmware), --restore DIR, or --installer-at IP")
    run = Run(a.yes, a.yes_install, a.user, a.password)
    work = os.path.abspath("field-run-%s" % time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(work, exist_ok=True)
    m.LOG.open(os.path.join(work, "run.log"))
    m.say("arista-field: log in %s" % work)

    # ---- restore from a saved backup only (no AP contact yet) --------------
    if a.restore and not a.host:
        m.die("--restore needs --host too (the AP to write it to)")

    if a.host and not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", a.host):
        m.die("--host must be an IPv4 address")
    if a.installer_at and not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", a.installer_at):
        m.die("--installer-at must be an IPv4 address")
    pc_ip = a.pc_ip or m.pc_address(a.host or a.installer_at)
    prefix = pc_ip.rsplit(".", 1)[0]
    if pc_ip.startswith("169.254."):
        m.die("this PC has no address towards the AP")

    # ---- already at a running RAM installer (console netboot, or resume) ----
    if a.installer_at:
        m.say("")
        m.say("Using the RAM installer already running at %s." % a.installer_at)
        con = m.ssh_open(a.installer_at, "root", "", timeout=20)
        text, _ = m.ssh_exec(con, "cat /tmp/sysinfo/board_name 2>/dev/null; "
                                  "awk '$2 == \"/\" {print $3; exit}' /proc/mounts", 30)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        board = lines[0] if lines else "?"
        root = lines[1] if len(lines) > 1 else "?"
        if not board.startswith("arista,") or root not in ("tmpfs", "rootfs", "ramfs"):
            m.die("%s is not a RAM installer (board %r, root %r)" % (a.installer_at, board, root))
        model = board.split(",")[-1].replace("-", "")
        if model not in MODELS:
            m.die("no entry for board %r" % board)
        card = {"model": model, "board": board, "host": a.installer_at,
                "mac": board_mac(con)}   # so the verifier knows which unit to expect
        run.installer = con
        m.say("  %s, running from RAM." % board)
        itb, ubi, sums, tag = release_files(a, model)
        m.say("  Release %s" % tag)
        do_install(run, card, a.installer_at, ubi, sums)
        verify_openwrt(a.installer_at.rsplit(".", 1)[0], card.get("mac"))
        m.say("")
        m.say("Environment writes this run: %d  (%s)"
              % (len(run.env_writes), ", ".join(run.env_writes) or "none"))
        m.say("Flash writes this run:      %d  (%s)"
              % (len(run.flash_writes), ", ".join(run.flash_writes) or "none"))
        m.say("Log: %s" % os.path.join(work, "run.log"))
        return

    ap_ip = a.ap_ip or ap_address(a.host, pc_ip)

    global THIS_RUN
    THIS_RUN = run
    step("Connect to the AP",
         "Log in to %s and get a root shell without touching its flash or\n"
         "environment (Arista's config shell, then prep_upgrade.sh's eval)." % a.host,
         "The AP must be powered, on the network, and on its factory firmware.\n"
         "If it already runs OpenWrt, use --installer-at <its address> instead.\n"
         "This uses Arista's own upgrade command, whose download path is evaled:\n"
         "checked in both models' firmware (C-230 8.9.0-63.1, C-260 11.0.1-49).\n"
         "C-230 its /tmp/offline file blocks that command, so clear offline mode.\n"
         "If a unit's shell never offers 'Continue?', the console procedure in the\n"
         "repository README is the way in, and this tool is not.")
    kind, shell, card = connect(a.host, a.user, a.password)
    run.shell = shell
    card["host"] = a.host
    m.say("  connected: %s" % ("factory firmware" if kind == "stock" else "OpenWrt"))

    backup_dir = a.restore
    manifest = None
    if a.restore:
        manifest = load_manifest(a.restore)
        m.say("")
        m.say("Using the saved firmware in %s (%s %s, %d partitions)"
              % (a.restore, manifest["name"], manifest.get("version", "?"), len(manifest["partitions"])))
        if card.get("mac") and manifest.get("mac") and card["mac"] != manifest["mac"]:
            m.die("that backup belongs to an AP with MAC %s, but this one is %s; refusing"
                  % (manifest["mac"], card["mac"]))

    if kind == "stock":
        step("Verify the AP",
             "Check the model, firmware version, flash layout and the U-Boot\n"
             "variables the fallback needs, before changing anything.",
             "This AP does not match what this tool knows (layout, size, or a\n"
             "missing boot variable).  Nothing was written.  Keep the log and\n"
             "report it; do not force an install on an unexpected layout.")
        verify_stock(shell, card)
        if a.verify_only:
            m.say("")
            m.say("--verify-only: this AP matches the expected C-230 layout.")
            m.say("Nothing was written to it.")
            return
        if not a.restore:
            step("Download this AP's own firmware",
                 "An AP in the wild may be the only copy of its firmware.  Every\n"
                 "partition is read, gzipped on the AP, posted to this PC, and\n"
                 "checked against the AP's own hash of the same flash.",
                 "Free space on this PC, and let TCP 8069 in from the AP.  The AP\n"
                 "is unchanged and still on its own firmware; nothing was written.")
            reused = None
            if a.skip_download:
                m.say("")
                m.say("Looking for this AP's firmware already saved on this PC.")
                reused, m2 = newest_backup(card["model"], card["mac"])
                if reused:
                    manifest = m2
                    backup_dir = reused
                    m.say("  Using %s: %d partitions, taken %s, re-verified now."
                          % (reused, len(m2["partitions"]), m2.get("when", "?")))
                else:
                    m.say("  None that verifies; downloading it again.")
            if not reused:
                outdir = a.backup_dir or os.path.join(BACKUPS, "%s-%s" % (
                    card["model"], card["mac"].replace(":", "")),
                    time.strftime("%Y%m%d-%H%M%S"))
                manifest = download_firmware(shell, card, outdir, include_slot1=not a.fast,
                                             pc_ip=pc_ip)
                backup_dir = outdir
            if a.backup_only:
                pty_clean(shell, a.host)
                m.say("")
                m.say("--backup-only: this AP's firmware is saved and verified.")
                m.say("Nothing was written to the AP's flash or environment.")
                return
    else:
        m.say("")
        m.say("This AP is already on OpenWrt, so there is no factory firmware to")
        m.say("download from it.  %s" % ("Using %s." % a.restore if a.restore else
                                         "To put its original firmware back, give --restore DIR."))

    # ---- the choice --------------------------------------------------------
    step("Choose what to do with this AP",
         "The firmware is safe on this PC either way.",
         "Nothing is written unless you choose 1 or 2.")
    action = None
    if a.backup_only:
        action = "stop"
    elif a.install:
        action = "install"
    elif a.restore:
        action = "restore"
    else:
        m.say("")
        m.say("The AP is a %s, firmware %s, MAC %s."
              % (MODELS[card["model"]]["name"], card.get("version", "?"), card.get("mac", "?")))
        if manifest:
            m.say("Its own firmware is saved in %s and verified." % backup_dir)
        else:
            m.say("No original firmware is available for this AP, so a revert is not")
            m.say("possible; only the OpenWrt install is offered here.")
        m.say("")
        m.say("  [1] Install OpenWrt on this AP")
        if manifest:
            m.say("      (its own firmware stays on this PC; --restore puts it back)")
            m.say("  [2] Put this AP's original firmware back")
            m.say("  [3] Nothing -- stop here (the firmware is already saved)")
        else:
            m.say("  [2] Nothing -- stop here")
        m.say("")
        m.say("  Answer within a minute if you can: Arista's shell expires, and")
        m.say("  reconnecting costs a reboot of the AP (the tool does it for you).")
        choice = ask("Which? [1/2/3] ", "--install or --restore DIR").strip()
        if choice == "1":
            action = "install"
        elif choice == "2" and manifest:
            action = "restore"
        elif choice in ("2", "3"):
            action = "stop"
        else:
            m.die("no choice made; nothing was written")

    if action == "stop":
        if kind == "stock":
            pty_clean(shell, a.host)
        m.say("")
        m.say("Nothing was written to the AP's flash or environment.")
        return
    m.say("  chosen: %s" % ("install OpenWrt" if action == "install" else
                            "put this AP's own firmware back"))

    # ---- the release files -------------------------------------------------
    itb, ubi, sums, tag = release_files(a, card["model"])
    m.say("  release %s" % tag)

    # ---- netboot the installer --------------------------------------------
    step("Boot the RAM installer",
         "One U-Boot environment write makes the AP fetch a RAM-only installer\n"
         "over TFTP.  Arista's own boot stays the last step of the same value,\n"
         "so if the fetch fails the AP boots its own firmware as usual.",
         "Check UDP 69 is free on this PC and that the AP's gateway is the one\n"
         "printed above.  If the netboot fails the AP boots its own firmware; a\n"
         "second run costs nothing.")
    boot_installer(run, card, pc_ip, ap_ip, itb, kind)
    # The installer comes up on the AP's own segment, not on this PC's: ask the
    # operator for its address, and only scan the AP's /24 as a fallback.
    con, ip, board, root = wait_installer(a, a.host.rsplit(".", 1)[0], mac=card.get("mac"))
    if con is None:
        m.die("the RAM installer never appeared.  U-Boot should have fallen back to "
              "this AP's own boot; check the AP is up, then run again with --host.")
    run.installer = con
    m.say("  RAM installer up at %s (%s)." % (ip, board))
    m.say("  It takes its address from DHCP on its LAN port; with no DHCP server on")
    m.say("  that segment it cannot be reached, and the AP is unchanged.")

    if action == "install":
        step("Install OpenWrt",
             "Push factory.ubi to the RAM installer over SSH, let its own checks\n"
             "run, then it writes ONE environment variable and NAND slot 0.\n"
             "Slot 1 stays as Arista's fallback.",
             "The installer refuses rather than write a half-checked image, and the\n"
             "AP's own firmware is still on this PC.  A failed install leaves the\n"
             "AP booting its own firmware; run again when the cause is fixed.")
        do_install(run, card, ip, ubi, sums)
    else:
        step("Put this AP's own firmware back",
             "Write the saved slot 0 image and the U-Boot environment back from\n"
             "the backup just verified, over the RAM installer.",
             "Slot 1 and the other NOR partitions are left as found.  If the write\n"
             "fails the AP still boots its own firmware; nothing else was touched.")
        do_restore(run, card, manifest, backup_dir)

    # ---- report ------------------------------------------------------------
    if a.verify:
        if action == "install":
            verify_openwrt(a.host.rsplit(".", 1)[0], card.get("mac"))
        else:
            ip2, text = find_stock(a.host.rsplit(".", 1)[0], 420)
            m.say("")
            if ip2 is None:
                m.say("Arista's firmware is not answering yet; power-cycle and look "
                      "at the console.")
            else:
                m.say("This AP is back on its own firmware at %s." % ip2)
                for line in text.splitlines():
                    if "Device Build" in line or "Device Version" in line:
                        m.say("  " + line.strip())
    else:
        m.say("")
        m.say("Done. The AP is restarting; find it on your DHCP server's lease list.")
        if action == "install":
            m.say("  it will appear as hostname OpenWrt, MAC %s" % (card.get("mac") or "?"))
            m.say("  booting from NAND, pri=1; Arista's slot 1 is still there as the fallback")
            m.say("  Wi-Fi is off and there is no root password until you set one")
        else:
            m.say("  it will appear on Arista's firmware, at its usual address")
        m.say("  (add --verify to have this tool wait for it and check instead)")

    m.say("")
    m.say("The AP's firmware as found: %s" % backup_dir)
    m.say("Environment writes this run: %d  (%s)"
          % (len(run.env_writes), ", ".join(run.env_writes) or "none"))
    m.say("Flash writes this run:      %d  (%s)"
          % (len(run.flash_writes), ", ".join(run.flash_writes) or "none"))
    m.say("Log: %s" % os.path.join(work, "run.log"))


def load_manifest(d):
    p = os.path.join(d, "manifest.json")
    if not os.path.exists(p):
        m.die("no manifest.json in %s" % d)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    main()


