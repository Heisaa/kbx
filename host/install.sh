#!/usr/bin/env bash
# kbx host installer: check everything kbx needs, install what is missing.
#
#   host/install.sh [--check] [--yes] [--skip STEP[,STEP…]] [--kata-version vX.Y.Z]
#                   [--kata-runtime go|rs] [--hypervisor clh|qemu] [--bridge NAME] [--build]
#
#   --check        only report; change nothing (exit 1 if something required is missing)
#   --yes          do not ask before installing or restarting anything
#   --skip         any of: packages, docker, kata, runtime, firewall, clipboard, link
#   --kata-runtime go (default: the Go runtime with `kata-runtime check`) or rs
#                  (runtime-rs, the Rust runtime Kata 4 ships in kata-static)
#   --build        run `kbx build` at the end
#
# Supported package managers: pacman (Arch, Manjaro, EndeavourOS), apt (Debian,
# Ubuntu, Mint), dnf (Fedora, RHEL-likes), zypper (openSUSE). Kata Containers is
# installed from the upstream release tarball on every distribution. Run as your
# normal user; privileged steps use sudo.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
checkout="$(dirname "$here")"
check_only=false
assume_yes=false
skip=","
kata_version="${KATA_VERSION:-latest}"
hypervisor=clh
kata_runtime=go
bridge=""
run_build=false

while [ $# -gt 0 ]; do
  case "$1" in
    --check) check_only=true; shift ;;
    --yes | -y) assume_yes=true; shift ;;
    --skip) skip=",${2:?},"; shift 2 ;;
    --kata-version) kata_version="${2:?}"; shift 2 ;;
    --hypervisor) hypervisor="${2:?}"; shift 2 ;;
    --kata-runtime) kata_runtime="${2:?}"; shift 2 ;;
    --bridge) bridge="${2:?}"; shift 2 ;;
    --build) run_build=true; shift ;;
    -h | --help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install.sh: unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
done
case "$hypervisor" in clh | qemu) ;; *) echo "install.sh: --hypervisor must be clh or qemu" >&2; exit 2 ;; esac
case "$kata_runtime" in go | rs) ;; *) echo "install.sh: --kata-runtime must be go or rs" >&2; exit 2 ;; esac

# --- output helpers ---

if [ -t 1 ]; then bold=$'\e[1m' green=$'\e[32m' yellow=$'\e[33m' red=$'\e[31m' reset=$'\e[0m'
else bold="" green="" yellow="" red="" reset=""; fi
missing_required=0
step() { printf '\n%s== %s%s\n' "$bold" "$1" "$reset"; }
ok() { printf '  %s✓%s %s\n' "$green" "$reset" "$1"; }
warn() { printf '  %s⚠%s %s\n' "$yellow" "$reset" "$1"; }
bad() { printf '  %s✗%s %s\n' "$red" "$reset" "$1"; missing_required=$((missing_required + 1)); }
skipped() { case "$skip" in *",$1,"*) return 0 ;; *) return 1 ;; esac; }
have() { command -v "$1" >/dev/null 2>&1; }
confirm() { # question
  $assume_yes && return 0
  [ -t 0 ] || { warn "not a terminal: pass --yes to $1"; return 1; }
  local answer
  read -r -p "  → $1? [Y/n] " answer
  case "$answer" in "" | [Yy]*) return 0 ;; *) return 1 ;; esac
}
as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

# --- distribution and packages ---

# shellcheck disable=SC1091 # provided by the distribution
. /etc/os-release 2>/dev/null || true
distro_ids=" ${ID:-} ${ID_LIKE:-} "
pm=""
case "$distro_ids" in
  *" arch "* | *" manjaro "* | *" endeavouros "*) pm=pacman ;;
  *" debian "* | *" ubuntu "*) pm=apt ;;
  *" fedora "* | *" rhel "* | *" centos "*) pm=dnf ;;
  *" suse "* | *" opensuse "* | *" sles "*) pm=zypper ;;
esac
have "$pm" 2>/dev/null || pm=""

