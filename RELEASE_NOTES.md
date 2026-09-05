# pve-drive 0.10.0

VMs without snapshots now upload compressed VMA archives through parallel Google Drive transfers automatically. The normal upload command uses 256 MiB parts, eight transfers, and 128 MiB Drive upload chunks. Upload staging is bounded to 2.25 GiB plus 1 GiB of reserved headroom, independently of the VM's virtual capacity or QCOW2 file lengths.

Drive quota blocks pause production while completed uploads release staging space. A fully produced stream can resume remaining uploads after interruption; interrupted production restarts in a separate attempt because regenerated VMA bytes may differ. Existing incomplete attempts remain available to guarded cleanup.

Restore downloads compressed parts concurrently and verifies every part, the reconstructed whole-file SHA-256, and zstd integrity before invoking qmrestore. Native QCOW2 snapshot archives and all older archive formats remain supported. Upgrade restore nodes to 0.10.0 for the new VMA format.

Live Hetzner-to-Drive throughput remains to be measured; BENCHMARK.md includes the comparison procedure.
