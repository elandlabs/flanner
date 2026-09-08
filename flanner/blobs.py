"""Keeping a file somebody attached, without putting it in the database.

A screenshot of the architecture that a decision refers to. The PDF a
constraint came from. The log archive behind a lesson. The memory says why
the file matters; the file is the evidence.

**Not in SQLite.** A blob column would inflate the catalog, slow every
backup of it, and make the one thing that is supposed to be a rebuildable
index into the only copy of something irreplaceable. Files go on disk,
addressed by the hash of their content, and the database holds a row that
points at one.

**Content-addressed**, so the same file attached twice is stored once, a
transfer can be resumed against a known digest, and the store can be
checked against itself. The path is `blobs/sha256/ab/<full digest>`: the
two-character shard exists because directories with tens of thousands of
entries are slow to list on every filesystem people actually use.

**Streamed.** Hashing and writing happen in one pass over chunks, so
attaching a large file never holds it in memory and the size cap is
enforced on the way through rather than after.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .exceptions import StorageError, ValidationError

#: Read size. Large enough that hashing is not syscall-bound, small enough
#: that a hundred concurrent attachments would not be worth worrying about.
CHUNK = 1024 * 1024

#: Where the store lives under the flanner home.
BLOB_DIR = Path("blobs") / "sha256"

#: How many leading hex characters name the shard directory.
SHARD = 2

#: What a file's first bytes say it is.
#:
#: Read from the content, never from the extension. A name is supplied by
#: whoever attached the file and a `.png` that is really a script should be
#: stored and served as what it is, not as what it claims.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"ID3", "audio/mpeg"),
    (b"\xff\xfb", "audio/mpeg"),
    (b"\xff\xf3", "audio/mpeg"),
    (b"\xff\xf2", "audio/mpeg"),
    (b"OggS", "audio/ogg"),
    (b"fLaC", "audio/flac"),
    (b"\x1f\x8b", "application/gzip"),
    (b"PK\x03\x04", "application/zip"),
)

#: Signatures that need a second look further into the file, because the
#: first four bytes are a length rather than a marker.
_RIFF = b"RIFF"
_FTYP = b"ftyp"

#: Fallback when nothing matches and the bytes are not text.
UNKNOWN = "application/octet-stream"


@dataclass(frozen=True)
class Stored:
    """One file, once it is in the store."""

    digest: str
    size_bytes: int
    mime_type: str
    path: Path
    #: False when an identical file was already here. The caller may want
    #: to say "attached" either way, but a size check should not count it
    #: against the memory's budget twice.
    written: bool


def blob_root(home: Path) -> Path:
    return Path(home) / BLOB_DIR


def path_for(home: Path, digest: str) -> Path:
    """Where a digest lives, whether or not it is there yet."""
    if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
        raise ValidationError(f"{digest!r} is not a sha-256 digest")
    return blob_root(home) / digest[:SHARD] / digest


def detect_mime(head: bytes) -> str:
    """What these first bytes are, by signature and then by decodability.

    Text last, and only if nothing else matched: almost anything decodes as
    Latin-1, so guessing text first would call every unknown format a text
    file. UTF-8 is strict enough to be a real signal.
    """
    for magic, mime in _SIGNATURES:
        if head.startswith(magic):
            return mime

    # RIFF and ISO base media both put their real marker past the length.
    if head[:4] == _RIFF and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[:4] == _RIFF and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == _FTYP:
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4"
        return "video/mp4"

    if b"\x00" in head:
        return UNKNOWN
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return UNKNOWN
    return "text/plain"


def sanitise_name(name: str) -> str:
    """A display name with nothing in it that could address a path.

    Kept for showing a person which file this was. Never used to build a
    path: the digest decides where a blob lives, so a hostile name has
    nowhere to escape to and this is defence in depth rather than the
    mechanism.
    """
    base = Path(name.replace("\\", "/")).name
    cleaned = "".join(c for c in base if c.isprintable() and c not in '<>:"|?*')
    cleaned = cleaned.strip(". ")
    return cleaned[:120] or "attachment"


def store(source: Path, *, home: Path, max_bytes: int, mime_hint: str | None = None) -> Stored:
    """Copy a file into the store, hashing it as it goes.

    The size cap is enforced during the copy rather than from a stat before
    it, because a file can grow between the two and because a stat says
    nothing about a stream. Going over stops the copy and removes the
    partial file.

    Nothing is written under the digest until the whole file has been read,
    so an interrupted attach leaves a temporary file rather than a blob
    that lies about its own name.
    """
    if not source.is_file():
        raise ValidationError(f"{source} is not a file")

    root = blob_root(home)
    root.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256()
    size = 0
    head = b""

    fd, tmp_name = tempfile.mkstemp(dir=root, suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as incoming:
            while chunk := incoming.read(CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise ValidationError(
                        f"{sanitise_name(source.name)} is larger than the "
                        f"{max_bytes // (1024 * 1024)} MB limit for one attachment"
                    )
                if not head:
                    head = chunk[:64]
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())

        if size == 0:
            raise ValidationError(f"{sanitise_name(source.name)} is empty")

        hexdigest = digest.hexdigest()
        final = path_for(home, hexdigest)
        final.parent.mkdir(parents=True, exist_ok=True)

        if final.exists():
            # Already held. The bytes are identical by definition, so the
            # copy is dropped rather than rewritten over a good file.
            tmp.unlink(missing_ok=True)
            return Stored(
                digest=hexdigest,
                size_bytes=final.stat().st_size,
                mime_type=mime_hint or detect_mime(head),
                path=final,
                written=False,
            )

        os.replace(tmp, final)
        return Stored(
            digest=hexdigest,
            size_bytes=size,
            mime_type=mime_hint or detect_mime(head),
            path=final,
            written=True,
        )
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_text(digest: str, *, home: Path, limit: int) -> str:
    """A text blob's content, for indexing. Empty for anything else.

    Capped, because the point is to make an attachment findable rather than
    to put a whole document into a search index that also has to answer
    quickly.
    """
    path = path_for(home, digest)
    if not path.is_file():
        return ""
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def collect(home: Path, *, keep: set[str]) -> tuple[int, int]:
    """Delete blobs nothing references. Returns how many, and how big.

    Called only when asked. A store that tidied itself on a timer would be
    deleting somebody's evidence on a schedule they did not choose, and the
    set of live digests is only knowable from the database.
    """
    root = blob_root(home)
    if not root.is_dir():
        return 0, 0

    removed = 0
    freed = 0
    for shard in sorted(root.iterdir()):
        if not shard.is_dir():
            continue
        for blob in sorted(shard.iterdir()):
            if blob.name in keep or blob.suffix == ".part":
                continue
            try:
                size = blob.stat().st_size
                blob.unlink()
            except OSError as e:  # pragma: no cover - reported, never fatal
                raise StorageError(f"could not remove {blob}: {e}") from None
            removed += 1
            freed += size
        with_nothing_left = not any(shard.iterdir())
        if with_nothing_left:
            shard.rmdir()
    return removed, freed


def total_size(home: Path) -> int:
    """How much disk the store is using."""
    root = blob_root(home)
    if not root.is_dir():
        return 0
    return sum(
        blob.stat().st_size
        for shard in root.iterdir()
        if shard.is_dir()
        for blob in shard.iterdir()
        if blob.is_file()
    )


def export(digest: str, *, home: Path, destination: Path) -> Path:
    """Copy a blob back out under a name a person can read."""
    source = path_for(home, digest)
    if not source.is_file():
        raise StorageError(f"no attachment stored for {digest[:12]}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination
