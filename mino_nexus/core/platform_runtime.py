"""Cross-platform local filesystem and cooperative user-process service adapter.

No PID-based killing, privileged service installation, or additional listener port.
A lifetime OS lock proves exit; stop requests are consumed by the native app.
"""
from contextlib import contextmanager
import json
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def executable(path):
    return Path(str(path) + '.exe') if os.name == 'nt' else Path(path)


def sync_directory(path):
    if os.name == 'nt':
        return  # Windows flushes the file before atomic replace; directories cannot be fsynced.
    fd = os.open(path, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


@contextmanager
def file_lock(path, shared=False, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink(): raise ValueError('Lock file cannot be a link')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(fd).st_size == 0: os.write(fd, b'0')
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBRLCK if shared else msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK): raise
                    if not blocking: raise BlockingIOError('Lock is held') from exc
                    time.sleep(.05)
        else:
            import fcntl
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def current_release(root):
    root = Path(root)
    if os.name == 'nt':
        build = json.loads((root/'current.json').read_text())['build_id']
        if not isinstance(build, str) or Path(build).name != build or build in ('.', '..'):
            raise ValueError('Invalid current build')
        target = root/'releases'/build
    else:
        if not (root/'current').is_symlink(): raise ValueError('Current release is not registered')
        target = root/'current'
    target = target.resolve(strict=True)
    if target.parent != (root/'releases').resolve(): raise ValueError('Current release escapes installation')
    return target


def switch_release(root, build):
    root = Path(root)
    if not isinstance(build, str) or Path(build).name != build or build in ('.', '..'):
        raise ValueError('Invalid build')
    if not (root/'releases'/build).is_dir(): raise ValueError('Release missing')
    if os.name == 'nt':
        fd, tmp = tempfile.mkstemp(prefix='.current-', dir=root)
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump({'build_id':build}, f); f.flush(); os.fsync(f.fileno())
            os.replace(tmp, root/'current.json')
        finally:
            Path(tmp).unlink(missing_ok=True)
    else:
        tmp = root/('.current-'+next(tempfile._get_candidate_names()))
        tmp.symlink_to(Path('releases')/build)
        try: os.replace(tmp, root/'current')
        finally: tmp.unlink(missing_ok=True)
    sync_directory(root)


def spawn(command, root, env=None):
    root = Path(root)
    (root/'logs').mkdir(exist_ok=True)
    options = {'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS} if os.name == 'nt' else {'start_new_session':True}
    with (root/'logs/process.log').open('ab') as log:
        return subprocess.Popen(list(map(str, command)), cwd=root, env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, **options)


def running(root):
    try:
        with file_lock(Path(root)/'updates/service-runtime.lock', blocking=False): return False
    except BlockingIOError: return True


def start(root, command):
    root = Path(root)
    with file_lock(root/'updates/service-control.lock'):
        if running(root): return
        (root/'updates/stop-request').unlink(missing_ok=True)
        child = spawn(command, root)
        until = time.monotonic()+30
        while not running(root):
            if child.poll() is not None: raise RuntimeError('Service launcher exited; see logs/process.log')
            if time.monotonic()>until: raise RuntimeError('Service did not acquire its lifetime lock')
            time.sleep(.1)


def stop(root):
    root = Path(root)
    with file_lock(root/'updates/service-control.lock'):
        if not running(root): return
        (root/'updates/stop-request').write_text('stop\n')
        until = time.monotonic()+60
        while running(root):
            if time.monotonic()>until: raise RuntimeError('Service has not exited; no force kill performed')
            time.sleep(.1)


def run_service(root, command, env=None):
    root = Path(root)
    with file_lock(root/'updates/service-runtime.lock', blocking=False):
        environment = dict(os.environ if env is None else env, MINO_SERVICE_STOP_FILE=str(root/'updates/stop-request'))
        return subprocess.run(list(map(str, command)), env=environment, cwd=root).returncode


def watch_stop(callback):
    raw = os.environ.get('MINO_SERVICE_STOP_FILE')
    if not raw: return
    import threading
    def watch():
        while not Path(raw).exists(): time.sleep(.2)
        callback()
    threading.Thread(target=watch, daemon=True).start()
