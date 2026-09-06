# pve-drive 0.11.0

Upload now keeps the source VM and its disks by default. Add `--delete-vm` only when the source should be removed after every verification succeeds. This applies to both normal upload and advanced archive. The VM remains stopped.

**Command change:** `--keep-vm` has been removed. Remove it from existing commands and automation; add `--delete-vm` only to jobs intended to remove the source.

Console messages now show the server's local date and time alongside total elapsed time. Numbered steps report their start and completion with duration, and startup shows the operation plan and source-retention policy. Long reads retain byte progress, speed, and ETA. Source rechecks explain why they occur before upload, before publication, and after the completion marker.

Native multipart preparation avoids duplicate pre-upload staging/source checksum passes. Staged SHA-256 and MD5 verification share one read-back pass before transfer. Source checks around publication, remote verification, and restore integrity checks remain required.

Parallel Google Drive transfers, bounded compressed VMA staging, quota recovery, native snapshot preservation, and older archive readers remain supported.

Update after active operations finish:

```bash
cd /opt/pve-drive && git pull --ff-only && ./install.sh
pve-drive --version
```

Routine use:

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a upload 100
pve-drive --remote gdrive:pve-archive --source pve-site-a list
pve-drive --remote gdrive:pve-archive --source pve-site-a restore 100
```

See the [README](https://github.com/PacoCotera/pve-drive/blob/v0.11.0/README.md) and [administrator guide](https://github.com/PacoCotera/pve-drive/blob/v0.11.0/ADMIN.md) for setup, recovery, and supported layouts.
