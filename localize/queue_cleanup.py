"""Clear validated pipeline scratch queues without removing their roots."""

import os
import shutil


def clear_queue_contents(folder: str | os.PathLike[str]) -> None:
    """Retain queue ownership and mount points; propagate content cleanup errors.

    Callers must validate the queue path before entering the cleanup lifecycle.
    The parent directory may be owned by root even when the queue is writable.
    """
    try:
        entries = os.scandir(folder)
    except FileNotFoundError:
        return
    with entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
