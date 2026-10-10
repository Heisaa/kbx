# Host setup

One-time: KVM, Docker, Kata Containers as a Docker runtime, and the kbx
firewall. Then run the phase 0 spike (`host/spike.sh`) to confirm everything.
Linux only.

## The installer

`host/install.sh` checks every item below and installs what is missing, asking
before each change. It supports pacman (Arch and derivatives), apt (Debian,
Ubuntu), dnf (Fedora) and zypper (openSUSE); Kata comes from the upstream
release on every distribution. Run it as the user who will use kbx:

```sh
host/install.sh --check      # report only, change nothing
host/install.sh              # install what is missing (asks first)
host/install.sh --yes --build
```

Options: `--skip packages,docker,kata,runtime,firewall,clipboard,link`,
`--kata-version 4.2.0`, `--kata-runtime go|rs`, `--hypervisor clh|qemu`,
`--bridge NAME`. It never adds you to the `docker` group or restarts Docker
without asking (`--yes` answers yes). The sections below are the manual
equivalent.

## 1. KVM

```sh
ls -l /dev/kvm                       # must exist
grep -c -E 'vmx|svm' /proc/cpuinfo   # > 0: hardware virtualization
systemd-detect-virt                  # "none" on bare metal
```

If the host is itself a VM, enable nested virtualization on the outer
hypervisor. Your user needs access to `/dev/kvm` only through Docker (the Kata
shim runs as root).

## 2. Kata Containers

Install a release from <https://github.com/kata-containers/kata-containers/releases>.
Since Kata 4 there are two self-contained tarballs (hypervisors, guest kernel
and images included): `kata-go-static` with the Go runtime, used here, and
`kata-static` with the newer runtime-rs (shim in `/opt/kata/runtime-rs/bin`,
config in `/etc/kata-containers/runtime-rs/configuration.toml`;
`install.sh --kata-runtime rs`).

```sh
ver=$(curl -s https://api.github.com/repos/kata-containers/kata-containers/releases/latest | jq -r .tag_name)
curl -fLO "https://github.com/kata-containers/kata-containers/releases/download/$ver/kata-go-static-$ver-amd64.tar.zst"
sudo tar --zstd -xf "kata-go-static-$ver-amd64.tar.zst" -C /
sudo ln -sf /opt/kata/bin/containerd-shim-kata-v2 /usr/local/bin/containerd-shim-kata-v2
sudo ln -sf /opt/kata/bin/kata-runtime /usr/local/bin/kata-runtime
sudo modprobe vhost_vsock vhost_net   # persist in /etc/modules-load.d/
sudo kata-runtime check               # host capability check
```

On Arch the release tarball is the simplest route; AUR packages also exist.

Pick the hypervisor. Start with Cloud Hypervisor (fast boot, virtio-fs) and
fall back to QEMU if something is missing:

```sh
sudo mkdir -p /etc/kata-containers
sudo cp /opt/kata/share/defaults/kata-containers/configuration-clh.toml /etc/kata-containers/configuration.toml
```

Then apply the settings in [`host/kata-configuration.toml`](../host/kata-configuration.toml)
to `/etc/kata-containers/configuration.toml`.

### VM memory and the desktop

