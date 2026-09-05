# pve-drive

Move Proxmox QEMU VMs to Google Shared Drive with rclone, then restore them by VMID—even on another server or under another VMID.

**Experimental:** test an upload with `--keep-vm`, a restore, and snapshot rollback before relying on VM deletion.

## Install

Run as root on the Proxmox node. Requires Git, Python 3, rclone, zstd, and the standard Proxmox tools.

```bash
apt-get install git python3 rclone zstd
git clone https://github.com/PacoCotera/pve-drive.git /opt/pve-drive &&
cd /opt/pve-drive && ./install.sh
```

Update after active operations finish:

```bash
cd /opt/pve-drive && git pull --ff-only && ./install.sh
```

## Configure

Run `rclone config`, create a Google Drive remote named `gdrive`, and select your Shared Drive. See [rclone’s setup guide](https://rclone.org/drive/).

```bash
rclone lsd gdrive:
rclone mkdir gdrive:pve-archive
```

Give each source server a unique label, such as `pve-site-a`. Keep that original label when listing or restoring its VMs. Global options go before the command.

## Upload, list, restore

Upload and verify a test archive, keeping the original VM stopped:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a upload 100 --keep-vm
```

**Omit `--keep-vm` to delete the original VM and its disks after successful verification.**

List archived VMs, including sizes and snapshots:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a list
```

Restore the latest archive to its original VMID and storage:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a restore 100
```

Or restore on a destination node under another VMID and storage:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a \
  restore 100 --target-vmid 200 --storage destination-dir --unique
```

Restore refuses occupied VMIDs and leaves the VM stopped. `--unique` changes MAC addresses, not guest static IPs. The cloud archive is retained.

## What to expect

- Supported internal QCOW2 disk snapshots are preserved; unsupported layouts are rejected.
- Upload verification compares cloud sizes and MD5 hashes. Add `--deep-verify` for a full read-back. Restore checks SHA-256.
- Staging defaults to `/var/lib/vz/pve-drive`. Use `--work-dir PATH` before the command if that filesystem is short on space. Native restore needs room for both the download and restored disks.

See [ADMIN.md](ADMIN.md) for recovery, storage requirements, supported layouts, and diagnostics. For command help, run `pve-drive --help` or `pve-drive restore --help`.

[MIT License](LICENSE) · © 2026 Paco Cotera
