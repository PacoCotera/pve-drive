# Admin quick start — version 0.4.3

## Install and update

Clone once into a separate source directory (authenticate to the private repository when prompted):

```bash
git clone https://github.com/PacoCotera/pve-drive.git /opt/pve-drive &&
cd /opt/pve-drive &&
sudo ./install.sh
```

For later updates:

```bash
cd /opt/pve-drive
git pull --ff-only && sudo ./install.sh
```

The installed command is `/usr/local/sbin/pve-drive`. If that path is an old directory, the installer preserves it as `pve-drive.previous-<timestamp>-<suffix>` before installing the executable. VM disks, staging files, and rclone configuration are not moved. An active pve-drive task blocks installation until it finishes. The installer does not install dependencies: Git and Python 3 must already be available, along with rclone and the normal Proxmox tools for operation.

Keep using the same configured rclone remote and a unique source label for each server. Run `pve-drive --help` for usage.

## Upload VM 100 and remove it from Proxmox

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a upload 100
```

The VM is shut down, archived, uploaded, and verified before deletion. Internal QCOW2 snapshots are preserved automatically for the supported directory-storage layout. Unsupported snapshot layouts stop with an error instead of discarding history. Add `--keep-vm` for a test upload that leaves the original VM stopped on the server.

## List cloud VMs

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a list
```

Shows VMID, name, archive format, snapshot names, and archive date for the latest complete version of each VM. Internal backup IDs are hidden unless you request `list --all-versions`.

## Restore VM 100

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a restore 100
```

Selects the latest complete archive automatically, restores as VM 100 using its original storage, and leaves it stopped for inspection. It refuses to overwrite an existing VM. The cloud copy remains available. Start it when ready with `qm start 100`.

To restore source VM 100 as VM 200, including on a different PVE server:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a \
  restore 100 --target-vmid 200 --storage destination-dir --unique
```

Run this on the destination PVE. Keep the **original** source label (`pve-site-a`) to select the correct backup. `--target-vmid` selects the destination VMID; without it, the source VMID is reused. `--storage` overrides destination storage, and `--unique` generates new MAC addresses if the original VM will coexist. Native archives originally spanning multiple storage IDs require a destination `--storage` override. The script records both the source label and actual source node name in each backup manifest.

Successful upload/restore removes its temporary local files by default; append `--keep-local` to retain them. Failed operations retain recovery files. Allow staging space for the full archive size. Restore also needs space for the destination copy. Attached ISO contents and Proxmox host/cluster settings are not included; see README for supported resources and recovery details.

The older `archive` and explicit-backup-ID restore commands remain available for advanced use. Simulated tests pass, but test a real restore and snapshot rollback before relying on VM deletion in production.

## Retry a failed native local copy

Version 0.4.2 uses sequential reads/writes instead of accelerated `cp`. It verifies the complete file hash and retains mismatch diagnostics. This changes the copy method; an unexplained host mismatch still needs investigation if it recurs.

After updating the script, a failed native copy from before manifest creation can reuse its existing staging directory:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a \
  upload 100 --resume /var/lib/vz/pve-drive/native-100-EXAMPLE --keep-vm
```

Use the exact staging directory printed by your failed operation. Leave the VM stopped with its backup lock; do not unlock before this command. Resume verifies the full VM/snapshot configuration, replaces the failed local copies in that same directory, then performs all normal checks. It does not create a second staging directory. It is not a general interrupted-upload resume and refuses stages with a manifest or completion marker. `--keep-vm` retains the original VM for this recovery test. Sparse output is based only on buffers actually read as zero; filesystem hole reporting and reflinks are not used. Disk-full or integrity errors still stop the operation and retain recovery files.

## Built-in help

Run `pve-drive --help`, `pve-drive upload --help`, `pve-drive list --help`, or `pve-drive restore --help` for examples and defaults. Global options go before the command. For this private repository, use a GitHub personal access token at the Git password prompt, not your account password. Wait for active uploads/restores to finish before running the installer.
