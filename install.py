#!/usr/bin/env python3
"""Install the standalone pve-drive command; preserve an old directory layout."""
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone
import uuid


def install_to(source, target):
    source, target = Path(source), Path(target)
    data = source.read_bytes()
    compile(data, str(source), 'exec')  # Validate before changing the installation.
    if target.is_symlink():
        raise ValueError(f'Refusing to replace symlink: {target}')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.pve-drive-install-', dir=target.parent)
    staged = Path(name)
    previous = None
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        staged.chmod(0o755)
        if staged.read_bytes() != data:
            raise ValueError('Installer copy verification failed')
        if target.is_dir():
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            previous = target.with_name(f'{target.name}.previous-{stamp}-{uuid.uuid4().hex[:8]}')
            target.rename(previous)
        try:
            os.replace(staged, target)
        except BaseException:
            if previous is not None and not target.exists():
                previous.rename(target)
            raise
    finally:
        if staged.exists():
            staged.unlink()
    return previous


def main():
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise ValueError('Run on the Proxmox server as root: sudo ./install.sh')
    import fcntl
    # Share the application lock: do not replace its files during an active task.
    with open('/run/lock/pve-drive.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A pve-drive operation is running; install after it finishes')
        target = Path('/usr/local/sbin/pve-drive')
        previous = install_to(Path(__file__).resolve().with_name('pve_drive.py'), target)
    print(f'Installed {target}')
    if previous:
        print(f'Previous directory preserved at {previous}')
    print('Usage: pve-drive --help')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Install failed: {exc}', file=sys.stderr)
        sys.exit(1)
