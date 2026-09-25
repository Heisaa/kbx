#!/usr/bin/env bash
# Phase 0 host spike: check the risky assumptions (R1–R7) on this host.
#
#   host/spike.sh [--runtime kata] [--network kbx]
#
# Needs Docker with the Kata runtime registered (see host/README.md). Creates
# and removes throwaway containers/volumes named kbx-spike-*. R4 needs the kbx
# network and firewall (host/install-firewall); it is skipped without them.
# Single-quoted scripts below run inside containers, not in this shell.
# shellcheck disable=SC2016
set -uo pipefail

runtime=kata
network=kbx
while [ $# -gt 0 ]; do
  case "$1" in
    --runtime) runtime="${2:?}"; shift 2 ;;
    --network) network="${2:?}"; shift 2 ;;
    *) echo "usage: spike.sh [--runtime NAME] [--network NAME]" >&2; exit 2 ;;
  esac
done

pass=0 fail=0
result() { # id status detail
  printf '%-4s %-5s %s\n' "$1" "$2" "$3"
  case "$2" in PASS) pass=$((pass + 1)) ;; FAIL) fail=$((fail + 1)) ;; esac
}
run() { docker run --rm --runtime "$runtime" "$@"; }
cleanup() {
  docker rm -f kbx-spike-dind >/dev/null 2>&1
  docker volume rm kbx-spike-r2-privileged-varlibdocker kbx-spike-r2-privileged-store kbx-spike-r2-caps-varlibdocker kbx-spike-r2-caps-store kbx-spike-r7 >/dev/null 2>&1
}
trap cleanup EXIT

echo "== host"
printf 'kvm:     %s\n' "$(ls -l /dev/kvm 2>&1)"
printf 'cpu:     %s\n' "$(grep -o -m1 -E 'vmx|svm' /proc/cpuinfo || echo 'no vmx/svm flag')"
virt="$(systemd-detect-virt 2>/dev/null)" || true # prints "none" and exits 1 on bare metal
virt="${virt:-unknown}"
printf 'virt:    %s\n' "$virt"
printf 'kernel:  %s\n' "$(uname -r)"
printf 'docker:  %s\n' "$(docker version --format '{{.Server.Version}}' 2>&1)"
printf 'runtimes:%s\n' "$(docker info --format '{{range $k, $v := .Runtimes}} {{$k}}{{end}}' 2>&1)"
echo

echo "== smoke"
# Pull first, so pull progress never mixes into the results below.
for image in alpine docker:dind hello-world; do
  docker pull -q "$image" >/dev/null || echo "warning: cannot pull $image" >&2
done
guest="$(run alpine uname -r 2>&1)"
if [ "$guest" != "$(uname -r)" ] && [ -n "$guest" ]; then
  result smoke PASS "guest kernel $guest (host $(uname -r))"
else
  result smoke FAIL "guest kernel is '$guest'; is --runtime $runtime a VM runtime?"
  echo "Fix the Kata install first (host/README.md)."
  exit 1
fi

# R1: does --privileged pass host devices into the guest?
host_devs="$(find /dev -maxdepth 1 | wc -l)"
guest_list="$(run --privileged alpine sh -c 'ls /dev' 2>&1)"
guest_devs="$(wc -w <<<"$guest_list")"
if grep -qE '(^|[[:space:]])(nvme[0-9]|sd[a-z]|dm-[0-9]|tpm[0-9])' <<<"$guest_list"; then
  result R1 FAIL "host block/TPM devices visible in a --privileged guest ($guest_devs entries vs $host_devs on host): use [runtime] privileges = \"caps\" or privileged_without_host_devices"
else
  result R1 PASS "--privileged guest shows $guest_devs /dev entries (host $host_devs), no host disks"
fi

# R2: inner dockerd with overlay on (a) a volume and (b) a loop-mounted ext4 image,
# first with --privileged, then (if that fails) with kbx's "caps" mode.
# Keep caps_flags in sync with privilege_args() in kbx/sandbox.py.
caps_flags=(--cap-add SYS_ADMIN --cap-add NET_ADMIN --cap-add SYS_PTRACE --cap-add MKNOD
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined --security-opt systempaths=unconfined
  --device-cgroup-rule "c 10:237 rwm" --device-cgroup-rule "b 7:* rwm")
