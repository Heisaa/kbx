#!/usr/bin/env bash
# Runs at `kbx build`, as root: a PulseAudio server that is the sandbox's
# default ALSA device, alsa-utils for Claude's `arecord`, and a fixed copy of
# the ALSA pulse plugin.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
plugins_version=1.2.12
plugins_sha256=7bd8a83d304e8e2d86a25895d8dcb0ef0245a8df32e271959cdbdc6af39b66f2
plugin_dir="/usr/lib/$(gcc -print-multiarch)/alsa-lib"
installed() { dpkg-query -W -f='${Package}\n' | sort; }

apt-get update
apt-get install -y --no-install-recommends pulseaudio pulseaudio-utils alsa-utils libasound2-plugins

# pulse_delay() compares pa_stream_get_latency()'s negative error code with a
# positive PA_ERR_NODATA, so snd_pcm_delay() fails with EIO until the stream's
# first timing update arrives. cpal (Codex voice) treats that as a lost device
# when Codex reopens the speaker. Rebuild the PCM plugin with the sign fixed.
before="$(installed)"
apt-get install -y --no-install-recommends libasound2-dev libpulse-dev
build_deps="$(comm -13 <(echo "$before") <(installed))"
work="$(mktemp -d)"
curl -fsSL -o "$work/plugins.tar.bz2" \
  "https://www.alsa-project.org/files/pub/plugins/alsa-plugins-${plugins_version}.tar.bz2"
echo "${plugins_sha256}  $work/plugins.tar.bz2" | sha256sum -c -
tar -xjf "$work/plugins.tar.bz2" -C "$work"
src="$work/alsa-plugins-${plugins_version}/pulse"
grep -q 'if (err != PA_ERR_NODATA) {' "$src/pcm_pulse.c"
sed -i 's/if (err != PA_ERR_NODATA) {/if (err != -PA_ERR_NODATA) {/' "$src/pcm_pulse.c"
dpkg-divert --local --rename --add "$plugin_dir/libasound_module_pcm_pulse.so"
# shellcheck disable=SC2046 # pkg-config prints separate flags
gcc -O2 -fPIC -shared -DPIC -o "$plugin_dir/libasound_module_pcm_pulse.so" \
  "$src/pcm_pulse.c" "$src/pulse.c" $(pkg-config --cflags --libs alsa libpulse)
rm -rf "$work"
# shellcheck disable=SC2086 # one package per word
apt-get purge -y $build_deps
rm -rf /var/lib/apt/lists/*

install -m 0644 "$KBX_MODULE_DIR/build/asound.conf" /etc/asound.conf
install -d /etc/pulse/client.conf.d
install -m 0644 "$KBX_MODULE_DIR/build/client.conf" /etc/pulse/client.conf.d/50-kbx-voice.conf
pulseaudio --version
arecord --version
