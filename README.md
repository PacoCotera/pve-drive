# pve-drive

Archive Proxmox QEMU virtual machines to a Google Shared Drive using rclone. Restore archives by source VM ID, with support for alternative destination nodes, VM IDs, and storage.

**Status: experimental.** Validate upload, restore, and snapshot rollback with `--keep-vm` before enabling source VM deletion.

## Installation

Run installation and VM operations as root on the Proxmox node. Dependencies: Git, Python 3, rclone, zstd, and the standard Proxmox command-line tools.

```bash
apt-get install git python3 rclone zstd
git clone https://github.com/PacoCotera/pve-drive.git /opt/pve-drive &&
cd /opt/pve-drive && ./install.sh
```

Update the installed executable after all active operations have completed:

```bash
cd /opt/pve-drive && git pull --ff-only && ./install.sh
```

## Configuration

Use `rclone config` to configure a Google Drive remote named `gdrive` and select the destination Shared Drive. Refer to the [rclone Google Drive documentation](https://rclone.org/drive/) for authentication and configuration requirements.

```bash
rclone lsd gdrive:
rclone mkdir gdrive:pve-archive
```

## Usage

```text
pve-drive --remote REMOTE:FOLDER --source SOURCE_LABEL COMMAND [VMID] [OPTIONS]
```

Uppercase terms represent argument values. Square brackets denote optional syntax; `VMID` is required for `upload` and `restore` and omitted for `list`. Global options precede `COMMAND`; command-specific options follow its positional arguments.

### Upload

Create and verify an archive while retaining the source VM:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a upload 100 --keep-vm
```

The example archives VM `100` to `gdrive:pve-archive` under source identifier `pve-site-a`. The source VM is shut down before archiving and retained in a stopped state after verification.

| Argument | Description |
| --- | --- |
| `--remote gdrive:pve-archive` | Destination folder `pve-archive` on the configured rclone remote `gdrive`. |
| `--source pve-site-a` | Persistent identifier for the source node. Use the same identifier for listing and restoration, including restoration on another node. |
| `upload` | Archive and verify the VM. Execute on the node that owns the source VM. |
| `100` | Proxmox ID of the source VM. |
| `--keep-vm` | Retain the source VM and its disks after verification. The VM remains stopped. |

**Without `--keep-vm`, a successful upload deletes the source VM and its disks after verification.**

For VMs without snapshots, `--stream` uploads directly without creating a local archive:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a upload 100 --stream --keep-vm
```

Streaming requires a remote with streaming upload and MD5 support, such as Google Drive. Interrupted transfers restart from the beginning. Native QCOW2 snapshot archives require staging.

Streaming upload progress shows an estimated maximum size and ETA based on configured disk capacities plus an overhead allowance. Compression can make the actual archive substantially smaller.

Native QCOW2 archives automatically use parallel Google Drive transfers while preserving the exact disk bytes and supported internal snapshots. Defaults are 4 GiB parts, eight transfers, and 128 MiB upload chunks. Normal commands are unchanged; advanced tuning is available in `upload --help` and `restore --help`.

Drive quota blocks are retried hourly up to 24 times. If interrupted, repeat the same upload command to resume one recorded unfinished multipart attempt. Keep its staging files and the source VM stopped and backup-locked. See [recovery and tuning](ADMIN.md), the [archive format](MULTIPART_FORMAT.md), and the [live benchmark procedure](BENCHMARK.md). Streaming VMA retains its existing 32 MiB default and restart behavior.

### List

List the latest complete archive for each source VM, including archive size and snapshot names:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a list
```

### Restore

Restore the latest complete archive to its original VM ID and storage:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a restore 100
```

To specify a different destination, use `--target-vmid` for an unused VM ID and `--storage` for an existing Proxmox storage ID. The `--unique` option generates new MAC addresses:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a \
  restore 100 --target-vmid 200 --storage destination-dir --unique
```

Restoration rejects occupied VM IDs and leaves the restored VM stopped. Guest static IP addresses are unaffected by `--unique`. The cloud archive is retained.

Use `--stream` to restore a VMA archive without downloading a staging copy:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a \
  restore 100 --target-vmid 200 --storage destination-dir --unique --stream
```

Streaming restoration checks SHA-256 as data is restored. A failed operation may leave unverified destination disks; inspect the target before retrying. Destination storage still needs sufficient capacity for the restored disks.

## Cleanup

Successful operations remove local staging unless `--keep-local` is used. Failed operations retain recovery files. To inspect and discard an abandoned attempt:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a cleanup
pve-drive --remote gdrive:pve-archive --source pve-site-a cleanup 100
pve-drive --remote gdrive:pve-archive --source pve-site-a cleanup 100 --apply
```

`cleanup VMID` finds recorded upload attempts for the selected source and previews removal. `--apply` discards their local recovery files and recorded incomplete cloud uploads. Completed cloud archives, VM disks, locks, and diagnostic logs are retained. Use the matching `--work-dir` for staging on another filesystem. Run cleanup after active tasks finish. Advanced: `--stage PATH` selects one attempt, including restore staging or older directories without source records.

## Operational notes

- Successful upload, restore, and recovery remove local staging files by default. Use `--keep-local` to retain them. Files needed after an interrupted operation are preserved.

- Supported internal QCOW2 disk snapshots are preserved; unsupported layouts are rejected.
- GPU and HD-audio PCI passthrough assignments are preserved. Review the hardware assignments before starting a restored VM on another node.
- Upload verification compares local file sizes and MD5 hashes against remote metadata. `--deep-verify` performs full read-back verification. Restoration validates SHA-256 checksums.
- The default staging directory is `/var/lib/vz/pve-drive`; override it with the global option `--work-dir PATH`. Native restoration requires capacity for both the downloaded archive and the restored disks.

Refer to [ADMIN.md](ADMIN.md) for recovery procedures, storage requirements, supported layouts, and diagnostics. Use `pve-drive --help` or `pve-drive COMMAND --help` for the command reference.

[Changelog](CHANGELOG.md) · [MIT License](LICENSE) · © 2026 Paco Cotera
