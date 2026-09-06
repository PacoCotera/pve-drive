# pve-drive 0.12.0

Manage existing Proxmox backup files across multiple stores, with parallel Google Drive transfers and an optional interactive terminal menu.

Run `pve-drive` to discover configured remotes and browse VMs, local backups, cloud backups, restoration, and cleanup. Or use the existing remote/source arguments followed by `interactive`. Multiple selections support batch operations; an action summary precedes execution. Explicit commands never prompt.

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a backups stores
pve-drive --remote gdrive:pve-archive --source pve-site-a backups list
pve-drive --remote gdrive:pve-archive --source pve-site-a backups upload vzdump-qemu-100-2026_09_06-12_00_00.vma.zst
pve-drive --remote gdrive:pve-archive --source pve-site-a backups download BACKUP_ID --storage auto
```

Local backup files are retained by default. `--delete-local` explicitly moves an unprotected file after all cloud verification succeeds. The source PVE, original storage ID, filename, exact bytes, and available notes/protection metadata are preserved. Backup-file downloads return files to PVE storage without creating a VM.

Destination discovery prefers the original usable store, then the only suitable alternative. The menu offers numbered recommendations when several qualify; unattended jobs can use `--storage auto` or specify a store ID.

Existing-file uploads use eight transfers, 256 MiB parts, 128 MiB Drive chunks, and a bounded 2 GiB payload spool plus 1 GiB headroom by default. Quota waits and retries reuse unchanged source files and verified remote parts. Downloads validate part and whole-file SHA-256 before publication and need twice the backup size plus 1 GiB, less retained recovery data. Existing destination files are never overwritten.

Use `backups cleanup` for incomplete library attempts. QEMU VMA and LXC/OpenVZ tar backup files are supported; PBS datastores and container lifecycle operations are excluded. Schedule transfers outside independent backup/pruning work. Older VM archive formats remain readable.

Update after active operations finish:

```bash
cd /opt/pve-drive && git pull --ff-only && ./install.sh
```

See [README](https://github.com/PacoCotera/pve-drive/blob/v0.12.0/README.md) and [backup-file guide](https://github.com/PacoCotera/pve-drive/blob/v0.12.0/BACKUP_FILES.md) for complete usage and recovery details.
