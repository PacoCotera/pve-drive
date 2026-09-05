#!/usr/bin/env python3
"""Archive stopped Proxmox QEMU VMs to rclone; restore verified archives."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
import uuid

__version__ = '0.4.2'

def run(*args, capture=False):
    print('+ ' + ' '.join(map(str, args)), file=sys.stderr, flush=True)
    return subprocess.run(list(map(str, args)), check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def file_details(path):
    s = Path(path).stat()
    return {'path': str(path), 'size': s.st_size, 'mtime_ns': s.st_mtime_ns,
            'ctime_ns': s.st_ctime_ns, 'device': s.st_dev, 'inode': s.st_ino}


def stream_copy(source, destination):
    """Read every byte, using holes only for buffers actually read as zero."""
    source, destination = Path(source), Path(destination)
    if destination.is_symlink() or (destination.exists() and os.path.samefile(source, destination)):
        raise ValueError('Refusing to overwrite a symlink or the source disk')
    total = source.stat().st_size
    block_size = 8 * 1024 * 1024
    zero_block = bytes(block_size)
    copied = 0
    last_report = time.monotonic()
    print(f'Sequential copy: {source} -> {destination} ({total} bytes)', file=sys.stderr, flush=True)
    with source.open('rb') as src, destination.open('wb') as dst:
        while True:
            data = src.read(block_size)
            if not data:
                break
            if data == zero_block[:len(data)]:
                dst.seek(len(data), os.SEEK_CUR)
            else:
                dst.write(data)
            copied += len(data)
            if time.monotonic() - last_report >= 15:
                print(f'Copied {copied}/{total} bytes ({100 * copied / max(total, 1):.1f}%)', file=sys.stderr, flush=True)
                last_report = time.monotonic()
        dst.truncate(copied)
        dst.flush()
        os.fsync(dst.fileno())
    print(f'Copy finished: {copied} bytes; verifying SHA-256.', file=sys.stderr, flush=True)


def verified_native_copy(source, destination, report_path):
    """Never accept a mismatch; retain evidence to distinguish a changing source."""
    before = file_details(source)
    source_hash = sha256(source)
    stream_copy(source, destination)
    destination_hash = sha256(destination)
    after = file_details(source)
    destination_details = file_details(destination)
    if destination_hash != source_hash or before != after:
        current_source_hash = sha256(source)
        report = {'source_before': before, 'source_after': after,
                  'destination': destination_details, 'source_sha256_before': source_hash,
                  'source_sha256_after': current_source_hash,
                  'destination_sha256': destination_hash}
        Path(report_path).write_text(json.dumps(report, indent=2) + '\n')
        reason = ('Source changed during checksum/copy' if before != after or current_source_hash != source_hash
                  else 'Copied bytes differ from the source')
        raise ValueError(f'{reason}. No upload or VM deletion performed. '
                         f'Diagnostic details: {report_path}')
    return source_hash


def vmid(value):
    if not re.fullmatch(r'[1-9][0-9]{2,8}', str(value)):
        raise ValueError('VMID must be an integer from 100 to 999999999')
    return str(value)


def backup_id(value):
    if not re.fullmatch(r'[1-9][0-9]{2,8}/[0-9]{8}T[0-9]{6}Z-[a-f0-9]{32}', value):
        raise ValueError('Invalid backup ID; use an ID printed by list')
    return value


def source_name(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        raise ValueError('Source must be 1-64 letters, digits, underscores or hyphens')
    return value


def safe_config(config):
    """Reject unbacked disks/devices; return removable media dependencies."""
    external_media = {}
    for key, value in config.items():
        value = str(value)
        if key in ('lock', 'args', 'hookscript', 'cicustom', 'vmstate'):
            raise ValueError(f'Unsupported configuration: {key}')
        if key in ('protection', 'template') and value == '1':
            raise ValueError(f'VM has {key}=1')
        if re.fullmatch(r'(unused|hostpci|usb|virtiofs)\d+', key):
            raise ValueError(f'Unbacked resource: {key}')
        if re.fullmatch(r'(ide|sata|scsi|virtio|efidisk|tpmstate)\d+', key):
            volume = value.split(',')[0]
            if 'media=cdrom' in value.split(','):
                if volume == 'none' or re.fullmatch(r'[^:]+:.*cloudinit', volume):
                    continue
                if not re.fullmatch(r'[^:/]+:iso/.+', volume):
                    raise ValueError(f'Unsupported host CD-ROM device: {key}')
                external_media[key] = volume
                continue
            if re.search(r'(?:^|,)backup=(?:0|no|off)(?:,|$)', value):
                raise ValueError(f'Disk excluded from backup: {key}')
            if ':' not in volume or volume.startswith('/'):
                raise ValueError(f'Unmanaged disk: {key}')
        if re.fullmatch(r'(serial|parallel)\d+', key) and value != 'socket':
            raise ValueError(f'Host device: {key}')
    return external_media


def report_media(media):
    for device, volume in media.items():
        print(f'NOTICE: {device} references external ISO {volume}; ISO contents are not '
              'archived. Reattach or eject this media after restore.', file=sys.stderr)


DISK_KEY = re.compile(r'(?:ide|sata|scsi|virtio|efidisk|tpmstate)\d+')


def parse_native_config(raw):
    sections = {'current': {}}
    section = sections['current']
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('['):
            if not re.fullmatch(r'\[[A-Za-z0-9_-]+\]', line) or line[1:-1] in sections:
                raise ValueError('Unsupported/duplicate native configuration section')
            name = line[1:-1]
            if name == 'PENDING':
                raise ValueError('Resolve pending configuration before native archive')
            section = sections.setdefault(name, {})
        else:
            key, sep, value = line.partition(': ')
            if not sep or key in section:
                raise ValueError('Invalid/duplicate native configuration entry')
            section[key] = value
    for name, cfg in sections.items():
        safe_config(cfg)
        if 'snapstate' in cfg:
            raise ValueError('Snapshot operation is incomplete')
        if cfg.get('parent') and cfg['parent'] not in sections:
            raise ValueError('Snapshot parent is missing')
    for name in sections:
        seen = set()
        cursor = name
        while cursor:
            if cursor in seen:
                raise ValueError('Snapshot parent cycle')
            seen.add(cursor)
            cursor = sections[cursor].get('parent')
    return sections


def native_volumes(sections, ident):
    """Initially support directory QCOW2, without RAM or changing disk topology."""
    expected = None
    for cfg in sections.values():
        disks = {}
        for key, value in cfg.items():
            if not DISK_KEY.fullmatch(key):
                continue
            volume = value.split(',')[0]
            if 'media=cdrom' in value.split(','):
                if 'cloudinit' in volume:
                    raise ValueError('Native mode does not yet support cloud-init volumes')
                continue
            if not re.fullmatch(r'[A-Za-z0-9_-]+:' + re.escape(ident) +
                                r'/vm-' + re.escape(ident) + r'-disk-[A-Za-z0-9_-]+\.qcow2', volume):
                raise ValueError('Native mode requires VM-owned QCOW2 disk files: ' + volume)
            disks[key] = volume
        if expected is None:
            expected = disks
        elif disks != expected:
            raise ValueError('Native mode requires the same disk attachments in every snapshot')
    if not expected or len(set(expected.values())) != len(expected):
        raise ValueError('Expected distinct QCOW2 disks')
    return expected


def remap_native_config(sections, volume_map, unique=False):
    macs = {}
    output = []
    for name, original in sections.items():
        cfg = dict(original)
        if name != 'current':
            output.extend(['', f'[{name}]'])
        else:
            cfg['onboot'] = '0'
            cfg['lock'] = 'create'
        for key, value in cfg.items():
            if DISK_KEY.fullmatch(key):
                volume, sep, options = value.partition(',')
                value = volume_map.get(volume, volume) + (sep + options if sep else '')
            if unique and re.fullmatch(r'net\d+', key):
                def replace_mac(match):
                    old = match[0].lower()
                    if old not in macs:
                        macs[old] = '02:' + ':'.join(f'{b:02x}' for b in os.urandom(5))
                    return macs[old]
                value = re.sub(r'(?i)(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', replace_mac, value)
            output.append(f'{key}: {value}')
    return '\n'.join(output) + '\n'


class Manager:
    def __init__(self, args):
        self.a = args
        self.base = args.remote.rstrip('/')
        if not re.fullmatch(r'[A-Za-z0-9_-]+:.+', self.base):
            raise ValueError('Use a named rclone remote with a dedicated folder, e.g. gdrive:pve-archive')
        if args.source:
            self.base += '/sources/' + source_name(args.source)
        elif args.command in ('archive', 'upload', 'move-to-cloud', 'move-from-cloud'):
            raise ValueError('Archiving requires --source; legacy layout is read-only')
        self.work = Path(args.work_dir).resolve()

    def rc(self, *args, capture=False):
        return run('rclone', '--config', self.a.rclone_config,
                   '--retries', '5', '--low-level-retries', '10', *args, capture=capture)

    def api(self, path):
        return json.loads(run('pvesh', 'get', path, '--output-format', 'json', capture=True))

    def vm_path(self, ident):
        # /etc/pve/local follows Proxmox's canonical node name, not the FQDN.
        node = Path('/etc/pve/local').resolve().name
        return f'/nodes/{node}/qemu/{ident}'

    def config(self, ident):
        return self.api(self.vm_path(ident) + '/config')

    def stopped(self, ident):
        if run('qm', 'status', ident, capture=True).strip() != 'status: stopped':
            raise ValueError('VM must remain stopped throughout archive operation')

    def unchanged(self, ident, expected):
        current = self.config(ident)
        for cfg in (current, expected):
            cfg.pop('digest', None)
        if current != expected:
            raise ValueError('VM configuration changed; refusing deletion')
        self.stopped(ident)

    def stage(self, prefix):
        self.work.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=self.work))
        print(f'Local recovery files: {path}', file=sys.stderr)
        return path

    def archive(self):
        ident = vmid(self.a.vmid)
        if not self.a.delete_vm and not self.a.keep_vm:
            raise ValueError('Choose --delete-vm or --keep-vm')
        cfg = self.config(ident)
        if getattr(self.a, 'resume', None):
            if cfg.get('lock') != 'backup':
                raise ValueError('Resume requires the stopped VM to retain its backup lock')
            cfg.pop('lock')
        external_media = safe_config(cfg)
        report_media(external_media)
        if any(x.get('sid') == f'vm:{ident}' for x in self.api('/cluster/ha/resources')):
            raise ValueError('Remove this VM from HA management before archiving')
        if any(str(x.get('guest')) == ident for x in self.api('/cluster/replication')):
            raise ValueError('Remove replication jobs before archiving')
        if any('pending' in x or 'delete' in x for x in self.api(self.vm_path(ident) + '/pending')):
            raise ValueError('Resolve pending VM configuration changes before archiving')
        snapshots = self.api(self.vm_path(ident) + '/snapshot')
        snapshot_names = [s['name'] for s in snapshots if s.get('name') != 'current']
        if self.a.format == 'native-qcow2' or (self.a.format == 'auto' and snapshot_names) or getattr(self.a, 'resume', None):
            return self.archive_native(ident, cfg, snapshot_names)
        if snapshot_names:
            if self.a.delete_vm and not self.a.allow_snapshot_loss:
                raise ValueError('VM has snapshots. Archive contains current state only; '
                                 'use --allow-snapshot-loss with --delete-vm to explicitly '
                                 'accept deleting snapshot history, or use --keep-vm')
            print('NOTICE: Archive contains current state only. Snapshots excluded: '
                  + ', '.join(snapshot_names) + ('. Original snapshots remain with --keep-vm.'
                  if self.a.keep_vm else '. Snapshot history will be deleted with the VM.'),
                  file=sys.stderr)
        # Fail before shutting down if the destination cannot be listed.
        self.rc('mkdir', self.base)
        self.rc('lsjson', self.base, '--max-depth', '1', capture=True)
        stage = self.stage(f'archive-{ident}-')
        run('qm', 'shutdown', ident, '--timeout', self.a.shutdown_timeout)
        self.stopped(ident)
        self.unchanged(ident, cfg.copy())
        run('vzdump', ident, '--mode', 'stop', '--compress', 'zstd',
            '--dumpdir', stage, '--remove', '0')
        files = list(stage.glob(f'vzdump-qemu-{ident}-*.vma.zst'))
        if len(files) != 1 or files[0].stat().st_size == 0:
            raise ValueError('Expected exactly one nonempty VMA backup')
        archive = files[0]
        run('zstd', '--test', archive)
        self.unchanged(ident, cfg.copy())
        # Hold an ordinary Proxmox lock during the long upload/verification.
        # No pre-existing lock is bypassed. On failure this lock is retained.
        latest = self.config(ident)
        if 'lock' in latest:
            raise ValueError('VM became locked')
        run('qm', 'set', ident, '--lock', 'backup', '--digest', latest['digest'])
        locked = dict(cfg, lock='backup')
        self.unchanged(ident, locked.copy())
        ident_backup = f"{ident}/{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}"
        destination = f'{self.base}/{ident_backup}'
        # Only the archive and manifest are published; local vzdump logs remain.
        payload = stage / 'payload'
        payload.mkdir()
        archive = archive.rename(payload / archive.name)
        manifest = {'schema': 2, 'backup_id': ident_backup, 'vmid': int(ident),
                    'source': self.a.source,
                    'source_node': Path('/etc/pve/local').resolve().name,
                    'external_media': external_media,
                    'excluded_snapshots': snapshot_names,
                    'created_utc': datetime.now(timezone.utc).isoformat(),
                    'filename': archive.name, 'size': archive.stat().st_size,
                    'sha256': sha256(archive), 'config': cfg}
        (payload / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        self.rc('copy', payload, destination, '--immutable')
        # Full read-back; no reliance on size-only comparisons or backend hashes.
        self.rc('check', payload, destination, '--download')
        marker = stage / 'COMPLETE'
        marker.write_text(sha256(payload / 'manifest.json') + '\n')
        self.rc('copyto', marker, f'{destination}/COMPLETE', '--immutable')
        if self.rc('cat', f'{destination}/COMPLETE', capture=True) != marker.read_text():
            raise ValueError('Completion marker verification failed')
        print(f'Verified source: {self.a.source}; backup ID: {ident_backup}', flush=True)
        self.unchanged(ident, locked.copy())
        if self.a.delete_vm:
            # Bypass only the lock just created and checked above.
            run('qm', 'destroy', ident, '--skiplock', '1', '--purge', '1')
            if any(str(x.get('vmid')) == ident for x in self.api('/cluster/resources')):
                raise ValueError('VM still appears in cluster; retaining local backup')
        else:
            run('qm', 'unlock', ident)
        if self.a.cleanup_local:
            shutil.rmtree(stage)
        print('Archive complete. VM ' + ('deleted.' if self.a.delete_vm else 'retained, stopped.'))

    def manifest(self, bid):
        destination = f'{self.base}/{backup_id(bid)}'
        marker = self.rc('cat', f'{destination}/COMPLETE', capture=True).strip()
        raw = self.rc('cat', f'{destination}/manifest.json', capture=True)
        if hashlib.sha256(raw.encode()).hexdigest() != marker:
            raise ValueError('Manifest checksum mismatch or incomplete backup')
        m = json.loads(raw)
        expected_schemas = (2, 3) if self.a.source else (1,)
        if m.get('schema') not in expected_schemas or m.get('backup_id') != bid or str(m.get('vmid')) != bid.split('/')[0]:
            raise ValueError('Invalid manifest identity/schema')
        if self.a.source and m.get('source') != self.a.source:
            raise ValueError('Manifest source does not match selected source')
        if m['schema'] == 3:
            self.validate_native_manifest(m)
            return destination, m
        if not re.fullmatch(r'vzdump-qemu-[0-9]+-[A-Za-z0-9_-]+\.vma\.zst', m.get('filename', '')):
            raise ValueError('Unsafe archive filename')
        if not re.fullmatch(r'[a-f0-9]{64}', m.get('sha256', '')) or not isinstance(m.get('size'), int) or m['size'] <= 0:
            raise ValueError('Invalid archive checksum/size')
        return destination, m

    def download(self):
        destination, m = self.manifest(self.a.backup_id)
        if m.get('schema') == 3:
            return self.download_native(destination, m)
        report_media(m.get('external_media', {}))
        if m.get('excluded_snapshots'):
            print('NOTICE: This archive does not contain the original snapshot history: '
                  + ', '.join(m['excluded_snapshots']), file=sys.stderr)
        stage = self.stage('restore-')
        if shutil.disk_usage(stage).free < m['size'] + 1024 ** 3:
            raise ValueError('Insufficient staging space (requires archive size plus 1 GiB)')
        archive = stage / m['filename']
        self.rc('copyto', f"{destination}/{m['filename']}", archive)
        if archive.stat().st_size != m['size'] or sha256(archive) != m['sha256']:
            raise ValueError('Downloaded archive failed SHA-256/size verification')
        run('zstd', '--test', archive)
        return stage, archive

    def restore(self):
        ident = vmid(self.a.vmid)
        if any(str(x.get('vmid')) == ident for x in self.api('/cluster/resources')):
            raise ValueError('Target VMID already exists in cluster; refusing overwrite')
        stage, archive = self.download()
        if isinstance(archive, dict):
            return self.restore_native(ident, stage, archive)
        args = ['qmrestore', archive, ident]
        if self.a.storage:
            args += ['--storage', self.a.storage]
        if self.a.unique:
            args += ['--unique', '1']
        run(*args)
        # Restores intentionally stay stopped for network/hardware inspection.
        run('qm', 'set', ident, '--onboot', '0')
        self.stopped(ident)
        if self.a.cleanup_local:
            shutil.rmtree(stage)
        print(f'Restored VM {ident}, stopped with onboot disabled. Drive backup retained.')

    def listing(self):
        rows = json.loads(self.rc('lsjson', self.base, '--recursive', '--files-only', capture=True))
        complete = sorted(x['Path'][:-9] for x in rows if x['Path'].endswith('/COMPLETE'))
        if not getattr(self.a, 'all_versions', False):
            latest = {}
            for bid in complete:
                backup_id(bid)
                latest[bid.split('/')[0]] = bid
            complete = list(latest.values())
        print('VMID\tNAME\tFORMAT\tSNAPSHOTS\tARCHIVED UTC' +
              ('\tBACKUP ID' if getattr(self.a, 'all_versions', False) else ''))
        for bid in complete:
            _, m = self.manifest(bid)
            kind = m.get('format', 'vzdump')
            history = ','.join(m.get('snapshots', [])) or '-'
            print(f"{m['vmid']}\t{m.get('config', {}).get('name', '')}\t{kind}\t{history}\t{m.get('created_utc', '')}" +
                  (f'\t{bid}' if getattr(self.a, 'all_versions', False) else ''))

    def verify(self):
        stage, _ = self.download()
        if self.a.cleanup_local:
            shutil.rmtree(stage)
        print('Remote archive verified by download and SHA-256.')

    def config_file(self, ident):
        return Path(f'/etc/pve/qemu-server/{ident}.conf')

    def move_to_cloud(self):
        self.a.delete_vm = not self.a.keep_vm
        self.a.allow_snapshot_loss = False
        self.a.format = 'auto'
        self.archive()

    def move_from_cloud(self):
        ident = vmid(self.a.vmid)
        rows = json.loads(self.rc('lsjson', f'{self.base}/{ident}',
                                 '--recursive', '--files-only', capture=True))
        candidates = []
        for row in rows:
            path = row['Path']
            if path.endswith('/COMPLETE'):
                candidates.append(backup_id(ident + '/' + path[:-9]))
        if not candidates:
            raise ValueError(f'No complete cloud backup for source {self.a.source}, VM {ident}')
        # UTC names sort chronologically. Never silently restore an older copy
        # when validation of the newest complete archive fails.
        self.a.backup_id = max(candidates)
        _, manifest = self.manifest(self.a.backup_id)
        if manifest.get('schema') == 3 and not self.a.storage:
            storages = {d['volume'].split(':')[0] for d in manifest['disks']}
            if len(storages) != 1:
                raise ValueError('Archive uses multiple storages; specify --storage for this restore')
            self.a.storage = storages.pop()
        print(f'Restoring latest complete archive of {self.a.source} VM {ident} '
              f'from {manifest.get("created_utc", self.a.backup_id)}', flush=True)
        self.a.vmid = getattr(self.a, 'target_vmid', None) or ident
        self.restore()

    def qcow_info(self, path, snapshots):
        info = json.loads(run('qemu-img', 'info', '--output=json', '--backing-chain', path, capture=True))
        if len(info) != 1 or info[0].get('format') != 'qcow2' or info[0].get('backing-filename'):
            raise ValueError('Native mode requires standalone QCOW2 files without backing chains')
        item = info[0]
        features = item.get('format-specific', {}).get('data', {})
        if item.get('encrypted') or item.get('data-file') or features.get('data-file') or features.get('encrypt'):
            raise ValueError('External data files/encrypted QCOW2 are unsupported')
        if item.get('dirty-flag') or features.get('corrupt'):
            raise ValueError('QCOW2 is dirty or corrupt')
        if set(s['name'] for s in item.get('snapshots', [])) != set(snapshots):
            raise ValueError('QCOW2 snapshots do not match Proxmox snapshot configuration')
        if any(s.get('vm-state-size', 0) for s in item.get('snapshots', [])):
            raise ValueError('Native mode does not yet support snapshots with saved RAM')
        run('qemu-img', 'check', '-q', path)
        return item

    def archive_native(self, ident, cfg, snapshots):
        resume = getattr(self.a, 'resume', None)
        if resume:
            requested = Path(resume)
            stage = requested.resolve()
            if requested.is_symlink() or stage.parent != self.work or not stage.name.startswith(f'native-{ident}-'):
                raise ValueError('Resume path must be this VM native staging directory directly under --work-dir')
            payload = stage / 'payload'
            if not payload.is_dir() or payload.is_symlink() or (payload / 'vm.conf').is_symlink():
                raise ValueError('Invalid native staging directory')
            if (payload / 'manifest.json').exists() or (stage / 'COMPLETE').exists():
                raise ValueError('Resume is only supported before manifest creation/upload')
            raw = (payload / 'vm.conf').read_text()
            self.stopped(ident)
            locked_raw = self.config_file(ident).read_text()
            if re.sub(r'^lock: backup\n', '', locked_raw, flags=re.M) != raw:
                raise ValueError('VM or snapshot configuration differs from the failed operation')
            print(f'Resuming failed local copy in {stage}; retaining the VM lock.', file=sys.stderr)
        else:
            raw = self.config_file(ident).read_text()
        sections = parse_native_config(raw)
        if set(sections) - {'current'} != set(snapshots):
            raise ValueError('Snapshot configuration changed during preflight')
        volumes = native_volumes(sections, ident)
        sources = {}
        for key, volume in volumes.items():
            storage = self.api('/storage/' + volume.split(':')[0])
            if storage.get('type') != 'dir':
                raise ValueError('Native archive currently supports directory storage only')
            path = Path(run('pvesm', 'path', volume, capture=True).strip()).resolve()
            if not path.is_file():
                raise ValueError('Native disk is not a regular file')
            sources[key] = path
        self.rc('mkdir', self.base)
        self.rc('lsjson', self.base, '--max-depth', '1', capture=True)
        if not resume:
            stage = self.stage(f'native-{ident}-')
            total = sum(p.stat().st_size for p in sources.values())
            if shutil.disk_usage(stage).free < total + 1024 ** 3:
                raise ValueError(f'Native staging needs at least {total + 1024 ** 3} free bytes')
            run('qm', 'shutdown', ident, '--timeout', self.a.shutdown_timeout)
            self.unchanged(ident, cfg.copy())
            if self.config_file(ident).read_text() != raw:
                raise ValueError('Full VM configuration changed before lock')
            latest = self.config(ident)
            run('qm', 'set', ident, '--lock', 'backup', '--digest', latest['digest'])
            locked_raw = self.config_file(ident).read_text()
            if re.sub(r'^lock: backup\n', '', locked_raw, flags=re.M) != raw:
                raise ValueError('Unexpected configuration change while acquiring lock')
        locked = dict(cfg, lock='backup')
        self.unchanged(ident, locked.copy())
        payload = stage / 'payload'
        if not resume:
            payload.mkdir()
            (payload / 'vm.conf').write_bytes(raw.encode('utf-8'))
        allowed = {'vm.conf'} | {key + '.qcow2' for key in sources}
        if any(p.name not in allowed or p.is_symlink() or not p.is_file() for p in payload.iterdir()):
            raise ValueError('Unexpected files in native payload')
        records = []
        for key, path in sources.items():
            info = self.qcow_info(path, snapshots)
            dest = payload / (key + '.qcow2')
            source_hash = verified_native_copy(path, dest, stage / 'copy-mismatch.json')
            self.qcow_info(dest, snapshots)
            records.append({'filename': dest.name, 'volume': volumes[key], 'device': key,
                            'virtual_size': info['virtual-size'], 'size': dest.stat().st_size,
                            'sha256': source_hash})
        bid = f"{ident}/{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}"
        m = {'schema': 3, 'format': 'native-qcow2', 'backup_id': bid, 'vmid': int(ident),
             'source': self.a.source, 'source_node': Path('/etc/pve/local').resolve().name,
             'created_utc': datetime.now(timezone.utc).isoformat(), 'config': cfg,
             'snapshots': snapshots, 'disks': records,
             'config_sha256': sha256(payload / 'vm.conf'),
             'size': sum(x['size'] for x in records) + (payload / 'vm.conf').stat().st_size}
        (payload / 'manifest.json').write_bytes((json.dumps(m, indent=2) + '\n').encode('utf-8'))
        destination = self.base + '/' + bid
        self.rc('copy', payload, destination, '--immutable')
        self.rc('check', payload, destination, '--download')
        marker = stage / 'COMPLETE'
        marker.write_bytes((sha256(payload / 'manifest.json') + '\n').encode('ascii'))
        self.rc('copyto', marker, destination + '/COMPLETE', '--immutable')
        if self.rc('cat', destination + '/COMPLETE', capture=True) != marker.read_text():
            raise ValueError('Completion marker verification failed')
        print(f'Verified source: {self.a.source}; native backup ID: {bid}; snapshots: {snapshots}', flush=True)
        self.unchanged(ident, locked.copy())
        if self.config_file(ident).read_text() != locked_raw:
            raise ValueError('Snapshot configuration changed; refusing deletion')
        for d in records:
            if sha256(sources[d['device']]) != d['sha256']:
                raise ValueError('Original disk changed after archive; refusing deletion')
        if self.a.delete_vm:
            run('qm', 'destroy', ident, '--skiplock', '1', '--purge', '1')
            if any(str(x.get('vmid')) == ident for x in self.api('/cluster/resources')):
                raise ValueError('VM still appears in cluster; local copy retained')
        else:
            run('qm', 'unlock', ident)
        if self.a.cleanup_local:
            shutil.rmtree(stage)
        print('Native archive complete; ' + ('VM deleted.' if self.a.delete_vm else 'VM retained, stopped.'))

    def validate_native_manifest(self, m):
        if m.get('format') != 'native-qcow2' or not isinstance(m.get('disks'), list) or not m['disks']:
            raise ValueError('Invalid native manifest')
        if not isinstance(m.get('snapshots'), list) or not all(isinstance(s, str) and re.fullmatch(r'[A-Za-z0-9_-]+', s) for s in m['snapshots']):
            raise ValueError('Invalid snapshot names')
        if len(set(m['snapshots'])) != len(m['snapshots']) or 'current' in m['snapshots']:
            raise ValueError('Duplicate/reserved snapshot name')
        if not re.fullmatch(r'[a-f0-9]{64}', m.get('config_sha256', '')):
            raise ValueError('Invalid configuration checksum')
        names, devices, volumes = set(), set(), set()
        for d in m['disks']:
            if not DISK_KEY.fullmatch(d.get('device', '')) or d.get('filename') != d['device'] + '.qcow2':
                raise ValueError('Unsafe disk filename/device')
            if not re.fullmatch(r'[a-f0-9]{64}', d.get('sha256', '')):
                raise ValueError('Invalid disk checksum')
            if any(type(d.get(k)) is not int or d[k] <= 0 for k in ('size', 'virtual_size')):
                raise ValueError('Invalid disk size')
            if d['filename'] in names or d['device'] in devices or d.get('volume') in volumes:
                raise ValueError('Duplicate native disk')
            names.add(d['filename']); devices.add(d['device']); volumes.add(d.get('volume'))
        if type(m.get('size')) is not int or m['size'] < sum(d['size'] for d in m['disks']):
            raise ValueError('Invalid total native size')

    def download_native(self, destination, m):
        stage = self.stage('native-download-')
        if shutil.disk_usage(stage).free < m['size'] + 1024 ** 3:
            raise ValueError('Insufficient staging space for native disk files')
        self.rc('copyto', destination + '/vm.conf', stage / 'vm.conf')
        if sha256(stage / 'vm.conf') != m['config_sha256']:
            raise ValueError('Native configuration checksum mismatch')
        sections = parse_native_config((stage / 'vm.conf').read_text())
        for section in sections.values():
            report_media(safe_config(section))
        volumes = native_volumes(sections, str(m['vmid']))
        if volumes != {d['device']: d['volume'] for d in m['disks']} or set(sections) - {'current'} != set(m['snapshots']):
            raise ValueError('Native manifest/configuration mismatch')
        for d in m['disks']:
            target = stage / d['filename']
            self.rc('copyto', destination + '/' + d['filename'], target)
            if target.stat().st_size != d['size'] or sha256(target) != d['sha256']:
                raise ValueError('Native disk checksum mismatch')
            info = self.qcow_info(target, m['snapshots'])
            if info['virtual-size'] != d['virtual_size']:
                raise ValueError('Native disk virtual size mismatch')
        (stage / 'manifest.json').write_text(json.dumps(m, indent=2) + '\n')
        return stage, m

    def restore_native(self, ident, stage, m):
        storage = self.a.storage
        if not re.fullmatch(r'[A-Za-z0-9_-]+', storage):
            raise ValueError('Invalid storage ID')
        storage_cfg = self.api('/storage/' + storage)
        if storage_cfg.get('type') != 'dir':
            raise ValueError('Native restore requires directory storage supporting QCOW2')
        sections = parse_native_config((stage / 'vm.conf').read_text())
        total = sum(d['size'] for d in m['disks'])
        storage_path = Path(storage_cfg['path'])
        if shutil.disk_usage(storage_path).free < total + 1024 ** 3:
            raise ValueError('Destination needs space for another full native disk copy; '
                             'use staging on another filesystem if necessary')
        # qm create reserves the ID cluster-wide and fails if another task took it.
        run('qm', 'create', ident, '--name', f'native-restore-{ident}', '--lock', 'create')
        recovery = {'vmid': ident, 'allocated': [], 'status': 'restoring'}
        recovery_path = stage / 'restore-state.json'
        recovery_path.write_text(json.dumps(recovery, indent=2))
        mapping = {}
        for index, d in enumerate(m['disks']):
            filename = f'vm-{ident}-disk-{index}.qcow2'
            run('pvesm', 'alloc', storage, ident, filename,
                (d['virtual_size'] + 1023) // 1024, '--format', 'qcow2')
            volume = f'{storage}:{ident}/{filename}'
            recovery['allocated'].append(volume)
            recovery_path.write_text(json.dumps(recovery, indent=2))
            dest = Path(run('pvesm', 'path', volume, capture=True).strip()).resolve()
            if not dest.is_file():
                raise ValueError('Allocated destination is not a regular file')
            stream_copy(stage / d['filename'], dest)
            if sha256(dest) != d['sha256']:
                raise ValueError('Installed native disk checksum mismatch')
            self.qcow_info(dest, m['snapshots'])
            mapping[d['volume']] = volume
        self.stopped(ident)
        if self.config(ident).get('lock') != 'create':
            raise ValueError('Restore placeholder lock changed')
        restored = remap_native_config(sections, mapping, self.a.unique)
        self.config_file(ident).write_text(restored)
        if self.config_file(ident).read_text() != restored:
            raise ValueError('Restored configuration read-back mismatch')
        actual = self.api(self.vm_path(ident) + '/snapshot')
        if set(s['name'] for s in actual if s['name'] != 'current') != set(m['snapshots']):
            raise ValueError('Proxmox did not recognize restored snapshots')
        self.config(ident)  # Confirm Proxmox can parse the restored configuration.
        run('qm', 'unlock', ident)
        recovery['status'] = 'complete'
        recovery_path.write_text(json.dumps(recovery, indent=2))
        if self.a.cleanup_local:
            shutil.rmtree(stage)
        print(f'Native VM {ident} restored, stopped, onboot disabled; snapshots: {m["snapshots"]}. Drive copy retained.')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--version', action='version', version=__version__)
    p.add_argument('--remote', required=True, help='Configured rclone remote:dedicated-folder')
    sources = p.add_mutually_exclusive_group(required=True)
    sources.add_argument('--source', type=source_name,
                         help='Unique source server label; select original source for restore/list/verify')
    sources.add_argument('--legacy-layout', action='store_true',
                         help='Read backups created before source folders were introduced')
    p.add_argument('--rclone-config', default='/root/.config/rclone/rclone.conf')
    p.add_argument('--work-dir', default='/var/lib/vz/pve-drive')
    sub = p.add_subparsers(dest='command', required=True)
    to = sub.add_parser('upload', aliases=['move-to-cloud'], help='Archive VM and its supported snapshot history, verify, then delete VM')
    to.add_argument('vmid', type=vmid)
    to.add_argument('--keep-vm', action='store_true', help='Test upload without deleting the original VM')
    to.add_argument('--resume', metavar='STAGING_DIR', help='Retry a failed native local copy in its existing staging directory')
    to.add_argument('--keep-local', dest='cleanup_local', action='store_false', default=True)
    to.add_argument('--shutdown-timeout', type=int, default=300)
    fr = sub.add_parser('move-from-cloud', help='Restore latest complete archive to its original VMID')
    fr.add_argument('vmid', type=vmid)
    fr.add_argument('--target-vmid', type=vmid, help='Restore under another VMID')
    fr.add_argument('--storage', help='Override original storage on destination server')
    fr.add_argument('--unique', action='store_true', help='Generate new MAC addresses')
    fr.add_argument('--keep-local', dest='cleanup_local', action='store_false', default=True)
    a = sub.add_parser('archive', help='Shutdown, backup, upload, verify, optionally delete VM')
    a.add_argument('vmid', type=vmid)
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument('--delete-vm', action='store_true')
    g.add_argument('--keep-vm', action='store_true')
    a.add_argument('--shutdown-timeout', type=int, default=300)
    a.add_argument('--format', choices=['vzdump', 'native-qcow2'], default='vzdump',
                   help='native-qcow2 preserves internal disk snapshots on directory storage')
    a.add_argument('--allow-snapshot-loss', action='store_true',
                   help='Permit VM deletion even though its snapshot history is not archived')
    a.add_argument('--cleanup-local', action='store_true')
    r = sub.add_parser('restore')
    r.add_argument('backup_id', metavar='VMID', help='VMID to restore latest backup; advanced: explicit backup ID followed by target VMID')
    r.add_argument('vmid', type=vmid, nargs='?', help=argparse.SUPPRESS)
    r.add_argument('--target-vmid', type=vmid, help='Restore the selected source VM under another VMID')
    r.add_argument('--storage', help='Override original storage on destination server')
    r.add_argument('--unique', action='store_true', help='Generate new MAC addresses')
    r.add_argument('--keep-local', dest='cleanup_local', action='store_false', default=True)
    r.add_argument('--cleanup-local', action='store_true', help=argparse.SUPPRESS)
    listing = sub.add_parser('list')
    listing.add_argument('--all-versions', action='store_true', help='Show older versions and internal backup IDs')
    v = sub.add_parser('verify')
    v.add_argument('backup_id', type=backup_id)
    v.add_argument('--cleanup-local', action='store_true')
    return p


def main():
    args = parser().parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise ValueError('Run as root on Linux (archive/restore require a Proxmox node)')
    import fcntl
    os.umask(0o077)
    # One operation per node, regardless of work directory or destination.
    with open('/run/lock/pve-drive.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manager = Manager(args)
        if args.command == 'restore' and args.vmid is None:
            args.vmid = vmid(args.backup_id)
            manager.move_from_cloud()
        else:
            method = {'list': 'listing', 'upload': 'move_to_cloud'}.get(args.command, args.command.replace('-', '_'))
            getattr(manager, method)()


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print(f'ERROR: {exc}\nIf failure occurred during preflight, no VM changes were made. '
              'If shutdown or backup already started, inspect the task and VM state before retrying; '
              'see README recovery. Any recovery files created are retained.', file=sys.stderr)
        sys.exit(1)
