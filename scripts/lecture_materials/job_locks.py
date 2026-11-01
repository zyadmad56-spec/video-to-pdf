"""Process-owned exclusion for a job's transcription and checkpoint resets."""

from contextlib import contextmanager
import errno
import os
from pathlib import Path

from .jobs import load_job, register_generated


@contextmanager
def speech_lock(root):
    root = Path(root).resolve()
    load_job(root)
    path = root / "work/speech.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            raise ValueError("Transcription is already running for this job; stop its owner before retrying or resetting") from error
        try:
            register_generated(root, path)
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    # Keep the inode: unlinking a lock file can let a third process bypass an owner.