# Logical dependency → package name(s) for each package manager ("-" = not packaged).
package_for() { # dependency
  case "$pm:$1" in
    pacman:python) echo python ;;         apt:python | dnf:python | zypper:python) echo python3 ;;
    *:git) echo git ;;
    *:curl) echo curl ;;
    *:jq) echo jq ;;
    *:zstd) echo zstd ;;
    *:iptables) echo iptables ;;
    pacman:docker | zypper:docker) echo docker ;;
    apt:docker) echo docker.io ;;
    dnf:docker) echo moby-engine ;;
    *:buildx) echo docker-buildx ;;
    *:wl-clipboard) echo wl-clipboard ;;
    *:xclip) echo xclip ;;
    *:clipnotify) echo - ;;
    *) echo - ;;
  esac
}

# A second package to try when the first did not provide the command.
fallback_for() { # dependency
  case "$pm:$1" in
    apt:docker) echo docker-cli ;; # Debian splits the CLI out of docker.io
    *) echo - ;;
  esac
}

refreshed=false
pkg_install() { # packages…
  [ $# -gt 0 ] || return 0
  case "$pm" in
    pacman)
      # Arch does not support partial upgrades: if the sync database is stale or
      # missing, the only safe retry is a full upgrade.
      as_root pacman -S --needed --noconfirm "$@" ||
        { confirm "sync and upgrade the system (pacman -Syu), then retry" &&
          as_root pacman -Syu --needed --noconfirm "$@"; } ;;
    apt)
      $refreshed || { as_root apt-get update -qq; refreshed=true; }
      as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@" ;;
    dnf) as_root dnf install -y -q "$@" ;;
    zypper) as_root zypper --non-interactive install --no-recommends "$@" ;;
    *) return 1 ;;
  esac
}

# Install each missing dependency; a required one still missing counts as a problem.
# Always returns 0: problems are counted, and the run continues to report them all.
ensure() { # required(yes|no) command dependency [hint]
  local required="$1" command="$2" dep="$3" hint="${4:-}" pkg
  if have "$command"; then ok "$dep ($command)"; return 0; fi
  pkg="$(package_for "$dep")"
  if $check_only || skipped packages || [ -z "$pm" ] || [ "$pkg" = - ]; then
    local why="missing"
    [ "$pkg" = - ] && [ -n "$pm" ] && why="missing (not in the $pm repositories)"
    if [ "$required" = yes ]; then bad "$dep: $why${hint:+. $hint}"; else warn "$dep: $why${hint:+. $hint}"; fi
    return 0
  fi
  local fallback
  fallback="$(fallback_for "$dep")"
  if confirm "install $dep ($pkg with $pm)" && { pkg_install "$pkg" || true; } &&
    { have "$command" || { [ "$fallback" != - ] && pkg_install "$fallback" && have "$command"; }; }; then
    ok "$dep installed"
  elif [ "$required" = yes ]; then
    bad "$dep could not be installed${hint:+. $hint}"
  else
    warn "$dep not installed${hint:+. $hint}"
  fi
}

echo "${bold}kbx host installer${reset} — ${PRETTY_NAME:-unknown distribution}, package manager: ${pm:-none found}"
$check_only && echo "(check only: nothing will be changed)"
[ -n "$pm" ] || warn "no supported package manager; missing packages must be installed by hand"
if [ "$(uname -s)" != Linux ]; then echo "kbx needs Linux (Kata needs KVM)" >&2; exit 1; fi

# --- 1. virtualization ---

step "KVM"
if [ -e /dev/kvm ]; then
  ok "/dev/kvm present"
else
  module=""
  grep -q -m1 vmx /proc/cpuinfo && module=kvm_intel
  grep -q -m1 svm /proc/cpuinfo && module=kvm_amd
  if [ -z "$module" ]; then
    bad "no vmx/svm CPU flag: enable virtualization (VT-x/AMD-V) in the firmware, or nested virtualization if this is a VM"
  elif ! $check_only && confirm "load the $module kernel module" && as_root modprobe "$module" && [ -e /dev/kvm ]; then
    ok "/dev/kvm present after loading $module"
  else
    bad "/dev/kvm missing (try: sudo modprobe $module)"
  fi
