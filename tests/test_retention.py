"""The removed-rows ledger: append-only, written once per row, never lost."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from wispr_flow_exporter.retention import (
    keep_superseded,
    key_digest,
    ledger_path,
    lost_content,
    project_record,
    read_ledger,
    record_removals,
    replace_payload,
    still_removed,
)
from wispr_flow_exporter.schema import EXPECTED
from wispr_flow_exporter.secure_io import FILE_MODE
from wispr_flow_exporter.store import Archive

SPEC = EXPECTED["Dictionary"]
WHEN = "2026-09-01T10:00:00+00:00"
LATER = "2026-09-08T10:00:00+00:00"
HUSH = {"id": "d-hush", "phrase": "hush", "replacement": "Hush", "isDeleted": 0}
MURMUR = {"id": "d-murmur", "phrase": "murmur", "replacement": None, "isDeleted": 0}


def test_the_ledger_sits_beside_its_file(tmp_path: Path) -> None:
    """One ledger per snapshot or shard, named for it."""
    assert ledger_path(tmp_path / "dictionary.ndjson").name == "dictionary.removed.ndjson"
    assert ledger_path(tmp_path / "2026-08-30.ndjson").name == "2026-08-30.removed.ndjson"


def test_a_removal_is_recorded_once(tmp_path: Path) -> None:
    """A run interrupted after the ledger and before the main file must not double it."""
    ledger = tmp_path / "dictionary.removed.ndjson"

    assert record_removals(ledger, SPEC, [HUSH], when=WHEN) == 1
    assert record_removals(ledger, SPEC, [HUSH], when=LATER) == 0

    entries = read_ledger(ledger)
    assert entries == [{"missing_since": WHEN, "row": HUSH}]


def test_the_ledger_is_only_ever_appended_to(tmp_path: Path) -> None:
    """Existing bytes are kept exactly, including a line this code cannot parse."""
    ledger = tmp_path / "dictionary.removed.ndjson"
    ledger.write_bytes(b'{"missing_since": "x", "row": {"id": "d-old"}}\n{ torn')

    record_removals(ledger, SPEC, [MURMUR], when=WHEN)

    text = ledger.read_bytes()
    assert text.startswith(b'{"missing_since": "x", "row": {"id": "d-old"}}\n{ torn\n')
    assert json.loads(text.splitlines()[-1])["row"] == MURMUR


def test_an_unparsed_line_is_kept_as_text(tmp_path: Path) -> None:
    """Damage in the old main file is carried into the ledger, not dropped."""
    ledger = tmp_path / "dictionary.removed.ndjson"

    record_removals(ledger, SPEC, [], ["{ half a row"], when=WHEN)
    record_removals(ledger, SPEC, [], ["{ half a row"], when=LATER)

    assert read_ledger(ledger) == [{"missing_since": WHEN, "unparsed": "{ half a row"}]


def test_a_row_that_comes_back_is_no_longer_removed(tmp_path: Path) -> None:
    """Its ledger line stays as history; it just stops being reported as gone."""
    ledger = tmp_path / "dictionary.removed.ndjson"
    record_removals(ledger, SPEC, [HUSH, MURMUR], when=WHEN)

    removed = still_removed(read_ledger(ledger), SPEC, present={"d-hush"})

    assert [row["id"] for row, _ in removed] == ["d-murmur"]
    assert len(read_ledger(ledger)) == 2


def test_a_changed_removed_row_keeps_its_first_missing_date(tmp_path: Path) -> None:
    """A second version of the same gone row is kept; it went missing once."""
    ledger = tmp_path / "dictionary.removed.ndjson"
    record_removals(ledger, SPEC, [HUSH], when=WHEN)
    record_removals(ledger, SPEC, [{**HUSH, "replacement": "HUSH"}], when=LATER)

    removed = still_removed(read_ledger(ledger), SPEC, present=set())

    assert len(read_ledger(ledger)) == 2
    assert removed == [({**HUSH, "replacement": "HUSH"}, WHEN)]


def test_a_ledger_is_owner_only(tmp_path: Path) -> None:
    """It holds exactly the rows the archive exists to keep."""
    ledger = tmp_path / "dictionary.removed.ndjson"
    record_removals(ledger, SPEC, [HUSH], when=WHEN)

    assert stat.S_IMODE(ledger.stat().st_mode) == FILE_MODE


def test_a_day_fingerprint_ignores_order() -> None:
    """Membership is a set; the order the database returns it in is not."""
    assert key_digest(["b", "a"]) == key_digest(["a", "b"])
    assert key_digest(["a"]) != key_digest(["a", "b"])
    assert key_digest([]) == key_digest(())


# --- content loss -----------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "lost"),
    [
        ({"summary": "halve the quota"}, {"summary": None}, True),
        ({"summary": "halve the quota"}, {}, True),
        ({"summary": "halve the quota"}, {"summary": "keep the quota"}, False),
        ({"summary": None}, {}, False),
        ({"names": ["Hush", "Murmur"]}, {"names": ["Hush"]}, True),
        ({"names": ["Hush"]}, {"names": ["Hush", "Murmur"]}, False),
        ({"map": {"a": 1}}, {"map": "flattened"}, True),
        ({"a": {"b": "x"}}, {"a": {"b": ""}}, True),
        ({"count": 5}, {"count": 0}, False),
        ({"on": True}, {"on": False}, False),
        ({}, {"new": "field"}, False),
    ],
)
def test_what_counts_as_losing_content(old: object, new: object, lost: bool) -> None:
    """Loss is structural; replacing one value with another is an edit."""
    assert lost_content(old, new) is lost


def test_churn_does_not_count_as_loss() -> None:
    """A push flag going back to null is bookkeeping, not content."""
    spec = EXPECTED["Meetings"]
    old = {"id": "m", "summary": "s", "synced": 1}
    new = {"id": "m", "summary": "s", "synced": None}

    assert not lost_content(project_record(spec, old), project_record(spec, new))


def test_a_superseded_payload_is_kept_once(tmp_path: Path) -> None:
    """Content-addressed, so every later run that sees the loss adds nothing."""
    archive = Archive(root=tmp_path / "archive")

    assert keep_superseded(archive, "meetings", "m-1", {"summary": "s"}, when=WHEN)
    assert not keep_superseded(archive, "meetings", "m-1", {"summary": "s"}, when=LATER)

    kept = list((archive.root / "superseded" / "meetings" / "m-1").iterdir())
    assert len(kept) == 1
    assert json.loads(kept[0].read_text(encoding="utf-8")) == {
        "superseded_at": WHEN,
        "payload": {"summary": "s"},
    }


def test_a_replacement_keeps_only_what_it_would_lose(tmp_path: Path) -> None:
    """An edit replaces; a loss replaces and keeps the fuller version."""
    archive = Archive(root=tmp_path / "archive")
    path = archive.root / "cloud" / "insights.json"

    replace_payload(archive, path, {"days": [1, 2]}, entity="cloud", key="insights", when=WHEN)
    replace_payload(archive, path, {"days": [1, 3]}, entity="cloud", key="insights", when=WHEN)
    assert not (archive.root / "superseded").exists()

    replace_payload(archive, path, {"days": [3]}, entity="cloud", key="insights", when=LATER)

    assert json.loads(path.read_text(encoding="utf-8")) == {"days": [3]}
    kept = next((archive.root / "superseded" / "cloud" / "insights").iterdir())
    assert json.loads(kept.read_text(encoding="utf-8"))["payload"] == {"days": [1, 3]}
