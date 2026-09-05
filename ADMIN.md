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

Successful `upload` and `restore` remove staging by default; `--keep-local` retains it. Failures retain recovery files. `recover`, advanced `archive`, and `verify` retain staging unless `--cleanup-local` is supplied.

## Supported archives and limitations

| VM layout | Archive format |
| --- | --- |
| No snapshots | zstd-compressed VMA via `vzdump`, restored with `qmrestore` |
| Supported snapshots | Native QCOW2 files and full VM/snapshot configuration |

Native mode requires VM-owned standalone QCOW2 files on directory storage, with identical disk attachments across snapshot sections. Each disk’s internal snapshot names must match Proxmox’s snapshot configuration. Backing chains, external data files, encryption, saved RAM, raw disks, cloud-init disks, and changing snapshot disk attachments are rejected. Native archives must be restored by this script, not the Proxmox VMA backup UI.

Native copying reads bytes sequentially, preserves zero-filled regions as sparse output, flushes the destination, and checks SHA-256. It does not convert QCOW2 images or use reflinks/copy offloading.

Preflight rejects protected/template VMs, unsupported locks, pending configuration, HA/replication membership, unused or excluded data disks, passthrough devices, physical CD-ROMs, custom QEMU arguments, hooks, and external cloud-init snippets. Backup locks are accepted only by the matching resume/recovery paths. Standard cloud-init drives are supported in VMA mode only. LXC and Proxmox Backup Server repositories are not supported.

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

On success, the archive becomes available to `list`/`restore`, and the VM is retained, stopped, and unlocked. Recovery never deletes the VM. Staging is retained unless `--cleanup-local` is supplied. A matching existing cloud marker permits retry while the VM still holds its backup lock. Missing cloud files cannot be repaired by `recover`; they require a separate upload.

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
