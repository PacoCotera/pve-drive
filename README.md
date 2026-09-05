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

## Command structure

```text
pve-drive --remote <REMOTE:FOLDER> --source <SOURCE_LABEL> <COMMAND> [VMID] [OPTIONS]
```

Replace `<...>` with your values; brackets indicate optional arguments and are not typed. `upload` and `restore` require a VM ID; `list` does not. Global options (`--remote`, `--source`, `--work-dir`) go before the command; command options go after it.

## Upload, list, restore

For example, to upload a test archive:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a upload 100 --keep-vm
```

This command shuts down VM **100**, archives it in **pve-archive** on the **gdrive** remote, labels the archive’s source **pve-site-a**, and keeps the original VM stopped on the server.

| Part of the sample | Meaning |
| --- | --- |
| `--remote gdrive:pve-archive` | `gdrive` is the remote configured in rclone; `pve-archive` is the folder inside it. |
| `--source pve-site-a` | A unique label you choose for this source Proxmox server. Reuse it when listing or restoring its archives, even on another server. It does not connect to that server. |
| `upload` | Archive the VM and verify the uploaded files. Run this on the VM’s owning node. |
| `100` | The existing Proxmox VM ID to archive. Replace it with yours. |
| `--keep-vm` | Retain the original VM after verification, shut down and stopped. |

**Omit `--keep-vm` to delete the original VM and its disks after successful verification.**

List archived VMs, including sizes and snapshots:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a list
```

Restore the latest archive to its original VMID and storage:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a restore 100
```

Or restore on a destination node under another VMID and storage. `--target-vmid 200` selects an unused destination VM ID, `--storage destination-dir` selects an existing Proxmox storage ID, and `--unique` generates new MAC addresses:

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
