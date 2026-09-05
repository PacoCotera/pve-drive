# pve-drive 0.9.0

Native QCOW2 archives now use parallel Google Drive transfers with 4 GiB parts, eight concurrent transfers, and 128 MiB upload chunks by default. Original QCOW2 bytes, supported internal snapshots, and Proxmox configuration are preserved.

Multipart uploads resume interrupted attempts and retry recognized Drive quota blocks hourly. Restores verify each part and the reconstructed whole-file SHA-256 before creating the VM. Existing VMA and single-file native archives remain restorable; routine upload, list, and restore commands are unchanged.

Upgrade restore nodes to 0.9.0 or newer for multipart archives. Multipart restore staging needs twice the archive size plus headroom, with additional destination disk space. The format, advanced tuning, recovery, and a reproducible live throughput comparison are documented in MULTIPART_FORMAT.md, ADMIN.md, and BENCHMARK.md. Live Hetzner-to-Drive throughput remains to be measured.
