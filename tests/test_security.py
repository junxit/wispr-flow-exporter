"""Security regressions, organized by the finding each one exists to hold.

Every section here corresponds to a guarantee stated in ``SECURITY.md``. The
value of grouping them this way is that a reader auditing the document can find
the test that proves each claim, and a reader deleting a test can see which
promise they would be breaking.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wispr_flow_exporter import paths
from wispr_flow_exporter.local_config import Policy, redact
from wispr_flow_exporter.schema import EXPECTED
from wispr_flow_exporter.secure_io import DIR_MODE, FILE_MODE
from wispr_flow_exporter.sqlite_source import open_source
from wispr_flow_exporter.store import Archive, UnsafeArchivePathError
from wispr_flow_exporter.sync import SyncOptions, sync_local

from conftest import (
    FAKE_JWT,
    FAKE_SESSION_KEY,
    HISTORY_A,
    MEETING_A,
    OWNER,
    SECOND,
    TITLE_FRONTMATTER,
    TITLE_TRAVERSAL,
)

SPEAKER_MAP = json.dumps(
    {
        "people": {"p-1": {"name": OWNER}, "p-2": {"name": SECOND}},
        "assignments": {"1": {"consensus": "p-1"}},
    }
)


def _row(**overrides: object) -> dict[str, object]:
    """Build a Meetings row.

    Args:
        **overrides: Columns to replace.

    Returns:
        The row.
    """
    row: dict[str, object] = {
        "id": MEETING_A,
        "title": "Quarterly whisper budget",
        "createdAt": "2026-08-21 21:00:58.565 +00:00",
        "modifiedAt": "2026-08-21 21:33:32.711 +00:00",
        "summary": "Reviewed with <@speaker:1>.",
        "speakerMap": SPEAKER_MAP,
        "isDeleted": 0,
    }
    row.update(overrides)
    return row


@pytest.fixture
def synced(tmp_path: Path, wispr_db: Callable[..., Path]) -> Callable[..., Archive]:
    """Build a source and run one local sync against it.

    Returns:
        A factory taking meeting rows plus sync options, returning the archive.
    """

    def build(
        rows: list[dict[str, object]] | None = None,
        *,
        history: list[dict[str, object]] | None = None,
        session: bool = False,
        artifacts: bool = True,
        **options: object,
    ) -> Archive:
        data_dir = tmp_path / "Wispr Flow"
        data_dir.mkdir(exist_ok=True)
        payload: dict[str, object] = {"Meetings": rows if rows is not None else [_row()]}
        if history is not None:
            payload["History"] = history
        wispr_db(payload).replace(data_dir / "flow.sqlite")

        if artifacts:
            for row in payload["Meetings"]:  # type: ignore[index]
                if not isinstance(row.get("id"), str):
                    continue
                directory = data_dir / "meetings" / str(row["id"])
                try:
                    directory.mkdir(parents=True, exist_ok=True)
                except (OSError, ValueError):
                    continue
                (directory / "refined.ndjson").write_text(
                    json.dumps(
                        {
                            "id": "u-1",
                            "timestamp": "00:24",
                            "text": "Right, the budget.",
                            "speaker": {"id": 1, "source": "refined"},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (directory / "upload.ogg").write_bytes(b"OggS" + bytes(64))

        if session:
            (data_dir / "session.json").write_text(
                json.dumps(
                    {
                        FAKE_SESSION_KEY: json.dumps(
                            {
                                "access_token": FAKE_JWT,
                                "refresh_token": "refresh-token",
                                "expires_at": 4102444800,
                                "user": {"id": "u", "email": "murmur@example.invalid"},
                            }
                        )
                    }
                ),
                encoding="utf-8",
            )

        archive = Archive(root=tmp_path / "archive")
        resolved = paths.resolve(data_dir=data_dir)
        with open_source(resolved.db) as source:
            sync_local(
                archive,
                source,
                resolved,
                SyncOptions(**options),  # type: ignore[arg-type]
                policy=Policy("store_normally", "never_delete", datetime.now(tz=UTC)),
            )
        return archive

    return build


# -- path traversal from untrusted ids and titles (finding 1) ---------------


@pytest.mark.parametrize(
    "parts",
    [
        ("..", "escaped"),
        ("meetings", "..", "..", "x"),
        tuple([".."] * 7 + ["x"]),
        ("/etc/passwd",),
        ("meetings", "/etc/passwd"),
        ("meetings", "..", "..", "..", "..", "..", "..", "..", "etc"),
    ],
)
def test_no_path_escapes_the_archive_root(tmp_path: Path, parts: tuple) -> None:
    """Traversal and absolute components are both refused.

    The absolute case matters as much as the dotted one: joining an absolute
    component discards everything to its left, so a path can leave the archive
    without a single ".." appearing in it.
    """
    archive = Archive(root=tmp_path / "archive")
    with pytest.raises(UnsafeArchivePathError):
        archive.resolve(*parts)


def test_a_traversal_title_cannot_leave_the_archive(
    synced: Callable[..., Archive],
) -> None:
    """The id makes the directory safe; the slug is only ever readability."""
    archive = synced([_row(title=TITLE_TRAVERSAL)])

    entry = archive.entry("meetings", MEETING_A)
    resolved = (archive.root / entry["path"]).resolve()
    assert archive.root in resolved.parents
    assert ".." not in entry["path"]


def test_a_malformed_meeting_id_never_becomes_a_path(
    synced: Callable[..., Archive],
) -> None:
    """Ids are validated before they are joined to anything."""
    archive = synced([_row(id="../../../../etc/passwd")])

    assert archive.count("meetings") == 0
    assert not (archive.root / "etc").exists()


# -- local index tampering (finding 2) --------------------------------------


def test_a_tampered_index_cannot_redirect_a_write(tmp_path: Path) -> None:
    """index.json is untrusted input even though this tool wrote it."""
    archive = Archive(root=tmp_path / "archive")
    archive.put("meetings", MEETING_A, path="../../../etc/passwd")

    assert archive.existing_path("meetings", MEETING_A) is None
    assert not archive.relocate(
        "meetings", MEETING_A, archive.resolve("meetings", "x")
    )


def test_a_tampered_index_cannot_move_a_file_out(tmp_path: Path) -> None:
    """Relocation follows the index, so the index must not be trusted."""
    archive = Archive(root=tmp_path / "archive")
    canary = tmp_path / "canary.txt"
    canary.write_text("not yours", encoding="utf-8")
    archive.put("meetings", MEETING_A, path=f"../{canary.name}")

    archive.relocate("meetings", MEETING_A, archive.resolve("meetings", "moved"))

    assert canary.exists()


# -- file modes, with the Wispr twist (finding 3) ---------------------------


def test_every_archived_path_is_owner_only(synced: Callable[..., Archive]) -> None:
    """Files 0600 and directories 0700, at every level.

    The intermediate directories matter as much as the leaves: their children's
    names carry meeting titles and participant names.
    """
    archive = synced()

    for path in archive.root.rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        expected = DIR_MODE if path.is_dir() else FILE_MODE
        assert mode == expected, f"{path.relative_to(archive.root)} is {oct(mode)}"


def test_a_copied_recording_does_not_inherit_its_source_mode(
    tmp_path: Path, synced: Callable[..., Archive]
) -> None:
    """Wispr Flow's own files are 0644 and 0666, and copy2 would preserve that."""
    archive = synced()
    entry = archive.entry("meetings", MEETING_A)
    audio = archive.root / entry["path"] / "media" / "upload.ogg"

    assert audio.is_file()
    assert stat.S_IMODE(audio.stat().st_mode) == FILE_MODE


