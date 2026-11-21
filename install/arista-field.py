#!/usr/bin/env python3
"""Put OpenWrt on an Arista access point in the field -- or put that AP's own
firmware back -- with no serial cable.

One script for every model: what differs (FIT configuration, partition names,
installer, NOR size) is in MODELS, and nothing else is per-model.

The rule this tool is built around: an AP in the wild may be the only copy of
its own firmware.  So it always downloads the AP's flash first, verifies every
byte of it, and only then offers to change anything.

    1. Find the AP and ask it what it is (its own CLI; nothing is changed).
    2. Ask once: install OpenWrt, only save the firmware, or put it back.
    3. Fetch the release for that model and check it.
    4. Connect (SSH as config, then a root shell out of the CLI's radartool)
       and verify the AP: model, firmware version, flash layout, U-Boot
       variables the fallback needs.  Refuses to go on if anything is unexpected.
    5. Download all of its flash (gzip, streamed to this PC, never through the
       AP's tmpfs), hashing both ends and comparing.
    6. Do it, wait for the AP, and say where it is and what was written.

    python arista-field.py                                   # finds the AP, asks
    python arista-field.py --host 192.168.1.191              # that AP, asks
    python arista-field.py --host 192.168.1.191 --install
    python arista-field.py --host 192.168.1.61 --update      # an AP already on OpenWrt
    python arista-field.py --host 192.168.1.191 --backup-only
    python arista-field.py --host 192.168.1.50 --ramboot     # C-460: run from RAM, no install
    python arista-field.py --host 192.168.1.50 --ramboot --image build.itb
    python arista-field.py --restore backups/c230-30862d73b66f   # from OpenWrt or Arista
    python arista-field.py --list-releases                   # what each release carries
    python arista-field.py --fetch w318                      # a model's images, verified
"""

import argparse
import atexit
import concurrent.futures
import contextlib
import gzip
import hashlib
import importlib.util
import json
import logging
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

# A Windows console is often cp1252: any non-ASCII byte -- from this script or
# from the AP -- would otherwise raise UnicodeEncodeError mid-run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = "WiFiGUY512/openwrt-arista-images"
# The GitHub API.  ARISTA_FIELD_API points it elsewhere (the tests' local mock).
API = os.environ.get("ARISTA_FIELD_API", "https://api.github.com").rstrip("/")
# A release's index.json lists the models it carries and their files (schema 1,
# written by publish-release.py).  Releases older than it are matched by file name.
INDEX = "index.json"

# Tool version -- bump on every change to how the tool drives an AP.
#   1.0  first field tool; proven on the C-230 and C-260 (Arista 11.0.1), 2026-10.
#   1.1  Arista firmware 16.x: read the model from 'show device info' when
#        'version' is gone, and accept the 16.x "Continue?/Confirm? [y]/n:"
#        prompt wording as well as 11.x's "([y]/n)" (2026-11-04).
#   1.2  16.x's 'upgrade' dialogue is a different shape, not just different
#        wording: it asks for the server/host before "Continue?" (11.x asks
#        Continue? first, then two optional build sub-prompts, then the
#        server address), and has no separate "Confirm?" after.  The tool
#        now waits for whichever leading prompt shows up first and drives
#        the rest from that, instead of assuming 11.x's order (2026-11-04,
#        from a hand-run transcript on a C-260, build 16.1.0-51.6).
#   1.3  16.x's "Upgrade from Server/Host []" is an *echo* of the source, not
#        a prompt: nothing reads a host there, so a payload typed at it
#        answers Continue? and starts an upgrade from nothing.  A root shell
#        now comes from radartool instead -- 'radartool params ;sh; radio 0'
#        evals its arguments, dropping to BusyBox ash (proven on a C-260 on
#        16.1.0-51.6 and a C-360 on 8.9/11.x, and documented for firmware 18
#        by Arista-AP-firmware).  The upgrade wizard remains only as a
#        fallback, its 16.x/18.x source now passed as 'upgrade url <url>' and
#        believed only when the echo line shows our host (2026-11-04).
#   2.0  One way in for a person: run it with no options.  It looks for the AP
#        on this PC's network (only hosts with an Arista MAC prefix are asked
#        who they are), reads the model at the AP's own CLI, takes the newest
#        accepted release that has the model (the newest testing one where a
#        model has no accepted release yet), asks once, and then runs to the
#        end, waiting for the AP and saying where it is.  The screen carries
#        one line per step and a progress line; everything else is in the log
#        (--verbose puts it back on screen).  New: --update (sysupgrade of an
#        AP that already runs the port), the kexec helper files fetched from
#        the repository, port checks before the AP is touched, logs under
#        logs/ and backups under backups/ next to this file (2026-11-15).
TOOL_VERSION = "2.0"

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
        self.path = path
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


# Paramiko logs a full traceback for every host that accepts TCP and then does
# not answer with an SSH banner -- normal while a scan walks a /24 and an AP is
# still booting.  Keep it off the screen; the failures themselves are handled.
logging.getLogger("paramiko").setLevel(logging.CRITICAL)


def _old_ssh(t):
    """Let one connection talk to the SSH server in Arista's oldest firmware.

    A W-118 on 8.7.1 offers one key exchange (diffie-hellman-group1-sha1) and
    one host key type (ssh-rsa, signed with SHA-1), and current paramiko has
    dropped both (read on a unit, 2026-11-15: "no acceptable kex algorithm").
    Both are a few lines on top of what paramiko still has: the group-14
    exchange with the 1024-bit prime of RFC 2409 section 6.2 and SHA-1, and its
    RSA key with SHA-1 allowed for the host's signature.  They are added to
    this one connection, after the ordinary attempt has failed, and to nothing
    else."""
    from hashlib import sha1
    from cryptography.hazmat.primitives import hashes
    from paramiko import kex_group14, rsakey

    class KexGroup1(kex_group14.KexGroup14SHA256):
        P = int("FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
                "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
                "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
                "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE65381FFFFFFFFFFFFFFFF", 16)
        G = 2
        name = "diffie-hellman-group1-sha1"
        hash_algo = sha1

    class OldRSAKey(rsakey.RSAKey):
        HASHES = dict(rsakey.RSAKey.HASHES)
        HASHES["ssh-rsa"] = hashes.SHA1

    t._kex_info = dict(t._kex_info)
    t._kex_info[KexGroup1.name] = KexGroup1
    t._key_info = dict(t._key_info)
    t._key_info["ssh-rsa"] = OldRSAKey
    t._preferred_kex = tuple(t._preferred_kex) + (KexGroup1.name,)
    t._preferred_keys = tuple(t._preferred_keys) + ("ssh-rsa",)
    t._preferred_ciphers = tuple(t._preferred_ciphers) + tuple(
        c for c in ("aes128-cbc", "aes256-cbc", "3des-cbc") if c not in t._preferred_ciphers)
    t._preferred_macs = tuple(t._preferred_macs) + tuple(
        c for c in ("hmac-sha1",) if c not in t._preferred_macs)


# Hosts whose SSH server needed _old_ssh, so later connections skip the failed try.
OLD_SSH_HOSTS = set()


def ssh_open(host, user, password, timeout=15, tries=1, delay=2):
    """An SSH client.  OpenWrt's root has no password, and dropbear accepts a
    blank one only through the "none" method -- so that is asked for by name
    rather than left to paramiko's password path."""
    import paramiko
    last = None
    attempts = [host in OLD_SSH_HOSTS] * tries
    while attempts:
        old = attempts.pop(0)
        t = None
        try:
            sock = socket.create_connection((host, 22), timeout)
            t = paramiko.Transport(sock)
            t.banner_timeout = timeout
            if old:
                _old_ssh(t)
            try:
                t.start_client(timeout=timeout)
            except Exception as e:
                # A server too old for this library: once more, with the old
                # exchange added.  Anything else is the caller's to see.
                if not old and re.search(r"no acceptable|Incompatible ssh", str(e)):
                    OLD_SSH_HOSTS.add(host)
                    attempts.insert(0, True)
                raise
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


# What the run is for decides which files it needs: a first install (or a
# restore) needs the RAM installer and the install image; updating an AP that
# already runs this port needs only the sysupgrade image.
PURPOSE = "install"


def files_for(model, purpose=None):
    """The release files this model needs, by kind.  The C-models install from
    factory.ubi and its install sums; the W-models' images are the RAM installer
    and the sysupgrade image.  An unknown model is named the C-models' way, so
    asking for one finds no release rather than failing."""
    d = MODELS.get(model) or {"target": "qualcommax/ipq807x", "device": "arista_c-" + model[1:]}
    base = "openwrt-%s-%s" % (d["target"].replace("/", "-"), d["device"])
    if (purpose or PURPOSE) == "update":
        return {"sysupgrade": base + "-squashfs-sysupgrade.bin"}
    names = {"itb": "%s-%s" % (base, d.get("initramfs", "initramfs-uImage.itb"))}
    if d.get("image", "factory") == "factory":
        names["ubi"] = base + "-squashfs-factory.ubi"
        names["sums"] = "%s-install-SHA256SUMS" % model
    else:
        names["sysupgrade"] = base + "-squashfs-sysupgrade.bin"
    return names


def field_install(model):
    """Whether this tool installs and restores this model itself.  A model
    marked False is still in the releases (--list-releases, --fetch) and is
    pointed at its by-hand page.  No model is marked so today."""
    return MODELS[model].get("field_install", True)


BY_HAND = "https://github.com/WiFiGUY512/openwrt-arista/blob/main/%s/BY-HAND.md"


def refuse_unsupported(model):
    """Stop before anything is written, and say how this model is installed."""
    d = MODELS[model]
    die("the %s cannot be installed over the network yet: a way into Arista's\n"
        "firmware has not been confirmed on this model.  Nothing was changed.\n"
        "It installs with a console cable instead:\n"
        "  %s\n"
        "Its images, checked:  python %s --fetch %s"
        % (d["name"], BY_HAND % model, os.path.basename(sys.argv[0]) or "arista-field.py", model))


def http_get(url, accept=None):
    req = urllib.request.Request(url, headers={"User-Agent": "arista-net-install"})
    if accept:
        req.add_header("Accept", accept)
    return urllib.request.urlopen(req, timeout=60)


_INDEXES = {}
PICKED = None           # the release the last fetch took: tag, testing?, date


def list_releases(channel="stable"):
    """The repository's releases, newest first.  Drafts never; pre-releases
    (testing builds) only on the testing channel."""
    rels, page = [], 1
    while page <= 10:
        batch = json.load(http_get("%s/repos/%s/releases?per_page=100&page=%d" % (API, REPO, page),
                                   "application/vnd.github+json"))
        rels += batch
        if len(batch) < 100:
            break
        page += 1
    rels = [r for r in rels if not r.get("draft") and (channel == "testing" or not r.get("prerelease"))]
    rels.sort(key=lambda r: r.get("published_at") or r.get("created_at") or "", reverse=True)
    return rels


def release_index(rel):
    """A release's index.json, or None for one published before there was one."""
    tag = rel["tag_name"]
    if tag not in _INDEXES:
        _INDEXES[tag] = None
        for a in rel.get("assets", []):
            if a["name"] == INDEX:
                try:
                    _INDEXES[tag] = json.load(http_get(a["browser_download_url"]))
                except Exception as e:
                    note("%s: its %s is unreadable (%s); going by file names" % (tag, INDEX, e))
    return _INDEXES[tag]


def release_carries(rel, models):
    """Whether a release has every file these models need: its index.json must
    list the model with those files, when it has an index, and the files must
    be published either way."""
    assets = {a["name"] for a in rel.get("assets", [])}
    for model in models:                 # by file name first: that costs no request
        if not (set(files_for(model).values()) | {"SHA256SUMS"}) <= assets:
            return False
    idx = release_index(rel)
    for model in models:
        need = set(files_for(model).values())
        if idx is not None:
            entry = idx.get("models", {}).get(model)
            if not entry or not need <= {f["name"] for f in entry.get("files", {}).values()}:
                return False
    return True


def pick_release(models, channel=None):
    """The release a run takes: (release, newer_testing_release).

    "Accepted" is what the project's maintainer says it is: a release that is
    not marked pre-release on GitHub.  New builds go out as pre-releases
    (testing) and are promoted after a soak.
      channel None      the newest accepted release that has the model; when no
                        accepted release has it yet, the newest testing one
      channel stable    accepted only
      channel testing   the newest of either
    The second value is a testing release newer than the one picked, if any."""
    rels = list_releases("testing")
    newest = next((r for r in rels if release_carries(r, models)), None)
    if channel == "testing":
        return newest, None
    accepted = next((r for r in rels
                     if not r.get("prerelease") and release_carries(r, models)), None)
    if accepted is None:
        return (None, newest) if channel == "stable" else (newest, None)
    newer = newest if (newest is not None and newest.get("prerelease")
                       and newest["tag_name"] != accepted["tag_name"]) else None
    return accepted, newer


