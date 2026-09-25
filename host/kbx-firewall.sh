#!/usr/bin/env bash
# kbx host firewall: nothing from the sandbox bridge reaches the host itself,
# the LAN, CGNAT/VPN, link-local/metadata or multicast ranges; the internet
# stays reachable. Idempotent: it owns the KBX-* chains and recreates them.
#
#   kbx-firewall start|stop|status
#
# Settings come from /etc/kbx/firewall.conf (KEY=value lines, not sourced):
#   BRIDGE=br-kbx
#   EXTRA_BLOCKED="10.8.0.0/24 fd00::/8"   # extra ranges, e.g. a VPN
set -euo pipefail

CONF="${KBX_FIREWALL_CONF:-/etc/kbx/firewall.conf}"
BRIDGE=br-kbx
EXTRA_BLOCKED=""

if [ -f "$CONF" ]; then
  while IFS='=' read -r key value; do
    value="${value%\"}"
    value="${value#\"}"
    case "$key" in
      BRIDGE) BRIDGE="$value" ;;
      EXTRA_BLOCKED) EXTRA_BLOCKED="$value" ;;
      '' | \#*) ;;
      *) echo "kbx-firewall: ignoring unknown setting $key in $CONF" >&2 ;;
    esac
  done <"$CONF"
fi
[[ "$BRIDGE" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || { echo "kbx-firewall: invalid BRIDGE '$BRIDGE'" >&2; exit 1; }

BLOCKED_V4="0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4"

ensure_chain() { # table-tool chain
  "$1" -w -N "$2" 2>/dev/null || "$1" -w -F "$2"
}

ensure_jump() { # tool parent chain
  "$1" -w -C "$2" -j "$3" 2>/dev/null || "$1" -w -I "$2" 1 -j "$3"
}

remove_jump() { # tool parent chain
  while "$1" -w -D "$2" -j "$3" 2>/dev/null; do :; done
}

forward_parent() {
  # Docker's DOCKER-USER runs before Docker's own FORWARD rules. Without it
  # (e.g. Docker's nftables backend), a drop in FORWARD still drops.
  if iptables -w -n -L DOCKER-USER >/dev/null 2>&1; then echo DOCKER-USER; else echo FORWARD; fi
}

start() {
  # 1. Nothing from the sandbox bridge reaches the host itself: any host IP,
  #    any port, Docker's DNS, localhost-bound services via the gateway.
  ensure_chain iptables KBX-INPUT
  iptables -w -A KBX-INPUT -i "$BRIDGE" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  iptables -w -A KBX-INPUT -i "$BRIDGE" -j DROP
  ensure_jump iptables INPUT KBX-INPUT

  # 2. Forwarded traffic: drop private, CGNAT, link-local, metadata and
  #    multicast ranges, and sandbox-to-sandbox traffic on the bridge.
  ensure_chain iptables KBX-FORWARD
  iptables -w -A KBX-FORWARD -i "$BRIDGE" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  iptables -w -A KBX-FORWARD -i "$BRIDGE" -o "$BRIDGE" -j DROP
  local net
  for net in $BLOCKED_V4 $EXTRA_BLOCKED; do
    [[ "$net" == *:* ]] && continue
    iptables -w -A KBX-FORWARD -i "$BRIDGE" -d "$net" -j DROP
  done
  iptables -w -A KBX-FORWARD -j RETURN
  local parent
  parent="$(forward_parent)"
  ensure_jump iptables "$parent" KBX-FORWARD

  # 3. IPv6: the network has no IPv6, but drop anything from the bridge anyway.
  if command -v ip6tables >/dev/null 2>&1; then
    ensure_chain ip6tables KBX-INPUT
    ip6tables -w -A KBX-INPUT -i "$BRIDGE" -j DROP
    ensure_jump ip6tables INPUT KBX-INPUT
    ensure_chain ip6tables KBX-FORWARD
    ip6tables -w -A KBX-FORWARD -i "$BRIDGE" -j DROP
    ensure_jump ip6tables FORWARD KBX-FORWARD
  fi
  echo "kbx-firewall: active for bridge $BRIDGE (forward rules via $parent)"
}

stop() {
  remove_jump iptables INPUT KBX-INPUT
  remove_jump iptables DOCKER-USER KBX-FORWARD
  remove_jump iptables FORWARD KBX-FORWARD
  for chain in KBX-INPUT KBX-FORWARD; do
    iptables -w -F "$chain" 2>/dev/null || true
    iptables -w -X "$chain" 2>/dev/null || true
  done
  if command -v ip6tables >/dev/null 2>&1; then
    remove_jump ip6tables INPUT KBX-INPUT
    remove_jump ip6tables FORWARD KBX-FORWARD
    for chain in KBX-INPUT KBX-FORWARD; do
      ip6tables -w -F "$chain" 2>/dev/null || true
      ip6tables -w -X "$chain" 2>/dev/null || true
    done
  fi
  echo "kbx-firewall: removed"
}

status() {
  iptables -w -v -n -L KBX-INPUT
  echo
  iptables -w -v -n -L KBX-FORWARD
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: kbx-firewall start|stop|status" >&2; exit 2 ;;
esac
