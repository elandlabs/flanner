"""Evidence beside a memory, stored on disk rather than in the database.

Two properties carry this phase. An image, a PDF or an audio file can be
attached without going into SQLite, and a refused attachment cannot damage
the memory it was offered to.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from flanner import blobs
from flanner import memory_ops as mem
from flanner.database import create_project, get_session, init_database, list_attachments
from flanner.exceptions import NotFoundError, ValidationError
from flanner.memory_policy import Policy

#: Real first bytes, so detection is tested against what a file actually
#: starts with rather than against a name.
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 40
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"pixels" * 40
PDF = b"%PDF-1.7\n" + b"pages" * 40
WAV = b"RIFF\x24\x08\x00\x00WAVEfmt " + b"samples" * 40
MP3 = b"ID3\x03\x00\x00\x00" + b"frames" * 40


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FLANNER_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # noqa: S603,S607

    init_database(str(home / "data.db"))
    session = get_session()
    project = create_project(session, name="demo", project_root=str(repo), auto_gitignore=False)
    session.commit()
    memory, _ = mem.remember(
        session, content="The rate limit is per organisation.", category="fact", project=project
    )
    return session, project, memory, home


@pytest.fixture
def files(tmp_path):
    where = tmp_path / "files"
    where.mkdir()
    return where


def _file(where: Path, name: str, content: bytes) -> Path:
    path = where / name
    path.write_bytes(content)
    return path


# --- the store ----------------------------------------------------------------


def test_a_file_is_kept_on_disk_and_not_in_the_database(store, files):
    """Acceptance criterion 12. A blob column would make the catalog, which
    is supposed to be a rebuildable index, the only copy of something
    irreplaceable."""
    session, project, memory, home = store

    result = mem.attach(session, memory_id=memory.id, source=_file(files, "d.png", PNG))

    blob = blobs.path_for(home, result["digest"])
    assert blob.is_file()
    assert blob.read_bytes() == PNG
    row = list_attachments(session, memory.id)[0]
    assert not hasattr(row, "data")
    assert row.content_hash == result["digest"]


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("shot.png", PNG, "image/png"),
        ("photo.jpg", JPEG, "image/jpeg"),
        ("spec.pdf", PDF, "application/pdf"),
        ("note.wav", WAV, "audio/wav"),
        ("note.mp3", MP3, "audio/mpeg"),
        ("notes.txt", b"plain words", "text/plain"),
    ],
)
def test_the_type_comes_from_the_bytes(store, files, name, content, expected):
    session, project, memory, home = store

    result = mem.attach(session, memory_id=memory.id, source=_file(files, name, content))

    assert result["mime_type"] == expected


def test_a_lying_extension_is_stored_as_what_it_is(store, files):
    """A `.png` that is really something else must be recorded and served
    as what it is, not as what it claims."""
    session, project, memory, home = store

    result = mem.attach(session, memory_id=memory.id, source=_file(files, "innocent.png", PDF))

    assert result["mime_type"] == "application/pdf"


def test_the_same_file_on_two_memories_is_stored_once(store, files):
    """The reason for addressing by content."""
    session, project, memory, home = store
    other, _ = mem.remember(session, content="A second memory.", category="fact", project=project)
    shot = _file(files, "shot.png", PNG)

    first = mem.attach(session, memory_id=memory.id, source=shot)
    second = mem.attach(session, memory_id=other.id, source=shot)

    assert first["digest"] == second["digest"]
    assert second["deduplicated"] is True
    stored = list((home / "blobs" / "sha256").rglob("*"))
    assert len([p for p in stored if p.is_file()]) == 1


def test_attaching_the_same_file_twice_to_one_memory_says_so(store, files):
    session, project, memory, home = store
    shot = _file(files, "shot.png", PNG)
    mem.attach(session, memory_id=memory.id, source=shot)

    again = mem.attach(session, memory_id=memory.id, source=shot)

    assert again["attached"] is False
    assert len(list_attachments(session, memory.id)) == 1


def test_the_original_may_be_deleted_afterwards(store, files):
    """It is copied into the store, so somebody attaching from a downloads
    folder is not signing up to keep that folder."""
    session, project, memory, home = store
    shot = _file(files, "shot.png", PNG)
    result = mem.attach(session, memory_id=memory.id, source=shot)

    shot.unlink()

    path, _mime, _name = mem.open_attachment(session, UUID(result["id"]))
    assert path.read_bytes() == PNG


# --- refusing ------------------------------------------------------------------


def test_a_refused_attachment_changes_nothing(store, files):
    """Acceptance criterion 13. Attaching something must never be a risk to
    the memory it is offered to."""
    session, project, memory, home = store
    before = mem.describe(session, memory.id)
    big = _file(files, "big.bin", b"\x00" * (2 * 1024 * 1024))

    with pytest.raises(ValidationError, match="larger than"):
        mem.attach(session, memory_id=memory.id, source=big, policy=Policy(max_file_mb=1))

    after = mem.describe(session, memory.id)
    assert after["body"] == before["body"]
    assert after["status"] == before["status"]
    assert list_attachments(session, memory.id) == []


def test_an_oversized_file_leaves_no_partial_blob(store, files):
    """The cap is enforced during the copy, so the copy has to clean up
    after itself rather than leaving half a file under a name."""
    session, project, memory, home = store
    big = _file(files, "big.bin", b"\x00" * (2 * 1024 * 1024))

    with pytest.raises(ValidationError):
        mem.attach(session, memory_id=memory.id, source=big, policy=Policy(max_file_mb=1))

    root = home / "blobs" / "sha256"
    leftovers = [p for p in root.rglob("*") if p.is_file()] if root.is_dir() else []
    assert leftovers == []


def test_a_type_this_project_does_not_take_is_refused(store, files):
    session, project, memory, home = store
    policy = Policy(allowed_mime_prefixes=("image/",))

    with pytest.raises(ValidationError, match="not a type this project takes"):
        mem.attach(
            session,
            memory_id=memory.id,
            source=_file(files, "spec.pdf", PDF),
            policy=policy,
        )

    assert list_attachments(session, memory.id) == []


def test_no_prefixes_means_every_type(store, files):
    """The default. Somebody attaching a log archive to a debugging lesson
    should not have to configure that first."""
    session, project, memory, home = store

    result = mem.attach(
        session,
        memory_id=memory.id,
        source=_file(files, "logs.gz", b"\x1f\x8b" + b"compressed" * 20),
        policy=Policy(),
    )

    assert result["attached"] is True


def test_a_project_that_takes_no_attachments_says_so(store, files):
    session, project, memory, home = store

    with pytest.raises(ValidationError, match="does not take attachments"):
        mem.attach(
            session,
            memory_id=memory.id,
            source=_file(files, "shot.png", PNG),
            policy=Policy(attachments_enabled=False),
        )


def test_the_per_memory_budget_is_enforced_across_files(store, files):
    """One file under the limit, several over it, is still over it."""
    session, project, memory, home = store
    policy = Policy(max_file_mb=1, max_memory_mb=1)
    half = b"a" * (600 * 1024)
    mem.attach(session, memory_id=memory.id, source=_file(files, "one.bin", half), policy=policy)

    with pytest.raises(ValidationError):
        mem.attach(
            session,
            memory_id=memory.id,
            source=_file(files, "two.bin", b"b" * (600 * 1024)),
            policy=policy,
        )

    assert len(list_attachments(session, memory.id)) == 1


def test_an_empty_file_is_refused(store, files):
    session, project, memory, home = store

    with pytest.raises(ValidationError, match="is empty"):
        mem.attach(session, memory_id=memory.id, source=_file(files, "nothing.txt", b""))


def test_a_file_that_is_not_there_is_refused(store, files):
    session, project, memory, home = store

    with pytest.raises(ValidationError, match="is not a file"):
        mem.attach(session, memory_id=memory.id, source=files / "missing.png")


def test_attaching_to_a_memory_that_does_not_exist_says_so(store, files):
    session, project, memory, home = store

    with pytest.raises(NotFoundError):
        mem.attach(session, memory_id=uuid4(), source=_file(files, "shot.png", PNG))


# --- searching -----------------------------------------------------------------


def test_a_text_attachment_makes_its_memory_findable(store, files):
    session, project, memory, home = store

    mem.attach(
        session,
        memory_id=memory.id,
        source=_file(files, "notes.txt", b"the limiter is a token bucket"),
    )

    found = mem.recall(session, query="token bucket", project_id=project.id)
    assert [m["id"] for m in found["memories"]] == [str(memory.id)]


def test_a_filename_makes_its_memory_findable(store, files):
    """An image has no text, and its name is usually the only thing
    anybody would look for it by."""
    session, project, memory, home = store

    mem.attach(session, memory_id=memory.id, source=_file(files, "throttling.png", PNG))

    found = mem.recall(session, query="throttling", project_id=project.id)
    assert [m["id"] for m in found["memories"]] == [str(memory.id)]


def test_a_description_makes_its_memory_findable(store, files):
    session, project, memory, home = store

    mem.attach(
        session,
        memory_id=memory.id,
        source=_file(files, "a.png", PNG),
        description="the escalation path when a tenant is throttled",
    )

    found = mem.recall(session, query="escalation path", project_id=project.id)
    assert [m["id"] for m in found["memories"]] == [str(memory.id)]


def test_an_image_is_recorded_as_having_no_text_rather_than_as_a_failure(store, files):
    """`unsupported` is an answer. Saying "failed" would suggest looking
    again might help."""
    session, project, memory, home = store

    mem.attach(session, memory_id=memory.id, source=_file(files, "a.png", PNG))

    assert mem.attachments_of(session, memory.id)[0]["extraction_status"] == "unsupported"


# --- detaching and collecting ---------------------------------------------------


def test_detaching_keeps_the_file(store, files):
    """Another memory may hold the same one, so this cannot be the place
    that decides to delete bytes."""
    session, project, memory, home = store
    result = mem.attach(session, memory_id=memory.id, source=_file(files, "a.png", PNG))

    mem.detach(session, attachment_id=UUID(result["id"]))

    assert list_attachments(session, memory.id) == []
    assert blobs.path_for(home, result["digest"]).is_file()


def test_collecting_removes_what_nothing_points_at(store, files):
    session, project, memory, home = store
    result = mem.attach(session, memory_id=memory.id, source=_file(files, "a.png", PNG))
    mem.detach(session, attachment_id=UUID(result["id"]))

    outcome = mem.collect_blobs(session)

    assert outcome["removed"] == 1
    assert not blobs.path_for(home, result["digest"]).exists()


def test_collecting_keeps_what_something_still_points_at(store, files):
    """The case a reference count would get wrong."""
    session, project, memory, home = store
    other, _ = mem.remember(session, content="Another.", category="fact", project=project)
    shot = _file(files, "a.png", PNG)
    first = mem.attach(session, memory_id=memory.id, source=shot)
    mem.attach(session, memory_id=other.id, source=shot)

    mem.detach(session, attachment_id=UUID(first["id"]))
    outcome = mem.collect_blobs(session)

    assert outcome["removed"] == 0
    assert blobs.path_for(home, first["digest"]).is_file()


def test_purging_a_memory_leaves_its_blob_for_the_collector(store, files):
    """Purge removes the memory. The file it pointed at may be held by
    another memory, so it is the collector's decision, not purge's."""
    session, project, memory, home = store
    result = mem.attach(session, memory_id=memory.id, source=_file(files, "a.png", PNG))

    mem.forget(session, memory_id=memory.id, purge=True)

    assert blobs.path_for(home, result["digest"]).is_file()
    assert mem.collect_blobs(session)["removed"] == 1


def test_detaching_something_that_does_not_exist_says_so(store):
    session, project, memory, home = store

    with pytest.raises(NotFoundError):
        mem.detach(session, attachment_id=uuid4())


# --- the store on its own -------------------------------------------------------


def test_a_display_name_cannot_address_a_path(tmp_path):
    """Defence in depth. The digest decides where a blob lives, so a
    hostile name has nowhere to escape to; this stops it being shown."""
    assert blobs.sanitise_name("../../etc/passwd") == "passwd"
    assert blobs.sanitise_name(r"C:\Windows\System32\evil.dll") == "evil.dll"
    assert blobs.sanitise_name("....") == "attachment"
    assert blobs.sanitise_name("") == "attachment"


def test_a_digest_that_is_not_one_is_refused(tmp_path):
    for bad in ("", "nope", "../../etc/passwd", "z" * 64):
        with pytest.raises(ValidationError):
            blobs.path_for(tmp_path, bad)


def test_the_store_is_sharded(tmp_path, store, files):
    """Directories with tens of thousands of entries are slow to list on
    every filesystem people actually use."""
    session, project, memory, home = store
    result = mem.attach(session, memory_id=memory.id, source=_file(files, "a.png", PNG))

    blob = blobs.path_for(home, result["digest"])

    assert blob.parent.name == result["digest"][:2]
    assert blob.name == result["digest"]


def test_a_large_file_is_never_held_whole_in_memory(tmp_path):
    """Asserted by construction rather than by measurement: the copy reads
    in chunks, so a file larger than one chunk exercises the loop."""
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * (blobs.CHUNK * 2 + 17))
    home = tmp_path / "home"

    stored = blobs.store(source, home=home, max_bytes=blobs.CHUNK * 4)

    assert stored.size_bytes == blobs.CHUNK * 2 + 17
    assert stored.path.stat().st_size == stored.size_bytes
