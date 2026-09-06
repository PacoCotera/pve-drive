# pve-drive

**Large Proxmox VM archives. Parallel Google Drive transfers. Simple administration.**

pve-drive takes care of shutting down, archiving, verifying, and restoring Proxmox QEMU virtual machines using Google Drive and rclone. It helps you move large VMs off a host, retain verified cloud archives, and restore them on the same or another Proxmox node—without managing individual archive parts yourself.

## Why pve-drive?

- **Use more of your available bandwidth.** Eight parallel transfers upload independent archive parts instead of funneling a large image through one transfer. Restores download parts concurrently, too. Google Drive remains your storage backend.
- **Back up large VMs with little spare disk space.** VMs without snapshots stream compressed backups through a bounded spool: just 2.25 GiB of payload staging plus 1 GiB of free-space headroom with the defaults. You do not need room for a complete local backup before uploading.
- **Verify before trusting an archive.** Each part and the complete payload have recorded checksums. Upload checks every part against remote metadata and verifies the manifest before marking an archive complete. Restore checks each part and the reconstructed whole-file SHA-256 before creating the VM. Optional full read-back adds independent upload verification.
- **Wait out Google Drive upload quotas.** Recognized quota blocks retry hourly, up to 24 times by default. Streaming production pauses when the spool fills and continues when uploads can proceed. Retained recovery data lets eligible interrupted uploads and downloads resume.
- **Keep supported QCOW2 snapshots intact.** VMs with supported internal snapshots use native archives that preserve exact QCOW2 bytes and Proxmox snapshot configuration. No disk conversion is involved.
- **Keep administration focused on VMs.** Upload, list, and restore by VMID. Archives are organized by source node label and VMID, and incomplete attempts stay out of the complete-backup listing. Guarded cleanup removes abandoned attempts.
- **Restore where you need the VM.** Choose another node, an unused VMID, or destination storage. Keep the source by default, or explicitly reclaim its disks with `--delete-vm` after successful archive verification.

## Three everyday commands

After installation and remote configuration:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a upload 100
pve-drive --remote gdrive:pve-archive --source pve-site-a list
pve-drive --remote gdrive:pve-archive --source pve-site-a restore 100
```

`--source` identifies the originating PVE node; keep that label when restoring elsewhere. Upload stops the VM. **The source VM and disks are retained by default. Add `--delete-vm` only when you want them removed after successful verification.** Restore requires an unused destination VMID and leaves the restored VM stopped.

## The right archive for the VM

The normal upload command chooses automatically:

| VM layout | Archive | Practical benefit |
| --- | --- | --- |
| No snapshots | Compressed multipart VMA | Parallel uploads with bounded local staging, even for very large disks. |
| Supported internal QCOW2 snapshots | Native multipart QCOW2 and configuration | Exact disk bytes and supported snapshots preserved. Requires staging for the original QCOW2 file lengths. |

The small upload spool applies to compressed VMA, which preserves backed-up guest disks and configuration rather than the original QCOW2 container bytes. Multipart restores stage downloaded parts and their verified reconstruction: allow twice the archive payload size plus 1 GiB, in addition to destination disk capacity.

Quota recovery keeps the same VMA stream only while its producer remains alive. After interruption, fully produced VMA archives and native multipart uploads can resume from retained data; interrupted VMA production starts a new attempt. See the [administrator guide](ADMIN.md) for recovery details and supported layouts.

Throughput depends on the host, compression, network, and Drive. The [benchmark guide](BENCHMARK.md) provides a reproducible comparison with single-file uploads. Transfer and part tuning are available under `upload --help` and `restore --help`; routine use needs no extra switches.

**Status: experimental.** Validate upload, restore, and snapshot rollback before using `--delete-vm`.

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
pve-drive --remote gdrive:pve-archive --source pve-site-a upload 100
```

The example archives VM `100` to `gdrive:pve-archive` under source identifier `pve-site-a`. The source VM is shut down before archiving and retained in a stopped state after verification.

