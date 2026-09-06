# Existing Proxmox backup files

`backups` transports existing files independently of VM archiving. It never invokes `qm`, `qmrestore`, or `vzdump`. QEMU VMA and LXC/OpenVZ tar backup files are supported with standard `vzdump-TYPE-VMID-...` names and optional zstd, gzip, lzo, or bzip2 compression. This does not add LXC VM lifecycle support to `upload`/`restore`.

## Discovery and destination selection

The node storage API provides enabled/active stores and supported content types. `pvesm path` resolves the backup directory, including configured content-directory overrides. Each accessible filesystem backup store is listed through Proxmox's content API. Disabled or unavailable stores, PBS datastores, unsupported names, symlinks, and invalid paths are skipped/reported. No recursive search of arbitrary host directories is performed.

Use `backups stores` for store IDs, resolved paths, and free space. `backups list` shows local and cloud files; `--location local|cloud` limits the query. Source PVE label, actual source node, original storage ID, volume ID, filename, VMID, and available backup time are retained. Backup filenames and sizes are used only as inventory hints for `both*`; byte equality is checked during transfer. Age does not imply that a backup is permanent. Available PVE protection state is shown.

For download, the original store wins if it is active, writable, and has enough space. Otherwise the sole suitable store wins. With multiple alternatives, direct commands require `--storage auto` or an explicit ID; the menu displays numbered candidates with paths and free space. Auto chooses the greatest free space, breaking ties by store ID. The source label still refers to the original node when downloading on a different PVE node.

## Retention and interference

Local files are retained unless `backups upload ... --delete-local` is supplied. Protected backups cannot be deleted by this tool. Deletion uses `pvesm free` after remote completion read-back, a repeated source checksum/state/metadata check, and a fresh protection/storage check. No VM is stopped, deleted, or restored. Download always retains the cloud copy.

Transfers check for active `vzdump` tasks before work and before publication/deletion. Source file identity, exact size, timestamps, and checksums detect changes while reading. These checks do not lock independent PVE scheduling or pruning for the entire transfer: use a window in which other tools will not modify or prune the selected backups. Unknown or conflicting data causes failure rather than overwriting. File checksums preserve the existing backup's bytes; they do not prove that an old backup was valid before upload or that its guest can boot.

## Multipart transport

Default upload settings are 256 MiB parts, eight transfers, and 128 MiB Drive upload chunks. The source file is scanned for part SHA-256/MD5 and a whole-file SHA-256 without making a complete staging copy. At most eight part files occupy the upload spool (2 GiB at defaults), with 1 GiB additional headroom. Each uploaded part is checked against Drive size/MD5 metadata before its staging copy is removed. `--deep-verify` reads uploaded bytes back as well, including previously uploaded parts on retry.

Recognized quota blocks use the same hourly retries as VM archives, up to 24 by default. Advanced controls are in `backups upload --help`. Repeat the normal command to resume the same recorded attempt: the source must be unchanged, and checksum-matching remote parts are reused. A part whose resumable rclone session was lost may restart at its boundary. Multiple conflicting attempts require cleanup rather than guessing.

Download creates a hidden `.pve-drive-ID` directory inside the selected store's backup directory. It downloads parts concurrently, checks each SHA-256, reconstructs in manifest order, and checks the reconstructed whole-file SHA-256. Staging needs twice the backup size plus 1 GiB, less matching retained data on retry. The complete file is exposed through a same-filesystem hard link only after verification; the filesystem must support hard links. Existing destination files and metadata are never overwritten. Notes/protection metadata are published before the backup file. A retry recognizes its own links after an interrupted publication and verifies the completed file before removing recovery data.

No VM is created on download. After successful publication, PVE can discover the returned file through its normal backup inventory. Restore it through Proxmox when needed.

## Metadata and layout

Backup files use a separate namespace so existing VM-archive listings and older VM readers remain unchanged:

```text
REMOTE/backup-files/SOURCE/STORAGE/VMID/TIMESTAMP-UUID/
  part-000000
  part-000001
  ...
  manifest.json
  COMPLETE
```

The manifest has `schema: 1`, `format: "pve-backup-file"`, and `transport: {"format": "pve-drive-parts", "version": 1}`. This schema belongs to the backup-file namespace, independently of VM-archive schemas. It contains:

- `backup_id`, `source`, `source_node`, `storage`, `original_volume`, `vmid`, and `created_utc`.
- Original `filename`, exact `size`, whole-file `sha256`, and optional `backup_time` from inventory.
- `part_size` and ordered `parts`, each with deterministic filename, exact size, SHA-256, and MD5. All but the last part have the configured size; no empty trailing part exists.
- Boolean `protected` and a `sidecars` map of original auxiliary basenames to base64-encoded bytes. Recognized files are `FILENAME.notes`, `FILENAME.protected`, and the PVE backup log basename. Each sidecar is limited to 4 MiB. Oversized or nonregular sidecars fail safely rather than being silently dropped.

The completion marker is the lowercase SHA-256 of the exact manifest bytes followed by LF. Part names, sizes, order, metadata names, and version are validated. Upload reads back the manifest and marker before reporting completion. Incomplete directories are excluded from discovery. Checksums establish integrity, not authenticity against someone with write access to both metadata and data.

## Cleanup

```bash
pve-drive --remote gdrive:pve-archive --source pve-site-a backups cleanup
pve-drive --remote gdrive:pve-archive --source pve-site-a backups cleanup --stage PRINTED_PATH
pve-drive --remote gdrive:pve-archive --source pve-site-a backups cleanup --stage PRINTED_PATH --apply
```

Upload recovery lives under the configured work directory as `backup-file-*`; download recovery lives inside the selected backup store. Cleanup discovers both. Preview is the default. Apply validates the recorded source/remote/manifest, refuses symlinks and nested mounts, and removes only that attempt's staging plus its recorded incomplete remote upload. A completion marker protects the cloud backup. Cloud deletion is rechecked and confirmed before local recovery data is removed.

Cleanup never deletes original backup files, published destination backups, VMs, or diagnostic logs. Sidecars already linked into the destination during an interrupted publication are retained there; resume publication before cleanup if you want to complete that download. Do not run independent operations against the selected attempt during cleanup.

## Upstream interfaces

Implementation follows [Proxmox storage path resolution](https://github.com/proxmox/pve-storage/blob/master/src/PVE/Storage/Plugin.pm), [backup protection and auxiliary files](https://github.com/proxmox/pve-storage/blob/master/src/PVE/Storage.pm), and [active task discovery](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Tasks.pm). A live PVE/Drive validation is still needed for each deployment's storage configuration and backup formats.
