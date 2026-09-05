# Administrator guide

For installation and everyday commands, see [README.md](README.md).

## Source identity and archive selection

Run uploads on the VM’s owning node. Use a unique, stable `--source` label for each server; the tool does not enforce label ownership or connect to other nodes over SSH. A migrated VM may have older archives under its previous label.

Archives use `REMOTE/sources/SOURCE/VMID/TIMESTAMP-UUID/`. The manifest records the source label, actual node name, VM configuration, date, and checksums. A verified `COMPLETE` marker makes an archive eligible for listing and restore.

`list` shows the latest complete version per VM, with VMID, NAME, SIZE, FORMAT, SNAPSHOTS, and ARCHIVED UTC. SIZE comes from the manifest; it is not guest disk capacity or Drive quota usage. The table appears after metadata loads. `list --all-versions` includes older versions and their backup IDs.

Restore selects the newest complete timestamp. A bad latest manifest fails rather than silently selecting an older copy. Existing VMIDs are never overwritten. Restored VMs stay stopped with current `onboot=0`; inspect hardware and networking before starting. `--unique` changes MAC addresses throughout native snapshot configurations, but not guest IPs or SMBIOS identity. Native archives spanning multiple storage IDs require a destination `--storage` override.

Cloud archives are retained after restore. There is no automatic remote pruning.

## Configuration and installation

