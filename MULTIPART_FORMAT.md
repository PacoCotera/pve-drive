# Native QCOW2 multipart archive format

Introduced in pve-drive 0.9.0. This is a transport for exact QCOW2 bytes, not a conversion or a virtual filesystem. Google Drive remains the storage backend. No rclone chunker remote is used.

## Layout and publication

```text
REMOTE/sources/SOURCE/VMID/TIMESTAMP-UUID/
  scsi0.qcow2.part-000000
  scsi0.qcow2.part-000001
  ...
  vm.conf
  manifest.json
  COMPLETE
```

Each disk has its own ordered part list. All files are direct children of the backup directory. Names are deterministic within a UUID attempt; different attempts never share parts. Parts are independent Drive objects. `list` and restore discovery consider only `COMPLETE`, so a directory containing any number of parts and even a manifest remains incomplete until publication. SIZE is the sum of original disk sizes and configuration bytes, not the count or size of transport objects.

`COMPLETE` contains lowercase SHA-256 of the exact UTF-8 manifest bytes, followed by LF. The manifest is transferred after the parts and configuration. Upload verifies every local part against its SHA-256 and verifies the concatenation against the original SHA-256 before transfer. After transfer, it requires matching remote size and MD5 for every payload file (or a full read-back with `--deep-verify`), then exact manifest read-back. It checks the stopped, locked source and original disk hashes before publishing and again before deleting the VM. The completion marker itself must be read back successfully. Verification failures never authorize source deletion.

SHA-256 records describe the original disk and each part. Google Drive exposes MD5 rather than SHA-256; fast upload verification compares Drive's MD5 with MD5 calculated from the already SHA-256-verified local parts. No size-only fallback is accepted. These hashes detect corruption; they are not signatures protecting against an attacker who can rewrite both the manifest and marker.

## Manifest

This abbreviated example uses tiny illustrative sizes; normal parts are 4 GiB. `H` placeholders below represent 64 lowercase hexadecimal characters.

```json
{
  "schema": 4,
  "format": "native-qcow2",
  "transport": {"format": "pve-drive-parts", "version": 1},
  "backup_id": "100/20260905T010000Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "source": "pve-site-a",
  "source_node": "pve-node-a",
  "vmid": 100,
  "created_utc": "2026-09-05T01:00:00+00:00",
  "config": {"name": "example", "scsi0": "local:100/vm-100-disk-0.qcow2,size=64G"},
  "snapshots": ["before-change"],
  "config_sha256": "H",
  "size": 1234,
  "disks": [{
    "filename": "scsi0.qcow2",
    "original_filename": "vm-100-disk-0.qcow2",
    "device": "scsi0",
    "volume": "local:100/vm-100-disk-0.qcow2",
    "virtual_size": 68719476736,
    "size": 10,
    "sha256": "H",
    "part_size": 8,
    "parts": [
      {"filename": "scsi0.qcow2.part-000000", "size": 8, "sha256": "H"},
      {"filename": "scsi0.qcow2.part-000001", "size": 2, "sha256": "H"}
    ]
  }]
}
```

`filename` is the portable reconstructed archive filename; `original_filename` records the source disk basename. `size` within a disk is the exact QCOW2 file length, including all snapshot structures, allocation tables, padding, and unused regions. `virtual_size` retains the guest capacity used for Proxmox allocation. The top-level size includes `vm.conf`. All existing VM metadata remains in `config`, `snapshots`, `volume`, `device`, and the original full `vm.conf`, including supported snapshot sections and hardware assignments.

All parts except the last have `part_size` bytes; the last contains the remainder (a full part for an exact multiple). No empty trailing part exists. Part count must equal ceil(disk size / part size). Array order is authoritative and must also match the zero-based, six-digit filename sequence. Validators reject missing, duplicate, reordered, unsafe, or incorrectly sized entries and unsupported versions. Restore never glob-sorts arbitrary remote files.

## Restore and recovery

Restore verifies the marker and manifest, downloads only manifest-listed parts through one rclone scheduler (eight concurrent transfers by default), and validates each size and SHA-256. It concatenates in manifest order into a temporary file, checks the whole stream, then independently hashes the reconstructed file on disk before atomically giving it its final name. No Proxmox VM is created until all disks and the configuration pass validation and `qemu-img` checks. Installation also verifies the destination QCOW2 hash. There is no `qemu-img convert`; byte equality preserves supported internal snapshots.

Download staging retains both the parts and reconstructed images until success, requiring twice the original archive size plus 1 GiB. Destination allocation requires another original-size copy. A failed download or reconstruction can resume using `restore ... --resume STAGING_DIR`; verified local parts are skipped, corrupt or incomplete ones are downloaded again, and reconstruction restarts. A failure after VM creation is a separate Proxmox recovery case: inspect `restore-state.json`; occupied VM IDs are never overwritten.

Upload stages exact parts directly from the stopped source, so normal upload staging needs one original-size copy plus 1 GiB. Part files and JSON metadata use temporary names and atomic replacement. An interrupted split can be retried with the printed `upload --resume` path. After manifest creation, retry reuses the same backup ID and checksum-matching remote files. Incomplete individual Drive upload sessions may restart at the part boundary; sessions are not persisted across processes. Rclone retains its ordinary low-level retries. Existing remote objects with conflicting content fail under `--immutable` rather than being silently replaced.

A single recorded unfinished multipart upload for the selected remote/source/VM is automatically selected when the same normal upload command is repeated. Multiple attempts require explicit selection. The source must still be stopped, backup-locked, and unchanged. `recover` can publish an already fully uploaded and verified attempt without retransmitting disk parts and always retains the VM.

On recognized Drive upload-quota errors, pve-drive waits one hour and retries, up to 24 times by default. `--quota-retries 0` exits immediately with recovery data intact; `--quota-retry-delay` adjusts the wait. Ctrl-C also retains the attempt. Rclone's `--drive-stop-on-upload-limit` prevents its own retry loop from continuously hammering a daily quota. Reset times are not assumed to be midnight. Repeating the normal upload command after a reset resumes parts if the process was stopped. Quota detection depends on Google's/rclone's error strings; an unrecognized error exits safely and remains resumable. Streaming VMA retains its previous restart-from-beginning behavior.

`cleanup VMID` previews recorded attempts; `--apply` removes local recovery data and the exact recorded incomplete cloud directory. A cloud `COMPLETE` marker protects the remote archive, including all its parts. Remote listing/deletion failures preserve local recovery data. Existing symlink, path, mount, source, and VMID safeguards apply. Attempts lost with the staging disk cannot be automatically discovered by this local cleanup command; retain staging until success or explicit cleanup.

## Compatibility

Readers dispatch on the manifest schema, and schema 4 additionally requires the transport version above:

| Schema | Transport | Read support |
| --- | --- | --- |
| 1 | Legacy VMA layout, without a source label | Retained |
| 2 | Source-scoped staged or streamed VMA | Retained |
| 3 | Source-scoped single-file native QCOW2 | Retained |
| 4 | Source-scoped multipart native QCOW2, transport version 1 | New default for native archives |

Older pve-drive versions cannot restore schema 4. Upgrade the destination executable to 0.9.0 or newer before restoring it. `--single-file` is an advanced native upload comparison option that writes schema 3. Automatic VMA/native selection and unsupported snapshot-layout rejection are unchanged.
