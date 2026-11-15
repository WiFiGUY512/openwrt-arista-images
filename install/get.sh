#!/bin/sh
# OpenWrt for Arista access points: download the network installer and run it.
#
#   curl -fsSL https://raw.githubusercontent.com/WiFiGUY512/openwrt-arista-images/main/install/get.sh | sh
#
# It keeps everything in one folder, ~/arista-openwrt: the installer, its own
# private copy of the one Python library it needs, the firmware it saves from
# each AP (backups/) and its logs (logs/).  Nothing else on this computer
# changes.  Run the same line again later to get the newest installer.
#
# Options for the installer go after "sh -s --":
#   curl -fsSL .../get.sh | sh -s -- --host 192.168.1.50

set -eu
base="${ARISTA_OPENWRT_BASE:-https://raw.githubusercontent.com/WiFiGUY512/openwrt-arista-images/main/install}"
dir="${ARISTA_OPENWRT_DIR:-$HOME/arista-openwrt}"

py=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
        py="$c"
        break
    fi
done
if [ -z "$py" ]; then
    echo
    echo "This needs Python 3.8 or newer, and this computer has none."
    echo "  Debian/Ubuntu:  sudo apt install python3 python3-venv"
    echo "  Fedora:         sudo dnf install python3"
    echo "  macOS:          install it from https://www.python.org/downloads/"
    echo "Then run the same line again."
    exit 1
fi

mkdir -p "$dir/backups" "$dir/releases"
vpy="$dir/venv/bin/python"
if [ ! -x "$vpy" ]; then
    echo "Setting up (once, about a minute)..."
    if ! "$py" -m venv "$dir/venv" >/dev/null 2>&1; then
        rm -rf "$dir/venv"
        # Debian and Ubuntu ship Python without venv.  If the system's Python
        # already has the library, that will do; otherwise say what to install.
        if "$py" -c 'import paramiko' 2>/dev/null; then
            vpy="$(command -v "$py")"
        else
            echo
            echo "One more package is needed first.  On Debian or Ubuntu, either of:"
            echo "  sudo apt install python3-venv"
            echo "  sudo apt install python3-paramiko"
            echo "Then run the same line again."
            exit 1
        fi
    fi
fi
if ! "$vpy" -c 'import paramiko' 2>/dev/null; then
    if ! "$vpy" -m pip install --quiet --disable-pip-version-check paramiko; then
        echo "Could not download the SSH library (paramiko). Is this computer online?"
        exit 1
    fi
fi

tool="$dir/arista-field.py"
if curl -fsSL "$base/arista-field.py" -o "$tool.new" && [ "$(wc -c < "$tool.new")" -gt 100000 ]; then
    mv -f "$tool.new" "$tool"
else
    rm -f "$tool.new"
    if [ ! -f "$tool" ]; then
        echo "Could not download the installer. Is this computer online?"
        exit 1
    fi
    echo "Could not fetch the newest installer; using the copy already here."
fi

# Linux lets only an administrator open UDP 69, which the AP's boot loader
# fetches the installer from (the C-230, C-260 and C-360 need it; the C-460
# does not).  The tool says so itself if it comes to that; this is the command.
again="$vpy $tool"
if [ "$(uname -s)" = "Linux" ] && [ "$(id -u)" != "0" ]; then
    again="sudo $vpy $tool"
fi

echo
# Piped from curl, this script's own input is the script: the questions need
# the terminal.
if [ -t 0 ]; then
    "$vpy" "$tool" "$@" || true
elif [ -r /dev/tty ]; then
    "$vpy" "$tool" "$@" < /dev/tty || true
else
    echo "There is no terminal to ask questions on.  Run:  $again"
    exit 1
fi
echo
echo "To run it again:  $again"
