#!/usr/bin/env bash
# Xvfb for display :0, python3-xlib for the bridge, and the real xclip that
# Claude Code and pi call. wl-clipboard is deliberately not installed, so both
# take the xclip path.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends xvfb xauth python3-xlib xclip
rm -rf /var/lib/apt/lists/*
install -d -o root -g root -m 1777 /tmp/.X11-unix