| Argument | Description |
| --- | --- |
| `--remote gdrive:pve-archive` | Destination folder `pve-archive` on the configured rclone remote `gdrive`. |
| `--source pve-site-a` | Persistent identifier for the source node. Use the same identifier for listing and restoration, including restoration on another node. |
| `upload` | Archive and verify the VM. Execute on the node that owns the source VM. |
| `100` | Proxmox ID of the source VM. |
| `--delete-vm` | Optional: delete the source VM and disks after all verification succeeds. Omit to retain them stopped. |

**Upload retains the source VM by default. To explicitly archive and remove it, use `upload 100 --delete-vm`.**

VMs without snapshots automatically use compressed VMA streaming with parallel Google Drive uploads. The default upload spool is bounded to 2.25 GiB, with 1 GiB of additional free-space headroom required; it does not stage the VM's full QCOW2 files. No `--stream` option is needed.

VMs with supported internal QCOW2 snapshots retain exact native disk bytes and Proxmox snapshot configuration. Native upload still needs staging equal to the original QCOW2 file lengths.

Both paths use eight concurrent transfers and 128 MiB Drive upload chunks. Native parts default to 4 GiB; compressed VMA parts default to 256 MiB so uploads can overlap promptly. Advanced tuning is under `upload --help`.

Quota blocks retry hourly up to 24 times. Keep the process running to resume the same VMA stream after quota clears; production pauses when its spool fills. If interrupted after production completes, repeat the normal command to finish the remaining uploads. If production itself was interrupted, the command starts a new stream and retains the old incomplete attempt for `cleanup`. See [recovery and tuning](ADMIN.md), the [archive format](MULTIPART_FORMAT.md), and the [live benchmark procedure](BENCHMARK.md).

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

For older single-file VMA archives, use `--stream` to restore without a staging copy. New multipart VMA archives use the normal restore command and stage compressed data for verification:

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

## Progress you can follow

Console messages use the server's local date and time directly, alongside total elapsed time. Each numbered step reports its start and completion, including its duration. Long disk reads show byte progress, speed, and ETA; the operation plan and source-retention policy are printed at startup.

```text
[2026-09-06 16:33:29 | elapsed 00:39:50] Step 4 started: Verifying staged parts and whole-file SHA-256: scsi0.qcow2
[2026-09-06 16:49:10 | elapsed 00:55:31] Step 4 complete: Verifying staged parts and whole-file SHA-256: scsi0.qcow2 (duration 00:15:41)
```

Step counts depend on the disks, archive format, and retries. Repeated source checks explain their purpose: before upload, before archive publication, and after publishing the completion marker. The normal native upload combines staging SHA-256 and MD5 verification in one read-back pass.

**Upgrading from 0.10.0:** remove `--keep-vm` from commands and automation; it is no longer accepted. Add `--delete-vm` only to jobs that should remove the source. Update after active operations finish.

## Operational notes

- Successful upload, restore, and recovery remove local staging files by default. Use `--keep-local` to retain them. Files needed after an interrupted operation are preserved.

- Supported internal QCOW2 disk snapshots are preserved; unsupported layouts are rejected.
- GPU and HD-audio PCI passthrough assignments are preserved. Review the hardware assignments before starting a restored VM on another node.
- Upload verification compares local file sizes and MD5 hashes against remote metadata. `--deep-verify` performs full read-back verification. Restoration validates SHA-256 checksums.
- The default staging directory is `/var/lib/vz/pve-drive`; override it with the global option `--work-dir PATH`. Native restoration requires capacity for both the downloaded archive and the restored disks.

Refer to [ADMIN.md](ADMIN.md) for recovery procedures, storage requirements, supported layouts, and diagnostics. Use `pve-drive --help` or `pve-drive COMMAND --help` for the command reference.

[Changelog](CHANGELOG.md) · [MIT License](LICENSE) · © 2026 Paco Cotera