QEMU with virtio-fs normally backs guest RAM with shared files on the host's
`/dev/shm`. QEMU unlinks these files while keeping them open, so `ls /dev/shm`
can be empty while `df` reports a full mount. Memory is allocated as the guest
touches its pages; a collection of lightly used VMs can start successfully and
fill the mount later. An exhausted mount can cause SIGBUS in the VMs or the
desktop compositor. This does not imply a leak if stopping the VMs frees it.
See the [upstream Kata guide](https://github.com/kata-containers/kata-containers/blob/main/docs/how-to/how-to-use-virtio-fs-with-kata.md).

Prefer Cloud Hypervisor where supported. Its normal shared memory uses `memfd`
and is independent of the `/dev/shm` mount limit, but still consumes host RAM.
The installer defaults to Cloud Hypervisor for a new configuration; an existing
configuration is kept, so rerunning `install.sh --hypervisor clh` does not switch
an existing QEMU setup. To switch the standard Go runtime configuration:

1. Stop all Kata sandboxes and check that `df -h /dev/shm` falls back to low usage.
2. Back up `/etc/kata-containers/configuration.toml`, replace it with
   `/opt/kata/share/defaults/kata-containers/configuration-clh.toml`, and reapply
   the settings from `host/kata-configuration.toml` to the matching keys.
3. Start one sandbox and verify the running process is `cloud-hypervisor`:
   `pgrep -af 'cloud-hypervisor|qemu-system'`. Check host RAM and `/dev/shm` while
   the sandbox runs. The new configuration applies when the next VM starts.

kbx checks memory before each new VM start and serializes concurrent launches.
Running VMs are charged by the memory they have touched, so several lightly used
8 GiB sandboxes can run side by side. kbx refuses a start only when the new VM
could not boot in the host RAM (or, for QEMU and unknown configurations,
`/dev/shm`) that is free beyond the reserve: the greater of 4 GiB or 20% of RAM,
and of 512 MiB or 10% of the mount. If the VM could not grow to its full limit,
kbx warns and starts it anyway; keep an eye on `free -h` and `df -h /dev/shm`
when running many busy sandboxes. Set `memory_budget` or `shm_budget` to a size
to refuse instead whenever the total VM limits would exceed it.

To reserve less per sandbox, lower `[launcher] memory` (for example `"4g"`) and
recreate the existing containers so their stored limits change. Sandboxes stop
5 minutes after their last session and shell close (`idle_stop`).

With Cloud Hypervisor, `reclaim_guest_freed_memory = true` (in
`host/kata-configuration.toml`; the installer sets it in a new configuration)
returns memory the guest frees to the host. Without it a running VM keeps every
page it has touched until it stops, so one large build leaves it near its limit.
See [configuration](configuration.md) for `memory_budget`, `shm_budget`, and
`runtime.memory_overhead`. An unreadable or ambiguous standard Kata config,
runtime-rs, or a custom runtime name is treated conservatively as shared-memory
backed; set `memory_overhead` to at least that runtime's actual base VM RAM.
Stop existing VMs before changing hypervisors or base memory, so their running
configuration matches what admission reads.

Increasing the size of `/dev/shm` raises its filesystem limit without adding RAM
or enforcing an aggregate VM budget. Admission covers kbx starts by one user;
keep direct Docker launches and other host workloads within the same RAM budget.
If shared-memory usage remains high after stopping the sandboxes, inspect
`sudo lsof -nP -a +L1 /dev/shm` for surviving holders before launching more.

## 3. Register the runtime with Docker

`/etc/docker/daemon.json` (merge with what is there):

```json
{ "runtimes": { "kata": { "runtimeType": "io.containerd.kata.v2" } } }
```

```sh
sudo systemctl restart docker
docker run --rm --runtime kata alpine uname -r   # prints the GUEST kernel, not $(uname -r)
```

If containers under Kata have no network (only `lo`), update Kata: recent
Docker releases create the network namespace late, which older Kata releases do
not handle.

## 4. Firewall

```sh
sudo host/install-firewall                     # bridge br-kbx by default
sudo host/install-firewall --bridge br-kbx --extra-blocked "10.8.0.0/24"
```

This installs `/usr/local/sbin/kbx-firewall`, `/etc/kbx/firewall.conf` and
`kbx-firewall.service` (after and part of `docker.service`, so it is re-applied
when Docker restarts). The rules live in their own chains:

- `KBX-INPUT` (from `INPUT`): drop everything from the sandbox bridge to the
  host itself — every host IP and port, Docker's DNS, localhost-bound services
  reached through the gateway, a TCP Docker API.
- `KBX-FORWARD` (from `DOCKER-USER`, or `FORWARD` if Docker's nftables backend
  is used): drop forwarded traffic from the bridge to RFC 1918, CGNAT
  (100.64/10, e.g. Tailscale), link-local and metadata (169.254/16), loopback,
  multicast and reserved ranges, and sandbox-to-sandbox traffic.
- IPv6: the network is created without IPv6; `ip6tables` drops anything from
  the bridge anyway.

Add other VPN ranges that are not RFC 1918 with `--extra-blocked`. The host's
public IP is reachable from the internet anyway; the point is that sandboxes
cannot reach what is bound to localhost or the LAN.

`kbx` checks the firewall on **every launch and attach**: from inside the
sandbox it connects to the bridge gateway with a 2 s timeout. A timeout means
the packet was dropped; a refusal means the host answered, and kbx refuses to
attach. Inspect with `sudo kbx-firewall status` (drop counters).

The bridge name must match `[network] bridge` in `~/.config/kbx/config.toml`.
kbx creates the Docker network itself on first launch.

## 5. Phase 0 spike

```sh
host/spike.sh                        # after `kbx build` and one `kbx start`, for R3/R4
```

It checks, with throwaway containers:

| # | Question | Pass means / fallback |
| --- | --- | --- |
| R1 | Does `--privileged` under Kata pass host devices into the guest? | No host disks visible. (Kata 4 cannot start `--privileged` containers with volumes at all, which is why kbx defaults to `privileges = "caps"`) |
| R2 | Can the inner dockerd use overlay and run a container? | Tried with `--privileged`, then `caps`; the passing mode and storage are printed as config settings |
| R3 | Is DNS usable inside the guest? | A public resolver works. kbx-init switches to `[launcher] dns` when Docker's embedded resolver is unreachable |
| R4 | Firewall: host blocked, internet open? | Gateway dropped, `https://github.com` reachable |
| R5 | VM sizing | `--memory/--cpus` reflected in the guest; else set `default_memory`/`default_vcpus` |
| R6 | Nested virtualization | Bare metal, or nested KVM enabled |
| R7 | Docker's volume copy-up under Kata | Informational: kbx-init copies the image's home template into a new volume itself |
| R8 | Mount mode: does a bind-mounted checkout write through with the host user's uid, and does `docker pause` work? | Both; otherwise use `[workspace] mode = "clone"` |

Record the results in `host/README.md` for your setup.

## Uninstall

```sh
kbx rm                               # per project (asks; clone mode lists unfetched work)
sudo systemctl disable --now kbx-firewall.service
sudo rm /usr/local/sbin/kbx-firewall /etc/systemd/system/kbx-firewall.service /etc/kbx/firewall.conf
docker network rm kbx
docker image rm kbx-agent
rm -rf ~/.local/share/kbx            # stage, logs, guard state (config stays in ~/.config/kbx)
```
