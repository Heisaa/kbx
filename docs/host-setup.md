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

Record the results in `host/README.md` for your setup.

## Uninstall

```sh
kbx rm                               # per project (asks; lists unfetched work)
sudo systemctl disable --now kbx-firewall.service
sudo rm /usr/local/sbin/kbx-firewall /etc/systemd/system/kbx-firewall.service /etc/kbx/firewall.conf
docker network rm kbx
docker image rm kbx-agent
rm -rf ~/.local/share/kbx            # stage and logs (config stays in ~/.config/kbx)
```
