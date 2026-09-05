# Google Drive throughput benchmark

The 0.9.0 defaults are provisional: 4 GiB parts, eight concurrent transfers, and 128 MiB Drive upload chunks. No live Hetzner-to-Google-Drive result was obtained during development. A local integration test establishes correctness, not Drive throughput. Use this procedure on the actual Hetzner PVE node with its existing Google Drive remote before deciding whether six or eight transfers is preferable.

## Controlled native archive comparison

Choose a stopped test VM with supported internal QCOW2 snapshots and a representative 100-150+ GiB disk file. Keep the original VM throughout. Run as root after installing the candidate release; use a dedicated benchmark source label so routine backups remain separate. Substitute the VM ID and staging filesystem for your environment. Do not change compression, rclone configuration, network routing, or disk contents between cases.

```bash
mkdir -p /mnt/backup-space/pve-drive-staging /root/pve-drive-benchmark
cd /root/pve-drive-benchmark
rclone version > environment.txt
pve-drive --version >> environment.txt
uname -a >> environment.txt

# A: previous single-file transport, previous 32 MiB Drive chunks.
/usr/bin/time -v -o single32.time \
  pve-drive --remote gdrive:pve-archive --source pve-benchmark \
  --work-dir /mnt/backup-space/pve-drive-staging --verbose \
  upload 100 --keep-vm --single-file --transfers 1 --drive-chunk-size 32M \
  > single32.log 2>&1

# B: new default implementation, same VM and original QCOW2 bytes.
/usr/bin/time -v -o multipart8.time \
  pve-drive --remote gdrive:pve-archive --source pve-benchmark \
  --work-dir /mnt/backup-space/pve-drive-staging --verbose \
  upload 100 --keep-vm \
  > multipart8.log 2>&1

pve-drive --remote gdrive:pve-archive --source pve-benchmark list --all-versions
```

These commands exercise the actual native upload implementation, including local preparation, hashing, parallel transfers, and verification. They create two distinct complete archives. If the test VM has no snapshots, use the advanced command `archive 100 --format native-qcow2 --keep-vm --cleanup-local` instead of `upload 100 --keep-vm` in both cases to force the same native comparison.

Each log includes timestamped rclone statistics. Record (1) transfer elapsed time and original bytes / transfer elapsed seconds / 1048576 for aggregate MiB/s; (2) end-to-end elapsed time from the `.time` file; (3) local split/copy and verification time; (4) peak RAM, disk read/write rates, CPU utilization, retries and quota errors. `iostat -xz 2`, `pidstat -dru 2`, and network interface counters in another terminal help distinguish disk, CPU, and network limits. `/usr/bin/time` RSS alone is not total simultaneous process-tree RAM; sample rclone and Python RSS during the transfer. A gigabit link has a raw ceiling of about 119 MiB/s before overhead.

For the next quota window, compare single-file 128M (isolates upload-chunk changes) and multipart with `--transfers 6`. If useful, compare `--part-size 2G` or `8G` with concurrency fixed. Alternate case order over subsequent windows to reduce time-of-day and warm-cache bias. Use at least 32 GiB for eight 4 GiB parts; a 2 GiB input cannot test eight-way part concurrency. Prefer representative full-size QCOW2 data over zero-only synthetic data. Keep the same original whole-file SHA-256 between cases.

## Quota and retry measurement

