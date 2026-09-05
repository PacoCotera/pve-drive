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