def fetch_release(tag, models, cache, channel="stable"):
    """The release for these models, downloaded into cache/<tag> and verified.

    With a tag, that release, which must carry the models.  Without one, the
    newest release on the channel that carries them: releases come out one
    after another, and not every release has every model."""
    names = "/".join(MODELS[x]["name"] if x in MODELS else x for x in models)
    if tag:
        rel = json.load(http_get("%s/repos/%s/releases/tags/%s" % (API, REPO, tag),
                                 "application/vnd.github+json"))
        if not release_carries(rel, models):
            die("the release %s has no %s files.  Leave out --tag to take the newest "
                "release that has them, or see what each release has with "
                "--list-releases." % (tag, names))
    else:
        rel, _newer = pick_release(models, channel)
        if rel is None:
            die("no %srelease has the %s files yet.  Publish that model's files in a "
                "release%s, or copy them into %s and run with --offline --tag <tag>."
                % ("accepted " if channel == "stable" else "", names,
                   " (or try --channel testing)" if channel == "stable" else "", cache))
    tag = rel["tag_name"]
    assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
    d = os.path.join(cache, tag)
    os.makedirs(d, exist_ok=True)
    say("Release %s%s" % (tag, "  (testing build)" if rel.get("prerelease") else ""))
    global PICKED
    PICKED = {"tag": tag, "testing": bool(rel.get("prerelease")),
              "published": (rel.get("published_at") or "")[:10]}
    with open(os.path.join(d, "release.json"), "w") as f:
        json.dump({"tag": tag, "name": rel.get("name"), "prerelease": bool(rel.get("prerelease")),
                   "published_at": rel.get("published_at")}, f)
    idx = release_index(rel)
    if idx is not None:
        with open(os.path.join(d, INDEX), "w") as f:
            json.dump(idx, f)
    want = ["SHA256SUMS"] + [f for x in models for f in files_for(x).values()]
    for name in want:
        if name not in assets:
            die("the release %s has no %s for this model.  Either publish that "
                "model's files in a release, or copy them into %s and run with "
                "--offline --tag %s." % (tag, name, cache, tag))
        path = os.path.join(d, name)
        if not os.path.exists(path):
            say("  downloading %s" % name)
            with http_get(assets[name]) as r, open(path + ".part", "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
                    done += len(b)
                    status("downloading %s  %d%s MiB" % (
                        name[-46:], done >> 20, (" of %d" % (total >> 20)) if total else ""),
                        log=False)
            os.replace(path + ".part", path)
    check_sums(d, want)
    if idx is not None:
        for x in models:
            listed = {f["name"]: f["sha256"] for f in idx["models"][x]["files"].values()}
            for name in files_for(x).values():
                if listed.get(name) != sha256_file(os.path.join(d, name)):
                    die("%s does not match the release's %s; delete %s and run again" % (name, INDEX, d))
        say("  and against the release's %s" % INDEX)
    return d, tag


def list_cmd():
    """What each release carries, newest first, testing builds marked."""
    tell("Releases of %s, newest first:" % REPO)
    for r in list_releases("testing"):
        have = [MODELS[x]["name"] for x in sorted(MODELS) if release_carries(r, [x])]
        tell("  %-32s %s  %-28s%s%s" % (
            r["tag_name"], (r.get("published_at") or "")[:10], ", ".join(have) or "(none of these models)",
            "  [testing]" if r.get("prerelease") else "",
            "" if release_index(r) is not None else "  (no index; by file name)"))


def fetch_cmd(a):
    """--fetch MODEL: a model's newest release files, verified, with no AP: for
    an install by hand, or to fill a field laptop's cache before the site."""
    model = a.fetch.lower().replace("-", "")
    if model not in MODELS:
        die("no model %r; this tool knows %s" % (a.fetch, ", ".join(sorted(MODELS))))
    d, tag = fetch_release(a.tag, [model], os.path.abspath(a.files), a.channel)
    tell("")
    tell("%s, release %s, verified, in %s:" % (MODELS[model]["name"], tag, d))
    for kind, name in files_for(model).items():
        tell("  %-10s %s" % (kind, name))
    if not field_install(model):
        tell("")
        tell("This tool does not install the %s over the network yet.  By hand:"
             % MODELS[model]["name"])
        tell("  %s" % (BY_HAND % model))


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
    """This PC's address as the AP will see it.

    The system's route towards the AP names it -- unless a VPN on this PC has
    just taken that route, and then the address named is the VPN's, which the
    AP cannot reach (a run stopped at the backup that way, 2026-11-15: the AP
    was told to post to a 100.64.x.x address).  An address of this PC on the
    AP's own /24 is the one the AP can reach, so it is preferred when the route
    names anything else."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((towards, 9))
        ip = s.getsockname()[0]
    finally:
        s.close()
    net = towards.rsplit(".", 1)[0] + "."
    if ip.startswith(net) or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", towards):
        return ip
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if info[4][0].startswith(net):
                return info[4][0]
    except Exception:
        pass
    return ip



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
    """Where AP firmware is kept: backups/ next to this file.  A folder further
    up is used only if it already holds this tool's backups (<model>-<mac>), so
    somebody's own ../backups is never mistaken for ours."""
    here = os.path.join(HERE, "backups")
    if os.path.isdir(here):
        return here
    for rel in (("..", "..", "backups"), ("..", "backups")):
        d = os.path.abspath(os.path.join(HERE, *rel))
        try:
            if any(re.fullmatch(r"[a-z]\d{3}-[0-9a-f]{12}", n) for n in os.listdir(d)):
                return d
        except OSError:
            pass
    return here


def _find_releases():
    """The release cache: releases/ next to this file, else the project's
    firmware/releases or an older home one where they exist."""
    here = os.path.join(HERE, "releases")
    if os.path.isdir(here):
        return here
    for d in (os.path.abspath(os.path.join(HERE, "..", "..", "firmware", "releases")),
              os.path.abspath(os.path.join(HERE, "..", "firmware", "releases")),
              os.path.join(os.path.expanduser("~"), "arista-openwrt-files")):
        if os.path.isdir(d):
            return d
    return here


def new_run_dir():
    """This run's folder for its log: logs/ next to this file, or the system's
    temporary folder when that cannot be written."""
    name = "field-run-%s" % time.strftime("%Y%m%d-%H%M%S")
    for base in (os.path.join(HERE, "logs"), os.path.join(os.path.expanduser("~"), "arista-openwrt-logs")):
        try:
            d = os.path.join(base, name)
            os.makedirs(d, exist_ok=True)
            return d
        except OSError:
            continue
    import tempfile
    return tempfile.mkdtemp(prefix=name + "-")


BACKUPS = _find_backups()
RELEASES = _find_releases()
TOOLS = _find_tools()

# What goes where.  The screen is for someone standing next to an AP who wants to
# know three things: what is happening, whether it is still working, and what to
# do next.  So:
#   tell()    a line the operator needs; always on screen
#   say()     detail; the log always, the screen only with --verbose
#   note()    a normal event that would read as a problem; same rule as say()
#   status()  progress that replaces itself on one line (a long step never
#             looks dead, and never scrolls)
# The log (logs/field-run-*/run.log) always gets everything, with the clock.
T0 = time.time()
VERBOSE = False
_OUT = threading.Lock()
_STATUS = {"len": 0}


def _tty():
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _log_line(line):
    if getattr(LOG, "f", None):
        LOG.write("\n### %s %s\n" % (time.strftime("%H:%M:%S"), line))


def _clear_status():
    if _STATUS["len"]:
        sys.stdout.write("\r" + " " * _STATUS["len"] + "\r")
        sys.stdout.flush()
        _STATUS["len"] = 0


def _print(line):
    with _OUT:
        _clear_status()
        if VERBOSE and line:
            line = "[%s] %s" % (time.strftime("%H:%M:%S"), line)
        print(line, flush=True)


def tell(msg=""):
    """A line the operator needs: always on screen, and in the log."""
    line = "" if msg is None else str(msg)
    _log_line(line)
    _print(line)


def say(msg=""):
    """Detail.  The log always; the screen only with --verbose."""
    line = "" if msg is None else str(msg)
    _log_line(line)
    if VERBOSE:
        _print(line)


def note(msg):
    """A normal event that would read as a problem.  Log always, screen only
    with --verbose."""
    if VERBOSE:
        say("   " + str(msg))
    elif getattr(LOG, "f", None):
        LOG.write("\n### %s note: %s\n" % (time.strftime("%H:%M:%S"), msg))


def status(text, log=True):
    """Progress on one line that replaces itself.  Without a terminal (piped
    output) it goes to the log only: a file full of progress lines helps nobody."""
    if log:
        _log_line("... " + text)
    if VERBOSE:
        if not log:
            return
        _print("    ... " + text)
        return
    if not _tty() or _STATUS.get("asking"):
        return
    try:
        width = os.get_terminal_size().columns
    except Exception:
        width = 80
    line = ("     " + text)[:max(20, width - 1)]
    with _OUT:
        pad = max(0, _STATUS["len"] - len(line))
        sys.stdout.write("\r" + line + " " * pad + ("\b" * pad))
        sys.stdout.flush()
        _STATUS["len"] = len(line)


def ask(prompt, hint):
    """A question needs a terminal.  With piped input `input()` blocks for ever
    and the run looks alive but is doing nothing (2026-10-25)."""
    if not sys.stdin.isatty():
        die("this run needs an answer to\n    %s\nand there is no terminal to ask on. "
            "Add %s, or run it from a terminal." % (prompt.strip(), hint))
    with _OUT:
        _clear_status()
        _STATUS["asking"] = True
    _log_line("ASK: " + prompt.strip())
    try:
        answer = input(prompt)
    except EOFError:
        # Input that ends is not an answer.  (Windows calls its null device a
        # terminal, so isatty() above does not catch every case; an empty
        # answer here once made a question repeat for ever.)
        _STATUS["asking"] = False
        print("", flush=True)
        die("this run needs an answer to\n    %s\nand its input has ended.  Add %s, or run "
            "it in a terminal window.  Nothing was changed by asking." % (prompt.strip(), hint))
    finally:
        _STATUS["asking"] = False
    _log_line("ANSWER: " + answer)
    return answer


def confirm(prompt, hint="--yes"):
    """A yes/no question; only a typed y or yes is a yes."""
    return ask(prompt + " [y/N] ", hint).strip().lower() in ("y", "yes")


def phase(text):
    say("")
    say("=" * 72)
    say("%s   (+%ds)" % (text, int(time.time() - T0)))
    say("=" * 72)


TOTAL_STEPS = 6
STEP = {"n": 0, "title": "", "hint": ""}
THIS_RUN = None


def step(title, why, hint):
    """One line per step on screen; the reasoning goes to the log, and to the
    screen only with --verbose."""
    STEP["n"] += 1
    STEP["title"] = title
    STEP["hint"] = hint
    tell("")
    tell("%d/%d  %s" % (STEP["n"], TOTAL_STEPS, title))
    if getattr(LOG, "f", None):
        LOG.write("\n    why:  %s\n    if it fails: %s\n"
                  % (str(why).replace("\n", " "), str(hint).replace("\n", " ")))
    if VERBOSE:
        for line in str(why).splitlines():
            say("    " + line.strip())


def log_path():
    return getattr(LOG, "path", None)


def die(msg, hint=None):
    """Stop, in plain words: what happened, what to do, and what was written.
    hint replaces the running step's own advice where that would mislead."""
    if hint is not None:
        STEP["hint"] = hint
    lines = [l.strip() for l in str(msg).strip().splitlines() if l.strip()]
    tell("")
    tell("Couldn't continue: %s" % (lines[0] if lines else "unknown problem"))
    # The rest of the message is part of the answer (for a model the tool does not
    # install it is the manual procedure), so it stays -- just bounded, with the
    # log for the rest.
    for line in lines[1:12]:
        tell("   " + line)
    if len(lines) > 12:
        tell("   ... (%d more lines in the log)" % (len(lines) - 12))
    if STEP["hint"]:
        hint = [l.strip() for l in str(STEP["hint"]).splitlines() if l.strip()]
        tell("   What to do: %s" % hint[0])
        for line in hint[1:]:
            tell("               %s" % line)
    written = []
    if THIS_RUN is not None:
        written = THIS_RUN.env_writes + THIS_RUN.flash_writes
    if written:
        tell("   Written so far: %s" % ", ".join(written))
        if THIS_RUN is not None and THIS_RUN.flash_writes:
            tell("   The AP's own firmware is saved on this PC; run the tool again.")
        else:
            tell("   The AP still boots its own firmware; nothing else was changed.")
    elif THIS_RUN is not None:
        tell("   Nothing was written to the AP.")
    if getattr(LOG, "f", None):
        LOG.write("\n    FULL: %s\n" % str(msg))
    if log_path():
        tell("   Full detail: %s" % log_path())
    try:
        _cleanup_stock()        # while the session to the AP is still open
    except Exception:
        pass
    sys.exit(1)


class Heartbeat:
    """Shows that a slow step is alive: one status line that replaces itself,
    with the seconds counting, so a long operation never looks like a hang."""

    def __init__(self, what, every=15):
        self.what = what
        self.every = every
        self.stop = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        # The screen ticks every second; the log (and --verbose) get a line
        # every `every` seconds, as before.
        start = time.time()
        n = 0
        while not self.stop.wait(1):
            n += 1
            mine = bool(Heartbeat.stack) and Heartbeat.stack[-1] is self
            if mine or n % self.every == 0:
                status("%s (%d s)" % (self.what, int(time.time() - start)),
                       log=(n % self.every == 0))

    def __enter__(self):
        Heartbeat.stack.append(self)
        status("%s" % self.what)
        self.t.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.t.join(2)
        if self in Heartbeat.stack:
            Heartbeat.stack.remove(self)
        with _OUT:
            _clear_status()
        return False


Heartbeat.stack = []        # a step's heartbeat may hold a finer one inside it

NETMASK = "255.255.255.0"

MODELS = {
    "c230": {
        "name": "C-230",
        "target": "qualcommax/ipq807x",
        "device": "arista_c-230",
        "fit_cfg": "config@ac02",
        "installer": "arista-c230-install",
        "slot0": ("rootfs", "ubi"),
        "slot1": ("rootfs_1",),
        "appsblenv": ("0:APPSBLENV", "appsblenv"),
        "need_nor": ("0:HLOS_1",),
        "need_env": ("bootcmd", "ba0", "ba1", "ubootscript"),
        "nor_total": 32 * 1024 * 1024,
        "slot_size": 0x10000000,
        # 8.9.0-63.1's CLI has no radartool command at all (its /sbin debug
        # radartool is a netlink-only binary), so do not spend a round trip
        # asking: the upgrade wizard is the way in on this model.
        "root_order": ("upgrade", "radartool"),
    },
    "c360": {
        "name": "C-360",
        "target": "qualcommax/ipq807x",
        "device": "arista_c-360",
        # OpenWrt main's FIT configuration for this board, and the load address
        # its size needs: the C-360's image goes to 0xA0000000, not 0x60000000
        # (from c360/BY-HAND.md).
        "fit_cfg": "config@hk09",
        "load_addr": "0xA0000000",
        "installer": "arista-c360-install",
        # Installed on a unit with this tool, 2026-11-15 (Arista 20.0.0-179,
        # running from slot 1).  Its way back to Arista by this tool has not
        # been run on one; "unproven" is the key for a note before the question.
        "slot0": ("rootfs", "ubi"),
        "slot1": ("rootfs_1",),
        "appsblenv": ("0:APPSBLENV", "0:appsblenv"),
        "need_nor": ("0:HLOS_1", "0:hlos_1"),
        "need_env": ("bootcmd", "ba0", "ba1", "ubootscript"),
        # Its NOR is 32 MiB with a 1 MiB ART (backups/nor_parts/MANIFEST.txt).
        "nor_total": 32 * 1024 * 1024,
        # NAND slot 0 is 0x18000000 on this model: 384 MiB, not 256 MiB.
        "slot_size": 0x18000000,
        # Its installer backs up 768 MiB of NAND plus NOR and wants >512 MiB free,
        # so it gets a bigger staging tmpfs than the C-260.
        "stage_mb": 1024,
    },
    "c260": {
        "name": "C-260",
        "target": "qualcommax/ipq807x",
        "device": "arista_c-260",
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
    "w318": {
        "name": "W-318",
        "target": "qualcommax/ipq60xx",
        "device": "arista_w-318",
        # eMMC, with its kernel in NOR 0:HLOS: no factory.ubi.  The RAM
        # installer's arista-w318-install writes the sysupgrade image itself.
        "image": "sysupgrade",
        # Its flash, as read on a unit on Arista 18.0.0-176.12 (2026-11-15): 22
        # NOR partitions adding up to 32 MiB, the two kernels among them
        # (0:HLOS at 0x7a0000, 0:HLOS_1 at 0xfa0000), and the two systems on the
        # eMMC.  The way in is radartool, as on the other 18.x and 20.x units.
        "storage": "emmc",
        "emmc_disk": "mmcblk0",
        "emmc_parts": (("rootfs", 1, 34, 262144),
                       ("rootfs_1", 2, None, 262144),
                       ("rootfs_data", 3, None, 524288),
                       ("rootfs_data_1", 4, None, 524288)),
        "slot0_parts": ("rootfs", "rootfs_data"),
        "slot1_parts": ("rootfs_1", "rootfs_data_1"),
        "overlay_part": "rootfs_data",
        "appsblenv": ("0:APPSBLENV", "0:appsblenv"),
        "need_nor": ("0:HLOS_1", "0:hlos_1"),
        "need_env": ("bootcmd", "ubootscriptv2", "ubootscript", "pri"),
        "nor_total": 32 * 1024 * 1024,
        # Arista's own boot command, exactly (fw_printenv on the unit).  It holds
        # double quotes, so the netboot puts it back inside single ones.
        "stock_bootcmd": 'env default default_env_version; if test -n "${default_env_version}"; '
                         'then run ubootscriptv2; else run ubootscript; fi;',
        "fit_cfg": "config@cp01-c3",
        "fdt_high": None,
        # Its uplink is a 2.5 G port: several short fetch attempts, each its own
        # variable so no line pasted into Arista's shell is long.
        "net_helper": True,
        "net_tries": 4,
        # The install is the image's own script, which checks this unit (its
        # layout, its calibration, and that slot 1 holds an Arista system with
        # the Wi-Fi firmware OpenWrt's first boot copies) and writes one
        # fw_setenv, the eMMC "rootfs" and NOR 0:HLOS.
        "installer": "arista-w318-install",
        "install_by": "script",
        "pri_note": "Arista calls slot 0 its primary on this unit, so the system that runs now is "
                    "the one the install replaces; the copy in slot 1 is what is kept.",
        # Back to Arista: its kept copy in slot 1, by the boot command alone.
        "revert": "bootcmd",
        "revert_names": ("owrt_boot", "arista_boot"),
        "revert_env": (("bootargs", "console=ttyMSM0,115200n8 root=/dev/mmcblk0p2 rootwait rw"),
                       ("pri", "1"), ("sr", "0")),
        "revert_note": ("Arista's firmware starts again from its second slot, where the install",
                        "left it.  OpenWrt stays in the first slot, no longer started.",
                        "That copy keeps its own settings and may not take the login the other",
                        "one did; the AP's reset button brings back config / config."),
    },
    "w118": {
        "name": "W-118",
        "target": "ipq40xx/generic",
        "device": "arista_w-118",
        # Its U-Boot (2012.07) has no LZMA, so the RAM installer is a zImage FIT.
        "initramfs": "initramfs-zImage.itb",
        # Installed with sysupgrade -n from the RAM installer; the release also
        # carries a factory.ubi, which that install does not use.
        "image": "sysupgrade",
        # Its flash, as read on a unit on Arista 8.7.1-03.2 (2026-11-15): two
        # 64 MiB NAND slots that Arista owns, eleven NOR partitions adding up to
        # 0xF80000, and an upper 128 MiB of NAND that Arista never uses.  OpenWrt
        # goes into that upper half, so the install replaces nothing of Arista's.
        "slot0": ("rootfs", "arista_rootfs"),
        "slot1": ("rootfs2", "arista_rootfs2"),
        "slot_size": 0x4000000,
        "appsblenv": ("0:APPSBLENV", "0:appsblenv"),
        "need_nor": ("0:HLOS1", "0:hlos1"),
        "need_env": ("bootcmd", "ba0", "ba1", "ubootscript"),
        "nor_total": 0xF80000,
        "keeps_arista": True,
        "revert": "bootcmd",
        "revert_note": ("Arista's firmware was never removed from this model; only the boot",
                        "command changes.  OpenWrt stays in the flash, no longer started."),
        # 8.7.1's CLI has no radartool; its upgrade wizard is the way in (and
        # its SSH server needs _old_ssh).
        "root_order": ("upgrade",),
        # U-Boot 2012.07: the image is loaded at 0x84000000 and booted with a
        # bare bootm (one configuration, no '#config' argument), and fdt_high is
        # left alone.
        "fit_cfg": None,
        "load_addr": "0x84000000",
        "fdt_high": None,
        # The boot command the install writes, one fw_setenv -s, from the RAM
        # installer.  owrt_boot is the form that has booted the lab unit since
        # 2026-10-19; the fallback puts Arista's own partition table and
        # arguments back first, so a failed OpenWrt boot cannot leave Arista
        # starting with OpenWrt's.  pri is not touched: Arista stays where it is.
        "install_env": (
            ("owrt_args", "console=ttyMSM0,115200n8"),
            ("owrt_boot", "setenv mtdids nand0=nand0 && setenv mtdparts "
                          "mtdparts=nand0:0x8000000@0x8000000(ubi) && ubi part ubi && "
                          "ubi read 0x84000000 kernel && iminfo 0x84000000 && bootm 0x84000000"),
            ("arista_boot", "setenv mtdids; setenv mtdparts; if test ${pri} = 0; then "
                            "setenv bootargs ${ba0}; else setenv bootargs ${ba1}; fi; "
                            "run ubootscript"),
            ("bootcmd", "setenv bootargs ${owrt_args}; run owrt_boot; run arista_boot"),
        ),
        "install_where": "the upper half of the NAND, which Arista's firmware does not use",
        "install_keeps": "Both of Arista's slots are left exactly as they are.",
    },
    "c200": {
        "name": "C-200",
        "target": "qualcommax/ipq60xx",
        "device": "arista_c-200",
        "fit_cfg": "config@cp01-c3",
        # The C-230's flash scheme (factory.ubi into NAND slot 0).  Its flash, as
        # read on a unit on Arista 11.0.0-36.19 (2026-11-15): 22 NOR partitions
        # adding up to 32 MiB, the two kernels among them under the names
        # HL_backup0 and HL_backup1, and two 256 MiB NAND slots.  11.0.0 has no
        # radartool; the upgrade wizard is the way in.
        "slot0": ("rootfs", "ubi"),
        "slot1": ("rootfs_1",),
        "appsblenv": ("0:APPSBLENV", "0:appsblenv"),
        "need_nor": ("HL_backup1",),
        "need_env": ("bootcmd", "ba0", "ba1", "ubootscript"),
        "nor_total": 32 * 1024 * 1024,
        "slot_size": 0x10000000,
        "root_order": ("upgrade", "radartool"),
        "fdt_high": None,                 # every C-200 RAM boot used Arista's own
        "net_helper": True,
        "net_tries": 3,
        # Its image carries no installer script, so the steps are made here
        # (do_install_ubiformat): the boot command first, then slot 0.  The
        # variables are the ones the unit has booted with since 2026-11-02
        # (c200/BY-HAND.md in the port); pri=1 makes Arista's own boot use
        # slot 1, the slot that is kept.
        "install_by": "ubiformat",
        "install_env": (
            ("owrt_args", "console=ttyMSM0,115200n8 rootwait panic=10"),
            ("owrt_parts", "setenv mtdids nand0=nand0; setenv mtdparts mtdparts=nand0:0x10000000@0x0(ubi)"),
            ("owrt_load", "ubi part ubi && ubi read 0x44000000 kernel"),
            ("owrt_boot", "run owrt_parts owrt_load && bootm 0x44000000#config@cp01-c3"),
            ("arista_unset", "setenv mtdids; setenv mtdparts"),
            ("arista_args", "if test ${pri} = 0; then setenv bootargs ${ba0}; else setenv bootargs ${ba1}; fi"),
            ("arista_boot", "run arista_unset arista_args ubootscript"),
            ("bootcmd", "setenv bootargs ${owrt_args}; run owrt_boot; run arista_boot"),
            ("pri", "1"),
        ),
        "pri_note": "Arista calls slot 0 its primary on this unit, so the system that runs now is "
                    "the one the install replaces; the copy in slot 1 is what is kept.",
        # Back to Arista: its kept copy in slot 1, by the boot command alone.
        "revert": "bootcmd",
        "revert_env": (("pri", "1"),),
        "revert_note": ("Arista's firmware starts again from its second slot, where the install",
                        "left it.  OpenWrt stays in the first slot, no longer started.",
                        "That copy keeps its own settings and may not take the login the other",
                        "one did; the AP's reset button brings back config / config."),
    },
    "c130": {
        "name": "C-130",
        # The port's device is the community one it builds on, "mojo_c-130"
        # (board name mojo,c-130): the image names carry that, and the RAM
        # installer and the installed system answer to it.
        "target": "ipq806x/generic",
        "device": "mojo_c-130",
        "board": "mojo,c-130",
        "initramfs": "initramfs-fit-uImage.itb",
        "image": "sysupgrade",
        # Its flash, as read on a unit on Arista 13.0.2-28.402 (2026-11-15): a
        # 128 MiB NAND cut into four 32 MiB partitions, two per slot (rootfs+opt,
        # rootfs2+opt2), with nothing spare; and seventeen partitions on the SPI
        # NOR adding up to 0x1F90000, the two 5 MiB kernels and a 256 KiB
        # environment among them.  The way in is radartool.
        "slot0": ("rootfs",),
        "slot1": ("rootfs2",),
        "slot_size": 0x2000000,
        "appsblenv": ("0:APPSBLENV", "0:appsblenv"),
        "need_nor": ("0:HLOS1", "0:hlos1"),
        "need_env": ("bootcmd", "ba0", "ba1", "ubootscript"),
        "nor_total": 0x1F90000,
        "root_order": ("radartool",),
        # U-Boot 2012.07 (a 2022 build): the image is fetched to 0x44000000 and
        # started with a bare bootm (its FIT's default configuration), fdt_high
        # left alone.  The boot loader unpacks the kernel to 0x42208000 itself, so
        # the unpacked kernel has to end below the file being read (and below
        # the 6 MiB the port reserves at 0x44000000): 20.8 MiB of the 31.4 there
        # are in the first build.  It has no 'sleep' command, so neither wait is
        # written (0); the fetch was seen 14 s after a restart without one (a
        # probe with marker files, 2026-11-15) and in the first RAM boot.
        "fit_cfg": None,
        "load_addr": "0x44000000",
        "fdt_high": None,
        "net_helper": True,
        "net_tries": 3,
        "net_settle": 0,
        "retry_sleep": 0,
        # The install is the W-118's shape: one fw_setenv, then OpenWrt's own
        # sysupgrade -n.  OpenWrt takes Arista's slot 0 (rootfs + opt, 64 MiB,
        # as one UBI); slot 1 (rootfs2 + opt2) and both kernels in NOR are never
        # written.  pri=1 makes Arista's own boot, the fallback, use slot 1.
        "install_env": (
            ("owrt_boot", "setenv mtdids nand0=nand0 && setenv mtdparts mtdparts=nand0:64m(ubi) && "
                          "ubi part ubi && ubi read 0x44000000 kernel && bootm 0x44000000"),
            ("arista_boot", "setenv mtdids; setenv mtdparts; if test ${pri} = 0; then "
                            "setenv bootargs ${ba0}; else setenv bootargs ${ba1}; fi; "
                            "run ubootscript"),
            ("bootcmd", "run owrt_boot; run arista_boot"),
            ("pri", "1"),
        ),
        "install_where": "NAND slot 0: Arista's rootfs and opt partitions, 64 MiB together",
        "install_keeps": "Slot 1 (rootfs2, opt2) and both kernels in NOR are not written; "
                         "Arista's copy there is the fallback (pri=1).",
        "pri_note": "Arista calls slot 0 its primary on this unit, so the system that runs now is "
                    "the one the install replaces; the copy in slot 1 is what is kept.",
        # Back to Arista: its kept copy in slot 1, by the boot command alone.
        "revert": "bootcmd",
        "revert_env": (("pri", "1"),),
        "revert_note": ("Arista's firmware starts again from its second slot, where the install",
                        "left it.  OpenWrt stays in the first slot, no longer started.",
                        "That copy keeps its own settings and may not take the login the other",
                        "one did; the AP's reset button brings back config / config."),
    },
    "c460": {
        "name": "C-460",
        "target": "qualcommbe/ipq95xx",
        "device": "arista_c-460",
        # One FIT configuration in the port's image (config@1), loaded where every
        # C-460 RAM boot has gone since 2026-11-10.  Keep what is read into RAM
        # there under ~96 MiB: U-Boot's stack is at 0x4A27FA80.
        "fit_cfg": "config@1",
        "load_addr": "0x44000000",
        # eMMC + NOR, not NAND: the slots are GPT partitions, and the install is
        # the sysupgrade image (kernel FIT to 0:HLOS, squashfs to rootfs) from
        # the RAM installer, after one boot-command write (c460/BY-HAND.md).
        "storage": "emmc",
        "image": "sysupgrade",
        "emmc_disk": "mmcblk0",
        # (GPT name, partition number, first LBA or None, blocks).  The number
        # and start matter: the boot command reads 0x10000 blocks from LBA 0x22
        # (0:HLOS), and the image's root is /dev/mmcblk0p3.  As read on the
        # unit (2026-11-12, firmware 18.2.0-32) and in its 11-10 backup.
        "emmc_parts": (("0:HLOS", 1, 34, 65536),
                       ("0:HLOS_1", 2, None, 65536),
                       ("rootfs", 3, None, 1572864),
                       ("rootfs_1", 4, None, 1572864),
                       ("rootfs_data", 5, None, 1572864),
                       ("rootfs_data_1", 6, None, 1572864)),
        # What the install replaces (and a restore writes back), and slot 1.
        "slot0_parts": ("0:HLOS", "rootfs", "rootfs_data"),
        "slot1_parts": ("0:HLOS_1", "rootfs_1", "rootfs_data_1"),
        # Arista's ext4 settings partition.  A copy read while it was mounted is
        # not written back (ext4_captured_mounted): the partition is left blank.
        "overlay_part": "rootfs_data",
        "appsblenv": ("0:APPSBLENV", "0:appsblenv"),
        "need_env": ("bootcmd", "ubootscript", "default_bootargs", "pri"),
        # 26 NOR partitions; RESERVED1/2 are 9.5 MiB each.
        "nor_total": 32 * 1024 * 1024,
        # Arista's own boot command, with its trailing ';' (fw_printenv on the unit).
        "stock_bootcmd": "run ubootscript;",
        # Stock's fdt_high (0x48500000) is what every C-460 RAM boot used.
        "fdt_high": None,
        # The 10 G RTL8261N links slowly and unpredictably after a reset:
        # measured 18-129 s (median 35) from the U-Boot banner to a successful
        # fetch across the manual boots (2026-11-12), and tftpboot aborts hard
        # ("No ethernet found") when no device is up yet -- netretry does not
        # help (it retries ARP only once a device exists).  So span a wide window
        # with many short attempts via the owrt_try helper (use_helper keeps each
        # fw_setenv short; a 12-attempt inline owrt_nb overran the AP shell line
        # and stalled fw_setenv, 2026-11-12).  netretry left at U-Boot's default.
        # NOT yet confirmed on a unit -- the netboot's env writes all land and
        # the fallback to stock is proven, but a successful RAM fetch is not.
        "netretry": None,
        "net_settle": 12,
        "net_tries": 12,
        "retry_sleep": 8,
        "net_helper": True,
        # From Arista's firmware the RAM installer is started by kexec instead
        # (boot_installer_kexec; bench-proven 2026-11-13, c460/kexec/README.md):
        # no U-Boot env write at all.  After a kexec from stock: our RPM glink
        # handshake resets the SoC (RPM off: no cpufreq, the CPU stays at stock's
        # lowest clock); BAM DMA (SPI) and SDCC ADMA (eMMC) time out (SPI-QUP in
        # FIFO, eMMC in PIO: 0:HLOS read back md5-exact, ~16 MB/s); CPU1-3 never
        # come back from stock's PSCI CPU_OFF (maxcpus=1); the QCN9074's MHI
        # power-on hangs (radio drivers kept out -- an installer needs none).
        # sdhci.debug_quirks REPLACES host->quirks: 0x12008008 is sdhci-msm's own
        # set, plus BROKEN_DMA|BROKEN_ADMA (0x60).
        "ram_boot": "kexec",
        # From an installed OpenWrt the installer is written into slot 0's kernel
        # partition and booted by U-Boot itself (boot_installer_slot0): the boot
        # command reads 0:HLOS and bootm's the FIT there, whatever it is.
        "openwrt_ram_boot": "slot0",
        "kexec": {
            "disable": ("/remoteproc",),
            "no_dma": ("qcom,spi-qup-v2.2.1",),
            "no_cqe": ("qcom,sdhci-msm-v5",),
            "cmdline": "console=ttyMSM0,115200n8 earlycon maxcpus=1 cnss2.probe_order=0x1324 "
                       "module_blacklist=mhi,ath11k_pci,ath12k,ath12k_wifi7 "
                       "sdhci.debug_quirks=0x12008068",
        },
        # The boot command the install writes, one fw_setenv -s.  If 0:HLOS holds
        # no bootable FIT, bootm returns and Arista boots from slot 1 (pri=1).
        "install_env": (("owrt_args", "console=ttyMSM0,115200n8 rootwait panic=10"),
                        ("owrt_load", "mmc read 0x44000000 0x22 0x10000"),
                        ("owrt_boot", "run owrt_load && bootm 0x44000000"),
                        ("arista_boot", "setenv bootargs ${default_bootargs}; run ubootscript"),
                        ("bootcmd", "setenv bootargs ${owrt_args}; run owrt_boot; run arista_boot"),
                        ("pri", "1")),
        "pri_note": "Arista runs a C-460 from slot 0 (pri=0): the install replaces "
                    "the system that runs now.",
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


# --- the ports this PC has to offer the AP -------------------------------------

def port_trouble(proto, port, err):
    """Why a port could not be opened, in words somebody can act on."""
    what = {69: "UDP 69 (TFTP), which the AP's boot loader fetches the installer from",
            8069: "TCP 8069, which the AP sends its firmware to"}.get(port, "%s %d" % (proto.upper(), port))
    if isinstance(err, PermissionError) and os.name != "nt":
        return ("this PC only lets an administrator open %s.\n"
                "Run the same command with sudo:\n"
                "  sudo %s %s" % (what, sys.executable, " ".join(sys.argv)))
    return ("this PC cannot open %s (%s).\n"
            "Another program is using it; close that program (a TFTP server, or\n"
            "another copy of this tool) and run again." % (what, err))


def check_ports(tftp, sink):
    """Before the AP is touched: can this PC serve what the AP will ask for?"""
    for wanted, proto, port in ((sink, "tcp", 8069), (tftp, "udp", 69)):
        if not wanted:
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if proto == "udp" else socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", port))
        except OSError as e:
            die(port_trouble(proto, port, e))
        finally:
            s.close()


# --- TFTP, with the option negotiation U-Boot asks for -----------------------

class Tftp:
    """Read-only TFTP that answers blksize/tsize, so 23 MB takes seconds rather
    than 46,000 512-byte round trips."""

    def __init__(self, files, port=69):
        self.files = files
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind(("0.0.0.0", port))
        except OSError as e:
            die(port_trouble("udp", port, e))
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
    m.note("the AP's shell had gone (Arista's upgrade helper times out after about "
           "a minute; %s took longer). Reconnecting." % why)
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


CONTINUE_PAT = r"Continue\?\s*\(?\[y\]/n\)?"
CONFIRM_PAT = r"Confirm\?\s*\(?\[y\]/n\)?"
SERVER_PAT = r"Server IP Address or Upgrade path"
SOURCE_ECHO_PAT = r"Upgrade from Server/Host \[([^\]]*)\]"
SHELL_PAT = r"\[config\]\$"


def _try_radartool(pty, host, payload):
    """A root shell out of the config CLI's radartool command.

    The CLI passes radartool's arguments to a shell unquoted, so
    'radartool params ;sh; radio 0' runs 'sh' after the tool -- a BusyBox ash
    as root.  Proven on a C-260 on 16.1.0-51.6 (2026-11-04), captured on a
    C-360 on the older firmware (2026-10-05), and documented for firmware 18
    by Arista-AP-firmware.  The ash banner is the proof the break-out worked,
    so nothing else is typed unless it appears.  Returns True with the pty
    sitting at the ash prompt, False with the config shell still on top.

    This is tried first on every model: it is a documented command rather than
    an eval in a download path.  Of the units seen so far only the C-230
    (8.9.0-63.1) has no radartool in its CLI -- it answers "Invalid command",
    which is caught here and falls straight through to the upgrade wizard.
    """
    say("  asking radartool for a shell (its arguments are evaluated).")
    mk = pty.mark()
    pty.send("radartool params ;sh; radio 0")
    # Either the ash banner, or the CLI refusing the command outright -- which is
    # what a build without radartool does, and what the C-230 (8.9.0) does.  The
    # refusal is instant, so there is no reason to sit out the whole timeout
    # before falling back to the upgrade wizard.
    got = pty.wait(r"(built-in shell \(ash\))|(Invalid command)|(not found)|"
                   r"(Invalid argument)|(Usage:)|(usage:)", 30, mark=mk)
    if not got:
        return False
    if "ash" not in got.group(0):
        note("no radartool here (%s); using Arista's upgrade wizard instead."
            % got.group(0).strip()[:40])
        return False
    mk2 = pty.mark()
    pty.send("id")
    if not pty.wait(r"uid=0", 10, mark=mk2):
        return False
    say("  root shell via radartool.")
    return True


def _upgrade_wizard(pty, host, payload):
    """Drive Arista's own 'upgrade' until it hands back a shell, or stop.

    The way in is the download command prep_upgrade.sh builds and evals.  The
    two firmware families ask for it in different places, and 16.x's wording
    looks like 11.x's while behaving the opposite way:

    11.x / 8.9 (proven on C-230 and C-260 hardware): Continue?, then "Enter
        upgrade build version string", "Enter upgrade build vendor version",
        then a genuine "Server IP Address or Upgrade path [x]:" prompt that
        reads a line -- the payload goes there -- then "Confirm?".

    16.x / 18.x (C-260, build 16.1.0-51.6, read on hardware 2026-11-04; and
        Arista-AP-firmware's notes for 18): 'upgrade' prints
        "Upgrade from Server/Host []" -- the source it is about to use, empty
        -- and goes straight on to "Continue? [y]/n:" without reading a host.
        Anything typed at that line is read as the answer to Continue?, which
        the AP takes as "not n, so continue": the upgrade starts from no
        source, the session dies, and no shell appears.  The source is given
        on the command line instead -- 'upgrade url <url>' -- and a bare
        "upgrade <url>" is rejected ("Invalid argument <url>"): the first
        positional argument is not the url.

    So on 16.x/18.x the source is passed as 'upgrade url <url>', and the echo
    line is what proves it was taken: an empty [] means it was not, and "y" is
    not sent on a source that is not ours.  Nothing is typed at the source
    line until staying silent has shown that nothing is reading it.
    """
    mk = pty.mark()
    pty.send("upgrade")
    first = pty.wait(r"(?:%s)|(?:%s)|(?:%s)" % (SERVER_PAT, SOURCE_ECHO_PAT, CONTINUE_PAT),
                     30, mark=mk)
    if not first:
        raise MethodFailed("'upgrade' offered neither Continue?, a server prompt, nor a source "
            "line: offline mode, or a build this tool has not been read on.\n%s"
            % pty.since(mk).strip()[-600:])

    if "Upgrade from Server/Host" not in first.group(0):
        # ---- 11.x: the wizard that is proven on hardware ----------------
        # 8.9 and 11.x ask for two version strings before the server; 8.7.1
        # (a W-118) asks for the server straight away.  Whatever comes is
        # answered as it comes, searching from one mark, so a server prompt
        # that is already on screen is not waited past.
        mky = pty.mark()
        if not re.search(SERVER_PAT, first.group(0)):
            pty.send("y")
        for _ in range(4):
            got = pty.wait(r"(Enter upgrade build version string)|"
                           r"(Enter upgrade build vendor version)|(%s)" % SERVER_PAT,
                           30, mark=mky)
            if not got:
                raise MethodFailed("the upgrade flow did not ask for a server address the usual way;\n"
                    "run 'upgrade' by hand once and send the full prompt sequence so the\n"
                    "tool can be matched to this build.\n%s" % pty.since(mk).strip()[-600:])
            if got.group(3):
                break
            mky = pty.mark()
            pty.send("1")
        else:
            raise MethodFailed("the upgrade flow kept asking for version strings:\n%s"
                               % pty.since(mk).strip()[-600:])
        pty.send(payload)
        # Confirm? follows the server address on 11.x; only answer it if it comes.
        if pty.wait(CONFIRM_PAT, 90):
            pty.send("y")
        return

    # ---- 16.x: is that source line a prompt, or an echo? ----------------
    # Nothing in the wording says, and guessing wrong answers Continue? with the
    # payload.  Stay silent first: if Continue? is already up, or turns up on
    # its own, nothing was reading the line.
    waiting = (not re.search(CONTINUE_PAT, pty.since(mk))
               and pty.wait(CONTINUE_PAT, 6) is None)
    if waiting:
        # A build that echoes the source and then waits for one anyway.
        pty.send(payload)
        if not pty.wait(CONTINUE_PAT, 30):
            raise MethodFailed("no Continue? after the upgrade source:\n%s"
                % pty.since(mk).strip()[-600:])
        pty.send("y")
        return

    # The source line was an echo and Continue? is up.  Decline it -- nothing
    # has started yet -- and give the source the way this CLI takes it, as
    # 'upgrade url <source>'.
    pty.send("n")
    if not pty.wait(SHELL_PAT, 20):
        raise MethodFailed("'n' at Continue? did not return to the config shell:\n%s"
            % pty.since(mk).strip()[-600:])
    line = "upgrade url %s" % payload
    say("  This build echoes the upgrade source instead of reading one, so it is")
    say("  passed as 'upgrade url <source>'; the echo line has to show this host")
    say("  before Continue? is answered.")
    mk2 = pty.mark()
    pty.send(line)
    got = pty.wait(r"(?:%s)|(?:Invalid argument)|(?:%s)"
                   % (CONTINUE_PAT, SHELL_PAT), 30, mark=mk2)
    text = pty.since(mk2)
    if not got:
        raise MethodFailed("'%s' was not answered within 30 s:\n%s" % (line, text.strip()[-600:]))
    if "Invalid argument" in text:
        raise MethodFailed("this build's 'upgrade' rejected that argument shape:\n%s\n"
            "Nothing was written.  Run 'upgrade ?' (or 'help upgrade') and send\n"
            "what it lists, so the tool can be matched to this build.\n%s"
            % (line, text.strip()[-600:]))
    if re.search(CONTINUE_PAT, text):
        mm = re.search(SOURCE_ECHO_PAT, text)
        src = (mm.group(1) if mm else "").strip()
        if host in src:
            pty.send("y")
            return
        raise MethodFailed("'%s' reached Continue? but the source is still '%s' -- it was not\n"
            "taken, so Continue? was left unanswered.  Nothing was written.\n%s"
            % (line, src, text.strip()[-600:]))
    raise MethodFailed("'%s' was answered unexpectedly:\n%s" % (line, text.strip()[-600:]))



class MethodFailed(Exception):
    """One way in did not produce a shell; the next one is tried."""


ROOT_METHOD_USED = None


def _cli_back(pty, timeout=20):
    """Get the config CLI back to its prompt after a method gave up."""
    try:
        pty.send("\x03")
        time.sleep(0.5)
        pty.send("")
    except Exception:
        return False
    return bool(pty.wait(SHELL_PAT, timeout))


def _try_upgrade(pty, host, payload):
    """Arista's upgrade wizard, then proof that it handed a shell back.

    The wizard's prompts are _upgrade_wizard's business; this adds the two
    things that decide whether it worked: the AP must not have started a real
    upgrade from a source it never read, and 'id' must answer as root.
    """
    mk0 = pty.mark()
    _upgrade_wizard(pty, host, payload)
    time.sleep(10)
    said = pty.since(mk0)
    if "Upgrade has started" in said:
        raise ShellNotReady("Arista started an upgrade from a source it never read: "
                            "the injected command did not run, so no root shell was "
                            "handed back.  Nothing was written to this AP.")
    out, rc = pty.run("id", 120)
    if "uid=0" not in out:
        raise ShellNotReady(out[-300:] or "(no output)")
    return True


# The ways in, in the order they are tried.  Each one either returns True with
# the pty sitting at a root shell, raises MethodFailed with the reason, or
# raises ShellNotReady when the AP is restarting itself (connect() retries).
ROOT_METHODS = (
    ("radartool", "radartool", _try_radartool),
    ("upgrade", "Arista's upgrade wizard", _try_upgrade),
)


def get_root_shell(pty, host, payload, model):
    """Try each way in, in this model's order, and say which one worked.

    radartool comes first everywhere: it is a documented CLI command whose
    arguments reach a shell, rather than an eval inside a download path.  A
    model can put the wizard first (MODELS[...]["root_order"]) where radartool
    is known to be absent, which is the C-230 at 8.9.0.
    """
    global ROOT_METHOD_USED
    order = MODELS.get(model, {}).get("root_order") or ("radartool", "upgrade")
    by_id = {m[0]: m for m in ROOT_METHODS}
    failures = []
    for mid in order:                      # the order is the model's, not the tuple's
        if mid not in by_id:
            continue
        _, label, attempt = by_id[mid]
        try:
            if attempt(pty, host, payload):
                ROOT_METHOD_USED = mid
                say("  root shell via %s." % label)
                return mid
        except MethodFailed as e:
            note("%s: %s" % (label, e))
            failures.append((label, str(e)))
        if not _cli_back(pty):
            failures.append((label, "the config shell did not come back afterwards"))
            break
    detail = "\n".join("  %s: %s" % (l, r) for l, r in failures) or "  (no method ran)"
    die("none of the ways in worked on this build (%s):\n%s"
        % (MODELS[model]["name"], detail))


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
    # An AP already on our OpenWrt has root, and no password until one is set.
    for o_user, o_pass in [("root", "")] + ([(user, password)] if user and password else []):
      try:
        con = m.ssh_open(host, o_user, o_pass, timeout=10)
        board = m.ssh_exec(con, "cat /tmp/sysinfo/board_name 2>/dev/null", 20)[0].strip()
        if port_board(board):
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
    # Firmware 16.x dropped the 'version' command ("Invalid command") and prints
    # the same facts under 'show device info' instead (seen on a C-260, build
    # 16.1.0-51.6, 2026-11-04).  Fall back to it whenever 'version' did not name
    # a model; 11.x still answers 'version', so this is additive.
    # Firmware 8.7 has neither: its command is 'get version' (a W-118 on
    # 8.7.1-03.2, 2026-11-15), with the same "Device Build: [X]" line as 11.x.
    for other in ("show device info", "get version"):
        if any(d["name"] in text for d in MODELS.values()):
            break
        mk2 = pty.mark()
        pty.send(other)
        time.sleep(3)
        text += "\n" + pty.since(mk2)
    model = None
    for key, d in MODELS.items():
        if d["name"] in text:
            model = key
    if not model:
        m.die("the AP does not say which model it is:\n%s" % text.strip()[-500:])
    if not field_install(model):
        # Before 'upgrade': whether it gives a W-model a root shell is unchecked.
        refuse_unsupported(model)
    fw = None
    for pat in (r"Device Build\s*:\s*\[([^\]]+)\]",   # 11.x: Device Build: [X]
                r"Device Build\s*:\s*([0-9]\S*)",      # 16.x: Device Build   : X
                r"Device Version\s*:\s*\[([^\]]+)\]",
                r"Device Version\s*:\s*([0-9]\S*)"):
        mm = re.search(pat, text)
        if mm:
            fw = mm.group(1).strip()
            break
    fw = fw or parse_device_info(text)["firmware"]
    m.say("  This AP is an Arista %s, firmware %s." % (MODELS[model]["name"], fw or "?"))
    # Arista's failure detector puts an AP into failsafe after >5 warm boots in
    # 30 min (a C-460 test series did, 2026-11-13): it resets the AP config to
    # factory defaults, and the CLI then refuses radartool's shell.  Older builds
    # answer "Invalid command", which is fine.
    mk = pty.mark()
    pty.send("show failsafe")
    pty.wait(r"Failsafe Mode:\s*\w+|Invalid|not found", 8, mark=mk)
    if "Failsafe Mode: On" in pty.since(mk):
        m.die("this AP is in Arista's FAILSAFE mode (a reboot loop was detected); there is\n"
              "no way in from that state.  In its CLI, 'failsafe recover' leaves it (the AP\n"
              "reboots).  Nothing was written.")
    payload = "http://%s/;sh</dev/tty>/dev/tty;" % host
    try:
        get_root_shell(pty, host, payload, model)
    except ShellNotReady:
        # Dropping the session is what makes Arista's helper give up and restart
        # the AP, which is the only way out of that state without hands.
        try:
            client.close()
        except Exception:
            pass
        raise
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
        if pty.chan.closed or not pty.chan.get_transport().is_active():
            return False
    except AttributeError:
        pass                                # a stand-in shell (the tests)
    except Exception:
        return False
    try:
        pty.send("sync; /sbin/reboot")
    except Exception:
        return False
    global _REBOOTED
    _REBOOTED = True
    if not wait:
        # Hold the session until the AP has taken the command: the shell dies
        # with the session, and a restart typed as it closed was lost (C-360,
        # 2026-11-15: after a stopped run the AP stayed up, in whatever state
        # the stopped step left it).  The AP dropping the connection is the sign.
        end = time.time() + 20
        while time.time() < end:
            time.sleep(1)
            try:
                if pty.chan.closed or not pty.chan.get_transport().is_active():
                    break
            except Exception:
                break
        return True
    time.sleep(25)
    end = time.time() + 300
    while time.time() < end:
        try:
            socket.create_connection((host, 22), 3).close()
            m.say("  back at %s." % host)
            return True
        except Exception:
            time.sleep(5)
    m.say("  it has not answered yet; it will come back on its own.")
    return True


# Whatever way this program ends -- including a refused check in the middle --
# the stock AP must not be left with sensord stopped.
_STOCK = None
_REBOOTED = False
_STOCK_HOST = None


def _cleanup_stock():
    """If we stop early (a refused check, Ctrl-C), still leave the AP in a
    defined state rather than with the upgrade service half-run."""
    global _STOCK
    if _STOCK is not None and not _REBOOTED:
        # Send the restart and go: waiting for the AP here would leave the
        # screen silent for minutes after the run has already said it stopped.
        # die() and the Ctrl-C ending call this themselves, while the session
        # is still open: left to interpreter exit it ran after the SSH library
        # had closed every session, sent nothing, and still said the AP was
        # restarting (C-360, 2026-11-15).  Say only what happened.
        shell, _STOCK = _STOCK, None
        try:
            sent = pty_clean(shell, _STOCK_HOST, wait=False)
        except Exception:
            sent = False
        if sent:
            tell("   The AP restarts on its own firmware now, as Arista's does when this")
            tell("   kind of session ends.  Give it two or three minutes.")
        else:
            tell("   Restart the AP before running the tool again: unplug it for ten")
            tell("   seconds, then give it two or three minutes.")


atexit.register(_cleanup_stock)


# --- 2. verify ---------------------------------------------------------------

NAND_ERASE = 0x20000          # 128 KiB PEBs; NOR partitions are 64 KiB


def is_ubi_volume(part):
    """The pseudo-MTD devices a running system creates for UBI volumes.

    They are not partitions: each is a view into a slot that is saved whole.
    By name where known, and by erase size for the rest: a volume's is the
    slot's block less two pages (0x3e000, 0x1f000), never a power of two as a
    real partition's is.  A C-360 lists four of them (ubi_rootfs, rootfs_data,
    rootfs_sig, ubi_logging), and its ubi_logging cannot even be opened while
    Arista has it mounted, which stopped a run at the backup (2026-11-15)."""
    if part["name"] in ("ubi_rootfs", "rootfs_data", "rootfs_sig", "ubi_logging"):
        return True
    erase = part.get("erase") or 0
    return part.get("kind") != "emmc" and erase > 0 and erase & (erase - 1) != 0


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


def is_emmc(model):
    return MODELS[model].get("storage") == "emmc"


# One line per eMMC partition: device, GPT name, first LBA, blocks.  Busybox on
# Arista's firmware and on OpenWrt both have this; no sgdisk/blkid needed.
EMMC_LIST = ("for p in /sys/class/block/%s/%sp*; do echo \"${p##*/} "
             "$(sed -n 's/^PARTNAME=//p' $p/uevent) $(cat $p/start) $(cat $p/size)\"; done")


def emmc_table(text):
    rows = []
    for line in text.splitlines():
        mm = re.match(r"(mmcblk\d+p(\d+))\s+(\S+)\s+(\d+)\s+(\d+)\s*$", line.strip())
        if mm:
            rows.append({"dev": mm.group(1), "num": int(mm.group(2)), "name": mm.group(3),
                         "start": int(mm.group(4)), "blocks": int(mm.group(5)),
                         "size": int(mm.group(5)) * 512, "erase": 512, "kind": "emmc"})
    return rows


def emmc_problems(rows, d):
    """What is wrong with an eMMC layout for this model, as a list of reasons."""
    problems = []
    for name, num, start, blocks in d["emmc_parts"]:
        r = find_part(rows, (name,))
        if not r:
            problems.append("no eMMC partition %s" % name)
            continue
        if r["num"] != num:
            problems.append("%s is partition %d, expected %d" % (name, r["num"], num))
        if start is not None and r["start"] != start:
            problems.append("%s starts at LBA %d, expected %d" % (name, r["start"], start))
        if r["blocks"] != blocks:
            problems.append("%s is %d blocks, expected %d" % (name, r["blocks"], blocks))
    return problems


def verify_stock_emmc(pty, card):
    """verify_stock for an eMMC model: NOR from /proc/mtd, the slots from GPT."""
    d = MODELS[card["model"]]
    mtd = pty.must("cat /proc/mtd", 30, "/proc/mtd")
    card["mtd"] = mtd_table(mtd)
    card["mtd_raw"] = mtd
    if not card["mtd"]:
        m.die("could not read the NOR layout")
    gpt_raw = pty.must(EMMC_LIST % (d["emmc_disk"], d["emmc_disk"]), 30, "the eMMC partitions")
    rows = emmc_table(gpt_raw)
    card["emmc"] = rows
    card["emmc_raw"] = "\n".join("%s %s %d %d" % (r["dev"], r["name"], r["start"], r["blocks"])
                                 for r in rows)
    problems = emmc_problems(rows, d)
    env = find_part(card["mtd"], d["appsblenv"])
    if not env:
        problems.append("no U-Boot environment partition (%s)" % "/".join(d["appsblenv"]))
    if d.get("need_nor") and not find_part(card["mtd"], d["need_nor"]):
        problems.append("no %s (the fallback kernel)" % d["need_nor"][0])
    # What the running firmware has mounted for writing changes under a read
    # (the W-318: its settings on mmcblk0p4 and CONFIG as jffs2); saved as read.
    mounts, _ = pty.run("cat /proc/mounts", 20)
    card["live"] = set()
    for dev, opts in re.findall(r"^/dev/(\S+)\s+\S+\s+\S+\s+(\S+)", mounts, re.M):
        if "rw" in opts.split(","):
            card["live"].add(re.sub(r"^mtdblock", "mtd", dev))
    nor_bytes = sum(r["size"] for r in card["mtd"])
    if nor_bytes != d["nor_total"]:
        problems.append("NOR partitions add up to %d bytes, expected %d"
                        % (nor_bytes, d["nor_total"]))
    env_text = pty.must("fw_printenv 2>&1", 30, "fw_printenv")
    card["env"] = env_text
    for v in d["need_env"]:
        if not re.search(r"^%s=" % re.escape(v), env_text, re.M):
            problems.append("the U-Boot environment has no %s" % v)
    bc = re.search(r"^bootcmd=(.*)$", env_text, re.M)
    if bc and bc.group(1).strip() != d["stock_bootcmd"]:
        # Not fatal: a unit someone already changed.  Arista's own is restored by
        # the netboot before anything else, and the backup keeps what was there.
        m.say("  NOTE: bootcmd is %r, not Arista's %r" % (bc.group(1).strip()[:60],
                                                         d["stock_bootcmd"]))
    pri = re.search(r"^pri=(\S+)", env_text, re.M)
    card["pri"] = pri.group(1) if pri else "?"
    m.say("  Arista's primary slot (pri): %s" % card["pri"])
    if card["pri"] == "0":
        m.say("  NOTE: %s" % d["pri_note"])
        m.say("        Slot 1 (%s) stays as the fallback, and the backup taken next is"
              % ", ".join(d["slot1_parts"]))
        m.say("        what puts slot 0 back if you want it.")
    root = pty.must("awk '$2 == \"/\" {print $3; exit}' /proc/mounts", 20, "root fs").strip()
    m.say("  root fs    %s" % root)
    if root not in ("overlay", "rootfs", "tmpfs"):
        problems.append("root filesystem is %r" % root)
    free_kb = pty.must("df -k /tmp | tail -1 | awk '{print $4}'", 20, "df").strip()
    m.say("  /tmp       %s KiB free" % free_kb)
    m.say("  flash      NOR %d partitions (%d MiB), eMMC %d partitions, env = %s"
          % (len(card["mtd"]), nor_bytes // 1048576, len(rows), env["dev"] if env else "?"))
    if problems:
        m.say("")
        m.die("this AP's flash is not laid out the way this tool expects for its model,\n"
              "so it stopped rather than guess.  Nothing was written.\n"
              + "\n".join("- " + p for p in problems))
    card["env_dev"] = env
    card["slot0"] = [find_part(rows, (n,)) for n in d["slot0_parts"]]
    card["slot1"] = [find_part(rows, (n,)) for n in d["slot1_parts"]]
    m.say("  layout ok  (slot 0 %s, slot 1 %s, env %s, %s)"
          % ("+".join(r["dev"] for r in card["slot0"]),
             "+".join(r["dev"] for r in card["slot1"]), env["dev"], card["version"]))


def backup_rows(card, include_slot1=True):
    """What download_firmware saves, as (row, skipped-reason) pairs.

    NAND models: every real MTD partition (a UBI volume a running system exposes
    is inside its raw slot already).  eMMC models: every NOR partition, then the
    eMMC slots -- not the whole disk: ramdump (3.7 GiB on a C-460), panicdump and
    reserved1 hold no firmware and the install does not touch them."""
    out = []
    if is_emmc(card["model"]):
        for r in card["mtd"]:
            out.append((r, None))
        for r in card["slot0"]:
            out.append((r, None))
        for r in card["slot1"]:
            out.append((r, None if include_slot1 else "--fast"))
        return out
    for r in card["mtd"]:
        if is_ubi_volume(r):
            continue
        out.append((r, "--fast" if (r is card["slot1"] and not include_slot1) else None))
    return out


def live_partitions(pty, card):
    """The flash partitions the running firmware has open for writing.

    Arista keeps its settings in a UBIFS volume inside the slot it booted from
    (and its logs in another partition), and writes to them all the time.  Two
    reads of such a partition need not agree, so it cannot be checked the way
    the others are: read once on the AP for a hash, read again to send.  Found
    on a C-360, 2026-11-15, the first time this ran on one: its slot 1 changed
    between the two reads and the run stopped.  Three sources, any of which is
    enough: the kernel's list of attached UBI devices, the slot named on the
    kernel command line, and the slot `pri` names."""
    live = set()
    out, _ = pty.run("cat /sys/class/ubi/ubi*/mtd_num 2>/dev/null", 20)
    for n in re.findall(r"^\s*(\d+)\s*$", out, re.M):
        live.add("mtd%s" % n)
    out, _ = pty.run("cat /proc/cmdline", 20)
    for name in re.findall(r"ubi\.mtd=([^\s,]+)", out):
        r = next((x for x in card["mtd"] if x["name"] == name or x["dev"] == name
                  or x["dev"] == "mtd%s" % name), None)
        if r:
            live.add(r["dev"])
    slot = {"0": card.get("slot0"), "1": card.get("slot1")}.get(str(card.get("pri")))
    if slot:
        live.add(slot["dev"])
    # And a NOR partition mounted for writing (Arista keeps CONFIG as jffs2).
    out, _ = pty.run("cat /proc/mounts", 20)
    for dev, opts in re.findall(r"^/dev/mtdblock(\d+)\s+\S+\s+\S+\s+(\S+)", out, re.M):
        if "rw" in opts.split(","):
            live.add("mtd%s" % dev)
    return live


def verify_stock(pty, card):
    """Everything that must be true before we change anything."""
    m.say("")
    m.say("Verifying the AP.")
    if is_emmc(card["model"]):
        hard = pty.must("uname -srm", 30, "uname")
        card["version"] = card.get("firmware") or "?"
        m.say("  firmware   %s" % card["version"])
        m.say("  kernel     %s" % hard.strip())
        card["mac"] = pty.must("cat /sys/class/net/br0/address 2>/dev/null", 30, "MAC").strip().lower()
        m.say("  MAC        %s" % card["mac"])
        return verify_stock_emmc(pty, card)
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
    if d.get("keeps_arista"):
        m.say("  Neither Arista slot is replaced on this model: OpenWrt goes into flash")
        m.say("  that Arista's firmware does not use.")
    elif card["pri"] == "0":
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
        m.die("this AP's flash is not laid out the way this tool expects for its model,\n"
              "so it stopped rather than guess.  Nothing was written.\n"
              + "\n".join("- " + p for p in problems))
    card["slot0"], card["slot1"], card["env_dev"] = s0, s1, env
    card["live"] = live_partitions(pty, card)
    if card["live"]:
        m.say("  in use     %s (the running firmware writes to it; saved as read)"
              % ", ".join(sorted(card["live"])))
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


def download_firmware(run, pty, card, outdir, include_slot1=True, pc_ip=None, port=8069):
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
    for r, skipped in backup_rows(card, include_slot1):
        # Every real partition, in either model's order (backup_rows decides).
        if skipped:
            m.say("  %-9s %-14s skipped (%s)" % (r["dev"], r["name"], skipped))
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", r["name"])
        fname = "%s_%s.bin.gz" % (r["dev"], safe)
        gz = os.path.join(outdir, fname)
        # A partition the running firmware writes to is read once and kept as
        # read: gzip's own checksum covers the trip, and a second read of it on
        # the AP would not agree anyway (live_partitions).
        live = r["dev"] in card.get("live", ())
        t0 = time.time()
        cmd = ("gzip -1 -c /dev/%s | curl -s -S --data-binary @- "
               "http://%s:%d/put/%s" % (r["dev"], pc_ip, port, fname))
        for attempt in (1, 2):
            ap_md5 = None
            if not live:
                out = pty.must("md5sum /dev/%s | cut -d' ' -f1" % r["dev"], 900, "md5 on the AP")
                ap_md5 = out.strip().splitlines()[-1].strip() if out.strip() else ""
                if not re.fullmatch(r"[0-9a-f]{32}", ap_md5):
                    m.die("the AP could not read its own %s (%s):\n%s"
                          % (r["dev"], r["name"], out.strip()[-300:] or "(no answer)"),
                          hint="Restart the AP and run the tool again.  If the same part fails again,\n"
                               "stop there and keep the log.\nThe AP is unchanged.")
            recv.got.pop(fname, None)
            with Heartbeat("%s %s (%.0f MiB)" % (r["dev"], r["name"], r["size"] / 1048576.0), 12):
                sent, _ = pty.run(cmd, 1800)
            if fname not in recv.got:
                if re.search(r"can't open|No such|busy|Input/output", sent):
                    m.die("the AP could not read its own %s (%s):\n%s"
                          % (r["dev"], r["name"], sent.strip()[-300:]),
                          hint="Restart the AP and run the tool again.  If the same part fails again,\n"
                               "stop there and keep the log.\nThe AP is unchanged.")
                m.die("%s (%s) did not arrive; the AP could not reach this PC at %s "
                      "(firewall on TCP %d?)\n%s" % (r["dev"], r["name"], pc_ip, port, sent.strip()[-300:]),
                      hint="Allow TCP %d in from the AP through this PC's firewall, and check there is\n"
                           "free disk space here.  If %s is not this PC's address on the AP's network\n"
                           "(a VPN can do that), give the right one with --pc-ip.\nThe AP is unchanged."
                           % (port, pc_ip))
            got = recv.got[fname]
            raw = gz + ".raw"
            sha, md5 = gunzip_sha_md5(gz, raw)
            os.remove(raw)
            if live or md5 == ap_md5:
                break
            if attempt == 1:
                m.say("  %-9s %-14s read twice and the two differ (AP %s, here %s); once more."
                      % (r["dev"], r["name"], ap_md5, md5))
                continue
            m.die("the copy of %s (%s) does not match what the AP reads from its own flash, "
                  "twice over (AP %s, here %s)." % (r["dev"], r["name"], ap_md5, md5),
                  hint="Run the tool again.  If the same part fails again, this AP's flash may be\n"
                       "failing: stop there and keep the log.\nThe AP is unchanged.")
        m.say("  %-9s %-14s %6.1f MiB -> %5.1f MiB gz, %s (%.1f s)"
              % (r["dev"], r["name"], r["size"] / 1048576.0, got["size"] / 1048576.0,
                 "saved as read (in use)" if live else "verified", time.time() - t0))
        entry = {"dev": r["dev"], "name": r["name"], "size": r["size"],
                 "erase": r["erase"], "file": fname, "sha256": sha, "md5": md5}
        if live:
            entry["live"] = True
        if r.get("kind") == "emmc":
            entry.update({"kind": "emmc", "num": r["num"], "start": r["start"],
                          "blocks": r["blocks"]})
        manifest["partitions"].append(entry)
        total += got["size"]
    for name, text in (("u-boot-env.txt", card.get("env")), ("proc-mtd.txt", card.get("mtd_raw")),
                       ("emmc-partitions.txt", card.get("emmc_raw"))):
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

def fit_config_for(path, preferred):
    """The FIT configuration to boot, taken from the image itself.

    A model's configuration name depends on which port built the image, not on
    the model: the C-360 is config@1 in 25.12's tree and config@hk09 in main's,
    and the C-260 is config@1 in 25.12 and config@hk01 in main.  Guessing wrong
    makes U-Boot answer "config not found", so the image is asked."""
    try:
        head = open(path, "rb").read()   # the FDT can sit anywhere in the FIT
    except Exception:
        return preferred, []
    # A name may hold '-', '_' and '.': the W-318's and C-200's is config@cp01-c3.
    # Without the hyphen it was read as "config@cp01", taken for the image's
    # only configuration, and U-Boot was told to boot one that is not there:
    # four fetches, four refused bootm, and no console to say why (2026-11-15).
    names = sorted({m.decode() for m in re.findall(rb"config@[A-Za-z0-9._-]+", head)})
    if preferred in names:
        return preferred, names
    if len(names) == 1:
        return names[0], names
    return preferred, names


def ap_address(a_host, pc_ip):
    return a_host.rsplit(".", 1)[0] + ".230"


def netboot_env(pc_ip, ap_ip, itb_name, cfg, load="0x60000000",
                stock_bootcmd="run ubootscript", fdt_high="0x4A3E0000",
                net_settle=5, net_tries=2, retry_sleep=3, netretry="no",
                use_helper=False):
    """The two variables the netboot needs, as (name, value) pairs.

    They are kept apart because the injected shell only lives about a minute and
    a single several-hundred-character fw_setenv is slow to paste and easy to
    get wrong.

    The order inside owrt_nb is the important part: **put Arista's own boot back
    and save it BEFORE the fetch**, not after it.  A run that stops here, or a
    fetch that is slow, or U-Boot sitting in its network retry, all used to leave
    the AP depending on this PC: every reboot re-entered the netboot, and a power
    cycle did the same, because bootcmd still pointed at it (a C-260 spent a
    night in exactly that state, 2026-10-26).  Now the first thing the netboot
    does is make the NEXT boot Arista's own, so any reset or power cycle recovers
    by itself.  It costs one environment write per attempt, counted like any
    other, and it happens before any RAM-only variable is touched, so the saved
    environment is Arista's own apart from bootcmd.

    net_settle is how long to wait for the link before the first tftpboot, then
    net_tries attempts in all with retry_sleep between.

    netretry controls U-Boot's own retry on a failed fetch.  "no" (the C-230/
    C-260) makes each tftpboot try once and return, so the RAM-only owrt_nb
    always reaches its reset and falls back to stock -- a C-260 once spent a
    night re-entering the netboot because bootcmd still pointed here (2026-10-26;
    now bootcmd is restored first, but "no" is kept for those).  netretry=None
    leaves it UNSET, U-Boot's default, so tftpboot re-probes the PHY and retries
    while the link comes up: the C-460's 10 G RTL8261N is slow to negotiate and
    a single-shot tftpboot hit "No ethernet found" every time, where the stock
    env (no netretry) and every manual boot fetched first try (2026-11-12).  A
    hung fetch is still recovered by a power-cycle, because owrt_clear saved
    Arista's own bootcmd before the fetch.

    stock_bootcmd is the model's own boot command (the C-460's carries a
    trailing ';'), and fdt_high=None leaves the model's own value alone (the
    C-460's 0x48500000 is what its RAM boots use)."""
    # A boot command that holds double quotes and ${...} of its own (the W-318's)
    # goes back inside single quotes, so U-Boot stores it as written instead of
    # expanding it when owrt_clear runs.
    clear = ("setenv bootcmd '%s'; saveenv" if '"' in stock_bootcmd
             else 'setenv bootcmd "%s"; saveenv') % stock_bootcmd
    # A model with one FIT configuration and an old U-Boot boots it with a bare
    # bootm (cfg None: the W-118).
    boot = "bootm %s#%s" % (load, cfg) if cfg else "bootm %s" % load
    head = ("%ssetenv ipaddr %s; setenv gatewayip %s; setenv serverip %s; %s"
            "setenv bootargs console=ttyMSM0,115200n8 rootwait panic=0; "
            % (("setenv netretry %s; " % netretry) if netretry else "",
               ap_ip, ap_ip.rsplit(".", 1)[0] + ".1", pc_ip,
               ("setenv fdt_high %s; " % fdt_high) if fdt_high else ""))
    out = [("owrt_clear", clear)]
    if use_helper:
        # Each fetch attempt is one short helper var; owrt_nb just runs it N times
        # after the settle, then resets.  This keeps every fw_setenv well under
        # the AP shell's line limit even with many attempts, which a slow,
        # variable link (the C-460's 10 G PHY: 18-129 s banner->fetch across the
        # manual boots, 2026-11-12) needs to span.  "tftpboot && bootm" boots on
        # the first success; on a miss it sleeps retry_sleep and the next run
        # tries again.
        # A boot loader without 'sleep' (the C-130's) gets neither wait: 0 for
        # net_settle and retry_sleep leaves the command out instead of writing
        # one it would answer with "Unknown command".  Its tftpboot waits for
        # the link by itself (first fetch 14 s after the restart, 2026-11-15).
        out.append(("owrt_try", "tftpboot %s %s && %s" % (load, itb_name, boot)
                    + ("; sleep %d" % retry_sleep if retry_sleep else "")))
        fetch = (head + ("sleep %d; " % net_settle if net_settle else "")
                 + "run owrt_try; " * net_tries + "reset")
        out.append(("owrt_nb", fetch))
    else:
        # The proven inline form (C-230/C-260): a settle, net_tries tftpboot calls,
        # then reset.  A several-hundred-character fw_setenv gets truncated by the
        # AP's tty (2026-10-25), which bounds how many attempts fit this way.
        attempts = "sleep %d; tftpboot %s %s && %s; " % (net_settle, load, itb_name, boot)
        for _ in range(max(0, net_tries - 1)):
            attempts += "sleep %d; tftpboot %s %s && %s; " % (retry_sleep, load, itb_name, boot)
        out.append(("owrt_nb", head + attempts + "reset"))
    out.append(("bootcmd", "run owrt_clear; run owrt_nb"))
    return out


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


# --- kexec RAM boot (for a U-Boot that cannot netboot unattended) -------------
#
# The C-460's U-Boot brings its 10G PHY up too slowly and unpredictably for an
# unattended TFTP (18-129 s, "No ethernet found"; 2026-11-12).  Its RAM installer
# is started from Arista's running firmware instead: stock's 5.4.213 kernel has
# no kexec but loads unsigned modules, so a kexec module built for that kernel
# (c460/kexec/build.sh; github.com/fabianishere/kexec-mod + fixes) loads our
# initramfs and jumps to it.  No U-Boot environment or flash write; a power-cycle
# returns the unit to stock.  Bench-proven 2026-11-13 (c460/kexec/README.md):
# what stock leaves behind needs a few workarounds, all in the model's "kexec"
# entry -- the DTB edits below and the kernel command line.

class FdtNode:
    """One node of a flattened device tree, properties kept in order."""

    def __init__(self, name):
        self.name = name
        self.props = []          # [(name, bytes)]
        self.kids = []           # [FdtNode]

    def get(self, name):
        for n, v in self.props:
            if n == name:
                return v
        return None

    def set(self, name, value):
        for i, (n, _) in enumerate(self.props):
            if n == name:
                self.props[i] = (name, value)
                return
        self.props.append((name, value))

    def delete(self, name):
        self.props = [(n, v) for n, v in self.props if n != name]

    def child(self, name):
        for k in self.kids:
            if k.name == name:
                return k
        return None


def fdt_parse(blob):
    """(root, reserve-map bytes, boot cpu, totalsize) from a DTB or FIT."""
    (magic, total, off_struct, off_strings, off_rsv, _ver, _lc, boot_cpu,
     _ssz, _stsz) = struct.unpack(">10I", blob[:40])
    if magic != 0xD00DFEED:
        raise ValueError("not a device tree blob")
    rsv, p = b"", off_rsv
    while True:
        addr, size = struct.unpack(">QQ", blob[p:p + 16])
        rsv += blob[p:p + 16]
        p += 16
        if addr == 0 and size == 0:
            break

    def cstr(off):
        return blob[off:blob.index(b"\0", off)].decode()

    root, stack, p = None, [], off_struct
    while True:
        tok = struct.unpack(">I", blob[p:p + 4])[0]
        p += 4
        if tok == 1:                                   # FDT_BEGIN_NODE
            end = blob.index(b"\0", p)
            node = FdtNode(blob[p:end].decode())
            p = (end + 4) & ~3
            if stack:
                stack[-1].kids.append(node)
            else:
                root = node
            stack.append(node)
        elif tok == 2:                                 # FDT_END_NODE
            stack.pop()
        elif tok == 3:                                 # FDT_PROP
            ln, nameoff = struct.unpack(">II", blob[p:p + 8])
            p += 8
            stack[-1].props.append((cstr(off_strings + nameoff), blob[p:p + ln]))
            p = (p + ln + 3) & ~3
        elif tok == 4:                                 # FDT_NOP
            continue
        elif tok == 9:                                 # FDT_END
            return root, rsv, boot_cpu, total
        else:
            raise ValueError("bad FDT token %d at %#x" % (tok, p - 4))


def fdt_build(root, rsv=b"\0" * 16, boot_cpu=0):
    """A version-17 DTB from a tree (header, reserve map, structure, strings)."""
    strings, offs, st = bytearray(), {}, bytearray()

    def stroff(s):
        if s not in offs:
            offs[s] = len(strings)
            strings.extend(s.encode() + b"\0")
        return offs[s]

    def pad():
        while len(st) % 4:
            st.append(0)

    def emit(node):
        st.extend(struct.pack(">I", 1))
        st.extend(node.name.encode() + b"\0")
        pad()
        for n, v in node.props:
            st.extend(struct.pack(">III", 3, len(v), stroff(n)))
            st.extend(v)
            pad()
        for k in node.kids:
            emit(k)
        st.extend(struct.pack(">I", 2))

    emit(root)
    st.extend(struct.pack(">I", 9))
    off_struct = 40 + len(rsv)                       # the reserve map sits at 40 (8-aligned)
    off_strings = off_struct + len(st)
    total = off_strings + len(strings)
    hdr = struct.pack(">10I", 0xD00DFEED, total, off_struct, off_strings, 40, 17, 16,
                      boot_cpu, len(strings), len(st))
    return hdr + rsv + bytes(st) + bytes(strings)


def fdt_find(root, path):
    node = root
    for part in [x for x in path.split("/") if x]:
        node = node.child(part) if node else None
    return node


def fdt_walk(node, path=""):
    yield (path or "/"), node
    for k in node.kids:
        for item in fdt_walk(k, path + "/" + k.name):
            yield item


def _fdt_u32(node, name, default):
    v = node.get(name)
    return struct.unpack(">I", v[:4])[0] if v else default


def _fdt_cells(value, n):
    return struct.pack(">I", value) if n == 1 else struct.pack(">II", value >> 32, value & 0xFFFFFFFF)


def _fdt_regs(node, acells, scells):
    """[(start, end)] from a reg property."""
    v, out, step = node.get("reg") or b"", [], 4 * (acells + scells)
    for i in range(0, len(v) - step + 1, step):
        words = struct.unpack(">%dI" % (acells + scells), v[i:i + step])
        a = 0
        for w in words[:acells]:
            a = (a << 32) | w
        s = 0
        for w in words[acells:]:
            s = (s << 32) | w
        out.append((a, a + s))
    return out


def fit_kernel_and_dtb(itb_path, cfg):
    """The (decompressed arm64 Image, DTB) a FIT configuration boots."""
    blob = open(itb_path, "rb").read()
    root, _, _, total = fdt_parse(blob)
    images, confs = root.child("images"), root.child("configurations")
    conf = confs.child(cfg)
    if conf is None:
        conf = confs.child((confs.get("default") or b"").rstrip(b"\0").decode())
    if conf is None:
        raise ValueError("no configuration %s in %s" % (cfg, itb_path))

    def data(node):
        v = node.get("data")
        if v is not None:
            return v
        size = _fdt_u32(node, "data-size", 0)
        if node.get("data-position") is not None:
            off = _fdt_u32(node, "data-position", 0)
        else:                                          # external data after the FDT
            off = ((total + 3) & ~3) + _fdt_u32(node, "data-offset", 0)
        return blob[off:off + size]

    k = images.child(conf.get("kernel").split(b"\0")[0].decode())
    comp = (k.get("compression") or b"none\0").rstrip(b"\0").decode()
    raw = data(k)
    if comp == "gzip":
        img = gzip.decompress(raw)
    elif comp == "lzma":
        import lzma
        img = lzma.decompress(raw, format=lzma.FORMAT_ALONE)
    elif comp == "none":
        img = raw
    else:
        raise ValueError("kernel compression %s is not handled" % comp)
    if img[0x38:0x3C] != b"ARM\x64":
        raise ValueError("the FIT kernel is not an arm64 Image")
    dtb = data(images.child(conf.get("fdt").split(b"\0")[0].decode()))
    return img, dtb


def iomem_ram(text):
    """Top-level "System RAM" ranges of a /proc/iomem, as [(start, end)]."""
    out = []
    for line in text.splitlines():
        if line[:1] in (" ", "\t") or "System RAM" not in line:
            continue
        a, b = line.split(":")[0].strip().split("-")
        out.append((int(a, 16), int(b, 16) + 1))
    return sorted(out)


def _subtract(spans, cuts):
    out = []
    for a, b in spans:
        pieces = [(a, b)]
        for c, d in cuts:
            nxt = []
            for x, y in pieces:
                if d <= x or c >= y:
                    nxt.append((x, y))
                    continue
                if c > x:
                    nxt.append((x, c))
                if d < y:
                    nxt.append((d, y))
            pieces = nxt
        out.extend(pieces)
    return out


def ram_holes(base, size, ram):
    """What stock's kernel kept out of its RAM inside [base, base+size): its
    carve-outs (radio firmware memory, TZ, ramoops ...), page-aligned."""
    return [(a & ~0xFFF, (b + 0xFFF) & ~0xFFF)
            for a, b in _subtract([(base, base + size)], ram) if b > a]


def kexec_dtb(dtb, mem_base, mem_size, holes, kx):
    """Our DTB made bootable after a kexec from stock.  Returns (dtb, added).

    U-Boot fills the memory node in; here it comes from stock's live DT.  Every
    region stock's kernel kept out of its RAM and ours does not reserve gets a
    no-map node, so a device stock left running cannot write into our memory.
    Then the model's workarounds: nodes to disable (the RPM: our glink handshake
    resets the SoC after stock), DMA off SPI-QUP and command queueing off the
    eMMC (peripheral DMA times out after the kexec; FIFO/PIO works)."""
    root, rsv, cpu, _ = fdt_parse(dtb)
    ac, sc = _fdt_u32(root, "#address-cells", 2), _fdt_u32(root, "#size-cells", 2)
    mem = next((k for k in root.kids if k.name.split("@")[0] == "memory"), None)
    if mem is None:
        raise ValueError("the DTB has no memory node")
    mem.set("reg", _fdt_cells(mem_base, ac) + _fdt_cells(mem_size, sc))

    resv = root.child("reserved-memory")
    rac, rsc = _fdt_u32(resv, "#address-cells", ac), _fdt_u32(resv, "#size-cells", sc)
    have = []
    for k in resv.kids:
        have.extend(_fdt_regs(k, rac, rsc))
    added = _subtract(holes, have)
    for a, b in reversed(added):
        n = FdtNode("kexec-stock@%x" % a)
        n.set("reg", _fdt_cells(a, rac) + _fdt_cells(b - a, rsc))
        n.set("no-map", b"")
        resv.kids.insert(0, n)

    for path in kx.get("disable", ()):
        node = fdt_find(root, path)
        if node is not None:
            node.set("status", b"disabled\0")
    for _, node in fdt_walk(root):
        status = (node.get("status") or b"okay\0").rstrip(b"\0")
        if status not in (b"okay", b"ok"):
            continue
        compat = {c.decode() for c in (node.get("compatible") or b"").split(b"\0") if c}
        if compat & set(kx.get("no_dma", ())):
            node.delete("dmas")
            node.delete("dma-names")
        if compat & set(kx.get("no_cqe", ())):
            node.delete("supports-cqe")
    return fdt_build(root, rsv, cpu), added


def _hexdump_bytes(text):
    """Bytes back out of a busybox 'hexdump -C'."""
    out = bytearray()
    for line in text.splitlines():
        body = line.split("|")[0].split()
        if not body or not re.fullmatch(r"[0-9a-f]{8}", body[0]):
            continue
        out.extend(int(t, 16) for t in body[1:] if re.fullmatch(r"[0-9a-f]{2}", t))
    return bytes(out)


def _ko_vermagic(path):
    m_ = re.search(rb"vermagic=([\x20-\x7e]+)", open(path, "rb").read())
    return m_.group(1).decode() if m_ else ""


class FileServer:
    """GET-only HTTP for a fixed set of files: the AP pulls with wget (Arista's
    sshd has no exec channel or sftp to push through)."""

    def __init__(self, files, ports=range(8069, 8080)):
        import http.server
        import socketserver
        outer = self
        self.files = dict(files)

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                name = self.path.lstrip("/")
                if name not in outer.files:
                    self.send_error(404)
                    return
                path = outer.files[name]
                self.send_response(200)
                self.send_header("Content-Length", str(os.path.getsize(path)))
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                with open(path, "rb") as f:
                    for b in iter(lambda: f.read(1 << 20), b""):
                        self.wfile.write(b)

        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = False

        last = None
        for port in ports:
            try:
                self.srv = S(("0.0.0.0", port), H)
                self.port = port
                break
            except OSError as e:
                last = e
        else:
            raise last
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:
            pass


KEXEC_FILES = ("kexec_mod_arm64.ko", "kexec_mod.ko", "kexec")
RAW_KEXEC = "https://raw.githubusercontent.com/%s/main/install/kexec/%%s/" % REPO


def ensure_kexec(run, model):
    """The three helper files a kexec model's RAM installer starts with: the
    ones next to this file, else fetched from the project's repository and
    checked against its SHA256SUMS.  Called before the AP is touched."""
    kdir = getattr(run, "kexec_dir", None) or os.path.join(HERE, "kexec", model)
    if all(os.path.isfile(os.path.join(kdir, n)) for n in KEXEC_FILES):
        run.kexec_dir = kdir
        return kdir
    name = MODELS[model]["name"]
    base = RAW_KEXEC % model
    try:
        with http_get(base + "SHA256SUMS") as r:
            sums = {}
            for line in r.read().decode().splitlines():
                parts = line.split()
                if len(parts) == 2:
                    sums[parts[1].lstrip("*")] = parts[0]
    except Exception as e:                            # noqa: BLE001
        die("the %s starts its installer with three helper files, and they are\n"
            "neither on this PC nor in the project's repository yet (%s).\n"
            "Nothing was changed on the AP." % (name, e))
    os.makedirs(kdir, exist_ok=True)
    for n in KEXEC_FILES:
        if n not in sums:
            die("the repository's helper files for the %s do not list %s" % (name, n))
        dest = os.path.join(kdir, n)
        status("downloading the %s installer helper %s" % (name, n))
        with http_get(base + n) as r, open(dest + ".part", "wb") as f:
            for b in iter(lambda: r.read(1 << 20), b""):
                f.write(b)
        if sha256_file(dest + ".part") != sums[n]:
            os.remove(dest + ".part")
            die("%s from the repository does not match its published checksum" % n)
        os.replace(dest + ".part", dest)
    run.kexec_dir = kdir
    return kdir


def boot_installer_kexec(run, card, pc_ip, itb):
    """Start the RAM installer straight from Arista's firmware with kexec."""
    global _REBOOTED
    mdl = MODELS[card["model"]]
    kx = mdl["kexec"]
    kdir = getattr(run, "kexec_dir", None) or os.path.join(HERE, "kexec", card["model"])
    bins = {n: os.path.join(kdir, n) for n in KEXEC_FILES}
    missing = [p for p in bins.values() if not os.path.isfile(p)]
    if missing:
        m.die("the %s boots its RAM installer by kexec, and these are missing:\n  %s\n"
              "(built by c460/kexec/build.sh; give their folder with --kexec-dir)"
              % (mdl["name"], "\n  ".join(missing)))
    vermagic = _ko_vermagic(bins["kexec_mod.ko"])

    pty = ensure_shell(run, card["host"], run.user, run.password, "the RAM installer boot")
    m.say("")
    m.say("RAM installer by kexec: Arista's kernel loads our installer and jumps to it.")
    rel = pty.must("uname -r", 20, "reading the kernel version").strip()
    if not vermagic or vermagic.split()[0] != rel:
        m.die("this AP's firmware runs Linux %s; the kexec module is built for %s.\n"
              "Nothing was written." % (rel, vermagic.split()[0] if vermagic else "?"))
    reg = _hexdump_bytes(pty.must("hexdump -C /sys/firmware/devicetree/base/memory/reg", 20,
                                  "reading the memory size"))
    if len(reg) < 16:
        m.die("could not read the memory node (%d bytes); nothing was written" % len(reg))
    mem_base, mem_size = struct.unpack(">QQ", reg[:16])
    holes = ram_holes(mem_base, mem_size, iomem_ram(pty.must("cat /proc/iomem", 30, "reading /proc/iomem")))

    cfg, _ = fit_config_for(itb, mdl["fit_cfg"])
    img, dtb = fit_kernel_and_dtb(itb, cfg)
    kdtb, added = kexec_dtb(dtb, mem_base, mem_size, holes, kx)
    work = getattr(run, "work", None) or os.path.abspath(".")
    files = dict(bins)
    files["Image"] = os.path.join(work, "kexec-Image")
    files["kexec.dtb"] = os.path.join(work, "kexec.dtb")
    open(files["Image"], "wb").write(img)
    open(files["kexec.dtb"], "wb").write(kdtb)
    sums = {n: md5_file(p) for n, p in files.items()}
    m.say("  stock kernel %s, RAM %#x + %d MiB, image %s (%d MiB)"
          % (rel, mem_base, mem_size >> 20, os.path.basename(itb), len(img) >> 20))
    for a, b in added:
        m.say("  reserved for stock's carve-out: %#x-%#x" % (a, b - 1))

    m.say("")
    m.say("The eMMC and the U-Boot environment are NOT written.  Arista's kernel loads")
    m.say("a kexec module and jumps to the installer; if anything goes wrong the AP")
    m.say("resets to its own firmware (a power-cycle does the same).")
    if not run.yes:
        if ask("Type 'yes' to go ahead: ", "--yes").strip().lower() not in ("y", "yes"):
            m.die("not confirmed; nothing was written")

    srv = FileServer(files)
    try:
        m.say("  serving %d files at http://%s:%d/ ; the AP fetches them." % (len(files), pc_ip, srv.port))
        pty.must("mkdir -p /tmp/kx", 20)
        for n in files:
            pty.must("wget -q -O /tmp/kx/%s http://%s:%d/%s" % (n, pc_ip, srv.port, n), 300,
                     "fetching %s" % n)
        got = {}
        for line in pty.must("cd /tmp/kx && md5sum %s" % " ".join(files), 120).splitlines():
            parts = line.split()
            if len(parts) == 2:
                got[parts[1]] = parts[0]
        bad = [n for n in files if got.get(n) != sums[n]]
        if bad:
            m.die("these arrived damaged on the AP: %s; nothing was written" % ", ".join(bad))
        m.say("  all %d files on the AP, checksums match." % len(files))
        pty.must("chmod +x /tmp/kx/kexec", 20)
        # kexec_mod_arm64 exports machine_kexec*, which kexec_mod uses: arm64 first.
        pty.must("grep -q '^kexec_mod_arm64 ' /proc/modules || "
                 "insmod /tmp/kx/kexec_mod_arm64.ko detect_el2=0", 30, "loading kexec_mod_arm64")
        pty.must("grep -q '^kexec_mod ' /proc/modules || insmod /tmp/kx/kexec_mod.ko", 30,
                 "loading kexec_mod")
        pty.must("[ -c /dev/kexec ] || mknod /dev/kexec c "
                 "$(awk '$2==\"kexec\"{print $1}' /proc/devices) 0", 20)
        pty.must("cd /tmp/kx && ./kexec -i --kexec-syscall -l Image --dtb=kexec.dtb "
                 "--command-line='%s'" % kx["cmdline"], 180, "loading the installer")
        if pty.must("cat /sys/kernel/kexec_loaded", 20).strip() != "1":
            m.die("kexec -l returned but nothing is loaded; nothing was written")
        m.say("  installer loaded into the kexec slot.")
        # Hand-over as U-Boot leaves it: all of stock's messages on the console,
        # its procd watchdog stretched (runtime only; our kernel takes it over at
        # qcom_wdt probe), and the cores at the lowest clock -- our kernel sets the
        # CPU rail to its minimum before cpufreq runs.
        pty.run("echo 8 > /proc/sys/kernel/printk", 20)
        pty.run("ubus call system watchdog '{\"timeout\": 120, \"frequency\": 1}'", 30)
        out, _ = pty.run("for c in /sys/devices/system/cpu/cpu[0-9]*/cpufreq; do "
                         "echo powersave > $c/scaling_governor; done; "
                         "cd /sys/devices/system/cpu/cpu0/cpufreq && cat scaling_cur_freq scaling_min_freq", 30)
        f = out.split()
        if len(f) >= 2 and f[-2] != f[-1]:
            m.die("stock's CPU did not settle at its lowest clock (%s vs %s); not jumping"
                  % (f[-2], f[-1]))
        m.say("  jumping.")
        _REBOOTED = True
        pty.send("cd /tmp/kx && nohup sh -c 'sleep 3; sync; exec ./kexec --kexec-syscall -e' "
                 "</dev/null >/tmp/kx/e.log 2>&1 &")
        time.sleep(2)
        try:
            pty.chan.get_transport().close()
        except Exception:
            pass
    finally:
        # The AP has the files once the jump starts; nothing more is served.
        time.sleep(3)
        srv.close()
    time.sleep(20)


RAM_ROOTS = ("tmpfs", "rootfs", "ramfs")


def boot_installer_slot0(run, card, itb):
    """From an installed OpenWrt on an eMMC model: boot the RAM installer from
    slot 0's kernel partition.

    The boot command this tool installs reads that partition and bootm's
    whatever FIT it holds, so the initramfs written there is the next boot --
    by U-Boot itself, with no network boot (the C-460's U-Boot cannot netboot
    unattended) and no environment write.  The installed OpenWrt stops booting
    from that moment; the restore that follows rewrites the whole slot.  If the
    FIT there does not boot, U-Boot falls through to Arista in slot 1."""
    global _REBOOTED
    con = run.shell
    d = MODELS[card["model"]]
    root = m.ssh_exec(con, "awk '$2 == \"/\" {print $3; exit}' /proc/mounts", 20)[0].strip()
    if root in RAM_ROOTS:
        m.say("")
        m.say("  This AP is already running a RAM installer; using it as it is.")
        return
    m.say("")
    m.say("RAM installer from slot 0: written to %s, booted by U-Boot." % d["slot0_parts"][0])
    rows = emmc_table(m.ssh_must(con, EMMC_LIST % (d["emmc_disk"], d["emmc_disk"]), 30))
    problems = emmc_problems(rows, d)
    if problems:
        m.die("this AP's eMMC is not the expected layout (%s); nothing was written"
              % "; ".join(problems))
    kern = find_part(rows, (d["slot0_parts"][0],))
    env = m.ssh_exec(con, "fw_printenv 2>&1", 30)[0]
    for name, want in d["install_env"]:
        if name not in ("bootcmd", "owrt_load", "owrt_boot"):
            continue
        got = re.search(r"^%s=(.*)$" % re.escape(name), env, re.M)
        if not got or got.group(1).strip() != want:
            m.die("this AP does not boot with the command this tool installs (%s is %r, "
                  "not %r), so U-Boot would not boot an installer put in slot 0.\n"
                  "Nothing was written." % (name, got.group(1).strip()[:70] if got else None, want))
    size = os.path.getsize(itb)
    if size > kern["blocks"] * 512:
        m.die("the installer (%d bytes) does not fit %s (%d bytes); nothing was written"
              % (size, kern["name"], kern["blocks"] * 512))
    m.say("  This replaces the installed OpenWrt's kernel: from here on the AP boots the")
    m.say("  RAM installer, until its own firmware is put back (next) or OpenWrt is")
    m.say("  installed again.  Arista's slot 1 and the boot command are not touched.")
    if not run.yes:
        if ask("Type 'yes' to go ahead: ", "--yes").strip().lower() not in ("y", "yes"):
            m.die("not confirmed; nothing was written")
    sha = sha256_file(itb)
    _send_file(con, itb, "/tmp/installer.itb", "the RAM installer (%.1f MiB)" % (size / 1048576.0))
    out = m.ssh_must(con, "sha256sum /tmp/installer.itb | cut -d' ' -f1", 120)
    if out.strip() != sha:
        m.die("the installer arrived damaged on the AP; nothing was written")
    mib = (size + 1048575) // 1048576
    cmd = ("dd if=/tmp/installer.itb of=/dev/%s bs=1M conv=fsync 2>/dev/null; sync; "
           "dd if=/dev/%s bs=1M count=%d 2>/dev/null | head -c %d | sha256sum | cut -d' ' -f1"
           % (kern["dev"], kern["dev"], mib, size))
    out, _ = m.ssh_exec(con, cmd, 600)
    got = [l.strip() for l in out.splitlines() if re.fullmatch(r"[0-9a-f]{64}", l.strip())]
    run.flash_writes.append("eMMC %s (RAM installer)" % kern["name"])
    if not got or got[-1] != sha:
        m.die("%s does not read back as the installer (got %s).  The AP may not boot "
              "OpenWrt now; U-Boot falls back to Arista in slot 1.  Run this again."
              % (kern["name"], got[-1] if got else "?"))
    m.say("  written to /dev/%s and read back identical.  Rebooting into it." % kern["dev"])
    _REBOOTED = True
    try:
        chan = con.get_transport().open_session()
        chan.exec_command("sync; reboot")
    except Exception:
        pass
    time.sleep(30)


def boot_installer(run, card, pc_ip, ap_ip, itb, kind):
    """Two short environment writes, then reboot into the RAM installer."""
    global _REBOOTED
    if kind == "stock" and MODELS[card["model"]].get("ram_boot") == "kexec":
        return boot_installer_kexec(run, card, pc_ip, itb)
    if kind == "openwrt" and MODELS[card["model"]].get("openwrt_ram_boot") == "slot0":
        return boot_installer_slot0(run, card, itb)
    itb_name = os.path.basename(itb)
    short = "%s.itb" % card["model"]      # short on purpose: it is pasted into a line
    mdl = MODELS[card["model"]]
    if mdl.get("fit_cfg") is None:
        cfg = None                      # booted with a bare bootm (the W-118)
    else:
        cfg, seen = fit_config_for(itb, mdl["fit_cfg"])
        if cfg != mdl["fit_cfg"]:
            m.note("this image boots %s (the %s default is %s); using what the image has"
                   % (cfg, mdl["name"], mdl["fit_cfg"]))
        if not seen:
            m.note("could not read a configuration name out of the image; using %s" % cfg)
    writes = netboot_env(pc_ip, ap_ip, short, cfg, mdl.get("load_addr", "0x60000000"),
                         mdl.get("stock_bootcmd", "run ubootscript"),
                         mdl.get("fdt_high", "0x4A3E0000"),
                         mdl.get("net_settle", 5), mdl.get("net_tries", 2),
                         mdl.get("retry_sleep", 3), mdl.get("netretry", "no"),
                         mdl.get("net_helper", False))
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
        if ask("Type 'yes' to go ahead: ", "--yes").strip().lower() not in ("y", "yes"):
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


def ramboot_itb(a, model):
    """The initramfs to RAM-boot: a local file with --image, else the release's.

    A RAM boot needs only the initramfs, not the sysupgrade/factory image, so
    --image takes any local .itb (a fresh port build, say) without a release."""
    if a.image:
        itb = os.path.abspath(a.image)
        if not os.path.exists(itb):
            m.die("--image %s does not exist" % a.image)
        if not itb.lower().endswith(".itb"):
            m.die("--image must be an initramfs .itb (what U-Boot RAM-boots)")
        m.say("  RAM image: %s" % itb)
        return itb, "local"
    itb, _img, _sums, tag = release_files(a, model)
    return itb, tag


def do_ramboot(run, a, card, kind, pc_ip, ap_ip):
    """Boot OpenWrt into RAM over the network and stop -- no eMMC write.

    This is boot_installer's netboot with nothing after it: U-Boot TFTPs the
    initramfs and runs it from RAM.  The only persistent change is the U-Boot
    environment the netboot writes, and it is written to put Arista's own boot
    back before the fetch, so a power-cycle returns the unit to stock on its own.
    The eMMC is never touched."""
    if kind == "stock":
        m.say("")
        m.say("Verifying the AP (read-only).")
        verify_stock(run.shell, card)       # sets card["mac"]; refuses an odd unit
    itb, tag = ramboot_itb(a, card["model"])
    kexec = kind == "stock" and MODELS[card["model"]].get("ram_boot") == "kexec"
    m.say("")
    if kexec:
        m.say("RAM boot: Arista's running kernel loads this image and jumps to it (kexec).")
        m.say("  release/image: %s" % tag)
        m.say("  The eMMC and the U-Boot environment are NOT touched; a power-cycle (or")
        m.say("  'reboot' from OpenWrt) brings the unit back to its own firmware.")
    else:
        m.say("RAM boot: U-Boot will fetch this image over TFTP and run it from RAM.")
        m.say("  release/image: %s" % tag)
        m.say("  The eMMC is NOT touched.  The only writes are the U-Boot environment")
        m.say("  variables the netboot needs, and they restore Arista's own boot before")
        m.say("  the fetch -- so a power-cycle brings the unit back to its own firmware.")
    boot_installer(run, card, pc_ip, ap_ip, itb, kind)
    con, ip, board, root = wait_installer(a, (card.get("host") or ap_ip).rsplit(".", 1)[0],
                                          mac=card.get("mac"))
    m.say("")
    if con is None:
        if kexec:
            m.die("the RAM image never came up.  A failed jump resets the AP to its own "
                  "firmware (nothing was written); the console shows how far it got.")
        m.die("the RAM image never came up.  U-Boot falls back to this AP's own boot "
              "if the fetch fails, so the unit is on its own firmware; check the "
              "network (UDP 69 free, the AP on a segment with DHCP) and run again.")
    info, _ = m.ssh_exec(con, "grep DISTRIB_DESCRIPTION /etc/openwrt_release 2>/dev/null; "
                              "awk '$2==\"/\"{print $3}' /proc/mounts; uptime", 30)
    m.tell("")
    m.tell("OpenWrt is running from RAM at %s (%s)." % (ip, board))
    for line in info.splitlines():
        if line.strip():
            m.say("  " + line.strip())
    m.tell("")
    m.tell("  Root filesystem is in RAM: nothing is installed.  SSH in as root (no")
    m.tell("  password).  Power-cycle the AP to return it to Arista's firmware; a")
    m.tell("  plain reboot does the same, because the saved bootcmd is Arista's own.")
    if kexec:
        m.tell("  (Started by kexec: one CPU, no Wi-Fi, eMMC in PIO -- an installer, not")
        m.tell("  a full system; a normal boot of the installed image has everything.)")
    m.say("")
    m.say("Environment writes this run: %d  (%s)"
          % (len(run.env_writes), ", ".join(run.env_writes) or "none"))
    m.say("Flash writes this run:      %d  (%s)"
          % (len(run.flash_writes), ", ".join(run.flash_writes) or "none (eMMC untouched)"))


# --- 5a. install OpenWrt -----------------------------------------------------

def _shq(s):
    """Single-quote a string for /bin/sh."""
    return "'" + s.replace("'", "'\\''") + "'"


def _send_file(con, local, remote, what):
    """Put a local file on the AP: sftp if the AP has a server, else a plain
    SSH channel (Arista's RAM installer and OpenWrt both lack sftp-server)."""
    with Heartbeat("sending %s" % what, 10):
        try:
            sftp = con.open_sftp()
            sftp.put(local, remote)
            sftp.close()
            return
        except Exception as e:                       # noqa: BLE001
            m.note("no sftp-server on the AP (%s); sending over a plain SSH channel" % e)
        chan = con.get_transport().open_session()
        chan.exec_command("cat > %s" % remote)
        with open(local, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                chan.sendall(b)
        chan.shutdown_write()
        chan.recv_exit_status()
        chan.close()


def do_install_emmc(run, card, ip, image, _sums):
    """Install for an eMMC / sysupgrade model (the C-460).

    The RAM installer is our own OpenWrt initramfs, so the write is sysupgrade's
    own eMMC path: the kernel FIT to 0:HLOS and the squashfs to rootfs, Arista's
    slot 1 (0:HLOS_1 / rootfs_1) left as the fallback.  One U-Boot environment
    write first sets the boot command: OpenWrt from slot 0, falling back to
    Arista at pri=1 if 0:HLOS holds no bootable image.  See c460/BY-HAND.md."""
    con = run.installer
    d = MODELS[card["model"]]
    m.say("")
    m.say("  Checking the installer has what it needs.")
    out, _ = m.ssh_exec(con, "command -v sysupgrade fw_setenv fw_printenv >/dev/null && echo ok", 60)
    if "ok" not in out:
        m.die("the RAM installer lacks sysupgrade or fw_setenv:\n%s" % out[-300:])
    # The env tools must be pointed at the right MTD, or a write would miss.
    out, _ = m.ssh_exec(con, "fw_printenv bootcmd 2>&1 | head -1", 30)
    if "bootcmd=" not in out:
        m.die("fw_printenv does not read the U-Boot environment on the installer "
              "(no fw_env.config?):\n%s" % out[-300:])

    stage = "/tmp/" + card["model"]
    m.ssh_must(con, "rm -rf %s; mkdir -p %s" % (stage, stage), 60)
    remote = stage + "/sysupgrade.bin"
    m.say("  Sending the sysupgrade image (%.1f MiB) to %s over SSH."
          % (os.path.getsize(image) / 1048576.0, remote))
    _send_file(con, image, remote, "the image")
    out, _ = m.ssh_exec(con, "sha256sum %s | cut -d' ' -f1" % remote, 600)
    if out.strip() != sha256_file(image):
        m.die("the image arrived wrong on the AP (sha256 mismatch)")
    m.say("    on the AP and sha256-verified.")

    m.say("")
    m.say("  Letting sysupgrade check the image for this board (writes nothing):")
    out, _ = m.ssh_exec(con, "sysupgrade -T %s 2>&1" % remote, 300)
    for line in out.splitlines():
        m.say("    " + line.rstrip())
    if re.search(r"not (compatible|supported)|Image check 'fwtool_check_image' failed|Invalid image",
                 out, re.I):
        m.die("sysupgrade rejects this image for this board; nothing was written.\n"
              + "\n".join(out.strip().splitlines()[-6:]))

    m.say("")
    m.say("  Checks passed.  About to write, on this AP:")
    m.say("    1 U-Boot environment write: the boot command (%s)"
          % ", ".join(n for n, _ in d["install_env"]))
    where = d.get("install_where") or "eMMC slot 0: the kernel to 0:HLOS and the root filesystem to rootfs"
    m.say("    %s" % where)
    m.say("    %s" % (d.get("install_keeps") or
                      "Arista's slot 1 (%s) is left as the fallback (pri=1)."
                      % ", ".join(d.get("slot1_parts", ()))))
    if not run.yes_install and not run.yes:
        mac = board_mac(con) or card.get("mac") or "?"
        tail = mac.replace(":", "")[-4:]
        if ask("  Type the last four characters of the MAC on the label (%s): " % mac,
               "--yes-install").strip().lower() != tail:
            m.die("no match; nothing was written")

    # 1) the boot command, as one fw_setenv script so the redundant env sector
    #    updates once.  Values are stored literally; U-Boot expands ${...} at boot.
    env_lines = "".join("%s %s\n" % (n, v) for n, v in d["install_env"])
    names = ", ".join(n for n, _ in d["install_env"])
    m.say("  Writing the boot command (%s)." % names)
    out, rc = m.ssh_exec(con, "printf %s | fw_setenv -s -" % _shq(env_lines), 120)
    m.LOG.write("\nENV WRITE (install_env): rc=%s %s\n" % (rc, out))
    if rc != 0:
        m.die("writing the boot command failed (exit %s): %s" % (rc, out[-300:]))
    chk, _ = m.ssh_exec(con, "fw_printenv bootcmd pri owrt_boot 2>&1", 30)
    for line in chk.splitlines():
        if line.startswith(("bootcmd=", "pri=")):
            m.say("    %s" % line.strip()[:70])
    if "run owrt_boot" not in chk or "run arista_boot" not in chk:
        m.die("the boot command did not read back as written:\n%s" % chk[-300:])
    run.env_writes.append("boot command (%s)" % names)

    # 2) the image: sysupgrade writes slot 0 and reboots into OpenWrt.  It detaches
    #    and kills processes before rebooting, so the SSH channel drops mid-way --
    #    expected, not a failure; verify_openwrt waits for the AP to return.
    m.say("  Writing the image; sysupgrade reboots into OpenWrt.")
    run.flash_writes.append("%s (sysupgrade -n)"
                            % ("eMMC 0:HLOS + rootfs" if is_emmc(card["model"]) else "NAND ubi"))
    with Heartbeat("sysupgrade", 20):
        out, _ = m.ssh_exec(con, "sysupgrade -n %s 2>&1" % remote, 600)
    for line in out.splitlines():
        if line.strip():
            m.say("    " + line.rstrip())
    m.say("  sysupgrade is writing the image and will reboot the AP into OpenWrt.")


def do_install_script(run, card, ip, image, _sums):
    """Install where the image carries its own installer that takes the
    sysupgrade file (the W-318's arista-w318-install).

    The script is the port's: it checks the unit (layout, calibration, that
    slot 1 holds an Arista system with what OpenWrt's first boot copies from
    it), writes the boot command with one fw_setenv, blanks NOR 0:HLOS so a
    stopped install boots Arista, writes the system and the kernel, and reads
    both back.  Here it is run twice: --dry-run, which writes nothing, and then
    for real.  Its last line says how it ended."""
    con = run.installer
    d = MODELS[card["model"]]
    tool = d["installer"]
    out, _ = m.ssh_exec(con, "command -v %s fw_printenv >/dev/null && echo ok" % tool, 60)
    if "ok" not in out:
        m.die("the RAM installer lacks %s; this build cannot install the %s:\n%s"
              % (tool, d["name"], out[-300:]))
    stage = "/tmp/" + card["model"]
    m.ssh_must(con, "rm -rf %s; mkdir -p %s" % (stage, stage), 60)
    remote = stage + "/sysupgrade.bin"
    m.say("  Sending the image (%.1f MiB) to %s over SSH."
          % (os.path.getsize(image) / 1048576.0, remote))
    _send_file(con, image, remote, "the image")
    out, _ = m.ssh_exec(con, "sha256sum %s | cut -d' ' -f1" % remote, 600)
    if out.strip() != sha256_file(image):
        m.die("the image arrived wrong on the AP (sha256 mismatch)")
    m.say("    on the AP and sha256-verified.")

    def result(text):
        mm = re.findall(r"INSTALL-RESULT:\s*(.*)", text)
        return mm[-1].strip() if mm else ""

    m.say("")
    m.say("  Running the installer's own checks (this writes nothing):")
    with Heartbeat("installer dry run", 20):
        out, _ = m.ssh_exec(con, "%s --dry-run %s 2>&1" % (tool, remote), 900)
    for line in out.splitlines():
        m.say("    " + line.rstrip())
    if not result(out).startswith("OK"):
        m.die("the installer's checks did not pass, so nothing was written:\n%s"
              % "\n".join(out.strip().splitlines()[-8:]))
    m.say("")
    m.say("  Checks passed.  The installer now writes, on this AP: the boot command")
    m.say("  (one U-Boot environment write), the system to eMMC slot 0 and the kernel")
    m.say("  to NOR 0:HLOS.  Slot 1 and the calibration are not written.")
    before, _ = m.ssh_exec(con, "fw_printenv bootcmd 2>&1", 30)
    with Heartbeat("Installing", 20):
        out, _ = m.ssh_exec(con, "%s --yes %s 2>&1" % (tool, remote), 1800)
    for line in out.splitlines():
        m.say("    " + line.rstrip())
    after, _ = m.ssh_exec(con, "fw_printenv bootcmd 2>&1", 30)
    if after.strip() != before.strip():
        run.env_writes.append("boot command (installer)")
    if not result(out).startswith("OK"):
        if "erase" in out.lower() or after.strip() != before.strip():
            run.flash_writes.append("slot 0, partly (installer stopped)")
        m.die("the installer stopped:\n%s\nIf it had started writing, slot 0 is incomplete and "
              "the AP boots Arista's\nfirmware from slot 1 by itself; run the tool again."
              % "\n".join(out.strip().splitlines()[-8:]))
    run.flash_writes.append("eMMC rootfs + NOR 0:HLOS (installer)")
    m.say("  Installed and read back.  Restarting the AP into OpenWrt.")
    m.ssh_exec(con, "sync; reboot", 20)


def do_install_ubiformat(run, card, ip, ubi, sums):
    """Install for a NAND model whose image has no installer script of its own
    (the C-200): the steps of its by-hand install, with the checks made first.

    Order matters.  The boot command is written before slot 0 is touched: it
    tries OpenWrt and falls back to Arista in slot 1, so from that write on a
    stopped install boots Arista by itself.  Then slot 0 is formatted with the
    factory image, and its volumes are read back against the release's sums."""
    con = run.installer
    d = MODELS[card["model"]]
    stage = "/tmp/" + card["model"]

    def need(cond, why):
        if not cond:
            m.die("the installer's checks did not pass, so nothing was written:\n%s" % why)

    m.say("")
    m.say("  Checking this unit from the RAM installer (this writes nothing):")
    out, _ = m.ssh_exec(con, "command -v ubiformat ubidetach ubiattach fw_setenv fw_printenv "
                             ">/dev/null && echo ok", 60)
    need("ok" in out, "the RAM installer lacks ubiformat or the U-Boot environment tools")
    rows = mtd_table(m.ssh_must(con, "cat /proc/mtd", 30))
    s0, s1 = find_part(rows, d["slot0"]), find_part(rows, d["slot1"])
    need(s0 and s0["size"] == d["slot_size"], "no slot 0 of the expected size (%s)" % (d["slot0"],))
    need(s1, "no slot 1 (%s)" % (d["slot1"],))
    k1 = find_part(rows, d["need_nor"])
    need(k1, "no %s (Arista's kernel for slot 1)" % d["need_nor"][0])
    # The fallback has to be there before slot 0 goes: slot 1 must hold a UBI,
    # and its kernel partition an image (Arista's is an ELF, a FIT elsewhere).
    out, _ = m.ssh_exec(con, "hexdump -n 4 -e '4/1 \"%%02x\"' /dev/%s; echo; "
                             "hexdump -n 4 -e '4/1 \"%%02x\"' /dev/%s; echo" % (s1["dev"], k1["dev"]), 60)
    magic = out.split()
    need(len(magic) == 2 and magic[0] == "55424923",
         "slot 1 (%s) holds no UBI, so there would be no Arista to fall back to" % s1["dev"])
    need(magic[1] in ("7f454c46", "d00dfeed"),
         "%s holds no kernel (%s), so there would be no Arista to fall back to" % (k1["name"], magic[1]))
    env, _ = m.ssh_exec(con, "fw_printenv 2>&1", 60)
    need(re.search(r"^ubootscript=", env, re.M) and re.search(r"^ba1=", env, re.M),
         "the U-Boot environment cannot be read from the RAM installer:\n%s" % env.strip()[-200:])
    m.say("    slot 0 %s, slot 1 %s (a UBI), %s (a kernel), environment readable"
          % (s0["dev"], s1["dev"], k1["name"]))

    m.ssh_must(con, "rm -rf %s; mkdir -p %s" % (stage, stage), 60)
    remote = stage + "/factory.ubi"
    m.say("  Sending the image (%.1f MiB) to %s over SSH."
          % (os.path.getsize(ubi) / 1048576.0, remote))
    _send_file(con, ubi, remote, "the image")
    want = sha256_file(ubi)
    out, _ = m.ssh_exec(con, "sha256sum %s | cut -d' ' -f1; hexdump -n 4 -e '4/1 \"%%02x\"' %s"
                        % (remote, remote), 600)
    got = out.split()
    need(len(got) == 2 and got[0] == want, "the image arrived wrong on the AP (sha256 mismatch)")
    need(got[1] == "55424923", "the image is not a UBI image")
    listed = {}
    if sums and os.path.exists(sums):
        for line in open(sums, encoding="utf-8", errors="replace"):
            f = line.split()
            if len(f) >= 2 and f[1] == "factory.ubi":
                need(f[0] == want, "the image does not match its release's install sums")
            elif len(f) >= 3 and f[1].startswith("volume:"):
                listed[f[1][7:]] = (f[0], int(f[2]))
    m.say("    on the AP and sha256-verified.")

    m.say("")
    m.say("  Checks passed.  About to write, on this AP: the boot command (one U-Boot")
    m.say("  environment write), then NAND slot 0 (%s).  Slot 1 and the calibration" % s0["dev"])
    m.say("  are not written.")
    env_lines = "".join("%s %s\n" % (n, v) for n, v in d["install_env"])
    names = ", ".join(n for n, _ in d["install_env"])
    out, rc = m.ssh_exec(con, "printf %s | fw_setenv -s -" % _shq(env_lines), 120)
    m.LOG.write("\nENV WRITE (install_env): rc=%s %s\n" % (rc, out))
    if rc != 0:
        m.die("writing the boot command failed (exit %s): %s\nNothing else was written." % (rc, out[-300:]))
    chk, _ = m.ssh_exec(con, "fw_printenv bootcmd pri 2>&1", 30)
    if "run owrt_boot" not in chk or "run arista_boot" not in chk or not re.search(r"^pri=1$", chk, re.M):
        m.die("the boot command did not read back as written:\n%s\nSlot 0 was not touched." % chk[-300:])
    run.env_writes.append("boot command (%s)" % names)
    m.say("    boot command written and read back (OpenWrt first, Arista in slot 1 as the fallback).")

    m.say("  Formatting slot 0 with the image (about a minute):")
    run.flash_writes.append("NAND slot 0 (ubiformat)")
    with Heartbeat("writing slot 0", 12):
        out, _ = m.ssh_exec(con, "ubidetach -p /dev/%s >/dev/null 2>&1; "
                                 "ubiformat /dev/%s -y -q -f %s 2>&1; echo UBIFORMAT_RC=$?; "
                                 "ubiattach -p /dev/%s 2>&1 | tail -1"
                            % (s0["dev"], s0["dev"], remote, s0["dev"]), 1800)
    for line in out.splitlines()[-6:]:
        if line.strip():
            m.say("    " + line.rstrip())
    attached = re.search(r"UBI device number (\d+)", out)
    if "UBIFORMAT_RC=0" not in out or not attached:
        m.die("slot 0 does not attach as a UBI after the write:\n%s\nThe AP boots Arista's firmware "
              "from slot 1 by itself; run the tool again." % out.strip()[-400:])
    n = attached.group(1)
    vols, _ = m.ssh_exec(con, "for v in /sys/class/ubi/ubi%s_*; do echo \"$(cat $v/name) $(basename $v)\"; done"
                         % n, 60)
    have = dict(l.split()[:2] for l in vols.splitlines() if len(l.split()) >= 2)
    for vol in ("kernel", "rootfs"):
        if vol not in have:
            m.die("slot 0 has no %s volume after the write (found: %s).\nThe AP boots Arista's firmware "
                  "from slot 1 by itself; run the tool again." % (vol, ", ".join(sorted(have)) or "none"))
        if vol in listed:
            sha, size = listed[vol]
            got, _ = m.ssh_exec(con, "head -c %d /dev/%s | sha256sum | cut -d' ' -f1" % (size, have[vol]), 600)
            if got.strip() != sha:
                m.die("the %s volume in slot 0 does not read back as the release's.\nThe AP boots "
                      "Arista's firmware from slot 1 if OpenWrt does not start; run the tool again." % vol)
    m.say("    slot 0 written; its kernel and rootfs volumes read back as the release's.")
    m.ssh_exec(con, "ubidetach -p /dev/%s >/dev/null 2>&1; sync" % s0["dev"], 60)
    m.say("  Restarting the AP into OpenWrt.")
    m.ssh_exec(con, "reboot", 20)


def do_install(run, card, ip, ubi, sums):
    if MODELS[card["model"]].get("install_by") == "script":
        return do_install_script(run, card, ip, ubi, sums)
    if MODELS[card["model"]].get("install_by") == "ubiformat":
        return do_install_ubiformat(run, card, ip, ubi, sums)
    # A model whose release image is the sysupgrade one is installed by
    # sysupgrade itself, after one boot-command write: the C-460 (eMMC) and the
    # W-118 (NAND, into flash Arista does not use).
    if is_emmc(card["model"]) or MODELS[card["model"]].get("image") == "sysupgrade":
        return do_install_emmc(run, card, ip, ubi, sums)
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
                m.note("no sftp-server on the AP (%s); sending over a plain SSH channel" % e)
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
    m.note("installer %s: --no-backup %s, %s has %d MiB free"
           % (inst, "supported" if no_backup else "not supported", stage, free_mb))
    if not no_backup and free_mb < 600:
        big = "/mnt/" + card["model"] + "stage"
        size = "%dM" % MODELS[card["model"]].get("stage_mb", 768)
        m.note("the installer wants >512 MiB for its own backup; staging on a tmpfs")
        m.ssh_must(con, "mkdir -p %s; mountpoint -q %s || mount -t tmpfs -o size=%s none %s"
                   % (big, big, size, big), 60)
        m.ssh_must(con, "cp %s/SHA256SUMS %s/factory.ubi %s/" % (stage, stage, big), 300)
        stage = big
        m.note("staging on %s" % stage)
    flags = "--dry-run --stick %s" if not no_backup else "--dry-run --no-backup --stick %s"
    flags = flags % stage

    m.say("")
    m.say("  Running the installer's own checks (this writes nothing):")
    with Heartbeat("installer dry run", 20):
        out, _ = m.ssh_exec(con, "%s %s 2>&1" % (inst, flags), 1800)
    for line in out.splitlines():
        m.say("    " + line)
    if "INSTALL-RESULT: OK" not in out:
        m.die("the installer's own checks did not pass, so nothing was written.\n"
              + "\n".join(out.strip().splitlines()[-8:]))
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
        run.flash_writes.append("NAND slot 0 (unfinished)")
        m.die("the install did not finish.  This AP's own firmware is safe on this PC.\n"
              + "\n".join(out.strip().splitlines()[-8:]))
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


BLANK_MIB = 16      # what is zeroed at the start of a partition left blank


def ext4_captured_mounted(gz_path):
    """Is this saved partition image an ext4 that was mounted read-write when it
    was read?  Its superblock then still asks for journal recovery.

    Such a copy is not a filesystem that mounts: the backup reads a live
    partition over many seconds, so the journal in it does not replay ("JBD2:
    Invalid checksum ... recovery failed").  Written back, Arista's boot falls
    to mkfs.ext4, which finds a filesystem there and stops at "Proceed anyway?
    (y,N)" on the serial console -- forever (C-460, 2026-11-13).  Arista's
    firmware has no fsfreeze to take a clean copy with."""
    try:
        with gzip.open(gz_path, "rb") as f:
            head = f.read(2048)
    except Exception:
        return False
    if len(head) < 0x464 or head[0x438:0x43A] != b"\x53\xef":      # s_magic 0xEF53
        return False
    return bool(struct.unpack_from("<I", head, 0x460)[0] & 0x4)     # INCOMPAT_RECOVER


def do_restore_emmc(run, card, manifest, backup_dir):
    """Put an eMMC model's own firmware back (the C-460).

    Slot 0 (0:HLOS, rootfs, rootfs_data) is written back by dd to the GPT
    partitions -- a block device needs no erase -- and the U-Boot environment to
    its NOR partition (flash_erase then dd).  Slot 1 and every other partition
    are left as found.  Each write is read back and matched to the backup."""
    con = run.installer
    d = MODELS[card["model"]] if card and card.get("model") else MODELS[manifest["model"]]
    m.say("")
    m.say("  Reading the installer's partitions.")
    rows = emmc_table(m.ssh_must(con, EMMC_LIST % (d["emmc_disk"], d["emmc_disk"]), 30))
    mtd = mtd_table(m.ssh_must(con, "cat /proc/mtd", 30))
    env = find_part(mtd, d["appsblenv"])
    by_name = {p["name"].lower(): p for p in manifest["partitions"]}
    want = [n for n in d["slot0_parts"] if n.lower() in by_name]
    if not want:
        m.die("this backup has none of slot 0 (%s)" % ", ".join(d["slot0_parts"]))
    m.say("  Writing back: %s" % ", ".join(want))
    m.say("  Slot 1 (%s) and every other partition stay as found."
          % ", ".join(d["slot1_parts"]))
    if not run.yes:
        if ask("  Type 'yes' to go ahead: ", "--yes").strip().lower() not in ("y", "yes"):
            m.die("not confirmed; nothing was written")

    # Never onto a live system: this runs from the RAM installer only, and none
    # of the partitions it writes may be mounted.
    root = m.ssh_exec(con, "awk '$2 == \"/\" {print $3; exit}' /proc/mounts", 20)[0].strip()
    if root not in RAM_ROOTS:
        m.die("the system at the installer's address runs from %r, not from RAM; refusing "
              "to write its eMMC.  Nothing was written." % root)
    mounts = m.ssh_exec(con, "cat /proc/mounts", 20)[0]
    for name in want:
        dev = find_part(rows, (name,))
        if dev and re.search(r"^/dev/%s\s" % re.escape(dev["dev"]), mounts, re.M):
            m.die("/dev/%s (%s) is mounted on the installer; refusing to write it.  "
                  "Nothing was written." % (dev["dev"], name))

    m.ssh_exec(con, "mkdir -p /tmp/restore", 30)
    for name in want:
        p = by_name[name.lower()]
        dev = find_part(rows, (name,))
        if not dev:
            m.die("the installer has no partition %s to write back" % name)
        if dev["blocks"] != p.get("blocks", dev["blocks"]):
            m.die("%s is %d blocks here but %d in the backup"
                  % (name, dev["blocks"], p.get("blocks")))
        if name == d.get("overlay_part") and ext4_captured_mounted(os.path.join(backup_dir, p["file"])):
            # Arista's settings partition, saved while mounted: not restorable as
            # it is.  Left blank, Arista's boot formats it without asking -- as it
            # does for a slot's overlay that was never used -- and takes its
            # settings from its own defaults and its CONFIG partition.
            m.say("    %s was saved while Arista had it mounted; that copy would not mount, and"
                  % name)
            m.say("    Arista's boot would stop at a console prompt.  Leaving it blank instead:")
            m.say("    Arista formats it on its first boot and starts from its default settings.")
            m.say("    (The saved copy stays in the backup: %s)" % p["file"])
            zero_md5 = hashlib.md5(b"\0" * (BLANK_MIB << 20)).hexdigest()
            cmd = ("dd if=/dev/zero of=/dev/%s bs=1M count=%d conv=fsync 2>/dev/null; sync; "
                   "echo 3 > /proc/sys/vm/drop_caches; "
                   "dd if=/dev/%s bs=1M count=%d 2>/dev/null | md5sum | cut -d' ' -f1"
                   % (dev["dev"], BLANK_MIB, dev["dev"], BLANK_MIB))
            out, _ = m.ssh_exec(con, cmd, 300)
            got = [l for l in out.splitlines() if re.fullmatch(r"[0-9a-f]{32}", l.strip())]
            run.flash_writes.append("eMMC %s (left blank)" % name)
            if not got or got[-1].strip() != zero_md5:
                m.die("%s does not read back blank; run the restore again before rebooting "
                      "the AP" % name)
            m.say("    %s: first %d MiB blank, read back." % (name, BLANK_MIB))
            continue
        rp = "/tmp/restore/%s.bin" % dev["dev"]
        # Staged in RAM one at a time (and removed after its write): the three
        # together are 1.5 GiB, more than the installer's tmpfs holds.
        free_kb = m.ssh_exec(con, "df -k /tmp | tail -1 | awk '{print $4}'", 20)[0].strip()
        if free_kb.isdigit() and int(free_kb) * 1024 < p["size"] + (32 << 20):
            m.die("the installer has %s KiB free in RAM, too little to stage %s (%d MiB).  "
                  "Nothing more was written; run the restore again."
                  % (free_kb, name, p["size"] >> 20))
        _send_gz(con, os.path.join(backup_dir, p["file"]), rp, name)
        out = m.ssh_must(con, "md5sum %s | cut -d' ' -f1" % rp, 600)
        if out.strip() != p["md5"]:
            m.die("%s on the AP does not match the backup (md5 %s, here %s)"
                  % (name, p["md5"], out.strip()))
        m.say("    %s staged and md5-verified; writing /dev/%s." % (name, dev["dev"]))
        # The read-back comes off the card, not the page cache (drop_caches), and
        # covers the whole partition device, which is what the backup hashed.
        cmd = ("dd if=%s of=/dev/%s bs=1M conv=fsync 2>/dev/null; rm -f %s; sync; "
               "echo 3 > /proc/sys/vm/drop_caches; "
               "md5sum /dev/%s | cut -d' ' -f1"
               % (rp, dev["dev"], rp, dev["dev"]))
        with Heartbeat("writing %s (%.0f MiB)" % (name, p["size"] / 1048576.0), 12):
            out, _ = m.ssh_exec(con, cmd, 1800)
        got = [l for l in out.splitlines() if re.fullmatch(r"[0-9a-f]{32}", l.strip())]
        if not got or got[-1].strip() != p["md5"]:
            m.die("%s does not read back as the backup (got %s, want %s)"
                  % (name, got[-1].strip() if got else "?", p["md5"]))
        m.say("    %s reads back identical to the backup." % name)
        run.flash_writes.append("eMMC %s (restore)" % name)

    envp = next((p for p in manifest["partitions"]
                 if any(p["name"].lower() == n.lower() for n in d["appsblenv"])), None)
    if envp and env:
        prefix = push_tools(con, ("flash_erase",))
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
    else:
        m.say("  (no saved environment in this backup; leaving it as found)")
    m.say("  Rebooting into the AP's own firmware.")
    m.ssh_exec(con, "sync; reboot", 20)


def do_restore(run, card, manifest, backup_dir):
    mdl = card["model"] if card and card.get("model") else manifest["model"]
    if is_emmc(mdl):
        return do_restore_emmc(run, card, manifest, backup_dir)
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
        if ask("  Type 'yes' to go ahead: ", "--yes").strip().lower() not in ("y", "yes"):
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


def do_restore_bootcmd(run, who, d):
    """Back to Arista on a model whose install never replaced it (the W-118):
    one environment write from OpenWrt, then a restart.

    The boot command goes back to the model's own, the variables the install
    added are removed, and bootargs is set to the slot `pri` names -- Arista's
    boot only rewrites it when `pri` changes, so a value left by anything else
    would be what Arista starts with."""
    con = ssh_open(who["ip"], who.get("user") or "root", who.get("password") or "", timeout=15)
    env, _ = ssh_exec(con, "fw_printenv 2>&1", 60)
    have = dict(l.split("=", 1) for l in env.splitlines() if "=" in l)
    if "ubootscript" not in have or "bootcmd" not in have:
        die("this AP's boot variables cannot be read from OpenWrt, so they were not changed:\n%s"
            % env.strip()[-300:])
    pri = have.get("pri", "0").strip()
    args = have.get("ba0" if pri == "0" else "ba1")
    lines = ["bootcmd %s" % d.get("stock_bootcmd", "run ubootscript")]
    setting = dict(d.get("revert_env", ()))
    if setting:
        # A model whose install took slot 0 and kept slot 1 (the W-318, C-200,
        # C-130): the values that make Arista's own boot choose the kept slot.
        lines += ["%s %s" % (n, v) for n, v in d["revert_env"]]
        pri = setting.get("pri", pri)
        args = have.get("ba0" if pri == "0" else "ba1")
    if args and "bootargs" not in setting:
        lines.append("bootargs %s" % args)
    for name in tuple(n for n, _v in d.get("install_env", ())) + tuple(d.get("revert_names", ())):
        # Not a variable this same write sets: the C-200's install sets pri, and
        # removing every name it set took pri out again right after putting it
        # back (2026-11-15; Arista still started, with pri undefined).
        if name != "bootcmd" and name not in setting and name in have:
            lines.append(name)
    for name in ("mtdids", "mtdparts"):
        if name in have:
            lines.append(name)
    say("  Writing Arista's boot command back (pri=%s):" % pri)
    for line in lines:
        say("    %s" % line[:110])
    out, rc = ssh_exec(con, "printf %s | fw_setenv -s -" % _shq("\n".join(lines) + "\n"), 120)
    LOG.write("\nENV WRITE (back to Arista): rc=%s %s\n" % (rc, out))
    if rc != 0:
        die("writing the boot command failed (exit %s): %s\nNothing else was changed." % (rc, out[-300:]))
    chk, _ = ssh_exec(con, "fw_printenv bootcmd 2>&1", 30)
    if chk.strip() != "bootcmd=%s" % d.get("stock_bootcmd", "run ubootscript"):
        die("the boot command did not read back as written:\n%s" % chk[-300:])
    run.env_writes.append("boot command back to Arista's")
    global _REBOOTED
    _REBOOTED = True
    ssh_exec(con, "sync; reboot", 20)
    try:
        con.close()
    except Exception:
        pass


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


def find_stock(prefix, timeout, mac=None):
    """The AP back on its own firmware: Arista's config shell answers.  With
    mac set, an AP that names a different MAC is not taken for it."""
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
            text = pty.since(0)
            # Firmware 16.x has no 'version'; 'show device info' prints the same.
            if "Device Build" not in text and "Device Version" not in text:
                pty.send("show device info")
                time.sleep(1.5)
                text = pty.since(0)
            said = parse_device_info(text)["mac"]
            if mac and said and said != mac.lower():
                return None
            return ip, text

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


# --- finding an AP before anything is known about it --------------------------

# MAC prefixes these access points carry (Arista, and Mojo before it), each seen
# on a unit.  Only a host with one of these is ever asked who it is: a factory
# login is never tried against the rest of somebody's network.
AP_OUIS = ("30:86:2d", "e4:d1:24", "e0:1c:a7",
           "88:b1:e1",              # the W-118's (the older Mojo block)
           "00:11:74")              # a C-130's (the AirTight block, older still)


def _private(ip):
    p = [int(x) for x in ip.split(".")]
    return p[0] == 10 or (p[0] == 172 and 16 <= p[1] <= 31) or (p[0] == 192 and p[1] == 168)


def local_prefixes():
    """The /24s this PC sits on, the one with the default route first."""
    ips = []
    try:
        ips.append(pc_address("192.0.2.1"))      # no packet is sent; it names the route
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except Exception:
        pass
    out = []
    for ip in ips:
        if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip) or not _private(ip):
            continue
        prefix = ip.rsplit(".", 1)[0]
        if prefix not in out:
            out.append(prefix)
    return out[:4]


def sweep(prefix, port=22, timeout=0.4):
    """The hosts of a /24 that accept TCP on a port.  It also fills this PC's
    neighbour table, which is where the MAC addresses then come from."""
    def one(i):
        socket.create_connection(("%s.%d" % (prefix, i), port), timeout).close()
        return "%s.%d" % (prefix, i)

    live = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as ex:
        for f in concurrent.futures.as_completed([ex.submit(one, i) for i in range(1, 255)]):
            try:
                live.append(f.result())
            except Exception:
                pass
    return live


def port_open(ip, port=22, timeout=3):
    try:
        socket.create_connection((ip, port), timeout).close()
        return True
    except Exception:
        return False


def parse_neighbours(text):
    """ip -> mac out of `arp -a` (Windows, macOS, Linux) or `ip neigh`."""
    out = {}
    for line in text.splitlines():
        ipm = re.search(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])", line)
        macm = re.search(r"(?<![0-9A-Fa-f:-])([0-9A-Fa-f]{1,2}(?:[:-][0-9A-Fa-f]{1,2}){5})(?![0-9A-Fa-f:-])", line)
        if not ipm or not macm:
            continue
        mac = ":".join("%02x" % int(x, 16) for x in re.split("[:-]", macm.group(1)))
        if mac not in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"):
            out[ipm.group(1)] = mac
    return out


def neighbours():
    text = ""
    for cmd in (["arp", "-a"], ["ip", "neigh"]):
        try:
            text += subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout + "\n"
        except Exception:
            pass
    return parse_neighbours(text)


def parse_device_info(text):
    """Model, firmware and MAC out of Arista's `show device info` or `version`.

    16.x and later:  "Device model: C-360", "Device build: 20.0.0-179",
                     "Device MAC address: 30:86:2D:..".
    11.x and 8.x:    "The device model is C-260.", "Device Build: [11.0.1-49]"."""
    info = {"name": None, "firmware": None, "mac": None}
    mm = re.search(r"device model(?:\s+is|\s*:)\s*\[?([A-Za-z]-?\d+\w*)", text, re.I)
    if mm:
        info["name"] = mm.group(1).upper()
    for pat in (r"Device Build\s*:\s*\[?\s*([0-9][^\]\s]*)",
                r"Device Version\s*:\s*\[?\s*([0-9][^\]\s]*)"):
        mm = re.search(pat, text, re.I)
        if mm:
            info["firmware"] = mm.group(1).strip()
            break
    mm = re.search(r"MAC address\s*:\s*\[?\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", text, re.I)
    if mm:
        info["mac"] = mm.group(1).lower()
    return info


def port_board(board):
    """Whether a board name is one of this port's: arista,<model>, or the
    community name the C-130's device keeps (mojo,c-130)."""
    return (board or "").startswith(("arista,", "mojo,"))


def model_key(name):
    """'C-260' or 'arista,c-260' -> 'c260' (whether or not the tool knows it)."""
    return re.sub(r"[^a-z0-9]", "", (name or "").split(",")[-1].lower())


def build_of(description):
    """The port's release tag out of DISTRIB_DESCRIPTION: '... (arista <tag>)'."""
    mm = re.search(r"\(arista\s+([^)\s]+)\)", description or "")
    return mm.group(1) if mm else None


def build_num(tag):
    """The build number in a release tag (v2026.10.17.1-157-g0500cfe -> 157)."""
    mm = re.search(r"-(\d+)-g[0-9a-f]+$", tag or "")
    return int(mm.group(1)) if mm else None


def short_tag(tag):
    """v2026.10.17.1-157-g0500cfe -> 'build 157' for the screen."""
    mm = re.search(r"-(\d+)-g[0-9a-f]+$", tag or "")
    return "build %s" % mm.group(1) if mm else (tag or "?")


def _probe_openwrt(ip, user, password, timeout):
    try:
        con = ssh_open(ip, user, password, timeout=timeout)
    except Exception:
        return None
    try:
        text, _ = ssh_exec(con, "echo B=$(cat /tmp/sysinfo/board_name 2>/dev/null); "
                                "echo R=$(awk '$2 == \"/\" {print $3; exit}' /proc/mounts); "
                                ". /etc/openwrt_release 2>/dev/null; "
                                "echo D=$DISTRIB_DESCRIPTION; echo V=$DISTRIB_RELEASE", 20)
        got = dict(l.strip().split("=", 1) for l in text.splitlines() if re.match(r"^[BRDV]=", l.strip()))
        board = got.get("B", "")
        if not port_board(board):
            return None
        key = model_key(board)
        return {"ip": ip, "kind": "openwrt", "board": board, "model": key if key in MODELS else None,
                "name": MODELS[key]["name"] if key in MODELS else board.split(",")[-1].upper(),
                "mac": board_mac(con), "release": got.get("D", ""), "series": got.get("V", ""),
                "build": build_of(got.get("D", "")), "ram": got.get("R", "") in RAM_ROOTS,
                "user": user, "password": password}
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def _probe_stock(ip, user, password, timeout):
    """Ask an AP on Arista's firmware what it is, at its own CLI.  Read-only,
    and no root shell is taken: this is what `show device info` is for."""
    try:
        client = ssh_open(ip, user, password, timeout=timeout)
    except Exception as e:
        return {"ip": ip, "kind": "locked"} if "uthentication" in type(e).__name__ + str(e) else None
    try:
        pty = Pty(client.invoke_shell(width=200, height=60))
        if not pty.wait(SHELL_PAT, 15):
            return None
        text = ""
        for cmd in ("show device info", "version"):
            mk = pty.mark()
            pty.send(cmd)
            time.sleep(0.3)
            pty.wait(SHELL_PAT, 8, mark=mk)
            text += pty.since(mk) + "\n"
            info = parse_device_info(text)
            if info["name"]:
                break
        if not info["name"]:
            return None
        key = model_key(info["name"])
        return {"ip": ip, "kind": "stock", "model": key if key in MODELS else None,
                "name": MODELS[key]["name"] if key in MODELS else info["name"],
                "mac": info["mac"] or "", "firmware": info["firmware"],
                "user": user, "password": password}
    except Exception:
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


def probe_host(ip, user=None, password=None, mac=None, timeout=6):
    """Who is at ip?  A dict (kind 'openwrt', 'stock' or 'locked'), or None when
    nothing this tool knows answers there.  Nothing is changed by asking."""
    who = _probe_openwrt(ip, "root", "", timeout)
    if not who and user and password is not None:
        who = _probe_openwrt(ip, user, password, timeout)
    if not who:
        who = _probe_stock(ip, user or "config", password if user else "config", timeout)
    if who and not who.get("mac") and mac:
        who["mac"] = mac
    return who


def describe(who):
    """One line for the screen: what an AP is and what it runs."""
    if who["kind"] == "locked":
        return "Arista access point (needs its password: the default login was refused)"
    if who["kind"] == "stock":
        return "%s on Arista firmware %s" % (who["name"], who.get("firmware") or "(version not shown)")
    what = "OpenWrt"
    if who.get("build"):
        what += ", " + short_tag(who["build"])
    elif who.get("series"):
        what += " " + who["series"]
    return "%s on %s%s" % (who["name"], what, " (running from RAM)" if who.get("ram") else "")


def discover():
    """The Arista access points on this PC's own networks, as probe_host dicts."""
    prefixes = local_prefixes()
    if not prefixes:
        return [], prefixes
    found = []
    with Heartbeat("Looking for Arista access points on %s"
                   % ", ".join(p + ".0/24" for p in prefixes)):
        live = []
        for p in prefixes:
            live += sweep(p)
        nb = neighbours()
        cands = [(ip, nb[ip]) for ip in live if nb.get(ip, "")[:8] in AP_OUIS]
        note("%d host(s) answer SSH; %d carry an Arista MAC prefix" % (len(live), len(cands)))
        if cands:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(cands))) as ex:
                for who in ex.map(lambda c: probe_host(c[0], mac=c[1]), cands):
                    if who:
                        found.append(who)
    found.sort(key=lambda w: tuple(int(x) for x in w["ip"].split(".")))
    return found, prefixes


def choose_ap(a):
    """No address was given: look for the AP, and ask which one."""
    found, prefixes = discover()
    if found:
        tell("")
        tell("Found on your network:")
        tell("")
        for i, who in enumerate(found, 1):
            tell("  %2d  %-15s  %s" % (i, who["ip"], describe(who)))
        tell("")
        while True:
            ans = ask("Which one? [%s, or type another AP's address] "
                      % ("1" if len(found) == 1 else "1-%d" % len(found)), "--host").strip()
            if not ans and len(found) == 1:
                return found[0]
            if ans.isdigit() and 1 <= int(ans) <= len(found):
                return found[int(ans) - 1]
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ans):
                return {"ip": ans, "kind": None}
            if ans.lower() in ("q", "quit", "exit"):
                tell("Nothing was changed.")
                sys.exit(0)
            tell("  (a number from the list, an address like 192.168.1.50, or q to quit)")
    tell("")
    if prefixes:
        tell("No Arista access point answered on %s."
             % ", ".join(p + ".0/24" for p in prefixes))
    tell("Its address is on your router's or DHCP server's list of clients.")
    while True:
        ans = ask("The AP's address (or q to quit): ", "--host").strip()
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ans):
            return {"ip": ans, "kind": None}
        if ans.lower() in ("q", "quit", "exit", ""):
            tell("Nothing was changed.")
            sys.exit(0)


def identify(a, who):
    """Fill in who an AP is (asking for its password if the factory one was
    changed), or stop with what to check."""
    ip = who["ip"]
    if not who.get("kind"):
        # A default login is not tried against something that is plainly not one
        # of these APs: its MAC prefix is looked at first, where this PC can see it.
        if not port_open(ip):
            die("nothing answers at %s.\n"
                "Check the address, that the AP has finished starting (two or three minutes\n"
                "after power-on), and that this PC can reach it (ping %s).  Nothing was changed."
                % (ip, ip))
        mac = neighbours().get(ip, "")
        if mac and mac[:8] not in AP_OUIS:
            tell("")
            tell("%s has the MAC address %s, which is not one of the Arista prefixes" % (ip, mac))
            tell("this tool knows (%s)." % ", ".join(AP_OUIS))
            if not (sys.stdin.isatty() and not a.yes) or not confirm("Is it really an Arista access point?"):
                die("%s does not look like an Arista access point.  Nothing was changed.\n"
                    "Check the address on your router's list of clients." % ip)
        with Heartbeat("Asking %s what it is" % ip):
            who = probe_host(ip, a.user, a.password, mac=mac) or {"ip": ip, "kind": None}
    tries = 0
    while who.get("kind") == "locked" and sys.stdin.isatty() and not a.yes and tries < 3:
        tries += 1
        tell("")
        tell("%s did not accept the default login, so its password was changed." % ip)
        tell("On Arista's firmware the user is 'config'; on OpenWrt it is 'root'.")
        import getpass
        user = ask("  User [config] (or q to quit): ", "--user").strip() or "config"
        if user.lower() in ("q", "quit", "exit"):
            tell("Nothing was changed.")
            sys.exit(0)
        with _OUT:
            _clear_status()
        try:
            password = getpass.getpass("  Password: ")
        except (EOFError, KeyboardInterrupt):
            tell("")
            tell("Nothing was changed.")
            sys.exit(0)
        a.user, a.password = user, password
        with Heartbeat("Asking %s what it is" % ip):
            who = probe_host(ip, user, password) or {"ip": ip, "kind": "locked"}
    if who.get("kind") == "locked":
        die("cannot log in to %s.\n"
            "On Arista's firmware the account is 'config' (factory password 'config');\n"
            "on OpenWrt it is 'root'.  Give this unit's with --user ... --password ...,\n"
            "or reset the AP to its factory defaults and run again.  Nothing was changed."
            % ip)
    if not who.get("kind"):
        die("nothing this tool knows answered at %s.\n"
            "Check the address, that the AP has finished starting (two or three minutes\n"
            "after power-on), and that this PC can reach it (ping %s)." % (ip, ip))
    return who


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
        self.kexec_dir = None        # kexec module + tool for ram_boot "kexec" models
        self.work = None             # this run's folder (field-run-*)


def _cached_meta(cache, tag):
    try:
        with open(os.path.join(cache, tag, "release.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def _newest_cached(cache, tags, channel="stable"):
    """The newest of these cached releases.  By publication date where the cache
    recorded it, else by when it was saved: tag text sorts -9 after -10."""
    accepted = [t for t in tags if not _cached_meta(cache, t).get("prerelease")]
    tags = list(tags) if channel == "testing" else (accepted or ([] if channel == "stable" else list(tags)))
    if not tags:
        return None
    return max(tags, key=lambda t: (_cached_meta(cache, t).get("published_at") or "",
                                    os.path.getmtime(os.path.join(cache, t))))


def _cache_ok(cache, tag, model):
    """Everything this model needs for `tag`, already on this PC?"""
    d = os.path.join(cache, tag)
    names = m.files_for(model)
    want = ["SHA256SUMS"] + list(names.values())
    if all(os.path.exists(os.path.join(d, n)) for n in want):
        return d, want
    return None, want


def _release_triple(d, n, tag):
    """(initramfs, install image, sums-or-None, tag) from a release dir and the
    files_for() names.  The install image is a factory.ubi for the NAND models
    and a sysupgrade.bin for the eMMC ones; only the NAND models have a per-model
    install-SHA256SUMS, so sysupgrade models verify against the file itself."""
    img = n.get("ubi") or n["sysupgrade"]
    sums = n.get("sums")
    return (os.path.join(d, n["itb"]), os.path.join(d, img),
            os.path.join(d, sums) if sums else None, tag)


def release_files(a, model):
    """The release files, from GitHub or from the copy already on this PC.

    A field laptop often has no uplink at the site, so a cached release is used
    when it is there and verifies, and `--offline` skips the API entirely."""
    if not field_install(model):
        refuse_unsupported(model)
    cache = os.path.abspath(a.files)
    if not a.offline:
        try:
            d, tag = m.fetch_release(a.tag, [model], cache, getattr(a, "channel", "stable"))
            return _release_triple(d, m.files_for(model), tag)
        except SystemExit:
            raise
        except Exception as e:
            m.say("  cannot reach GitHub (%s)" % e)
            m.say("  looking for the release already saved on this PC.")
    names = m.files_for(model)
    want = ["SHA256SUMS"] + list(names.values())
    if all(os.path.exists(os.path.join(cache, n)) for n in want):
        m.check_sums(cache, want)
        m.say("  using the images in %s, SHA256SUMS verified" % cache)
        return _release_triple(cache, names, "local")
    tag = a.tag
    if not tag:
        cands = [x for x in os.listdir(cache)
                 if _cache_ok(cache, x, model)[0]] if os.path.isdir(cache) else []
        tag = _newest_cached(cache, cands, getattr(a, "channel", "stable"))
        if not tag:
            m.die("no release on this PC and GitHub is unreachable.  Run once with "
                  "a connection, or copy %s\\ into %s" % ("<tag>", cache))
    d, want = _cache_ok(cache, tag, model)
    if not d:
        m.die("the cached release %s is incomplete in %s" % (tag, os.path.join(cache, tag)))
    m.check_sums(d, want)                      # never trust a cached file blindly
    m.say("  using %s from this PC (%s), SHA256SUMS verified" % (tag, d))
    return _release_triple(d, m.files_for(model), tag)


def verify_openwrt(prefix, mac=None, timeout=420):
    m.say("")
    m.say("Waiting for OpenWrt to come up from slot 0%s.  It is identified by its"
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
        if not lines or not port_board(lines[0]):
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
            # Only a system running from RAM is the installer: an installed
            # OpenWrt answers to the same name and MAC, and must never be
            # written as if it were one.
            if r and r[2] in RAM_ROOTS:
                m.say("")
                m.say("  the DHCP server publishes the installer as '%s' -> %s" % (name, ip))
                return r[0], ip, r[1], r[2]
            if r:
                r[0].close()
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
            if r and r[2] in RAM_ROOTS:
                return r[0], cand, r[1], r[2]
            if r:
                r[0].close()
                m.say("  [+%ds] %s answers as an installed system (root on %s), not the "
                      "RAM installer yet." % (int(time.time() - start), cand, r[2]))
                time.sleep(5)
                continue
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
        m.note("the backup in %s does not verify; ignoring it" % name)
    return None, None


def ensure_paramiko(interactive):
    """The one thing this file cannot carry: the SSH library.  Offer to fetch
    it rather than stop on a traceback."""
    try:
        import paramiko                               # noqa: F401
        return
    except ImportError:
        pass
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "paramiko"]
    tell("This tool needs one Python library, 'paramiko' (it speaks SSH to the AP).")
    if not interactive or not confirm("Install it now?", "the library (python -m pip install paramiko)"):
        die("the 'paramiko' library is not installed.\n"
            "Install it with:  %s -m pip install paramiko\n"
            "and run this again.  Nothing was changed." % sys.executable)
    with Heartbeat("Installing paramiko"):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            r = subprocess.run(cmd + ["--user"], capture_output=True, text=True)
    importlib.invalidate_caches()
    try:
        import paramiko                               # noqa: F401
    except ImportError:
        die("could not install 'paramiko' here.\n"
            "%s\n"
            "On Debian or Ubuntu:  sudo apt install python3-paramiko\n"
            "Or use the one-line installer from the project's page, which keeps its own\n"
            "private copy of Python's libraries.  Nothing was changed."
            % " ".join((r.stderr or r.stdout or "").strip().splitlines()[-1:]))


def saved_backup(model, mac):
    """The newest firmware saved for this AP on this PC: (folder, when) or
    (None, None).  Looked at, not verified: a restore verifies what it sends."""
    base = os.path.join(BACKUPS, "%s-%s" % (model, (mac or "").replace(":", "").lower()))
    if not mac or not os.path.isdir(base):
        return None, None
    for name in sorted(os.listdir(base), reverse=True):
        man = os.path.join(base, name, "manifest.json")
        if os.path.exists(man):
            try:
                with open(man, encoding="utf-8") as f:
                    when = (json.load(f).get("when") or name)[:10]
            except Exception:
                continue
            return os.path.join(base, name), when
    return None, None


def resolve_release(a, model):
    """Which build this run would take, for the screen, before anything is
    downloaded: {"tag", "testing", "published", "newer"}; {} when no release has
    the model; None when GitHub cannot be asked (the cache decides later)."""
    if a.offline:
        return None
    try:
        if a.tag:
            rel, newer = json.load(http_get("%s/repos/%s/releases/tags/%s" % (API, REPO, a.tag),
                                            "application/vnd.github+json")), None
        else:
            rel, newer = pick_release([model], a.channel)
    except SystemExit:
        raise
    except Exception as e:                            # noqa: BLE001
        note("GitHub could not be asked for the releases (%s)" % e)
        return None
    if rel is None:
        return {}
    return {"tag": rel["tag_name"], "testing": bool(rel.get("prerelease")),
            "published": (rel.get("published_at") or "")[:10],
            "newer": newer["tag_name"] if newer else None}


def build_words(rel):
    """'build 157 (testing, 2026-11-10)' for the screen."""
    if not rel:
        return "the build saved on this PC"
    return "%s (%s%s)" % (short_tag(rel["tag"]), "testing, " if rel.get("testing") else "",
                          rel.get("published") or "date unknown")


def choose_action(who, rel, backup_when, can_install):
    """The menu.  Only what makes sense for this AP is offered."""
    opts = []
    if who["kind"] == "stock":
        if can_install:
            opts.append(("install", "Install OpenWrt"))
            opts.append(("backup", "Only save this AP's own firmware to this PC (changes nothing)"))
            if backup_when:
                opts.append(("restore", "Put back the firmware saved from it on %s" % backup_when))
    else:
        if rel and rel.get("tag") and rel["tag"] != who.get("build"):
            opts.append(("update", "Update OpenWrt to %s, keeping its settings" % build_words(rel)))
        if backup_when and can_install:
            opts.append(("restore", "Put Arista's firmware back (saved from this AP on %s)" % backup_when))
    opts.append(("stop", "Nothing; leave it as it is"))
    if len(opts) == 1:
        tell("There is nothing for this tool to do on it.")
        return "stop"
    tell("")
    tell("What would you like to do?")
    tell("")
    for i, (_, text) in enumerate(opts, 1):
        tell("  %2d  %s" % (i, text))
    tell("")
    while True:
        ans = ask("Choice [1-%d]: " % len(opts), "--install, --backup-only, --update or --restore DIR").strip().lower()
        if ans.isdigit() and 1 <= int(ans) <= len(opts):
            return opts[int(ans) - 1][0]
        if ans in ("q", "quit", "exit", ""):
            return "stop"


def wait_gone(ip, up_to=180):
    """Wait until a host stops answering SSH (it is restarting)."""
    end = time.time() + up_to
    while time.time() < end:
        try:
            socket.create_connection((ip, 22), 2).close()
        except Exception:
            return True
        time.sleep(2)
    return False


def wait_openwrt(prefix, mac, timeout=480):
    """The installed OpenWrt, found by its MAC: (ip, board) or (None, None)."""
    con, ip, board, _root = find_ap(prefix, timeout, want_ram=False, mac=mac)
    if con is None:
        return None, None
    info, _ = ssh_exec(con, "grep -E 'DESCRIPTION' /etc/openwrt_release; uname -r; uptime", 60)
    for line in info.splitlines():
        say("  " + line.strip())
    try:
        con.close()
    except Exception:
        pass
    return ip, board


def do_update(run, who, image):
    """Update an AP that already runs this port: OpenWrt's own sysupgrade, after
    its own check of the image.  Settings are kept, except from the older 25.12
    port, whose names changed."""
    con = ssh_open(who["ip"], who.get("user") or "root", who.get("password") or "", timeout=15)
    remote = "/tmp/sysupgrade.bin"
    out, _ = ssh_exec(con, "df -k /tmp | tail -1 | awk '{print $4}'", 30)
    need_kb = os.path.getsize(image) // 1024 + 8192
    try:
        free_kb = int(out.strip().split()[0])
    except Exception:
        free_kb = need_kb
    if free_kb < need_kb:
        die("the AP has %d MiB free in /tmp and the image needs %d; nothing was written.\n"
            "Restart the AP and run this again." % (free_kb // 1024, need_kb // 1024))
    _send_file(con, image, remote, "the image")
    out, _ = ssh_exec(con, "sha256sum %s | cut -d' ' -f1" % remote, 600)
    if out.strip() != sha256_file(image):
        die("the image arrived wrong on the AP (checksum mismatch); nothing was written")
    out, _ = ssh_exec(con, "sysupgrade -T %s 2>&1; echo RC=$?" % remote, 300)
    if "RC=0" not in out:
        ssh_exec(con, "rm -f %s" % remote, 30)
        die("the AP's own check rejects this image, so nothing was written.\n"
            + "\n".join(l for l in out.strip().splitlines()[-6:] if not l.startswith("RC=")))
    keep = "25.12" not in (who.get("series") or "")
    if not keep:
        note("this AP runs the older 25.12 port; its settings cannot carry over (sysupgrade -n)")
    run.flash_writes.append("sysupgrade (%s)" % ("settings kept" if keep else "settings reset"))
    # sysupgrade ends every session before it writes, so the channel dropping is
    # the expected end of this command, not a failure.
    ssh_exec(con, "sysupgrade %s%s 2>&1" % ("" if keep else "-n ", remote), 120)
    try:
        con.close()
    except Exception:
        pass
    return keep


def main():
    ap = argparse.ArgumentParser(
        description="Put OpenWrt on an Arista access point over the network -- or put the "
                    "AP's own firmware back.  Run it with no options and it asks what it needs.")
    ap.add_argument("--version", action="version",
                    version="arista-field %s" % TOOL_VERSION)
    ap.add_argument("--host", help="the AP's address (default: look for it on this PC's network)")
    ap.add_argument("--pc-ip", help="this PC as the AP sees it")
    ap.add_argument("--user", help="log in as this user (default: root on OpenWrt, "
                                  "config on Arista's firmware)")
    ap.add_argument("--password", help="that user's password (default: none on "
                                      "OpenWrt, 'config' on Arista's firmware)")
    ap.add_argument("--ap-ip", help="the address U-Boot uses (default host/24 .230)")
    ap.add_argument("--tag", help="release tag (default: the newest accepted release that has this AP's model)")
    ap.add_argument("--channel", choices=("stable", "testing"), default=None,
                    help="stable: accepted releases only; testing: the newest build, "
                         "pre-releases included (default: accepted, or testing where a "
                         "model has no accepted release yet)")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="show every detail on screen, with the time (it is always in the log)")
    ap.add_argument("--list-releases", action="store_true",
                    help="show which models each release carries, and stop")
    ap.add_argument("--fetch", metavar="MODEL",
                    help="download and verify a model's newest release files "
                         "(%s) with no AP, and stop" % ", ".join(sorted(MODELS)))
    ap.add_argument("--files", default=RELEASES, help="release cache (default: %(default)s)")
    ap.add_argument("--backup-dir", help="where to save the firmware (default backups/<model>-<mac>)")
    ap.add_argument("--backup-only", action="store_true", help="save the AP's firmware and stop")
    ap.add_argument("--verify-only", action="store_true", help="check the AP and stop")
    ap.add_argument("--install", action="store_true", help="install OpenWrt (no menu)")
    ap.add_argument("--update", action="store_true",
                    help="update an AP that already runs this OpenWrt port (no menu)")
    ap.add_argument("--ramboot", action="store_true",
                    help="boot OpenWrt into RAM over the network and stop; the eMMC "
                         "is not touched (the C-460)")
    ap.add_argument("--image", metavar="PATH",
                    help="with --ramboot: a local initramfs .itb to RAM-boot (e.g. a "
                         "fresh build) instead of a release")
    ap.add_argument("--kexec-dir", metavar="DIR",
                    help="folder with kexec_mod_arm64.ko, kexec_mod.ko and kexec for models "
                         "whose RAM installer starts by kexec (default: kexec/<model> next "
                         "to this tool, else fetched from the repository)")
    ap.add_argument("--restore", metavar="DIR", help="put an AP's saved firmware back")
    ap.add_argument("--installer-at", metavar="IP",
                    help="the RAM installer is already running at this address "
                         "(resume after an interruption)")
    ap.add_argument("--no-wait", action="store_true",
                    help="do not wait for the AP to come back afterwards")
    ap.add_argument("--verify", action="store_true", help=argparse.SUPPRESS)   # now the default
    ap.add_argument("--installer-ip", metavar="IP",
                    help="the address the installer took from DHCP, for the netboot "
                         "that follows (default: found by its MAC)")
    ap.add_argument("--fast", action="store_true", help="skip saving the fallback slot")
    ap.add_argument("--offline", action="store_true",
                    help="use the release already on this PC; do not ask GitHub")
    ap.add_argument("--skip-download", action="store_true",
                    help="reuse this AP's firmware if it is already saved here and verifies")
    ap.add_argument("--yes", action="store_true", help="no questions (still refuses on bad checks)")
    ap.add_argument("--yes-install", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    global VERBOSE, PURPOSE, THIS_RUN, TOTAL_STEPS
    VERBOSE = a.verbose

    if a.list_releases:
        list_cmd()
        return
    if a.fetch:
        fetch_cmd(a)
        return
    interactive = sys.stdin.isatty() and not a.yes
    if not a.host and not a.installer_at and not interactive:
        m.die("give --host (the AP's address), or run this in a terminal and it will look for the AP")
    if a.restore and not a.host and not interactive:
        m.die("--restore needs --host too (the AP to write it to)")
    if a.ramboot and (a.restore or a.install or a.update or a.backup_only or a.verify_only or a.installer_at):
        m.die("--ramboot runs on its own (no install, restore, backup or verify); "
              "it boots OpenWrt into RAM and stops, writing nothing to the eMMC")
    if a.image and not a.ramboot:
        m.die("--image is only for --ramboot (the local .itb to RAM-boot)")
    if a.image and not os.path.exists(a.image):
        m.die("--image %s does not exist" % a.image)
    for flag, value in (("--host", a.host), ("--installer-at", a.installer_at)):
        if value and not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value):
            m.die("%s must be an IPv4 address" % flag)

    run = Run(a.yes, a.yes_install, a.user, a.password)
    work = new_run_dir()
    run.work = work
    run.kexec_dir = a.kexec_dir
    m.LOG.open(os.path.join(work, "run.log"))
    m.LOG.write("arista-field %s  %s  argv=%s\n" % (TOOL_VERSION, time.strftime("%Y-%m-%d %H:%M:%S"),
                                                    [x if p != "--password" else "***"
                                                     for p, x in zip([""] + sys.argv, sys.argv)]))
    tell("OpenWrt for Arista access points -- network installer %s" % TOOL_VERSION)
    ensure_paramiko(interactive)

    # ---- already at a running RAM installer (console netboot, or resume) ----
    if a.installer_at:
        THIS_RUN = run
        pc_ip = a.pc_ip or m.pc_address(a.installer_at)
        tell("")
        tell("Using the RAM installer already running at %s." % a.installer_at)
        con = m.ssh_open(a.installer_at, "root", "", timeout=20)
        text, _ = m.ssh_exec(con, "cat /tmp/sysinfo/board_name 2>/dev/null; "
                                  "awk '$2 == \"/\" {print $3; exit}' /proc/mounts", 30)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        board = lines[0] if lines else "?"
        root = lines[1] if len(lines) > 1 else "?"
        if not port_board(board) or root not in RAM_ROOTS:
            m.die("%s is not a RAM installer (board %r, root %r)" % (a.installer_at, board, root))
        model = model_key(board)
        if model not in MODELS:
            m.die("no entry for board %r" % board)
        if not field_install(model):
            refuse_unsupported(model)
        card = {"model": model, "board": board, "host": a.installer_at,
                "mac": board_mac(con)}   # so the verifier knows which unit to expect
        run.installer = con
        itb, ubi, sums, tag = release_files(a, model)
        tell("  %s (%s), running from RAM; build %s." % (MODELS[model]["name"], card["mac"], short_tag(tag)))
        if not a.yes and not confirm("Install OpenWrt on it now?"):
            tell("Nothing was changed.")
            return
        run.yes = run.yes_install = a.yes = True
        TOTAL_STEPS = 2
        step("Install OpenWrt",
             "Send the image, let the installer check it, then write slot 0.",
             "The installer refuses rather than write a half-checked image.")
        with Heartbeat("Installing"):
            do_install(run, card, a.installer_at, ubi, sums)
        finish_install(a, run, card, a.installer_at.rsplit(".", 1)[0], None, work)
        return

    # ---- which AP ----------------------------------------------------------
    who = {"ip": a.host, "kind": None} if a.host else choose_ap(a)
    who = identify(a, who)
    host = who["ip"]
    tell("")
    tell("%s is a %s%s." % (host, describe(who),
                           (", MAC " + who["mac"]) if who.get("mac") else ""))
    model = who.get("model")
    if model is None:
        m.die("this tool does not know the Arista %s.  Nothing was changed.\n"
              "It knows: %s." % (who["name"], ", ".join(MODELS[k]["name"] for k in sorted(MODELS))))
    mdl = MODELS[model]
    pc_ip = a.pc_ip or m.pc_address(host)
    if pc_ip.startswith("169.254."):
        m.die("this PC has no address towards the AP")
    prefix = host.rsplit(".", 1)[0]
    stock = who["kind"] == "stock"
    no_image = bool(mdl.get("no_image"))        # known and reachable, but no OpenWrt build yet
    can_install = field_install(model) and not no_image
    if who.get("ram"):
        m.die("%s is a RAM installer that is already running.\n"
              "Finish its install with:  --installer-at %s" % (host, host))

    # ---- what to do --------------------------------------------------------
    action = ("ramboot" if a.ramboot else "restore" if a.restore else "backup" if a.backup_only
              else "verify" if a.verify_only else "install" if a.install
              else "update" if a.update else None)
    backup_dir, backup_when = (a.restore, None) if a.restore else saved_backup(model, who.get("mac"))
    if action in ("update",) and stock:
        m.die("--update is for an AP that already runs OpenWrt; this one is on Arista's firmware "
              "(use --install).")
    if action in ("backup", "verify") and not stock:
        m.die("this AP already runs OpenWrt, so there is no factory firmware to read from it.")
    if stock and no_image and action not in ("backup", "verify"):
        m.die("there is no OpenWrt build for the %s yet, so it cannot be installed.\n"
              "Nothing was changed.  Its own firmware can be saved to this PC meanwhile:\n"
              "  python %s --host %s --backup-only"
              % (mdl["name"], os.path.basename(sys.argv[0]) or "arista-field.py", host))
    if stock and not can_install and not no_image:
        refuse_unsupported(model)
    PURPOSE = "update" if (action == "update" or (action is None and not stock)) else "install"
    asked_for = PURPOSE
    rel = resolve_release(a, model) if action not in ("backup", "verify") else None
    ahead = False
    if not stock and PURPOSE == "update" and not a.tag and build_num(who.get("build")) is not None:
        # Never offer an update that goes backwards.  An AP on a test build newer
        # than the accepted release follows the test builds.
        cur = build_num(who["build"])
        if rel and a.channel is None and (build_num(rel.get("tag")) or 0) < cur:
            a.channel = "testing"
            rel = resolve_release(a, model)
        if rel and rel.get("tag") and (build_num(rel["tag"]) or 0) < cur:
            ahead = True
    if rel == {} and (action in ("install", "update") or (action is None and stock)):
        m.die("no published build has the %s%s yet.  Nothing was changed.\n"
              "See what each release carries with --list-releases."
              % (mdl["name"], " on the accepted channel" if a.channel == "stable" else ""))
    if rel == {}:
        tell("No published build has the %s yet%s." % (
            mdl["name"], "" if stock else ", so there is nothing to update it to"))
        rel = None
    if ahead:
        if action == "update":
            m.die("this AP runs %s, which is newer than the newest published build (%s).\n"
                  "Nothing was changed.  To install an older build on purpose, name it with --tag."
                  % (short_tag(who["build"]), short_tag(rel["tag"])))
        tell("It runs a newer build than the newest published one (%s)." % short_tag(rel["tag"]))
        rel = dict(rel, tag=who["build"])          # nothing to offer
    if action is None:
        if not stock and not ahead and rel and rel.get("tag") == who.get("build"):
            tell("It already runs the newest %sbuild." % ("" if rel.get("testing") else "accepted "))
        action = choose_action(who, rel, backup_when, can_install)
    if action == "stop":
        tell("")
        tell("Nothing was changed.")
        return
    if action in ("install", "restore") and not can_install:
        refuse_unsupported(model)
    if action == "restore" and not backup_dir:
        m.die("there is no saved firmware for this AP on this PC.\n"
              "Give the folder with --restore DIR (the one the install printed).")

    # ---- one question, with everything that matters in it -------------------
    PURPOSE = "update" if action == "update" else "install"
    if rel and rel.get("tag") and PURPOSE == asked_for:
        a.tag = rel["tag"]                    # what was shown is what is fetched
    elif PURPOSE != asked_for:
        rel = None                            # the menu changed what the release is for
    if action in ("install", "update", "restore", "ramboot") and not a.yes:
        tell("")
        if action == "install":
            tell("Ready to install OpenWrt, %s, on this %s." % (build_words(rel), mdl["name"]))
            tell("")
            if stock:
                tell("  - Its own firmware is saved to this PC first, so it can be put back.")
            tell("  - Arista's firmware also stays in the AP's second slot as a fallback.")
            tell("  - The AP restarts a few times and serves no Wi-Fi for about 10 minutes.")
            tell("    Keep it powered and plugged in.")
            if mdl.get("unproven"):
                tell("  - Note: %s" % mdl["unproven"])
        elif action == "update":
            tell("Ready to update OpenWrt on this %s to %s." % (mdl["name"], build_words(rel)))
            tell("")
            if not who.get("build"):
                tell("  - This AP does not say which build it runs, so this one may not be newer.")
            tell("  - Settings are kept; packages you added yourself are not.")
            tell("  - The AP restarts once and is back in about 3 minutes.")
        elif action == "restore" and mdl.get("revert") == "bootcmd":
            tell("Ready to switch this %s back to Arista's firmware." % mdl["name"])
            tell("")
            note_lines = mdl.get("revert_note") or ("Only the boot command changes.",)
            tell("  - %s" % note_lines[0])
            for line in note_lines[1:]:
                tell("    %s" % line)
            tell("  - The AP restarts once and is back in about 4 minutes.")
        elif action == "restore":
            tell("Ready to put this %s's own firmware back, from" % mdl["name"])
            tell("  %s" % backup_dir)
            tell("")
            tell("  - OpenWrt and its settings on this AP are replaced.")
            tell("  - The AP restarts a few times; allow about 10 minutes.")
        else:
            tell("Ready to start OpenWrt from memory on this %s.  Nothing is installed;" % mdl["name"])
            tell("a restart brings its own firmware back.")
        if rel and rel.get("newer") and action in ("install", "update"):
            tell("  - A newer test build exists (%s); add --channel testing to take it."
                 % short_tag(rel["newer"]))
        if os.name == "nt" and stock:
            tell("  - If Windows asks whether Python may accept connections, allow it.")
        tell("")
        if not confirm("Go ahead?"):
            tell("Nothing was changed.")
            return
    run.yes = run.yes_install = a.yes = True       # asked once; not again further down
    ap_ip = a.ap_ip or ap_address(host, pc_ip)
    THIS_RUN = run

    # ---- update: OpenWrt's own sysupgrade ----------------------------------
    if action == "update":
        TOTAL_STEPS = 2 if a.no_wait else 3
        step("Get the OpenWrt build",
             "Download the image and check it against the release's checksums.",
             "Check this PC's internet connection; the AP has not been touched.")
        d, tag = m.fetch_release(a.tag, [model], os.path.abspath(a.files), a.channel) \
            if not a.offline else (None, None)
        if d is None:
            m.die("--update needs the release from GitHub (it is not combined with --offline)")
        image = os.path.join(d, m.files_for(model)["sysupgrade"])
        tell("     %s, checked" % short_tag(tag))
        step("Update OpenWrt",
             "Send the image, let the AP's own sysupgrade check it, then let it write.",
             "Nothing is written unless the AP's own check accepts the image.")
        with Heartbeat("Sending and checking the image"):
            who["user"], who["password"] = who.get("user") or "root", who.get("password") or ""
            keep = do_update(run, who, image)
        tell("     accepted by the AP; it is writing and will restart%s"
             % ("" if keep else " with default settings (older 25.12 port)"))
        if a.no_wait:
            tell("")
            tell("Done.  The AP is back in about 3 minutes.")
            return
        step("Wait for the AP to start",
             "The AP writes the image and restarts; then it is asked what it runs.",
             "Give it a few more minutes, then look for it at the same address.")
        now = None
        with Heartbeat("Waiting for the AP to restart"):
            wait_gone(host, 240)
            end = time.time() + 480
            while time.time() < end and not now:
                time.sleep(5)
                now = _probe_openwrt(host, who["user"], who["password"], 5)
                if not now and not who["password"]:
                    ip2, _b = wait_openwrt(prefix, who.get("mac"), 20)
                    now = _probe_openwrt(ip2, "root", "", 5) if ip2 else None
        tell("")
        if now and now.get("build") == tag:
            tell("Done.  This %s now runs OpenWrt %s, at %s." % (mdl["name"], short_tag(tag), now["ip"]))
        elif now:
            tell("The AP is back at %s, on %s." % (now["ip"], describe(now)))
            tell("That is not the build that was sent; the log has the detail.")
        else:
            tell("The image was accepted and written, but the AP has not answered yet.")
            tell("Give it a few minutes; if it still does not answer, power-cycle it.")
        tell("Log: %s" % log_path())
        return

    # ---- back to Arista where the install never replaced it -----------------
    if action == "restore" and mdl.get("revert") == "bootcmd":
        if stock:
            tell("")
            tell("This AP already runs Arista's firmware.  Nothing was changed.")
            return
        TOTAL_STEPS = 1 if a.no_wait else 2
        step("Put Arista's boot command back",
             "One boot-command change; Arista's firmware is still in its own flash.",
             "Nothing else was changed; OpenWrt still starts if the change was not made.")
        with Heartbeat("Changing the boot command"):
            who["user"], who["password"] = who.get("user") or "root", who.get("password") or ""
            do_restore_bootcmd(run, who, mdl)
        tell("     done; the AP is restarting on Arista's firmware")
        if a.no_wait:
            report_writes(run)
            tell("Log: %s" % log_path())
            return
        step("Wait for Arista's firmware to start",
             "The AP restarts from its own slot; it is found again by its MAC address.",
             "Give it a few more minutes; Arista's firmware takes about 3 minutes to start.")
        with Heartbeat("Waiting for Arista's firmware"):
            back = find_stock(prefix, 420, mac=who.get("mac"))
        tell("")
        if back and back[0]:
            tell("Done.  This %s runs Arista's firmware again, at %s." % (mdl["name"], back[0]))
        else:
            tell("The boot command is Arista's again, but the AP has not answered yet.")
            tell("Give it a few minutes; it may have taken a new address.")
        report_writes(run)
        tell("Log: %s" % log_path())
        return

    # ---- everything else starts from a RAM installer ------------------------
    manifest = None
    itb = ubi = sums = tag = None
    TOTAL_STEPS = {"install": 6 if stock else 5, "restore": 5, "backup": 2, "verify": 1,
                   "ramboot": 1}[action] - (1 if a.no_wait and action in ("install", "restore") else 0)
    if action in ("install", "restore"):
        step("Get the OpenWrt build" if action == "install" else "Get the installer",
             "Download the release files and check them, before the AP is touched.",
             "Check this PC's internet connection; the AP has not been touched.")
        itb, ubi, sums, tag = release_files(a, model)
        if stock and mdl.get("ram_boot") == "kexec":
            ensure_kexec(run, model)
        tell("     %s, checked against its published checksums" % short_tag(tag))
    if action == "restore":
        manifest = load_manifest(backup_dir)
        if who.get("mac") and manifest.get("mac") and who["mac"] != manifest["mac"]:
            m.die("that saved firmware belongs to the AP with MAC %s, and this one is %s.\n"
                  "Nothing was changed." % (manifest["mac"], who["mac"]))
    if stock:
        check_ports(tftp=action in ("install", "restore") and mdl.get("ram_boot") != "kexec",
                    sink=action in ("install", "backup"))

    step("Connect to the AP%s" % (" and check it" if stock else ""),
         "Log in, get a root shell, and check the model, firmware, flash layout and boot variables.",
         "Nothing was written.  If the AP was restarting, wait two minutes and run this again.")
    with Heartbeat("Logging in to %s" % host):
        kind, shell, card = connect(host, a.user, a.password)
    run.shell = shell
    card["host"] = host
    if (kind == "stock") != stock:
        m.die("the AP changed what it runs while this tool was looking at it; run it again.")

    if action == "ramboot":
        do_ramboot(run, a, card, kind, pc_ip, ap_ip)
        tell("Log: %s" % log_path())
        return

    if stock:
        with Heartbeat("Checking the AP"):
            verify_stock(shell, card)
        tell("     %s, firmware %s, MAC %s; laid out as expected"
             % (mdl["name"], card.get("version", "?"), card.get("mac", "?")))
        if manifest and card.get("mac") and manifest.get("mac") and card["mac"] != manifest["mac"]:
            m.die("that saved firmware belongs to the AP with MAC %s, and this one is %s.\n"
                  "Nothing was changed." % (manifest["mac"], card["mac"]))
        if action == "verify":
            pty_clean(shell, host, wait=False)
            tell("")
            tell("This AP matches what the tool expects.  Nothing was written to it.")
            tell("It restarts now, as Arista's firmware does when this kind of session ends.")
            return
        if action in ("install", "backup"):
            step("Save this AP's own firmware",
                 "Read every partition off the AP and check it here, before anything changes.",
                 "Free space on this PC, and allow TCP 8069 in from the AP (the firewall).\nThe AP is unchanged.")
            reused = None
            if a.skip_download:
                with Heartbeat("Checking the firmware already saved here"):
                    reused, m2 = newest_backup(card["model"], card["mac"])
                if reused:
                    manifest, backup_dir = m2, reused
            if not reused:
                backup_dir = a.backup_dir or os.path.join(
                    BACKUPS, "%s-%s" % (card["model"], card["mac"].replace(":", "")),
                    time.strftime("%Y%m%d-%H%M%S"))
                with Heartbeat("Saving the firmware"):
                    manifest = download_firmware(run, shell, card, backup_dir,
                                                 include_slot1=not a.fast, pc_ip=pc_ip)
            in_use = sum(1 for p in manifest["partitions"] if p.get("live"))
            tell("     %d partitions, each checked against the AP's own hash%s%s"
                 % (len(manifest["partitions"]), " (saved earlier)" if reused else "",
                    "" if not in_use else
                    ";\n     %d in use by the running firmware, kept as read" % in_use))
            tell("     kept in %s" % backup_dir)
            if action == "backup":
                pty_clean(shell, host, wait=False)
                tell("")
                tell("Done.  This AP's firmware is saved; nothing on the AP was changed.")
                tell("It restarts now, as Arista's firmware does when this kind of session ends.")
                tell("Log: %s" % log_path())
                return
    else:
        card.setdefault("mac", who.get("mac"))

    if kind == "stock" and mdl.get("ram_boot") == "kexec":
        why = "Arista's running kernel loads a RAM-only installer and jumps to it; nothing is written."
        hint = "If the jump fails the AP restarts on its own firmware; just run the tool again."
    elif kind == "openwrt" and mdl.get("openwrt_ram_boot") == "slot0":
        why = "The installer replaces OpenWrt's kernel in slot 0 and U-Boot boots it from there."
        hint = "If it does not come up, U-Boot falls back to Arista in slot 1; run the tool again."
    else:
        why = "One boot-command change fetches a RAM-only installer, with Arista kept as the fallback."
        hint = ("The AP fetches the installer from this PC over TFTP (UDP 69): allow it through\n"
                "this PC's firewall.  The AP has gone back to its own firmware; run the tool again.")
    step("Start the installer", why, hint)
    with Heartbeat("Starting the installer on the AP"):
        boot_installer(run, card, pc_ip, ap_ip, itb, kind)
    with Heartbeat("Waiting for the installer to answer"):
        con, ip, board, root = wait_installer(a, prefix, mac=card.get("mac"))
    if con is None:
        m.die("the installer never answered.\n"
              "It takes its address from DHCP on the AP's network port, so that network\n"
              "needs a DHCP server.  The AP falls back to its own firmware by itself.")
    run.installer = con
    tell("     running at %s" % ip)

    if action == "install":
        step("Install OpenWrt",
             "Send the image, let the installer check it, then write slot 0.",
             "The installer refuses rather than write a half-checked image, and this AP\nstill boots its own firmware.")
        with Heartbeat("Installing"):
            do_install(run, card, ip, ubi, sums)
        tell("     written and checked")
        finish_install(a, run, card, prefix, backup_dir, work)
        return

    step("Put this AP's own firmware back",
         "Write the saved slot 0 and boot environment back.",
         "Slot 1 and the other partitions are left as found; run the tool again.")
    with Heartbeat("Writing the saved firmware back"):
        do_restore(run, card, manifest, backup_dir)
    tell("     written and read back")
    ip2 = text = None
    if not a.no_wait:
        step("Wait for the AP to start",
             "The AP restarts on Arista's firmware and is asked for its version.",
             "Give it a few more minutes; it returns at its usual address.")
        with Heartbeat("Waiting for Arista's firmware to start"):
            ip2, text = find_stock(prefix, 480)
    tell("")
    if ip2:
        info = parse_device_info(text or "")
        tell("Done.  This %s is back on Arista's firmware%s, at %s."
             % (mdl["name"], (" " + info["firmware"]) if info["firmware"] else "", ip2))
    elif a.no_wait:
        tell("Done.  The AP is restarting on Arista's firmware.")
    else:
        tell("The firmware was written back, but the AP has not answered yet.")
        tell("Give it a few minutes; if it still does not answer, power-cycle it.")
    report_writes(run)
    tell("Log: %s" % log_path())


def report_writes(run):
    say("Environment writes this run: %d  (%s)"
        % (len(run.env_writes), ", ".join(run.env_writes) or "none"))
    say("Flash writes this run:      %d  (%s)"
        % (len(run.flash_writes), ", ".join(run.flash_writes) or "none"))


def finish_install(a, run, card, prefix, backup_dir, work):
    """After the write: wait for OpenWrt, and say what to do next."""
    name = MODELS[card["model"]]["name"]
    ip2 = None
    if not a.no_wait:
        step("Wait for OpenWrt to start",
             "The AP restarts from its flash; it is found again by its MAC address.",
             "OpenWrt was written.  Give it a few more minutes, then look for the name\n"
             "OpenWrt on your router's list of clients.")
        with Heartbeat("Waiting for OpenWrt to start"):
            ip2, _board = wait_openwrt(prefix, card.get("mac"))
    tell("")
    if ip2:
        tell("Done.  OpenWrt is running on this %s, at %s." % (name, ip2))
        tell("")
        tell("  Next:  open http://%s in a browser (user root, no password yet --" % ip2)
        tell("         set one first).  Wi-Fi is off until you turn it on there.")
    else:
        tell("OpenWrt is installed on this %s%s" % (name, "." if a.no_wait else
                                                    ", but it has not answered yet."))
        tell("")
        tell("  Next:  find it on your router's list of clients (name OpenWrt, MAC %s)"
             % (card.get("mac") or "on its label"))
        tell("         and open that address in a browser.  If it does not appear within")
        tell("         ten minutes, power-cycle it; if OpenWrt cannot start, the AP")
        tell("         falls back to Arista's firmware by itself.")
    if backup_dir:
        tell("  Kept:  this AP's original firmware, in")
        tell("         %s" % backup_dir)
        tell("         Run this tool again to put it back.")
    report_writes(run)
    tell("  Log:   %s" % log_path())


def load_manifest(d):
    p = os.path.join(d, "manifest.json")
    if not os.path.exists(p):
        m.die("no manifest.json in %s" % d)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def run_main():
    """main(), with the two endings nobody should meet as a traceback."""
    try:
        main()
    except KeyboardInterrupt:
        with _OUT:
            _clear_status()
        tell("")
        tell("Stopped (Ctrl-C).")
        written = (THIS_RUN.env_writes + THIS_RUN.flash_writes) if THIS_RUN is not None else []
        if written:
            tell("   Written so far: %s" % ", ".join(written))
            tell("   Run the tool again to finish, or to put this AP's firmware back.")
        else:
            tell("   Nothing was written to the AP.")
        if log_path():
            tell("   Full detail: %s" % log_path())
        try:
            _cleanup_stock()    # while the session to the AP is still open
        except Exception:
            pass
        sys.exit(130)
    except Exception as e:                            # noqa: BLE001
        import traceback
        if getattr(LOG, "f", None):
            LOG.write("\n" + traceback.format_exc())
        die("something unexpected happened inside the tool (%s: %s).\n"
            "This is a fault in the tool, not something you did.  Please open an issue\n"
            "with the log file named below." % (type(e).__name__, e))


if __name__ == "__main__":
    run_main()


