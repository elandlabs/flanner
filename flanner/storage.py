"""
Storage layer for Flanner

Handles file system operations for plan files.
"""

import contextlib
import logging
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .exceptions import DatabaseError, PlanFileNotFoundError
from .frontmatter import parse_frontmatter

logger = logging.getLogger(__name__)


#: Lock file naming and timing. Shared by every writer that needs one name
#: held across processes, so two domains cannot disagree about how long an
#: abandoned lock stays believed.
LOCK_PREFIX = ".flanner-"
LOCK_SUFFIX = ".lock"
LOCK_TIMEOUT_S = 10.0
LOCK_STALE_S = 30.0
LOCK_RETRY_S = 0.05


def atomic_write_text(path: Path, text: str) -> None:
    """Write text so a reader sees the whole file or the old one, never half.

    A temp file in the same directory, fsynced, then renamed. Same directory
    because ``os.replace`` is only atomic within a filesystem, and a temp
    directory can easily be on another one.

    Newlines are normalised to LF and written without OS translation.
    Browsers submit textarea content as CRLF, and text-mode writing on
    Windows would translate the LF again into CRLF-CR. On the next read
    universal newlines turns that into an extra blank line, so a file
    degrades a little on every edit. Doing it here rather than at each
    caller is the only way that stays true for a caller written later.

    The parent directory is created; the caller decides where, not whether.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(normalized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


@contextlib.contextmanager
def exclusive_lock(directory: Path, key: str) -> Iterator[None]:
    """Hold one named lock across processes, or give up saying so.

    Creation with O_EXCL is atomic on every supported platform, which is the
    whole mechanism. A lock older than ``LOCK_STALE_S`` is treated as
    abandoned by a crashed holder and taken over, because the alternative is
    a machine that stays wedged until somebody deletes a file they have
    never heard of.

    ``key`` must already be safe as a filename. Callers key on ids rather
    than user-supplied names for that reason, and because a rename would
    otherwise move a lock out from under whoever holds it.

    A crash strands one small file, cleared by the next writer of the same
    key. Nothing scans for them, so the name is prefixed and suffixed to
    stay out of the way of anything globbing for content.
    """
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / f"{LOCK_PREFIX}{key}{LOCK_SUFFIX}"
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - lock_path.stat().st_mtime > LOCK_STALE_S:
                    lock_path.unlink()
                    continue
            if time.monotonic() > deadline:
                raise DatabaseError(f"Timed out waiting for write lock at {lock_path}") from None
            time.sleep(LOCK_RETRY_S)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock_path.unlink()


def init_storage(base_path: str) -> None:
    """
    Initialize storage directory structure.

    Args:
        base_path: Base path for Flanner (e.g., ~/.flanner)
    """
    base = Path(base_path)
    base.mkdir(parents=True, exist_ok=True)

    logger.info("Storage initialized at: %s", base_path)


def save_plan_file_with_frontmatter(
    project_root: str, plan_directory: str, file_name: str, content: str
) -> str:
    """
    Save plan file to project's plan directory.

    Args:
        project_root: Absolute path to project root
        plan_directory: Relative path for plan files (e.g., ".plans")
        file_name: File name (e.g., "architecture_v1.md")
        content: Complete content including frontmatter

    Returns:
        Absolute path to saved file
    """
    # Construct full path
    full_plan_path = Path(project_root) / plan_directory
    full_plan_path.mkdir(parents=True, exist_ok=True)

    # The file name may address a subdirectory (e.g. "auth/login_v1.md"),
    # which `atomic_write_text` creates along with the plan directory.
    file_path = full_plan_path / file_name
    atomic_write_text(file_path, content)
    return str(file_path)


def load_plan_file(file_path: str) -> tuple[dict[str, Any], str]:
    """
    Load plan file and return frontmatter and content.

    Args:
        file_path: Absolute path to plan file

    Returns:
        Tuple of (frontmatter_dict, content_body)

    Raises:
        FileNotFoundError: If file doesn't exist
    """
    if not os.path.exists(file_path):
        raise PlanFileNotFoundError(f"Plan file not found: {file_path}")

    with open(file_path, encoding="utf-8") as f:
        content = f.read()

    return parse_frontmatter(content)


def load_plan_file_full(file_path: str) -> str:
    """
    Load complete plan file content.

    Args:
        file_path: Absolute path to plan file

    Returns:
        Complete file content (frontmatter + body)

    Raises:
        FileNotFoundError: If file doesn't exist
    """
    if not os.path.exists(file_path):
        raise PlanFileNotFoundError(f"Plan file not found: {file_path}")

    with open(file_path, encoding="utf-8") as f:
        return f.read()


def generate_file_path(project_root: str, plan_directory: str, file_name: str) -> str:
    """
    Generate absolute path for a plan file.

    Args:
        project_root: Absolute path to project root
        plan_directory: Relative path for plan files
        file_name: File name

    Returns:
        Absolute path to file
    """
    return str(Path(project_root) / plan_directory / file_name)


def delete_plan_file(file_path: str) -> bool:
    """
    Delete a plan file.

    Args:
        file_path: Absolute path to file

    Returns:
        True if deleted, False if file didn't exist
    """
    if not os.path.exists(file_path):
        return False

    os.remove(file_path)
    return True


def list_plan_files_in_directory(directory: str) -> list[str]:
    """
    List all markdown files in a directory.

    Args:
        directory: Path to directory

    Returns:
        List of filenames
    """
    if not os.path.exists(directory):
        return []

    path = Path(directory)
    # Recurse and return paths relative to the directory, so plans that live in
    # subdirectories (e.g. "auth/login_v1.md") are found and identifiable.
    return [f.relative_to(path).as_posix() for f in sorted(path.rglob("*.md"))]


def get_file_stats(file_path: str) -> dict[str, Any] | None:
    """
    Get file statistics.

    Args:
        file_path: Path to file

    Returns:
        Dictionary with file stats or None if file doesn't exist
    """
    if not os.path.exists(file_path):
        return None

    stat = os.stat(file_path)

    return {
        "size": stat.st_size,
        "created": stat.st_ctime,
        "modified": stat.st_mtime,
        "accessed": stat.st_atime,
    }


def backup_plan_file(file_path: str, backup_suffix: str = ".backup") -> str:
    """
    Create a backup copy of a plan file.

    Args:
        file_path: Path to original file
        backup_suffix: Suffix for backup file

    Returns:
        Path to backup file

    Raises:
        FileNotFoundError: If original file doesn't exist
    """
    if not os.path.exists(file_path):
        raise PlanFileNotFoundError(f"File not found: {file_path}")

    backup_path = file_path + backup_suffix

    # Read original
    with open(file_path, encoding="utf-8") as f:
        content = f.read()

    # Write backup
    with open(backup_path, "w", encoding="utf-8") as f:
        f.write(content)

    return backup_path


def ensure_plan_directory_exists(project_root: str, plan_directory: str) -> str:
    """
    Ensure plan directory exists within project root.

    Args:
        project_root: Absolute path to project root
        plan_directory: Relative path for plan directory

    Returns:
        Absolute path to plan directory
    """
    full_path = Path(project_root) / plan_directory
    full_path.mkdir(parents=True, exist_ok=True)
    return str(full_path)


def move_plan_file(old_path: str, new_path: str) -> bool:
    """
    Move a plan file to a new location.

    Args:
        old_path: Current file path
        new_path: New file path

    Returns:
        True if successful

    Raises:
        FileNotFoundError: If old file doesn't exist
    """
    if not os.path.exists(old_path):
        raise PlanFileNotFoundError(f"File not found: {old_path}")

    # Ensure destination directory exists
    dest_dir = Path(new_path).parent
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Move file
    os.rename(old_path, new_path)
    return True
