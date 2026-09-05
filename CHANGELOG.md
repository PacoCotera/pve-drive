# Changelog

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
