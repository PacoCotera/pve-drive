# Changelog

## 0.12.0

- Add existing backup-file inventory across enabled accessible PVE backup stores, including custom backup directories.
- Add bounded parallel upload/copy/move and verified download back to an original or selected local store, preserving file bytes and available notes/protection metadata.
- Add original-store discovery, automatic destination choice, quota/interruption recovery, and guarded backup-file cleanup.
- Add a terminal menu for VM archives, local/cloud backup files, VM restoration, batch selection, and cleanup. Explicit commands remain noninteractive.
- Keep originals by default; protected local backups cannot be moved. Backup-file downloads never create a VM.


## 0.11.0

- Retain source VMs by default; require explicit `--delete-vm`. Remove `--keep-vm`.
- Add server-local timestamps, numbered step starts/completions, durations, and operation/source-policy explanations.
- Combine native staging verification and MD5 collection into one pre-upload read-back; retain source and publication safety checks.
- Update command examples, administrator documentation, and regression tests.

## 0.10.0

- Automatically stream compressed VMA archives through parallel Drive uploads for VMs without snapshots, using a bounded 2.25 GiB default spool.
- Pause production on quota blocks, cancel workers cleanly, and recover fully produced streams without regenerating their bytes. Restart interrupted production in a separate incomplete attempt.
- Add schema 5 manifests, verified parallel compressed restore, guarded cleanup, and explicit legacy single-file comparison options.
- Preserve native QCOW2 snapshot transport and all older archive readers.

## 0.9.0

- Add versioned native QCOW2 multipart transport with 4 GiB parts, eight concurrent rclone transfers, and 128 MiB Google Drive upload chunks by default.
- Preserve original disk bytes, internal snapshots, and Proxmox configuration; require part and whole-file checksums and verified publication before source deletion.
- Resume interrupted multipart uploads, automatically retry Drive quota blocks, and reuse verified parts during download recovery.
- Retain legacy archive restore support, logical VM listings, and guarded cleanup of incomplete attempts.
- Add advanced tuning, archive format documentation, and a reproducible Hetzner-to-Drive benchmark procedure.

## 0.8.4

- Add `cleanup VMID` to discover recorded upload attempts by source and VM ID. Preview by default; add `--apply` to remove them without looking up staging paths.

## 0.8.3

- Add `upload --stream --drive-chunk-size` to tune Google Drive upload chunks. The default remains 32 MiB; 128 MiB can be selected for throughput testing without changing compression settings.

## 0.8.2

- Show a conservative estimated maximum archive size and ETA during streaming upload, using configured disk capacity plus an overhead allowance.
- Add `cleanup` to list staging directories, preview one attempt, and explicitly discard its local files and recorded incomplete cloud upload. Completed cloud archives, VM disks, locks, and diagnostic logs are retained.
- Document cleanup, recovery retention, and streaming progress in the command help and administrator guide.

## 0.8.1

- Fix streaming backup worker startup from SSH terminals by using non-terminal stdin.
- Include producer diagnostics when a downstream pipeline process fails first.

## 0.8.0

- Add streaming VMA upload and restore without full local archive staging.
- Verify stream hashes and compression integrity; require cloud size/MD5 verification before source deletion.
- Recover completed streams without retransmitting archive data.

## 0.7.3

- Support verified display and HD-audio PCI passthrough while preserving hardware assignments.

## 0.7.2

- Clean successful recovery staging by default and remove empty staging directories after failed space checks.