[ -e /dev/fuse ] && caps_flags+=(--device /dev/fuse)
r2_try() { # privilege-mode volume-target script → driver, or the error
  local mode="$1" target="$2" script="$3" out status flags=(--privileged)
  [ "$mode" = caps ] && flags=("${caps_flags[@]}")
  # Bypass the image's entrypoint: it points DOCKER_HOST at tcp://docker:2375.
  out="$(run "${flags[@]}" --dns 1.1.1.1 --dns 9.9.9.9 --entrypoint sh -e DOCKER_HOST=unix:///var/run/docker.sock \
    -v "kbx-spike-r2-$mode-${target//\//}:$target" docker:dind -c "$script" 2>&1)"
  status=$?
  if [ "$status" -eq 0 ]; then
    tail -1 <<<"$out"
  else
    # The useful part of a shim error is after "failed to create shim task:".
    { grep -i -m1 -E 'error|failed|denied|invalid' <<<"$out" || tail -1 <<<"$out"; } |
      sed 's/.*failed to create shim task: //' | cut -c1-600
  fi
}
# What kbx-init does before starting dockerd (prepare_cgroups in kbx_sandbox/init.py):
# a read-write cgroup2 mount of the container's own namespace, and cgroup v2 nesting.
cgroup_setup='if grep -q " /sys/fs/cgroup cgroup2 ro" /proc/mounts; then
    umount /sys/fs/cgroup && mount -t cgroup2 -o nosuid,nodev,noexec cgroup2 /sys/fs/cgroup || { echo "cgroup remount failed"; exit 1; }
  fi
  if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    mkdir -p /sys/fs/cgroup/init
    xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || :
    sed -e "s/ / +/g" -e "s/^/+/" < /sys/fs/cgroup/cgroup.controllers > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || :
  fi
  mount --make-rshared /'
hello_world='out="$(docker run --rm hello-world 2>&1)" || { echo "hello-world failed: $(echo "$out" | grep -iE "error|denied|failed" | tail -1 | cut -c1-400)"; exit 1; }'
wait_dockerd='(dockerd >/tmp/d.log 2>&1 &); for i in $(seq 60); do timeout 3 docker info >/dev/null 2>&1 && break; sleep 1; done
  timeout 3 docker info >/dev/null 2>&1 || { echo "dockerd error: $(tail -3 /tmp/d.log | tr "\n" " ")"; exit 1; }'
r2_volume="$cgroup_setup
  $wait_dockerd
  $hello_world
  docker info --format '{{.Driver}}'"
r2_loop="$cgroup_setup
  [ -e /dev/loop-control ] || mknod /dev/loop-control c 10 237
  for i in \$(seq 0 15); do [ -e /dev/loop\$i ] || mknod /dev/loop\$i b 7 \$i; done
  command -v mkfs.ext4 >/dev/null || apk add -q --no-cache e2fsprogs >/dev/null 2>&1 || { echo 'cannot install e2fsprogs (network?)'; exit 1; }
  truncate -s 4G /store/docker.img && mkfs.ext4 -q -F /store/docker.img || { echo 'mkfs.ext4 failed'; exit 1; }
  mkdir -p /var/lib/docker
  mount -o loop /store/docker.img /var/lib/docker 2>/tmp/m.err || { echo loop mount failed: \$(tail -1 /tmp/m.err); exit 1; }
  $wait_dockerd
  $hello_world
  docker info --format '{{.Driver}}'"
# Docker 29+ with the containerd snapshotter reports "overlayfs".
is_overlay() { [ "$1" = overlay2 ] || [ "$1" = overlayfs ]; }
r2_pass=false
for mode in privileged caps; do
  r2b="$(r2_try "$mode" /store "$r2_loop")"
  r2a="$(r2_try "$mode" /var/lib/docker "$r2_volume")"
  setting="[runtime] privileges = \"$mode\""
  if is_overlay "$r2b"; then
    result R2 PASS "$mode, loop ext4: $r2b + hello-world OK → $setting, docker_storage = \"loop\" (volume directly: '${r2a}')"
    r2_pass=true
  elif is_overlay "$r2a"; then
    result R2 PASS "$mode, volume: $r2a + hello-world OK, loop failed ('${r2b}') → $setting, docker_storage = \"volume\""
    r2_pass=true
  else
    printf '     %s: volume: %s\n     %s: loop:   %s\n' "$mode" "$r2a" "$mode" "$r2b"
  fi
  $r2_pass && break
done
$r2_pass || result R2 FAIL "inner Docker works in neither mode (errors above)"

# R3/R4 need the kbx network; create it the way kbx does if it is missing.
if ! docker network inspect "$network" >/dev/null 2>&1; then
  docker network create --driver bridge --ipv6=false --subnet 172.30.0.0/24 \
    -o com.docker.network.bridge.name=br-kbx --label kbx=1 "$network" >/dev/null &&
    echo "(created Docker network $network: 172.30.0.0/24, bridge br-kbx)"
fi

# R3: DNS inside the guest on a user-defined network.
if docker network inspect "$network" >/dev/null 2>&1; then
  embedded="$(run --network "$network" alpine sh -c 'nslookup github.com >/dev/null 2>&1 && echo ok || echo broken')"
  public="$(run --network "$network" --dns 1.1.1.1 alpine sh -c 'echo nameserver 1.1.1.1 >/etc/resolv.conf; nslookup github.com >/dev/null 2>&1 && echo ok || echo broken')"
  if [ "$public" = ok ]; then
    result R3 PASS "public resolver works (embedded 127.0.0.11: $embedded; kbx-init switches to --dns servers when it is unreachable)"
  else
    result R3 FAIL "no DNS with a public resolver (embedded: $embedded)"
  fi
else
  result R3 SKIP "network '$network' missing (kbx creates it on first launch; or: docker network create --subnet 172.30.0.0/24 -o com.docker.network.bridge.name=br-kbx $network)"
fi

# R4: firewall — the gateway must not answer; the internet must.
if docker network inspect "$network" >/dev/null 2>&1; then
  gateway="$(docker network inspect "$network" --format '{{(index .IPAM.Config 0).Gateway}}')"
  gateway="${gateway:-172.30.0.1}"
  probe="$(run --network "$network" --dns 1.1.1.1 alpine sh -c "
    echo nameserver 1.1.1.1 >/etc/resolv.conf
    if timeout 3 nc -z $gateway 22 2>/dev/null || timeout 3 nc -z $gateway 9 2>/dev/null; then echo host-open; else echo host-closed; fi
    wget -q -T 10 -O /dev/null https://github.com && echo net-ok || echo net-broken" 2>&1 | tr '\n' ' ')"
  if [[ "$probe" == *host-closed*net-ok* ]] && sudo -n iptables -n -L KBX-INPUT >/dev/null 2>&1; then
    result R4 PASS "gateway $gateway unreachable, internet reachable, KBX chains present"
  elif [[ "$probe" == *host-closed*net-ok* ]]; then
    result R4 PASS "gateway $gateway unreachable, internet reachable (could not list KBX chains without sudo)"
  else
    result R4 FAIL "$probe (is kbx-firewall.service running?)"
  fi
else
  result R4 SKIP "network '$network' missing"
fi

# R5: VM sizing follows --memory/--cpus.
read -r cpus mem < <(run --memory 8g --cpus 4 alpine sh -c 'echo "$(nproc) $(free -m | awk "/Mem:/ {print \$2}")"' 2>/dev/null) || true
if [ "${cpus:-0}" -ge 4 ] && [ "${mem:-0}" -ge 7800 ]; then
  result R5 PASS "--memory 8g --cpus 4 → $cpus cpus, $mem MiB (Kata adds them on top of the base VM)"
else
  result R5 FAIL "--memory 8g --cpus 4 → ${cpus:-?} cpus, ${mem:-?} MiB: raise default_memory/default_vcpus in the Kata config"
fi

# R6: nested virtualization.
if [ "$virt" = none ]; then
  result R6 PASS "bare metal"
elif [ "$virt" = unknown ]; then
  result R6 INFO "could not detect (systemd-detect-virt missing); if this host is a VM, enable nested KVM"
else
  result R6 INFO "host is a VM ($virt): nested KVM must be enabled on the outer hypervisor"
fi

# R7: does a new, empty named volume get the image's content (copy-up)?
docker volume create kbx-spike-r7 >/dev/null
copied="$(run -v kbx-spike-r7:/etc/apk alpine sh -c 'ls /etc/apk | wc -l' 2>&1)"
if [ "${copied:-0}" -gt 0 ] 2>/dev/null; then
  result R7 PASS "copy-up works ($copied entries). kbx-init copies the home template itself either way."
else
  result R7 INFO "no copy-up under $runtime: fine, kbx-init copies /opt/kbx/home-template on first boot"
fi

echo
echo "$pass passed, $fail failed. Paste this output into host/README.md (\"Spike results\")."
[ "$fail" -eq 0 ]