fi
virt="$(systemd-detect-virt 2>/dev/null)" || true # prints "none" and exits 1 on bare metal
virt="${virt:-none}"
[ "$virt" = none ] || warn "this host is a VM ($virt): nested KVM must be enabled on the outer hypervisor"
for module in vhost_vsock vhost_net; do
  if lsmod 2>/dev/null | grep -q "^$module "; then
    ok "$module loaded"
  elif ! $check_only && as_root modprobe "$module" 2>/dev/null; then
    ok "$module loaded"
    as_root sh -c "printf '%s\n' vhost_vsock vhost_net > /etc/modules-load.d/kbx-kata.conf"
  else
    warn "$module not loaded (Kata needs vsock; sudo modprobe $module)"
  fi
done

# --- 2. packages ---

step "Packages"
ensure yes python3 python "Python 3.11+"
if have python3; then
  if python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    ok "python3 is $(python3 -c 'import platform; print(platform.python_version())') (3.11+)"
  else
    bad "python3 is $(python3 -c 'import platform; print(platform.python_version())'); kbx needs 3.11+ as \`python3\`"
  fi
fi
ensure yes git git
ensure yes curl curl
ensure yes jq jq
ensure yes zstd zstd "needed to unpack the Kata release"
ensure yes iptables iptables "needed by Docker and the kbx firewall"

# --- 3. Docker ---

step "Docker Engine"
if ! skipped docker; then
  ensure yes docker docker "or install Docker Engine from https://docs.docker.com/engine/install/"
  if have docker; then
    if docker buildx version >/dev/null 2>&1; then
      ok "buildx (BuildKit) available"
    else
      if [ -n "$pm" ] && ! $check_only && ! skipped packages && confirm "install docker-buildx"; then
        pkg_install "$(package_for buildx)" || true
      fi
      if docker buildx version >/dev/null 2>&1; then
        ok "buildx available"
      else
        bad "docker buildx missing: kbx build needs BuildKit (RUN --mount)"
      fi
    fi
    if have systemctl && ! systemctl is-active --quiet docker 2>/dev/null; then
      if ! $check_only && confirm "enable and start docker.service"; then
        as_root systemctl enable --now docker.service && ok "docker.service started"
      else
        bad "docker.service is not running"
      fi
    fi
    if [ "$(id -u)" -eq 0 ]; then
      warn "running as root: run this script as the user who will use kbx to check Docker access"
    elif docker info >/dev/null 2>&1; then
      ok "$(id -un) can use Docker (server $(docker version --format '{{.Server.Version}}' 2>/dev/null))"
    else
      warn "$(id -un) cannot talk to the Docker daemon"
      echo "     Membership in the docker group is root-equivalent on this host. kbx"
      echo "     runs docker as your user, so it needs it (or rootful access another way)."
      if ! $check_only && confirm "add $(id -un) to the docker group (log out and in afterwards)"; then
        as_root usermod -aG docker "$(id -un)" && warn "added; log out and back in (or run: newgrp docker)"
      else
        bad "no Docker access for $(id -un)"
      fi
    fi
  fi
fi

# --- 4. Kata Containers ---

step "Kata Containers"
# Kata 4 release tarballs: kata-go-static holds the Go runtime (shim in
# /opt/kata/bin, configuration-<hv>.toml, kata-runtime), kata-static holds
# runtime-rs (shim in /opt/kata/runtime-rs/bin, runtime-rs/configuration-<hv>-runtime-rs.toml).
# Both include the hypervisors and guest images. Before Kata 4, kata-static was the Go runtime.
if [ "$kata_runtime" = rs ]; then
  kata_shim=/opt/kata/runtime-rs/bin/containerd-shim-kata-v2
  kata_config_src="runtime-rs/configuration-$hypervisor-runtime-rs.toml"
  kata_config_dest=/etc/kata-containers/runtime-rs/configuration.toml
else
  kata_shim=/opt/kata/bin/containerd-shim-kata-v2
  kata_config_src="configuration-$hypervisor.toml"
  kata_config_dest=/etc/kata-containers/configuration.toml
fi
if skipped kata; then
  warn "skipped"
elif have containerd-shim-kata-v2; then
  ok "shim: $(command -v containerd-shim-kata-v2) → $(readlink -f "$(command -v containerd-shim-kata-v2)")"
