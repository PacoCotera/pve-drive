# Proxmox VM archive on Google Shared Drive

For routine administration, use the three commands in [ADMIN.md](ADMIN.md): `upload VMID`, `list`, and `restore VMID`. Upload moves the VM off the server after verification; restore automatically selects its latest complete archive. These commands clean up successful local staging by default (`--keep-local` retains it). The detailed sections below describe archive formats and advanced commands.

`pve_drive.py` is a Python 3 command-line tool using Proxmox and rclone. Default mode uses `vzdump` / `qmrestore` for the current VM state. `--format native-qcow2` preserves supported internal disk snapshot histories using complete QCOW2 files and the full Proxmox configuration. It supports QEMU VMs, not LXC containers or Proxmox Backup Server repositories. Python uses only its standard library. Check the installed version with `./pve_drive.py --version` (0.4.0).

## Native QCOW2 snapshots (directory storage)

An example VM configuration has one standalone QCOW2 disk on directory storage and one internal disk-only snapshot, `before-change`. This is the native mode's supported case. It archives the entire QCOW2 file without `qemu-img convert`, plus `vm.conf` including the `[before-change]` section and parent relationships. See [QEMU's internal snapshot format](https://www.qemu.org/docs/master/interop/qcow2.html). This is a custom archive restored by this script, not a VMA backup importable through the Proxmox backup UI.

Copy the updated single Python file to the server. Start by retaining the original VM:

```bash
./pve_drive.py --remote gdrive:pve-archive --source pve-site-a \
  archive 100 --format native-qcow2 --keep-vm --cleanup-local

./pve_drive.py --remote gdrive:pve-archive --source pve-site-a list
```

Use the printed backup ID and a free VMID for a restore test. Replace `BACKUP_ID` below. Your `destination-dir` is configured as directory storage and is a candidate target if enabled for disk images on this node:

```bash
./pve_drive.py --remote gdrive:pve-archive --source pve-site-a \
  restore BACKUP_ID 200 --storage destination-dir --unique --cleanup-local
qm listsnapshot 200
```

The script detects native format automatically during restore and verification. It checks every downloaded disk hash and QCOW2 snapshot table, reserves the destination VMID using `qm create` with a create lock, allocates new disk files via `pvesm alloc`, copies the original QCOW2 bytes, remaps disk references in every configuration section, and checks that Proxmox recognizes the snapshot names before unlocking. The VM stays stopped with current `onboot=0`. `--unique` remaps MAC addresses consistently across the current and snapshot configurations. Guest static IPs and SMBIOS identity are not changed. Inspect network configuration before booting a test clone.

Once a real restore and snapshot rollback have been tested, native archive can use `--delete-vm` instead of `--keep-vm`. It requires no `--allow-snapshot-loss`: native mode preserves supported snapshots. Backup deletion remains gated by full remote read-back, local checksums, disk checks, and rechecking the source configuration and disk hashes. Normal operation must still have exclusive access to the VM.

Native mode currently requires directory storage, VM-owned standalone QCOW2 files, and the same disk attachments in every snapshot. It rejects backing chains, external data files, encrypted QCOW2, raw disks, saved RAM state, cloud-init disks, and unsupported configuration resources. It compares the complete internal snapshot name set against the Proxmox sections for every disk. Any unsupported case aborts; there is no fallback that discards history. Existing `vzdump` archives remain readable.

**Space:** QCOW2 files with snapshots can exceed the guest disk capacity. Native upload retains that file length and is uncompressed. Archive staging conservatively requires the full file length plus 1 GiB. Restore needs a staging copy plus a destination copy; on the same filesystem allow approximately twice the file length plus headroom. A failed destination-space check retains the downloaded staging files. `--cleanup-local` removes staging only after successful completion, so repeat tests do not accumulate another archive copy each time. Sparse/reflink copying can save local blocks where supported, but checks do not assume it will.

On native restore failure, keep the create lock while inspecting the partial VM and the staging directory's `restore-state.json`. That file records allocated destination volumes, which may not yet be attached to the placeholder VM. Do not merely unlock and start a partial restore. No automatic cleanup of partially allocated disks is attempted. The remote archive and downloaded files remain available. Native restore writes the complete Proxmox configuration after reserving the VMID; as with archive, no other task or administrator should change that VM during the operation.

## Install and configure

Copy `pve_drive.py` to `/usr/local/sbin/pve-drive` on the Proxmox node and run:

```bash
chmod 700 /usr/local/sbin/pve-drive
apt-get update
apt-get install python3 rclone zstd
rclone config
```

In rclone config, create a Google Drive remote called `gdrive`, authenticate an account with access to your Shared Drive, and select that Shared Drive when prompted. Use your own Google OAuth client ID; rclone documents that its shared client is being retired during 2026. A service account is also possible if it has appropriate Shared Drive membership. This tool uses the existing rclone configuration; it does not provision Google credentials.

Shared Drives use the `team_drive` setting. `shared_with_me` is a different feature. See [rclone's Google Drive setup](https://rclone.org/drive/) for authentication and Shared Drive configuration. Use a dedicated folder and restrict who can modify it. If you use rclone crypt over that folder, point this script at the crypt remote and preserve its configuration and encryption keys separately.

Confirm access under the same root account used by the script:

```bash
rclone lsd gdrive:
rclone mkdir gdrive:pve-archive
```

The default configuration path is `/root/.config/rclone/rclone.conf`. Override it with `--rclone-config PATH` before the subcommand. `--work-dir PATH` selects local staging storage; the default is `/var/lib/vz/pve-drive`. Allow enough free staging space for the compressed backup, and enough target storage for all restored disks. Backup size cannot reliably be predicted from compressed guest data; a full staging filesystem causes an error before deletion.

## Multiple servers sharing one Drive

Give each server a unique, stable `--source` label, for example `pve-site-b` and `lab-pve1`. Every server can use the same `--remote gdrive:pve-archive`. Labels are explicit because separate installations can have identical node hostnames. Choose distinct labels across all installations writing to this folder; labels are case-sensitive. The tool does not automatically register or enforce ownership of a label.

The remote layout is:

```text
pve-archive/sources/
  pve-site-b/100/<timestamp-uuid>/
  lab-pve1/100/<timestamp-uuid>/
```

Each backup contains the source label, actual Proxmox node name, original VMID, VM name/configuration, timestamp, and checksum. `list` prints the selected source, backup ID, size, and VM name. VM 100 on two independent servers therefore has separate folders. Run `rclone lsf gdrive:pve-archive/sources --dirs-only` to see source folders, then select a source with `list`.

For archiving, the source identifies the server being archived, and the command must run on the VM's owning node. For listing, verification, or restoration, the source selects the **original** server's backups. You can restore an `pve-site-b` backup while running on `lab-pve1`: keep `--source pve-site-b`, choose a free destination VMID, and specify destination storage. The script does not SSH to nodes or route commands to another node. In a cluster, the same VM may have archives under different source labels if it was migrated between backups.

Backups made with the earlier script have no source folder. Use `--legacy-layout` in place of `--source pve-site-b` to list, verify, or restore those backups. New archives always require a source. Legacy backups cannot reliably identify the original server and are not moved automatically.

## Commands

First exercise the workflow on a disposable VM using `--keep-vm`, and test restoration to a spare VMID:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-b archive 100 --keep-vm
pve-drive --remote gdrive:pve-archive --source pve-site-b list
```

Archive and delete a VM after verified upload:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-b archive 100 --delete-vm --cleanup-local
```

`--delete-vm` explicitly authorizes `qm destroy`, including removal of VM disks. `--purge 1` removes related Proxmox job references. Local recovery files remain by default; `--cleanup-local` removes only the staging directory created by this successful operation. The script never automatically deletes remote backups.

Copy the exact backup ID from `list` into the following commands (the ID below is an example):

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-b verify \
  100/20260905T010000Z-0123456789abcdef0123456789abcdef --cleanup-local

pve-drive --remote gdrive:pve-archive --source pve-site-b restore \
  100/20260905T010000Z-0123456789abcdef0123456789abcdef \
  200 --storage local-lvm --unique --cleanup-local
```

Restore can use the original VMID if free, or a new one. It refuses an existing VMID across the cluster and never passes `--force` to `qmrestore`. `--unique` generates new MAC addresses; omit it when preserving network identity. Restored VMs remain stopped with `onboot=0`; inspect bridges, storage, guest networking, and hardware before `qm start 200`. Remote backups remain available after restore. There is deliberately no automatic retention pruning: an archive may be the only copy of a deleted VM.

## Default vzdump guarantees and shared operating requirements

1. Refuse protected/template/locked VMs, HA or replication membership, and pending configuration. VMs with snapshots are accepted with `--keep-vm`; `--delete-vm` additionally requires `--allow-snapshot-loss` because deletion discards snapshot history. Refuse unused/excluded disks, host passthrough, physical CD-ROM devices, custom QEMU arguments, hooks, and external cloud-init snippets. A standard vzdump does not preserve all those resources or snapshot history.
2. Gracefully shut down the VM. A shutdown timeout aborts; no forced power-off occurs. The VM stays stopped, including on failure.
3. Create a zstd-compressed VMA backup in a unique local staging directory. Confirm the VM configuration is unchanged and acquire a Proxmox backup lock for the upload.
4. Upload the archive and a SHA-256 manifest into a unique remote directory. Use [rclone check --download](https://rclone.org/commands/rclone_check/) for a full read-back comparison, then publish a verified `COMPLETE` marker. This reads the entire backup back from Drive and adds transfer time.
5. Recheck the configuration, stopped state, and owned backup lock before destroying the VM. A failure before destruction prevents that call. Destruction itself is not transactional: a storage failure can leave partial deletion, with the verified remote and local backups retained.

Run archive on the node that owns the VM, in a maintenance window. Disable competing automation and do not start, modify, migrate, unlock, or replace that VM while the command runs. The script's process lock serializes its own operations on one node; it is not a distributed lock against administrators. Proxmox handles its normal backup lock during vzdump; there is a short transition before this tool acquires its upload lock. A separate administrator or automation bypassing locks can defeat the checks. Keep this exclusive maintenance requirement even in unattended usage.

Review `/etc/vzdump.conf` and any site backup hooks before use. Site defaults/hooks still apply. The archive preserves the guest backup, not host bridges, storage definitions, firewall files, cluster permissions, HA settings, or external resources. Recreate those as needed on another server. A byte-for-byte transfer check proves transfer integrity, not that the guest will boot or its applications are healthy; validate a restore before relying on this as your sole recovery process. See [Proxmox backup documentation](https://github.com/proxmox/pve-docs/blob/master/vzdump.adoc).

Attached storage ISO images are allowed without changing the VM configuration. Their references are saved as `external_media` in the manifest and reported during archive and download. The ISO contents are not uploaded by this tool. Reattach the ISO on the destination or eject it before starting the restored VM if it is unavailable. Empty CD-ROM drives and standard cloud-init drives are accepted as well.

Snapshots are not included in the default vzdump archive. Their names are recorded as `excluded_snapshots` in the manifest. With `--keep-vm`, the original snapshots remain on the server. With `--delete-vm --allow-snapshot-loss`, only the current VM state is recoverable from this backup; snapshot history is lost when the VM is destroyed. Use native mode above to preserve supported snapshot histories.

## Failures and recovery

If native local copy verification fails, do not remove the source disk or bypass verification. Version 0.4.1 records source/destination sizes, timestamps, and SHA-256 values in `copy-mismatch.json` inside that operation's staging directory. It distinguishes a changing source from a copy that differs from a stable source. This adds diagnostic evidence; it does not claim to fix an unexplained host copy mismatch. The original VM and staging files remain, and no upload or VM deletion is attempted at this failure point. Inspect both files before unlocking or retrying.

Console output names the staging directory and prints the verified backup ID before deletion. Save console output if running unattended. Failed uploads may leave directories without `COMPLETE`; `list` ignores these. Re-running archive creates a new backup rather than trusting a previous interrupted operation. Rclone retries transient transfer errors within an operation.

After failure, first verify no backup/upload process is still active and inspect `qm status VMID`, `qm config VMID`, and the printed local files. If this tool reached the upload phase, it intentionally leaves `lock: backup` to prevent unintentional starts. Once the failed task and files have been inspected, run `qm unlock VMID` manually before retrying or starting the VM. Never unlock an active task. A killed process can also leave the lock; automatic unlocking would be unsafe.

An interrupted restore may leave a partially created VM. The script retains both downloaded and remote copies and refuses to overwrite the partial VM on retry. Inspect and remove that partial VM manually if appropriate, then restore again. If cleanup alone fails, the archive or restore may already have succeeded; inspect state before retrying.

Remote archives are immutable by this script, not by Google Drive permissions. The completion marker is a checksum, not an authenticity signature. Trust and control the remote's writers. Partial remote uploads and retained local files can be removed manually after inspection. No automatic remote pruning is performed.

## Validation

Run `python3 -m unittest discover -s tests -v` from this directory. Tests simulate commands and inject upload/checksum/configuration failures. They do not execute a real VM deletion. Live Proxmox and Google Drive integration has not been tested in this workspace; perform the disposable-VM exercise before production use.
