# pve-drive

Move Proxmox QEMU VMs to Google Shared Drive using rclone, and restore them by VMID. Version **0.7.1**.

**Status: experimental.** Automated tests cover simulated lifecycle failures and local copying, but end-to-end Proxmox/Drive restore and snapshot rollback validation is still in progress. Test with `--keep-vm` before using this tool to delete a VM.

The routine commands are **upload**, **list**, and **restore**. Internal backup IDs and archive formats are handled automatically. [ADMIN.md](ADMIN.md) is the short admin guide.

## Install and update

Run on the Proxmox server. Clone the repository, then install the standalone command:

```bash
git clone https://github.com/PacoCotera/pve-drive.git /opt/pve-drive &&
cd /opt/pve-drive &&
sudo ./install.sh
```

Update after any active upload/restore finishes:

```bash
cd /opt/pve-drive && git pull --ff-only && sudo ./install.sh
```

The installer places the command at `/usr/local/sbin/pve-drive`. An old directory at that path is preserved under `pve-drive.previous-<timestamp>-<suffix>`. Installation is atomic and blocked while a pve-drive operation is running. VM storage, staging directories, and rclone credentials are not relocated. Installation does not install dependencies.

Requirements: Linux, root, Python 3, rclone, and the normal Proxmox tools (`pvesh`, `qm`, `pvesm`, `vzdump`, `qmrestore`, `qemu-img`, `zstd`). Git is needed for updates. Python uses only its standard library. LXC and Proxmox Backup Server repositories are not supported.

## Configure rclone once

```bash
apt-get update
apt-get install git python3 rclone zstd
rclone config
```