The default rclone configuration is `/root/.config/rclone/rclone.conf`; override it with `--rclone-config PATH` before the command. Shared Drives use rclone’s `team_drive` setting, not `shared_with_me`. A service account needs suitable Drive membership. See [rclone’s Drive documentation](https://rclone.org/drive/), including its OAuth client setup.

Crypt remotes require `--deep-verify` when they do not expose MD5. Preserve encryption keys and rclone configuration separately. Fast recovery requires remote MD5 support.

The installer places the executable at `/usr/local/sbin/pve-drive`. It preserves an old directory at that location as `pve-drive.previous-<timestamp>-<suffix>`. Installation is atomic and refused while a pve-drive task is active. It does not install dependencies or move VM disks, staging, or credentials.

## Space and staging

Default staging is `/var/lib/vz/pve-drive`. Check free space before starting. Native QCOW2 archives are uncompressed and can exceed the guest’s virtual disk capacity because of snapshots. Upload staging requires the full disk-file lengths plus 1 GiB. Restore requires space for a download and a destination copy: on one filesystem allow approximately twice the archive size plus headroom. Sparse savings are not assumed.

To stage a restore on a larger filesystem:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a \
  --work-dir /mnt/backup-space/pve-drive-staging \
  restore 100 --target-vmid 200 --storage destination-dir --unique
```

Use an existing mounted filesystem with adequate space. `--storage` chooses restored disk storage; `--work-dir` chooses temporary staging independently.

Successful `upload`, `restore`, and `recover` remove staging by default; `--keep-local` retains it. Failed operations preserve recovery data; empty staging directories are removed when the initial space check fails. Advanced `archive` and `verify` retain staging unless `--cleanup-local` is supplied.

## Streaming upload and restore

`upload VMID --stream` sends a zstd-compressed VMA directly from `vzdump --stdout` to `rclone rcat`. The relay uses bounded buffers, calculates SHA-256 and MD5, and feeds the same compressed bytes to `zstd --test`. It requires successful exits from all processes and checks that vzdump included every configured data disk. Cloud size and MD5 verification, manifest verification, and completion-marker read-back must succeed before source VM deletion.

Preflight requires the remote to advertise `PutStream` and MD5 support. Remotes that would spool an unknown-size upload to local disk are rejected. `--stream` cannot be combined with snapshots, `--resume`, or `--deep-verify`; use staged upload for those cases. Small logs, temporary Proxmox metadata, and recovery receipts remain under `--work-dir`. `--keep-local` retains these files, not an archive. Upload progress reports compressed bytes passed to the uploader, average rate, and elapsed time; final compressed size and ETA are unknown until the stream ends. A successful uploader exit and metadata verification establish remote completion.

`restore VMID --stream` supports both staged and streamed VMA archives, including older versions. It reads the verified manifest and checks the remote archive size before creating the VM; when the manifest includes MD5, that must match remote metadata too. Incoming compressed bytes are hashed, decompressed, and passed to `qmrestore` through pipes. SHA-256 and size must match before completion. No local archive is created. Native QCOW2 archives still require staged restoration to preserve internal snapshots. Destination storage must accommodate the restored disks; eliminating staging does not reduce that requirement.

Streaming restore writes destination disks before the final SHA-256 result is available. The target is never automatically started. Successful restoration disables onboot; a final checksum mismatch leaves the target backup-locked. Other failures can leave partial disks or a Proxmox restore lock. Inspect `restore-state.json`, the logs, and the target VM before removing a failed target and retrying. The script does not overwrite an occupied VM ID or delete partial restore disks automatically. Cloud archives are retained.

### Interrupted streams

An interrupted upload has no resumable local archive. Check the VM and Proxmox task state before retrying; keep the source stopped. If a backup lock remains, unlock only after confirming the failed task has ended. Run `upload VMID --stream` again to start a new attempt. Incomplete cloud folders remain hidden from `list`; their exact destination is recorded in `attempt.json`. They are retained for inspection and can be removed manually once the attempt is confirmed incomplete. No automatic remote pruning is performed.

If all stream processes completed but cloud verification or finalization failed, a `stream-complete.json` receipt enables recovery without retransmitting disk data:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a \
  recover 100 --resume /var/lib/vz/pve-drive/stream-100-EXAMPLE
```

Use the exact printed directory, with the matching `--work-dir` if overridden. Recovery validates the receipt, source identity, unchanged stopped VM configuration, and cloud checksums. It uploads only the small manifest/marker as necessary and always retains the source VM. A receipt is required: recovery cannot certify a partial stream. Preserve the receipt until recovery finishes. Successful recovery removes local metadata unless `--keep-local` is supplied.

See the upstream [vzdump documentation](https://pve.proxmox.com/pve-docs/vzdump.1.html) and [rclone rcat documentation](https://rclone.org/commands/rclone_rcat/) for streaming behavior. rcat cannot replay a failed stream; backend chunk retries remain available where supported.

## Supported archives and limitations

| VM layout | Archive format |
| --- | --- |
| No snapshots | zstd-compressed VMA via `vzdump`, restored with `qmrestore` |
| Supported snapshots | Native QCOW2 files and full VM/snapshot configuration |

Native mode requires VM-owned standalone QCOW2 files on directory storage, with identical disk attachments across snapshot sections. Each disk’s internal snapshot names must match Proxmox’s snapshot configuration. Backing chains, external data files, encryption, saved RAM, raw disks, cloud-init disks, and changing snapshot disk attachments are rejected. Native archives must be restored by this script, not the Proxmox VMA backup UI.

Native copying reads bytes sequentially, preserves zero-filled regions as sparse output, flushes the destination, and checks SHA-256. It does not convert QCOW2 images or use reflinks/copy offloading.

Preflight rejects protected/template VMs, unsupported locks, pending configuration, HA/replication membership, unused or excluded data disks, unsupported passthrough devices, physical CD-ROMs, custom QEMU arguments, hooks, and external cloud-init snippets. Backup locks are accepted only by the matching resume/recovery paths. Standard cloud-init drives are supported in VMA mode only. LXC and Proxmox Backup Server repositories are not supported.

Direct PCI passthrough is supported for display controllers and HD-audio devices. Before archiving, the script checks each assigned PCI function against the source node's Linux device classes, including all functions selected by an address without a function suffix. Storage controllers, USB controllers, other device classes, missing devices, resource mappings, and mediated devices are rejected. Native snapshot sections receive the same checks. PCI assignments are preserved in the VM and snapshot configurations; physical hardware and its state are not archived. Review these assignments before starting or rolling back a restored VM, particularly on another node. `--unique` does not change PCI assignments.

Storage ISO references are retained, but ISO contents are not archived. Reattach or eject unavailable media before starting a restored VM. Host bridges, storage definitions, firewall files, cluster permissions, HA settings, and other host resources are not packaged.

Use a maintenance window: do not start, modify, migrate, unlock, or replace a VM during an operation. The tool serializes its own tasks per node and uses Proxmox locks; administrators can still bypass them. Site defaults and hooks in `/etc/vzdump.conf` apply to VMA backups. Deletion is not transactional: storage errors may leave a partial deletion. Test a real restore and snapshot rollback before relying on deletion.

## Verification

By default, upload compares every payload file’s size and MD5 with remote metadata. Missing/malformed hashes, mismatches, missing/extra files, or duplicate paths fail before the completion marker or VM deletion. There is no size-only fallback. This reads local files for hashing but does not download the archive again.

For an independent read of stored bytes, or a remote without MD5, select full read-back at upload time:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a \
  upload 100 --keep-vm --deep-verify
```

Local copy checks, manifest checks, and restore SHA-256 checks remain required. Standalone `verify BACKUP_ID` always downloads and validates SHA-256. Checksums establish file integrity, not guest/application health. The completion marker is not a signature; restrict remote write access. Remote immutability is a tool convention, not a Drive permission guarantee.

## Recovery

First confirm the original operation has stopped. Inspect its log, staging path, `qm status VMID`, and `qm config VMID`. Do not manually unlock a VM before using either recovery path below.

### Failed native local copy, before a manifest exists

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a \
  upload 100 --resume /var/lib/vz/pve-drive/native-100-EXAMPLE --keep-vm
```

Use the exact printed staging path. The original VM must remain stopped and backup-locked, with matching full configuration. This replaces failed local copies in the same staging directory and continues normal upload checks. It refuses stages that already contain a manifest or completion marker. Hash failures retain `copy-mismatch.json` with sizes, timestamps, and checksum diagnostics; they cannot be bypassed.

### Uploaded files, interrupted verification or finalization

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a \
  recover 100 --resume /var/lib/vz/pve-drive/native-100-EXAMPLE
```

Run on the original source node with the original source label, complete local payload, and saved manifest. The VM must remain stopped and backup-locked. Recovery checks identity, configuration, local SHA-256, disk integrity, and cloud sizes/MD5. It computes SHA-256 and MD5 in one local read; it does not recopy, reupload, or download archive data. Only the completion marker is uploaded and read back.

On success, the archive becomes available to `list`/`restore`, and the VM is retained, stopped, and unlocked. Recovery never deletes the VM. Staging is removed after successful recovery; `--keep-local` retains it. The older `--cleanup-local` option remains accepted. A matching existing cloud marker permits retry while the VM still holds its backup lock. Missing cloud files cannot be repaired by `recover`; they require a separate upload.

### Other failures

Incomplete remote folders without `COMPLETE` are ignored by listing and automatic restore. Rclone retries transient errors within an operation; a fresh upload creates a new version. Neither recovery command provides general interrupted-download resumption.

Interrupted native restores may leave a create-locked placeholder VM and allocated disks. Staging’s `restore-state.json` records allocations, including disks that may not yet be attached. Inspect before cleanup; do not simply unlock and start the placeholder. VMA restore failures can also leave partial VMs. The script does not automatically delete these allocations and will refuse to overwrite the VMID on retry.

If only final cleanup failed, the operation may already have succeeded. Inspect state before retrying. Manually remove partial local/remote copies only after confirming what they contain. To abandon a failed operation, unlock manually only after all related tasks have stopped and the VM state has been checked.

## Diagnostics and advanced commands

Progress shows elapsed time, percentage, speed, and ETA when measurable. Each run prints its log path under `/var/log/pve-drive/`. Logs are retained and may include VM configuration and paths; review before sharing. Add `--verbose` before the command to display raw commands and tool output.

Advanced `archive` selects a format explicitly and requires `--keep-vm` or `--delete-vm`. VMA does not preserve snapshots; deleting a snapshotted VM in that mode requires explicit `--allow-snapshot-loss`. Routine `upload` never enables that flag.

Use `list --all-versions` to select a specific archive:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a verify BACKUP_ID --cleanup-local
pve-drive --remote gdrive:pve-archive --source pve-site-a restore BACKUP_ID 200 --storage destination-dir --unique
```

Old `move-to-cloud`/`move-from-cloud` aliases remain available. `--legacy-layout` replaces `--source` for reading archives from before source folders existed; it does not allow uploads or recovery.

Run `pve-drive COMMAND --help` for command options. From the checkout, `python3 -m unittest discover -s tests -v` runs simulated lifecycle tests and local copy/installer checks. These tests do not replace end-to-end Proxmox restore and snapshot rollback validation.

Completion messages distinguish local staging cleanup from retention of the cloud archive. Upgrading does not remove directories retained by earlier versions; inspect those before manual cleanup.