elif $check_only; then
  bad "Kata not installed (containerd-shim-kata-v2 not on PATH)"
elif confirm "install Kata Containers ($kata_version, $kata_runtime runtime, ~1.2 GB download) into /opt/kata"; then
  case "$(uname -m)" in
    x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; s390x) arch=s390x ;; ppc64le) arch=ppc64le ;;
    *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
  esac
  if [ "$kata_version" = latest ]; then
    kata_version="$(curl -fsSL https://api.github.com/repos/kata-containers/kata-containers/releases/latest | jq -r .tag_name)"
  fi
  flavour=kata-static
  if [ "$kata_runtime" = go ] && [ "${kata_version%%.*}" -ge 4 ] 2>/dev/null; then flavour=kata-go-static; fi
  tarball="$flavour-$kata_version-$arch.tar.zst"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  echo "  downloading $tarball…"
  curl -fL --progress-bar --retry 3 -o "$tmp/$tarball" \
    "https://github.com/kata-containers/kata-containers/releases/download/$kata_version/$tarball"
  as_root tar --zstd -xf "$tmp/$tarball" -C /
  rm -f "$tmp/$tarball"
  if [ -x "$kata_shim" ]; then
    as_root ln -sf "$kata_shim" /usr/local/bin/containerd-shim-kata-v2
    [ -x /opt/kata/bin/kata-runtime ] && as_root ln -sf /opt/kata/bin/kata-runtime /usr/local/bin/kata-runtime
    ok "Kata $kata_version ($kata_runtime runtime) installed"
  else
    bad "$tarball did not contain $kata_shim"
  fi
fi
if ! skipped kata && [ -d /opt/kata ]; then
  if [ -f "$kata_config_dest" ]; then
    ok "$kata_config_dest exists (left as is)"
  else
    source_config="/opt/kata/share/defaults/kata-containers/$kata_config_src"
    if [ ! -f "$source_config" ]; then
      warn "$source_config not found; Kata uses its default configuration"
    elif ! $check_only && confirm "use $hypervisor ($source_config) as the Kata configuration"; then
      as_root install -D -m 0644 "$source_config" "$kata_config_dest"
      ok "configured $hypervisor in $kata_config_dest (see host/kata-configuration.toml)"
    else
      warn "no $kata_config_dest; Kata uses its built-in default"
    fi
  fi
  if have kata-runtime; then
    if as_root kata-runtime check >/dev/null 2>&1; then ok "kata-runtime check passed"
    else warn "kata-runtime check reported problems: sudo kata-runtime check"; fi
  fi
fi

# --- 5. Docker runtime registration ---

step "Docker runtime \"kata\""
daemon_json=/etc/docker/daemon.json
runtime_state="$(python3 - "$daemon_json" <<'EOF' 2>/dev/null || echo error
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except FileNotFoundError:
    data = {}
kata = (data.get("runtimes") or {}).get("kata")
print("ok" if kata and kata.get("runtimeType") == "io.containerd.kata.v2" else ("other" if kata else "missing"))
EOF
)"
if skipped runtime; then
  warn "skipped"
elif [ "$runtime_state" = ok ]; then
  ok "registered in $daemon_json"
elif [ "$runtime_state" = error ]; then
  bad "$daemon_json is not valid JSON; fix it by hand"
elif $check_only; then
  bad "runtime kata not registered in $daemon_json"
elif confirm "register runtime kata in $daemon_json and restart Docker (running containers stop)"; then
  as_root python3 - "$daemon_json" <<'EOF'
import json, os, shutil, sys
path = sys.argv[1]
data = {}
if os.path.exists(path):
    shutil.copy2(path, path + ".kbx-backup")
    with open(path) as handle:
        data = json.load(handle)
data.setdefault("runtimes", {})["kata"] = {"runtimeType": "io.containerd.kata.v2"}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path + ".tmp", "w") as handle:
    json.dump(data, handle, indent=2)
    handle.write("\n")
os.replace(path + ".tmp", path)
EOF
  as_root systemctl restart docker.service
  ok "registered (previous file kept as $daemon_json.kbx-backup if it existed)"
else
  bad "runtime kata not registered"
