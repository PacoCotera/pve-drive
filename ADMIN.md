# Admin quick start — version 0.4.0

Copy `pve_drive.py` onto the Proxmox server. Keep using the same configured rclone remote and a unique source label for each server.

## Upload VM 100 and remove it from Proxmox

```bash
python3 pve_drive.py --remote gdrive:pve-archive --source pve-site-a upload 100
```

The VM is shut down, archived, uploaded, and verified before deletion. Internal QCOW2 snapshots are preserved automatically for the supported directory-storage layout. Unsupported snapshot layouts stop with an error instead of discarding history. Add `--keep-vm` for a test upload that leaves the original VM stopped on the server.

## List cloud VMs

```bash
python3 pve_drive.py --remote gdrive:pve-archive --source pve-site-a list
```

Shows VMID, name, archive format, snapshot names, and archive date for the latest complete version of each VM. Internal backup IDs are hidden unless you request `list --all-versions`.

## Restore VM 100

```bash
python3 pve_drive.py --remote gdrive:pve-archive --source pve-site-a restore 100
```

Selects the latest complete archive automatically, restores as VM 100 using its original storage, and leaves it stopped for inspection. It refuses to overwrite an existing VM. The cloud copy remains available. Start it when ready with `qm start 100`.

To restore source VM 100 as VM 200, including on a different PVE server:

```bash
python3 pve_drive.py --remote gdrive:pve-archive --source pve-site-a \
  restore 100 --target-vmid 200 --storage destination-dir --unique
```

Run this on the destination PVE. Keep the **original** source label (`pve-site-a`) to select the correct backup. `--target-vmid` selects the destination VMID; without it, the source VMID is reused. `--storage` overrides destination storage, and `--unique` generates new MAC addresses if the original VM will coexist. Native archives originally spanning multiple storage IDs require a destination `--storage` override. The script records both the source label and actual source node name in each backup manifest.

Successful upload/restore removes its temporary local files by default; append `--keep-local` to retain them. Failed operations retain recovery files. Allow staging space for the full archive size. Restore also needs space for the destination copy. Attached ISO contents and Proxmox host/cluster settings are not included; see README for supported resources and recovery details.

The older `archive` and explicit-backup-ID restore commands remain available for advanced use. Simulated tests pass, but test a real restore and snapshot rollback before relying on VM deletion in production.
