#!/usr/bin/env bash
# Runs at `kbx build`, as root. Installs the chosen Node version with the
# core's checksum-verifying installer (/opt/node/<version>, linked into
# /usr/local/bin), then extra apt packages and global npm packages.
set -euo pipefail

version="${KBX_OPT_NODE_TOOLCHAIN_VERSION-26}"
apt_packages="${KBX_OPT_NODE_TOOLCHAIN_APT-}"
npm_packages="${KBX_OPT_NODE_TOOLCHAIN_NPM-}"

if [ -n "$apt_packages" ]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  # shellcheck disable=SC2086 # space-separated list, validated by kbx
  apt-get install -y --no-install-recommends $apt_packages
  rm -rf /var/lib/apt/lists/*
fi

if [ -n "$version" ]; then
  /usr/local/lib/kbx/install-node "$version"
fi

if [ -n "$npm_packages" ]; then
  # Into /usr/local (root's npm prefix), never into Node's own directory,
  # which the next Node upgrade deletes along with every global package.
  # shellcheck disable=SC2086 # space-separated list, validated by kbx
  npm install -g --prefix /usr/local --no-fund --no-audit $npm_packages
fi
node --version
