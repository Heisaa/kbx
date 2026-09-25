# Host files (phase 0)

| File | Purpose |
| --- | --- |
| `install.sh` | Checks the host and installs what is missing (pacman, apt, dnf, zypper; Kata from upstream) |
| `spike.sh` | Checks the phase 0 risks R1–R7 on this host with throwaway containers |
| `kbx-firewall.sh` | Firewall rules (installed as `/usr/local/sbin/kbx-firewall`) |
| `kbx-firewall.service` | systemd unit: after and part of `docker.service` |
| `install-firewall` | Installs and starts the two above, writes `/etc/kbx/firewall.conf` |
| `kata-configuration.toml` | Kata settings kbx expects in `/etc/kata-containers/configuration.toml` |

Install steps: [docs/host-setup.md](../docs/host-setup.md).

## Chosen settings

| Setting | Choice | Why |
| --- | --- | --- |
| Kata runtime | Go runtime from `kata-go-static` (runtime-rs with `install.sh --kata-runtime rs`) | Mature Docker support, `kata-runtime check` |
| Hypervisor | Cloud Hypervisor (`configuration-clh.toml`), QEMU as fallback | Faster boot, virtio-fs |
| Docker runtime | `kata` → `io.containerd.kata.v2` | kbx default `[runtime] name` |
| Privileges | explicit capabilities (`[runtime] privileges = "caps"`); kbx-init remounts the container's own cgroup2 read-write and sets up cgroup v2 nesting for the inner dockerd | `--privileged` fails under Kata 4: the agent cannot recreate the host device list (R1) |
| Inner Docker storage | sparse ext4 image on the volume, loop-mounted (`docker_storage = "loop"`); loop nodes created by kbx-init, allowed by device cgroup rules | overlayfs cannot use the virtio-fs volume as its upper layer (R2); the guest kernel has loop built in |
| DNS | `--dns 1.1.1.1 --dns 9.9.9.9`; kbx-init writes them to `resolv.conf` | Docker's embedded resolver is a host service, unreachable in the guest (R3) |
| Home seeding | kbx-init copies `/opt/kbx/home-template` into a new volume | Works whether or not Docker's copy-up happens under Kata (R7) |
| `/run` | `--tmpfs /run` | Readiness marker and dtach sockets are fresh on every start |

## Spike results

2026-09-25: Arch Linux, kernel 7.2.6-arch2-1, Docker 29.8.1, Kata 4.2.0 (Go
runtime, Cloud Hypervisor, guest kernel 6.18.35), bare metal, Intel VT-x.

```
smoke PASS  guest kernel 6.18.35 (host 7.2.6-arch2-1)
R1   PASS  --privileged guest shows 131 /dev entries (host 221), no host disks
     privileged: volume: Creating container device LinuxDevice { path: "/dev/full", typ: C, major: 1, minor: 7, file_mode: Some(438), uid: Some(0), gid: Some(0) }
     privileged: loop:   Creating container device LinuxDevice { path: "/dev/full", typ: C, major: 1, minor: 7, file_mode: Some(438), uid: Some(0), gid: Some(0) }
R2   PASS  caps, loop ext4: overlayfs + hello-world OK → [runtime] privileges = "caps", docker_storage = "loop"
           (volume directly: hello-world failed: failed to mount … fstype: overlay … on the virtio-fs volume)
R3   PASS  public resolver works (embedded 127.0.0.11: broken; kbx-init switches to --dns servers when it is unreachable)
R4   PASS  gateway 172.30.0.1 unreachable, internet reachable
R5   PASS  --memory 8g --cpus 4 → 5 cpus, 10179 MiB (Kata adds them on top of the base VM)
R6   PASS  bare metal
R7   PASS  copy-up works (5 entries). kbx-init copies the home template itself either way.
```

What this decided (now kbx's defaults):

- **R1:** `--privileged` containers with a volume cannot start under Kata 4 (the
  agent fails on Docker's host device list). kbx uses `privileges = "caps"`:
  explicit capabilities, a writable cgroup2 mount of the container's own
  namespace with cgroup v2 nesting (kbx-init), and device cgroup rules for loop
  devices, whose nodes kbx-init creates.
- **R2:** overlayfs cannot use the virtio-fs volume as its upper layer, so the
  inner Docker's data root is a sparse ext4 image on the volume, loop-mounted
  by kbx-init (`docker_storage = "loop"`). The guest kernel has loop built in.
- **R3:** Docker's embedded resolver is unreachable in the guest; kbx-init
  writes the `[launcher] dns` servers to `resolv.conf`.