def test_no_temporary_file_survives_a_run(synced: Callable[..., Archive]) -> None:
    """A leftover .tmp would be a file whose mode nobody checked."""
    archive = synced()
    assert list(archive.root.rglob("*.tmp")) == []


# -- credentials are never archived (finding 4) -----------------------------


def test_no_credential_reaches_the_archive(synced: Callable[..., Archive]) -> None:
    """The archive is something an operator may back up or hand over."""
    archive = synced(session=True)

    for path in archive.root.rglob("*"):
        if not path.is_file():
            continue
        body = path.read_bytes()
        assert FAKE_JWT.encode() not in body, path
        assert b"refresh-token" not in body, path


def test_a_local_export_never_opens_the_session_file(
    tmp_path: Path, wispr_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing in the local path needs it.

    A backup tool that opens a credential it has no use for is a backup tool
    that will eventually copy one. Asserted by recording every path opened
    during a local sync rather than by checking access times, which are
    unreliable on mounts with relatime or noatime.
    """
    data_dir = tmp_path / "Wispr Flow"
    data_dir.mkdir()
    wispr_db({"Meetings": [_row()]}).replace(data_dir / "flow.sqlite")
    (data_dir / "session.json").write_text("{}", encoding="utf-8")

    opened: list[str] = []
    for name in ("open", "read_text", "read_bytes"):
        original = getattr(Path, name)

        def spy(self: Path, *args: object, _f=original, **kwargs: object):
            opened.append(str(self))
            return _f(self, *args, **kwargs)

        monkeypatch.setattr(Path, name, spy)

    archive = Archive(root=tmp_path / "archive")
    resolved = paths.resolve(data_dir=data_dir)
    with open_source(resolved.db) as source:
        sync_local(
            archive,
            source,
            resolved,
            SyncOptions(),
            entities=("meetings", "notes", "calendar", "dictionary", "todos"),
        )

    assert not any(path.endswith("session.json") for path in opened), (
        "the local export path opened the credential file"
    )


def test_redaction_covers_every_credential_shape() -> None:
    """Diagnostics pass through one redactor at the sink."""
    for text in (
        f"Authorization: Bearer {FAKE_JWT}",
        f"missing key {FAKE_SESSION_KEY}",
        "https://example.invalid/o?X-Amz-Signature=deadbeefcafebabe0123",
    ):
        assert "[redacted]" in redact(text)


def test_a_presigned_url_is_redacted_at_read_time(
    tmp_path: Path, wispr_db: Callable[..., Path]
) -> None:
    """A signed object URL grants access to whoever holds it."""
    path = wispr_db(
        {
            "NoteImages": [
                {
                    "id": HISTORY_A,
                    "presignedGetUrl": "https://example.invalid/o?X-Amz-Signature=abc",
                }
            ]
        }
    )
    with open_source(path) as source:
        record = next(source.records("NoteImages"))

    assert record.data["presignedGetUrl"] == {"__redacted__": "credential"}


# -- the source is never mutated (finding 5) --------------------------------


def test_a_full_sync_leaves_the_source_untouched(
    tmp_path: Path, wispr_db: Callable[..., Path]
) -> None:
    """The tool cannot leave Wispr Flow's database in a state it would not know."""
    data_dir = tmp_path / "Wispr Flow"
    data_dir.mkdir()
    wispr_db({"Meetings": [_row()]}).replace(data_dir / "flow.sqlite")
    meeting_dir = data_dir / "meetings" / MEETING_A
    meeting_dir.mkdir(parents=True)
    (meeting_dir / "refined.ndjson").write_text("{}\n", encoding="utf-8")

    before = {
        path: (path.stat().st_mtime_ns, path.read_bytes())
        for path in sorted(data_dir.rglob("*"))
        if path.is_file()
    }

    archive = Archive(root=tmp_path / "archive")
    resolved = paths.resolve(data_dir=data_dir)
    with open_source(resolved.db) as source:
        sync_local(archive, source, resolved, SyncOptions())

    after = {
        path: (path.stat().st_mtime_ns, path.read_bytes())
        for path in sorted(data_dir.rglob("*"))
        if path.is_file()
    }
    assert after == before


def test_the_connection_refuses_every_write(
    wispr_db: Callable[..., Path],
) -> None:
    """PRAGMA query_only and mode=ro both stand in the way."""
    with open_source(wispr_db()) as source:
        for statement in (
            'DELETE FROM "Meetings"',
            'INSERT INTO "Meetings" ("id") VALUES (\'x\')',
            'UPDATE "Meetings" SET title = \'x\'',
            'DROP TABLE "Meetings"',
        ):
            with pytest.raises(sqlite3.OperationalError):
                source.connection.execute(statement)


# -- symlink escape (finding 6) ---------------------------------------------


def test_a_symlinked_artifact_is_not_followed(
    tmp_path: Path, wispr_db: Callable[..., Path]
) -> None:
    """The meeting directory is written by another application.

    Following a link out of it would let that application choose what this
    tool copies into the archive.
    """
    data_dir = tmp_path / "Wispr Flow"
    data_dir.mkdir()
    wispr_db({"Meetings": [_row()]}).replace(data_dir / "flow.sqlite")
    meeting_dir = data_dir / "meetings" / MEETING_A
    meeting_dir.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("not yours", encoding="utf-8")
    (meeting_dir / "upload.ogg").symlink_to(secret)
    (meeting_dir / "refined.ndjson").symlink_to(secret)

    archive = Archive(root=tmp_path / "archive")
    resolved = paths.resolve(data_dir=data_dir)
    with open_source(resolved.db) as source:
        sync_local(archive, source, resolved, SyncOptions())

    for path in archive.root.rglob("*"):
        if path.is_file():
            assert b"not yours" not in path.read_bytes()


# -- resource exhaustion (finding 7) ----------------------------------------


def test_an_oversized_transcript_line_is_refused(tmp_path: Path) -> None:
    """A per-line cap keeps a hostile file from exhausting memory."""
    from wispr_flow_exporter.files_source import MAX_LINE_BYTES, read_transcript

    path = tmp_path / "refined.ndjson"
    path.write_text('{"text": "' + "x" * (MAX_LINE_BYTES + 10) + '"}\n', encoding="utf-8")

    read = read_transcript(path)

    assert read.turns == []
    assert read.malformed == 1


def test_an_enormous_blob_is_referenced_not_loaded(
    tmp_path: Path, wispr_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blob past the cap records its digest without being archived."""
    monkeypatch.setattr("wispr_flow_exporter.sqlite_source.MAX_BLOB_BYTES", 16)
    path = wispr_db(
        {
            "History": [
                {
                    "transcriptEntityId": HISTORY_A,
                    "timestamp": "2026-08-30 09:00:00.000 +00:00",
                    "audio": b"OggS" + bytes(1024),
                }
            ]
        }
    )
    with open_source(path) as source:
        record = next(source.records("History", include_blobs=True))

    assert record.data["audio"]["__blob__"]["truncated"] is True
    assert record.blobs == {}


# -- untrusted content into Markdown (finding 8) ----------------------------


def test_a_hostile_title_cannot_restructure_a_document(
    synced: Callable[..., Archive],
) -> None:
    """Frontmatter *and* body: the heading interpolates the title too."""
    archive = synced([_row(title=TITLE_FRONTMATTER)])

    document = (
        archive.root / archive.entry("meetings", MEETING_A)["path"] / "meeting.md"
    )
    body = document.read_text(encoding="utf-8")
    delimiters = [line for line in body.splitlines() if line.strip() == "---"]
    assert len(delimiters) == 2
    assert "\ntitle: injected" not in body


def test_a_user_typed_speaker_token_is_not_resolved_to_a_wrong_name(
    synced: Callable[..., Archive],
) -> None:
    """A token nobody assigned must survive verbatim, not become a guess."""
    archive = synced([_row(summary="Then <@speaker:9999> spoke.")])

    document = (
        archive.root / archive.entry("meetings", MEETING_A)["path"] / "summary.md"
    )
    body = document.read_text(encoding="utf-8")
    assert "<@speaker:9999>" in body
    assert OWNER not in body


# -- never_store must not read as success (finding 9) -----------------------


def test_an_archive_empty_by_policy_can_prove_it(
    tmp_path: Path, wispr_db: Callable[..., Path]
) -> None:
    """The highest-severity failure this tool has is silent and permanent.

    An archive with no dictation because a preference forbade storing it must
    be distinguishable from one that simply failed to read.
    """
    data_dir = tmp_path / "Wispr Flow"
    data_dir.mkdir()
    wispr_db().replace(data_dir / "flow.sqlite")
    archive = Archive(root=tmp_path / "archive")
    resolved = paths.resolve(data_dir=data_dir)

    with open_source(resolved.db) as source:
        sync_local(
            archive,
            source,
            resolved,
            SyncOptions(),
            entities=("dictation",),
            policy=Policy("never_store", "never_delete", datetime.now(tz=UTC)),
        )

    recorded = archive.source_state("wispr-local")["policy"]
    assert recorded["local_data_policy"] == "never_store"
    assert recorded["records_dictation"] is False
    assert recorded["observed_at"]


# -- screen context stays out unless asked for (finding 10) -----------------


def test_screen_context_is_excluded_by_name_not_by_filtering() -> None:
    """A new capture column is excluded until it is declared, and reported.

    Filtering a SELECT * would mean the failure mode of an upstream change is
    a silent screenshot dump rather than a missing field.
    """
    spec = EXPECTED["History"]
    available = ("transcriptEntityId", "screenshot", "axText", "axHTML")

    assert spec.projection(available, include_screen_context=False) == (
        "transcriptEntityId",
    )
    assert spec.projection(available, include_screen_context=True) == available


def test_both_capture_tables_are_covered() -> None:
    """FlowLensHistory is the one a History-only rule misses."""
    for table in ("History", "FlowLensHistory"):
        assert {"screenshot", "axText", "axHTML"} <= EXPECTED[table].screen_context


def test_screen_context_never_reaches_the_archive_by_default(
    synced: Callable[..., Archive],
) -> None:
    """End to end, not just at the projection."""
    archive = synced(
        history=[
            {
                "transcriptEntityId": HISTORY_A,
                "timestamp": "2026-08-30 09:00:00.000 +00:00",
                "formattedText": "Send the budget.",
                "axText": "a password manager window",
            }
        ]
    )

    for path in archive.root.rglob("*"):
        if path.is_file():
            assert b"password manager" not in path.read_bytes()


def test_the_cli_needs_two_flags_to_widen_that_far() -> None:
    """Opting in should not be reachable by autocompleting one flag."""
    from wispr_flow_exporter.cli import main

    with pytest.raises(SystemExit) as caught:
        main(["sync", "--include-screen-context"])
    assert caught.value.code == 2


# -- the umask must not decide anything (finding 3, second sink) ------------


def test_a_permissive_umask_does_not_widen_the_archive(
    synced: Callable[..., Archive],
) -> None:
    """Modes are set explicitly rather than inherited."""
    old = os.umask(0)
    try:
        archive = synced()
        for path in archive.root.rglob("*"):
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == (DIR_MODE if path.is_dir() else FILE_MODE)
    finally:
        os.umask(old)