fi
if ! skipped runtime && ! skipped kata && docker info >/dev/null 2>&1 && have containerd-shim-kata-v2; then
  if $check_only && ! docker image inspect alpine >/dev/null 2>&1; then
    warn "smoke test skipped in --check (needs to pull alpine)"
  else
    guest="$(docker run --rm --runtime kata alpine uname -r 2>&1 | tail -1)"
    if [ -n "$guest" ] && [ "$guest" != "$(uname -r)" ] && ! grep -qi error <<<"$guest"; then
      ok "smoke test: guest kernel $guest (host $(uname -r))"
    else
      bad "smoke test failed: $guest"
    fi
  fi
fi

# --- 6. firewall ---

step "Firewall"
if [ -z "$bridge" ]; then
  bridge="$(python3 - <<'EOF' 2>/dev/null || echo br-kbx
import os, tomllib
base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
try:
    with open(os.path.join(base, "kbx", "config.toml"), "rb") as handle:
        print(tomllib.load(handle).get("network", {}).get("bridge", "br-kbx"))
except FileNotFoundError:
    print("br-kbx")
EOF
)"
fi
installed_bridge="$(sed -n 's/^BRIDGE=//p' /etc/kbx/firewall.conf 2>/dev/null || true)"
if skipped firewall; then
  warn "skipped"
elif ! have systemctl; then
  bad "no systemd: run 'sudo host/kbx-firewall.sh start' after every boot and Docker restart"
elif systemctl is-active --quiet kbx-firewall.service && [ "$installed_bridge" = "$bridge" ]; then
  ok "kbx-firewall.service active for bridge $bridge"
elif $check_only; then
  bad "kbx-firewall.service not active for bridge $bridge"
elif confirm "install and start kbx-firewall.service for bridge $bridge"; then
  as_root "$here/install-firewall" --bridge "$bridge" >/dev/null
  if systemctl is-active --quiet kbx-firewall.service; then
    ok "kbx-firewall.service active"
  else
    bad "kbx-firewall.service failed: systemctl status kbx-firewall"
  fi
else
  bad "firewall not installed (kbx refuses to attach without it)"
fi

# --- 7. clipboard (optional) ---

step "Clipboard tools (image paste; optional)"
if skipped clipboard; then
  warn "skipped"
else
  if [ -n "${WAYLAND_DISPLAY:-}" ] || [ "${XDG_SESSION_TYPE:-}" = wayland ]; then
    ensure no wl-paste wl-clipboard
  fi
  # X11 path: X11 sessions, and Wayland compositors without data-control (GNOME) via XWayland.
  ensure no xclip xclip
  clip_hint="build it from https://github.com/cdown/clipnotify (make && sudo make install)"
  [ "$pm" = pacman ] && clip_hint="it is in the AUR (e.g. yay -S clipnotify)"
  ensure no clipnotify clipnotify "$clip_hint"
fi

# --- 8. kbx on PATH ---

step "kbx command"
target="$HOME/.local/bin/kbx"
if skipped link; then
  warn "skipped"
elif [ "$(readlink -f "$target" 2>/dev/null)" = "$checkout/bin/kbx" ]; then
  ok "$target → $checkout/bin/kbx"
elif $check_only; then
  warn "$target is not linked to this checkout"
elif [ -e "$target" ] && ! [ -L "$target" ]; then
  warn "$target exists and is not a symlink; leaving it"
elif confirm "link $target → $checkout/bin/kbx"; then
  mkdir -p "$HOME/.local/bin"
  ln -sfn "$checkout/bin/kbx" "$target"
  ok "linked"
fi
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "$HOME/.local/bin is not on PATH; add it in your shell profile" ;; esac

# --- summary ---

echo
if [ "$missing_required" -gt 0 ]; then
  printf '%s%d problem(s) left.%s Fix them and rerun host/install.sh.\n' "$red" "$missing_required" "$reset"
  exit 1
fi
printf '%sHost ready.%s\n' "$green" "$reset"
if $check_only; then exit 0; fi
if $run_build; then
  "$checkout/bin/kbx" build
else
  echo "Next: kbx build, then run host/spike.sh and \`kbx claude\` in a git repository."
fi
