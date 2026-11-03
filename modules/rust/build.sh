#!/usr/bin/env bash
# Runs at `kbx build`, as root. Installs rustup with its checksum verified, the
# chosen toolchains, components and targets under /opt/rust, and links the
# rustup proxies (cargo, rustc, rustup, …) into /usr/local/bin through small
# wrappers that find /opt/rust even where the module's environment is not set.
#
# /opt/rust is writable by every user: a project's rust-toolchain.toml can make
# rustup install another toolchain on demand. Such installs live in the
# container and are gone after `kbx recreate`; list pinned toolchains in the
# `extra` option to have them in the image. (The agent is root in its VM
# anyway, so this opens nothing.)
set -euo pipefail

toolchain="${KBX_OPT_RUST_TOOLCHAIN-stable}"
extra="${KBX_OPT_RUST_EXTRA-}"
components="${KBX_OPT_RUST_COMPONENTS-clippy rustfmt}"
targets="${KBX_OPT_RUST_TARGETS-}"
apt_packages="${KBX_OPT_RUST_APT-pkg-config libssl-dev}"

export RUSTUP_HOME=/opt/rust/rustup CARGO_HOME=/opt/rust/cargo
DIST="${RUSTUP_DIST_URL:-https://static.rust-lang.org/rustup/dist}"

log() { printf 'rust: %s\n' "$*"; }

if [ -n "$apt_packages" ]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  # shellcheck disable=SC2086 # space-separated list, validated by kbx
  apt-get install -y --no-install-recommends $apt_packages
  rm -rf /var/lib/apt/lists/*
fi

case "$(uname -m)" in
  x86_64) triple=x86_64-unknown-linux-gnu ;;
  aarch64 | arm64) triple=aarch64-unknown-linux-gnu ;;
  *) log "unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
curl -fsSL --retry 3 --retry-delay 2 -o "$work/rustup-init" "$DIST/$triple/rustup-init"
expected="$(curl -fsSL --retry 3 --retry-delay 2 "$DIST/$triple/rustup-init.sha256" | awk '{print $1}')"
actual="$(sha256sum "$work/rustup-init" | awk '{print $1}')"
if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
  log "rustup-init checksum mismatch (expected ${expected:-nothing}, got $actual)" >&2
  exit 1
fi
chmod +x "$work/rustup-init"

args=(-y --no-modify-path --profile minimal --default-toolchain "$toolchain")
for component in $components; do args+=(--component "$component"); done
for target in $targets; do args+=(--target "$target"); done
"$work/rustup-init" "${args[@]}"

rustup="$CARGO_HOME/bin/rustup"
for other in $extra; do
  "$rustup" toolchain install --profile minimal "$other"
  for component in $components; do "$rustup" component add --toolchain "$other" "$component"; done
  for target in $targets; do "$rustup" target add --toolchain "$other" "$target"; done
done

for proxy in "$CARGO_HOME"/bin/*; do
  name="$(basename "$proxy")"
  printf '#!/bin/sh\nRUSTUP_HOME="${RUSTUP_HOME:-%s}" exec %s "$@"\n' "$RUSTUP_HOME" "$proxy" > "/usr/local/bin/$name"
  chmod 0755 "/usr/local/bin/$name"
done

chmod -R a+rwX /opt/rust
"$rustup" show