Every comparison uploads another full logical QCOW2. Budget all account uploads before running; do not run a large comparison matrix in one quota window. Google documents a 750 GB upload/copy limit per user within 24 hours, and deleting benchmark files does not undo uploaded traffic. A quota-blocked run is not a throughput measurement. Record it separately and repeat after the quota clears. See [Google Shared Drive limits](https://support.google.com/a/answer/7338880) and [rclone quota detection](https://rclone.org/drive/#drive-stop-on-upload-limit).

To test manual quota recovery, select `--quota-retries 0` in the multipart case. After quota reset repeat that same command; it should select the retained attempt, skip checksum-matching completed parts, and publish only after all files verify. The default instead retries hourly up to 24 times. Ctrl-C during a multipart transfer provides a reproducible interruption test without deliberately exhausting the daily quota. Retain the printed staging path and source lock. Never edit part files or unlock the source while resuming.

## Restore and byte/snapshot acceptance

Restore the multipart backup to an unused VM ID on directory storage:

```bash
/usr/bin/time -v -o restore.time \
  pve-drive --remote gdrive:pve-archive --source pve-benchmark \
  --work-dir /mnt/backup-space/pve-drive-staging \
  restore 100 --target-vmid 200 --storage destination-dir --unique \
  > restore.log 2>&1
```

Before booting either VM, obtain each source/destination disk path with `pvesm path VOLUME_ID`, compare `sha256sum` and `stat -c %s`, and inspect `qemu-img snapshot -l` and `qm listsnapshot`. Hashes and exact lengths must match. On the test restore, roll back the known internal snapshot and verify its expected guest state. Account for guest IP/identity overlap when booting test copies. An interruption before VM creation can be resumed with `restore ... --resume PRINTED_STAGING_DIR`; verify from rclone logs that completed parts were skipped.

Keep a results table with case, original GiB, part size, concurrency, Drive chunk size, transfer seconds, MiB/s, end-to-end seconds, peak RAM, retry count, and quota status. Eight 128 MiB upload buffers require about 1 GiB plus rclone/Python overhead; six require about 768 MiB. Select a smaller concurrency only if measured throughput, memory, or throttling supports it. There is no claimed speedup until the live results exist.

After review, delete only the exact benchmark backup IDs you recorded if you no longer need them. `cleanup 100` handles incomplete attempts under `--source pve-benchmark`; it deliberately preserves complete cloud backups. Do not purge the normal archive root.

## Compressed VMA comparison for large VMs without snapshots (0.10.0)

This is the appropriate comparison when QCOW2 file lengths exceed local free space but VMA compression makes the backup much smaller. Use the same stopped VM and existing compression settings on the actual Hetzner-to-Drive path. The native comparison above does not represent this case.

```bash
# Previous compressed single-file stream; retain the VM.
/usr/bin/time -v -o vma-single.time \
  pve-drive --remote gdrive:pve-archive --source pve-benchmark \
  upload 100 --keep-vm --stream --single-file --drive-chunk-size 128M \
  > vma-single.log 2>&1

# New normal command: compressed multipart, eight transfers, bounded spool.
/usr/bin/time -v -o vma-multipart.time \
  pve-drive --remote gdrive:pve-archive --source pve-benchmark \
  upload 100 --keep-vm > vma-multipart.log 2>&1
```

Normal VMA upload needs only 3.25 GiB free for the default payload spool plus headroom, rather than the original QCOW2 lengths. Watch `du -sb /var/lib/vz/pve-drive/stream-100-*/spool` during transfer and a quota pause: the active spool should stay at or below 2.25 GiB. Logs and Proxmox temporary metadata are separate. The first upload starts after 256 MiB is produced; at least 2 GiB of compressed output is useful to exercise eight concurrent transfers. Smaller archives may finish before all workers become busy.

Compare final compressed bytes / upload elapsed seconds, end-to-end duration, peak aggregate rclone RSS, compression CPU, disk I/O, and quota/retry count. The VMA multipart progress counter counts remotely verified bytes; production can be up to one spool ahead. Do not compare VMA whole-file SHA-256 across separate vzdump runs as a determinism test: embedded timestamps/headers can differ. Each run's downloaded/reconstructed SHA-256 must match its own manifest.

Repeat with `--transfers 6` or `--part-size 512M` in later quota windows if useful. Keep 128M upload chunks for both cases to isolate independent-transfer concurrency. To measure the previous 32M behavior, run the legacy case separately with `--drive-chunk-size 32M`. Do not consume the account's daily upload budget with an entire matrix at once. No live throughput improvement is claimed before results exist.

To test quota recovery without changing the spool limit, let the process remain running while blocked and confirm it continues when Drive permits uploads again. For process interruption, Ctrl-C and rerun the normal command: fully certified production resumes remaining files; uncertified production explicitly restarts a new stream while retaining the old attempt for cleanup. Restore the completed backup to an unused VM ID with the normal restore command and verify expected guest data before considering the test successful.
