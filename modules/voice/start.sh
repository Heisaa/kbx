#!/bin/sh
# Runs at every start, as root, before the PulseAudio service: a fresh
# directory for its socket and FIFOs (/run is cleared at boot).
set -eu
rm -rf /run/kbx-voice
install -d -o agent -g agent -m 0700 /run/kbx-voice