Create a Google Drive remote called `gdrive`, authenticate an account with access to your Shared Drive, and select the Shared Drive. Use your own OAuth client ID; see [rclone's Drive documentation](https://rclone.org/drive/). Shared Drives use `team_drive`; `shared_with_me` is a different feature. A service account also works if granted appropriate membership.

```bash
rclone lsd gdrive:
rclone mkdir gdrive:pve-archive
```

Default rclone config: `/root/.config/rclone/rclone.conf`. Override using `--rclone-config PATH`. A crypt remote can be used; preserve its configuration and encryption keys separately.

## Routine commands

Global options (`--remote`, `--source`, `--work-dir`, `--rclone-config`) go **before** the command.

Upload VM 100, verify the archive, and delete the original VM and its disks:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a upload 100
```

Add `--keep-vm` for a test upload: the VM remains on the server, stopped. Supported snapshot history is automatically included; unsupported snapshot layouts abort rather than silently discarding history.

List the latest complete cloud archive for each VM:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a list
```

Columns: VMID, name, format, snapshot names, and UTC archive date. Incomplete uploads are ignored. Use `list --all-versions` only when older versions or internal backup IDs are needed.

Restore VM 100 using its latest complete archive, original VMID and original storage:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a restore 100
```

Restore source VM 100 under a different VMID and storage:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a \
  restore 100 --target-vmid 200 --storage destination-dir --unique
```

Run restore on the destination PVE, but keep the **original source label**. Existing VMIDs are refused. Restored VMs stay stopped with current `onboot=0`. Inspect hardware and networking before starting. `--unique` changes MAC addresses consistently, including snapshot sections for native archives; it does not change guest static IPs or SMBIOS identity. The cloud backup remains after restore.

Latest means the newest UTC timestamp in a complete backup ID. A bad latest manifest causes an error, not silent fallback to an older copy. Native archives that originally span multiple storage IDs require an explicit destination `--storage` override.

Successful upload and restore remove their staging files by default. Add `--keep-local` to retain them. Failures retain recovery files. No automatic remote pruning is performed: the remote archive may be the only remaining copy of a deleted VM.

## Source server identity

Assign each server a unique stable label such as `pve-site-a` or `pve-site-b`; do not reuse labels across independent servers with the same hostname. The manifest records both this label and the actual Proxmox node name, along with VMID, name/configuration, timestamp, and hashes. Label ownership is not automatically enforced.

```text
pve-archive/sources/
  pve-site-a/100/<timestamp-uuid>/
  pve-site-b/100/<timestamp-uuid>/
```

See source folders with `rclone lsf gdrive:pve-archive/sources --dirs-only`. The tool does not SSH to nodes. Upload must run on the VM's owning node. A migrated VM may have older archives under another source label.

## Archive formats and scope

`upload` chooses:

- **No snapshots:** zstd-compressed VMA using `vzdump`, restored with `qmrestore`.
- **Snapshots present:** native QCOW2 files plus the full VM configuration, including snapshot sections and parent relationships. Native archives are restored by this script, not through the Proxmox VMA backup UI.

Native mode currently supports VM-owned standalone QCOW2 files on directory storage, with identical disk attachments across all snapshot sections. It checks the internal snapshot table against the Proxmox snapshot names for every disk. It rejects backing chains, external data files, encryption, saved RAM, raw disks, cloud-init disks, and changing disk attachments between snapshots. See [QEMU's internal snapshot format](https://www.qemu.org/docs/master/interop/qcow2.html).

Native copying reads every byte sequentially, creates holes only for buffers actually read as zero, flushes the destination, and verifies SHA-256. It does not convert QCOW2 files or use reflinks, copy offloading, or filesystem hole reporting.

Both modes reject protected/template VMs, unsupported existing locks, pending configuration, HA/replication membership, unused/excluded data disks, host passthrough, physical CD-ROM devices, custom QEMU arguments, hooks, and external cloud-init snippets. A matching backup lock is accepted only by the explicit native local-copy resume flow.

Storage ISO references are allowed, but ISO contents are not archived. References are retained in the saved configuration; VMA manifests additionally record `external_media`. Reattach or eject unavailable ISOs before starting a restored VM. Empty CD-ROM drives are accepted. Standard cloud-init drives are accepted by VMA mode only.

The archive does not package host bridges, storage definitions, Proxmox firewall files, cluster permissions, HA settings, or external resources. Byte verification is not a guest boot/application health test. Perform a real restore and snapshot rollback test before relying on deletion of the original.

## Space and maintenance requirements

Default staging: `/var/lib/vz/pve-drive`. Set `--work-dir PATH` before the command to use another filesystem. QCOW2 files containing snapshots can be larger than the guest disk capacity. Native archives are uncompressed. New native staging requires the full file length plus 1 GiB; restore needs both a download and a destination copy. On one filesystem, allow approximately twice the archive size plus headroom. Sparse savings are not assumed. Failed space checks may leave downloaded files for inspection.

Use an exclusive maintenance window. Do not start, modify, migrate, unlock, or replace the VM during an operation. The tool serializes its own tasks on each node and uses a Proxmox backup lock. It does not prevent administrators from bypassing locks. In VMA mode, Proxmox holds its normal backup lock during vzdump, followed by the tool's upload lock. In native mode the tool holds a backup lock before copying.

For VMA backups, site defaults and hooks in `/etc/vzdump.conf` still apply. All upload paths require successful cloud verification and completion-marker publication before VM deletion. By default every uploaded file must have a matching size and MD5 hash reported by the remote. Use `--deep-verify` to perform full [rclone read-back verification](https://rclone.org/commands/rclone_check/) instead. Deletion itself is not transactional: storage errors can leave a partial deletion, with recovery copies retained.

## Resume a failed native local copy

This is limited to a failed copy **before manifest creation/upload**:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a \
  upload 100 --resume /var/lib/vz/pve-drive/native-100-EXAMPLE --keep-vm
```

Use your exact staging path. Leave the VM stopped with its backup lock; **do not unlock first**. Resume requires the saved and current full configuration to match, replaces failed copies in the same directory, and performs all normal verification. It refuses stages with a manifest or completion marker. It does not create a second staging directory, but rewriting still requires enough filesystem space. It is not general upload/download resumption.

A checksum failure writes sizes, timestamps, and before/after source and destination SHA-256 values to `copy-mismatch.json`. A mismatch stops the operation; it is never bypassed. Sequential copying was introduced after a real accelerated-copy mismatch. The cause of that host mismatch has not been established.

## Other failure recovery

The console prints the staging path. Failed uploads may leave remote directories without `COMPLETE`; listing and automatic restore ignore them. Rclone retries transient errors within an operation. A fresh upload uses a new staging directory/version rather than trusting an interrupted one.

First verify that no operation remains active, then inspect `qm status VMID`, `qm config VMID`, and recovery files. To abandon a failed operation, manually unlock only after inspection and after all related tasks have stopped. Do not apply that unlock step when using `--resume`, which requires the backup lock.

Interrupted native restores can leave a create-locked placeholder VM and allocated disks. `restore-state.json` in staging records allocations; some disks may not yet be attached to the placeholder. Inspect before cleanup; do not just unlock and start it. The tool never automatically deletes those partial allocations. Interrupted VMA restores can also leave partial VMs. Restore refuses to overwrite either case. If only final cleanup fails, the operation may already have succeeded; inspect state before retrying.

Remote immutability is a tool convention, not a Google permission guarantee. The completion checksum is not a signature; control who can write to the remote. Local and remote partial copies can be removed manually after inspection.

## Advanced commands

`archive` explicitly selects VMA or native mode and requires `--keep-vm` or `--delete-vm`. Unlike routine upload, it retains local staging by default; `--cleanup-local` removes staging after success. VMA archive does not contain snapshot history, and deleting a VM with snapshots in that mode requires `--allow-snapshot-loss`. Routine `upload` never sets that flag.

Use `list --all-versions` to obtain a backup ID for explicit verification or an older restore:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a verify BACKUP_ID --cleanup-local
pve-drive --remote gdrive:pve-archive --source pve-site-a restore BACKUP_ID 200 --storage destination-dir --unique
```

`verify` retains downloads unless `--cleanup-local` is supplied. Explicit restores use the same successful-cleanup default as routine restore. Old `move-to-cloud`/`move-from-cloud` command aliases remain available. `--legacy-layout` replaces `--source` for reading old archives made before source directories existed; it is not allowed for uploads.

## Help and validation

```bash
pve-drive --help
pve-drive upload --help
pve-drive list --help
pve-drive restore --help
pve-drive archive --help
pve-drive verify --help
```

From the source checkout, run `python3 -m unittest discover -s tests -v`. Tests include simulated Proxmox/rclone failure paths, real local sparse-file copies, resume guards, and installer rollback. They do not execute real VM deletion. GitHub Actions also checks installation and help on Linux. End-to-end Proxmox/Drive restore validation is still required.

## License

MIT License, copyright (c) 2026 Paco Cotera. See [LICENSE](LICENSE). Proxmox, QEMU, rclone, and other external dependencies retain their own licenses.

## Progress and troubleshooting

The default display shows stages and elapsed time. Disk copying, SHA-256 checks,
and rclone transfers show percentage, bytes, speed, and estimated time remaining
when totals are available. Stages without measurable byte progress show elapsed
time only. Optional deep verification reads the entire archive back from the cloud.
An interactive terminal refreshes the progress line; redirected output prints
periodic lines without terminal control codes.

Each run prints the path of its private diagnostic log under `/var/log/pve-drive/`.
Logs include commands, tool output, and archive IDs. They can contain VM names,
configuration, and storage paths: review before sharing. Logs are retained;
remove old logs when no longer needed.

To show raw commands and tool output as well, put `--verbose` before the command:

```bash
pve-drive --verbose --remote gdrive:pve-archive --source pve-site-a upload 100 --keep-vm
```

Display updates take effect on the next run after installation. Let an active
upload or restore finish before updating.

## Upload verification modes

The default upload/archive verification compares every local payload file's size
and MD5 against the remote's stored metadata. Google Drive supports MD5 for these
binary files. This reads the local files for hashing but does not download the
archive again. Local SHA-256 copy checks, manifest checks, restore SHA-256 checks,
and the completion marker remain required.

Missing or malformed MD5 hashes, mismatches, missing/extra files, and duplicate
remote paths stop the operation before completion-marker publication or VM deletion.
There is no fallback to size-only checks. For remotes without MD5 support, select
full read-back verification when starting the upload:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a upload 100 --deep-verify --keep-vm
```

`archive` also accepts `--deep-verify`. The standalone `verify BACKUP_ID` command
continues to download the backup and validate SHA-256; it audits existing archives.
Metadata verification detects transfer corruption but is not an independent read
of the stored bytes. Use deep verification when that additional check is wanted.
This change removes the default verification download; it does not accelerate the
upload itself. An already running operation keeps its original verification mode.

## Recover an upload interrupted during verification

If the archive files reached Drive but verification was interrupted, use `recover`
on the original PVE node with the original source label and exact staging path:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a \
  recover 100 --resume /var/lib/vz/pve-drive/native-100-EXAMPLE
```

Stop the original pve-drive operation first. Leave the VM stopped and backup-locked;
do not manually unlock it. The command requires the saved manifest and complete
local payload. It validates the source/node/VM identity, saved configuration,
local SHA-256 hashes, disk integrity, and remote file sizes/MD5 hashes. SHA-256 and
MD5 are computed together in one local read. It does not recopy disks, reupload
archive files, or download the archive. Only the small completion marker is
uploaded and read back. Both native QCOW2 and VMA archives are supported.

On success the cloud backup becomes available to `list` and `restore`, and the
original VM is retained, stopped, and unlocked. Recovery never deletes a VM.
Staging is retained by default; append `--cleanup-local` to remove it after success.
Missing/corrupt files, wrong source/node/configuration, unavailable MD5, or a failed
marker write leave recovery incomplete and the VM locked. A retry accepts an
already published matching marker if the VM still holds its backup lock.

`upload --resume` repairs a failed local copy before a manifest exists;
`recover --resume` finalizes an existing upload after a manifest exists. Recovery
does not upload missing files or bypass verification. An incomplete cloud payload
requires a separate upload; it cannot be marked complete by this command.

```bash
pve-drive recover --help
```

## Archive listing

`list` displays VMID, NAME, SIZE, FORMAT, SNAPSHOTS, and ARCHIVED UTC in aligned columns. SIZE is the recorded archive size from the manifest, including native snapshot data; it is not the guest virtual disk capacity or Drive quota usage. No extra Drive requests are needed for size. The command displays "Loading cloud archives..." while fetching metadata, then prints the complete table. `--all-versions` also shows BACKUP ID.
