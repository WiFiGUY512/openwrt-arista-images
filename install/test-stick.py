"""Checks for --make-stick: the C-360's installer files onto a USB drive.
No AP, no network and no real drive: a release is faked in a temp folder.

    python test-stick.py

What has to hold: the drive gets exactly the four files arista-c360-install
and U-Boot expect, under c360/; SHA256SUMS carries the RAM installer's sum and
the release's install sums unchanged (factory.ubi and the kernel and rootfs
volumes inside it); the U-Boot line in README.txt is the C-360's (USB power on
first, its load address, its FIT configuration); and nothing is copied when the
release's files do not match their checksums.
"""
import hashlib
import importlib.util
import io
import os
import sys
import tempfile
import types

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


class Died(Exception):
    pass


OUT = []
af.tell = lambda msg="": OUT.append(str(msg))
af.say = lambda msg="": None
af.status = lambda text, log=True: None


def die(msg, hint=None):
    OUT.append("DIED: %s" % msg)
    raise Died(str(msg))


af.die = die

TAG = "v0-test"
NAMES = af.files_for("c360", "install")
ITB, UBI = b"itb" * 5000, b"ubi" * 70000
VOLS = ["%s volume:kernel 6348800" % ("a" * 64), "%s volume:rootfs 13967360" % ("b" * 64)]


def fake_release(ubi_sum=None, vols=VOLS):
    d = tempfile.mkdtemp(prefix="af-stick-rel-")
    with open(os.path.join(d, NAMES["itb"]), "wb") as f:
        f.write(ITB)
    with open(os.path.join(d, NAMES["ubi"]), "wb") as f:
        f.write(UBI)
    with open(os.path.join(d, NAMES["sums"]), "w", newline="\n") as f:
        f.write("%s  factory.ubi\n" % (ubi_sum or hashlib.sha256(UBI).hexdigest()))
        for v in vols:
            f.write(v + "\n")
    return d


def run(dest, rel):
    del OUT[:]
    af.fetch_release = lambda tag, models, cache, channel="stable": (rel, TAG)
    a = types.SimpleNamespace(make_stick=dest, tag=None, files=tempfile.mkdtemp(), channel=None)
    try:
        af.make_stick_cmd(a)
        return True
    except Died:
        return False


# --- the happy path ------------------------------------------------------------------
drive = tempfile.mkdtemp(prefix="af-stick-drive-")
ok = run(drive, fake_release())
c360 = os.path.join(drive, "c360")
check("it finishes", ok, OUT[-1] if OUT else "")
check("the drive holds exactly the four files, under c360/",
      sorted(os.listdir(c360)) == ["README.txt", "SHA256SUMS", "factory.ubi", "initramfs.itb"]
      and os.listdir(drive) == ["c360"], os.listdir(drive))
check("the RAM installer and factory.ubi are byte for byte the release's",
      open(os.path.join(c360, "initramfs.itb"), "rb").read() == ITB
      and open(os.path.join(c360, "factory.ubi"), "rb").read() == UBI)
sums = io.open(os.path.join(c360, "SHA256SUMS"), newline="").read()
check("SHA256SUMS: the RAM installer, factory.ubi, then the two volumes, with Unix line ends",
      sums == "%s  initramfs.itb\n%s  factory.ubi\n%s\n%s\n"
      % (hashlib.sha256(ITB).hexdigest(), hashlib.sha256(UBI).hexdigest(), VOLS[0], VOLS[1]), repr(sums[:200]))
readme = open(os.path.join(c360, "README.txt"), "rb").read()
text = readme.decode()
check("README.txt has DOS line ends (it is read on any PC)", b"\r\n" in readme and b"\n" not in readme.replace(b"\r\n", b""))
check("its U-Boot line powers the USB port before 'usb start'",
      text.index("mw.l 0x0101D004 0x2; mw.l 0x0101D000 0x201;") < text.index("usb start"))
check("it loads c360/initramfs.itb at the C-360's address and boots its configuration",
      "fatload usb 0:1 0xA0000000 c360/initramfs.itb" in text and "bootm 0xA0000000#config@hk09" in text)
check("it sets fdt_high below U-Boot's own device tree, and saves nothing",
      "setenv fdt_high 0x4A3E0000" in text and "saveenv\r\n" not in text.replace("never type saveenv", ""))
check("it names the release and the installer command", TAG in text and "arista-c360-install" in text)
said = "\n".join(OUT)
check("the ending says the console cable is still needed and that no AP was installed from a stick",
      "console cable" in said and "No AP has been installed" in said)
check("it says how many backups fit", "Free space" in said)

# --- a second run over the same drive replaces the files -----------------------------
ok = run(drive, fake_release())
check("a second run over the same drive finishes too", ok)

# --- what must stop it ----------------------------------------------------------------
empty = tempfile.mkdtemp(prefix="af-stick-drive-")
ok = run(empty, fake_release(ubi_sum="0" * 64))
check("factory.ubi not matching the install sums: stops, nothing copied",
      not ok and not os.path.exists(os.path.join(empty, "c360")), os.listdir(empty))
ok = run(empty, fake_release(vols=VOLS[:1]))
check("install sums without both volumes: stops, nothing copied",
      not ok and not os.path.exists(os.path.join(empty, "c360")))
ok = run(os.path.join(empty, "no-such-drive"), fake_release())
check("a drive that is not there: stops before any download", not ok and "is not there" in "\n".join(OUT))

real_fs = af.stick_filesystem
af.stick_filesystem = lambda path: "exFAT"
ok = run(empty, fake_release())
check("a drive formatted exFAT: stops, says FAT32, nothing copied",
      not ok and "FAT32" in "\n".join(OUT) and not os.path.exists(os.path.join(empty, "c360")))
af.stick_filesystem = lambda path: "FAT32"
ok = run(empty, fake_release())
check("a drive formatted FAT32: goes ahead", ok)
af.stick_filesystem = real_fs
check("a plain folder is not asked for its file system", af.stick_filesystem(os.path.join(empty, "c360")) is None)

print()
if FAILED:
    print("%d FAILED: %s" % (len(FAILED), "; ".join(FAILED)))
    sys.exit(1)
print("all stick checks pass")
