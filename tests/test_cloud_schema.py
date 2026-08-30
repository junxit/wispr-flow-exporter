"""Drift detection for the cloud backend.

The local backend's schema tests exist because Wispr Flow ships roughly twenty
migrations a month. These exist for a harsher reason: the API is not versioned
at all, has no changelog, and its shapes were confirmed by asking it once. The
fingerprint is what turns "it changed" from a discovery into a report.

Nothing here touches the network. The two tests that read the installed app are
at the bottom and skip when it is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wispr_flow_exporter import paths
from wispr_flow_exporter.cloud_api import ENDPOINTS, Endpoint, EndpointResult
from wispr_flow_exporter.cloud_schema import (
    CLIENT_PIN,
    ClientPin,
    detect_cloud_drift,
    field_names,
    fingerprint,
    observe,
    pin_from_endpoints,
    skeleton,
)
from wispr_flow_exporter.local_config import read_config
from wispr_flow_exporter.schema import DriftClass

from conftest import MEETING_A

_TABLE = {
    "good": Endpoint("/api/v1/user/profile"),
    "gone": Endpoint("/api/v1/notes/sync", expected_status=405),
}

# A pin for _TABLE, so these tests do not move every time the real declaration
# does. The local drift tests use the `clean_pin` fixture for the same reason.
_PIN = ClientPin(app_version="1.0.0", count=2, sha256="a" * 64)


def _result(name: str, status: int | None, payload: object = None) -> EndpointResult:
    """Build a result the way the client would.

    Args:
        name: Endpoint name.
        status: HTTP status.
        payload: Decoded body, when there was one.

    Returns:
        The result.
    """
    reason = None if payload is not None else f"HTTP {status}"
    return EndpointResult(
        name=name, path="/api/v1/x", status=status, payload=payload, reason=reason
    )


# --- the fingerprint ------------------------------------------------------


def test_a_longer_list_is_not_a_different_shape() -> None:
    """The count of records must not look like a schema change.

    This is the property the whole design rests on. A fingerprint that moved
    when a meeting was added would fire on every ordinary run, and the archive
    would rewrite itself every pass -- which is the zero-bytes invariant gone.
    """
    one = {"notes": [{"id": "n-1", "title": "a"}]}
    many = {"notes": [{"id": "n-1", "title": "a"}, {"id": "n-2", "title": "b"}]}

    assert fingerprint(one) == fingerprint(many)


def test_an_empty_list_still_describes_its_container() -> None:
    """An account with no records must not read as a broken endpoint."""
    assert fingerprint({"notes": []}) != fingerprint({"notes": [{"id": "n-1"}]})
    assert fingerprint({"notes": []}) == fingerprint({"notes": []})


def test_different_values_are_the_same_shape() -> None:
    """Content must not reach the state file, even as a digest input."""
    assert fingerprint({"word": "hush"}) == fingerprint({"word": "murmur"})


def test_a_renamed_field_moves_the_fingerprint() -> None:
    """The thing it is for: a field renamed upstream is reported, not absorbed."""
    assert fingerprint({"next_cursor": None}) != fingerprint({"nextCursor": None})


def test_a_retyped_field_moves_the_fingerprint() -> None:
    """A count that becomes a string breaks a renderer silently otherwise."""
    assert fingerprint({"total": 3}) != fingerprint({"total": "3"})


def test_a_boolean_is_not_an_integer() -> None:
    """bool subclasses int in Python, so the naive check erases the difference."""
    assert skeleton(True) == "bool"
    assert fingerprint({"ok": True}) != fingerprint({"ok": 1})


def test_an_id_keyed_map_does_not_name_an_id() -> None:
    """A map keyed by UUID describes its values without recording one.

    The skeleton is hashed, so nothing leaks either way -- but the ledger also
    stores field names in the clear, and this is what keeps an id out of them.
    """
    described = skeleton({MEETING_A: {"title": "x"}})

    assert MEETING_A not in described
    assert "<dynamic>" in described
    assert field_names({MEETING_A: {"title": "x"}}) == ()


def test_field_names_read_through_a_record_list() -> None:
    """A drift report should name the field, not just say a digest moved."""
    assert field_names({"a": 1, "b": 2}) == ("a", "b")
    assert field_names([{"id": "n-1"}, {"id": "n-2", "extra": 1}]) == ("extra", "id")
    assert field_names("not a container") == ()


def test_a_deep_response_stops_rather_than_recursing_forever() -> None:
    """A pathological body must not make fingerprinting expensive."""
    deep: object = "leaf"
    for _ in range(40):
        deep = {"next": deep}

    assert fingerprint(deep)


# --- the pin --------------------------------------------------------------


def test_the_pin_ignores_declaration_order() -> None:
    """A reordered table is the same table."""
    forward = pin_from_endpoints(_TABLE, "1.0.0")
    backward = pin_from_endpoints(dict(reversed(list(_TABLE.items()))), "1.0.0")

    assert forward == backward


def test_the_pin_moves_when_a_path_is_edited() -> None:
    """An edited path with an unchanged count must still move the pin."""
    edited = {**_TABLE, "good": Endpoint("/api/v1/user/profile/v2")}

    assert pin_from_endpoints(edited, "1.0.0") != pin_from_endpoints(_TABLE, "1.0.0")


def test_the_pin_moves_when_an_expected_status_is_corrected() -> None:
    """Learning that an endpoint answers 405 is a change worth pinning."""
    corrected = {**_TABLE, "gone": Endpoint("/api/v1/notes/sync", expected_status=404)}

    assert pin_from_endpoints(corrected, "1.0.0") != pin_from_endpoints(_TABLE, "1.0.0")


def test_the_declared_pin_describes_the_declared_table() -> None:
    """CLIENT_PIN must not drift from the table it claims to fingerprint.

    Unlike the live checks below this needs nothing installed, so it runs in
    CI: an endpoint added without refreshing the pin fails here.
    """
    assert pin_from_endpoints(ENDPOINTS, CLIENT_PIN.app_version) == CLIENT_PIN


# --- the ledger -----------------------------------------------------------


def test_an_unchanged_shape_keeps_its_first_observation(tmp_path: Path) -> None:
    """Otherwise the state file churns every run and zero bytes is gone.

    ``Policy.as_dict`` carries ``observed_at`` forward for exactly this reason;
    this is the same problem one file over.
    """
    results = {"good": _result("good", 200, {"a": 1})}
    first = observe(results)

    second = observe(results, first)

    assert second["good"]["observed_at"] == first["good"]["observed_at"]


def test_a_changed_shape_takes_a_new_observation() -> None:
    """A moved shape is news, and news is dated."""
    first = observe({"good": _result("good", 200, {"a": 1})})
    first["good"]["observed_at"] = "2020-01-01T00:00:00Z"

    second = observe({"good": _result("good", 200, {"a": "1"})}, first)

    assert second["good"]["observed_at"] != "2020-01-01T00:00:00Z"


def test_a_failed_endpoint_records_a_status_and_no_shape() -> None:
    """A 405 is a fact about the endpoint and belongs in the ledger."""
    ledger = observe({"gone": _result("gone", 405)})

    assert ledger["gone"]["status"] == 405
    assert ledger["gone"]["shape"] is None


# --- classification -------------------------------------------------------


def test_a_first_run_establishes_a_baseline_rather_than_reporting_one() -> None:
    """A fresh archive is not nine endpoints' worth of drift."""
    results = {
        "good": _result("good", 200, {"a": 1}),
        "gone": _result("gone", 405),
    }

    drift = detect_cloud_drift(results, None, _TABLE, "1.0.0", _PIN)

    assert drift.kind is DriftClass.OK
    assert drift.unreachable == ("gone",)


def test_a_documented_failure_is_not_drift() -> None:
    """Three endpoints answer 405 or 404 forever. That is the declaration."""
    results = {"gone": _result("gone", 405)}
    ledger = observe(results)

    drift = detect_cloud_drift(results, ledger, _TABLE, "1.0.0", _PIN)

    assert drift.kind is DriftClass.OK
    assert drift.broke == ()
    assert "documented unreachable" in drift.summary()


def test_a_new_field_is_additive() -> None:
    """Upstream adding a field must not stop an archival run."""
    ledger = observe({"good": _result("good", 200, {"a": 1})})
    results = {"good": _result("good", 200, {"a": 1, "b": 2})}

    drift = detect_cloud_drift(results, ledger, _TABLE, "1.0.0", _PIN)

    assert drift.kind is DriftClass.ADDITIVE
    assert drift.new_fields == {"good": ("b",)}
    assert not drift.blocks_rendering


def test_a_removed_field_is_breaking() -> None:
    """A field that vanishes is what quietly empties a rendered document."""
    ledger = observe({"good": _result("good", 200, {"a": 1, "b": 2})})
    results = {"good": _result("good", 200, {"a": 1})}

    drift = detect_cloud_drift(results, ledger, _TABLE, "1.0.0", _PIN)

    assert drift.kind is DriftClass.BREAKING
    assert drift.missing_fields == {"good": ("b",)}
    assert drift.blocks_rendering


def test_an_endpoint_that_stops_answering_is_breaking() -> None:
    """The failure this backend is most likely to meet."""
    ledger = observe({"good": _result("good", 200, {"a": 1})})
    results = {"good": _result("good", 404)}

    drift = detect_cloud_drift(results, ledger, _TABLE, "1.0.0", _PIN)

    assert drift.kind is DriftClass.BREAKING
    assert drift.broke == ("good",)


def test_an_endpoint_that_starts_answering_is_additive() -> None:
    """Good news is still news.

    If /api/v1/notes/sync ever answers a read, meetings and notes become
    reachable from the cloud and this tool's shape changes. Reporting it loudly
    is the only way anyone would notice.
    """
    ledger = observe({"gone": _result("gone", 405)})
    results = {"gone": _result("gone", 200, {"notes": []})}

    drift = detect_cloud_drift(results, ledger, _TABLE, "1.0.0", _PIN)

    assert drift.kind is DriftClass.ADDITIVE
    assert drift.recovered == ("gone",)


def test_an_older_app_is_stale_not_broken() -> None:
    """A downgraded app is a different source, not a failure."""
    results = {"good": _result("good", 200, {"a": 1})}
    ledger = observe(results)

    drift = detect_cloud_drift(results, ledger, _TABLE, "0.9.0", _PIN)

    assert drift.kind is DriftClass.STALE_SOURCE


def test_a_newer_app_with_no_visible_change_is_additive() -> None:
    """The pin moved; nothing observable did. Report it, do not fail on it."""
    results = {"good": _result("good", 200, {"a": 1})}
    ledger = observe(results)

    drift = detect_cloud_drift(results, ledger, _TABLE, "1.1.0", _PIN)

    assert drift.kind is DriftClass.ADDITIVE
    assert "1.1.0" in drift.summary()


def test_an_unreadable_version_is_not_reported_as_a_downgrade() -> None:
    """A version format nobody anticipated must not look like a rollback."""
    results = {"good": _result("good", 200, {"a": 1})}
    ledger = observe(results)

    drift = detect_cloud_drift(results, ledger, _TABLE, "nightly", _PIN)

    assert drift.kind is not DriftClass.STALE_SOURCE


def test_the_summary_is_never_empty() -> None:
    """"No output" and "nothing checked" look identical in a log."""
    drift = detect_cloud_drift({}, {}, _TABLE, "1.0.0", _PIN)

    assert drift.summary()


# --- against a live installation ------------------------------------------


def _live_app_version() -> str | None:
    """Read the installed app's version, or report that there isn't one.

    Returns:
        The version string, or ``None`` when Wispr Flow is not installed.
    """
    config = paths.resolve().config
    if not config.exists():
        return None
    return read_config(config).app_version


def test_the_pin_matches_the_installed_app() -> None:
    """The declaration must describe the app it was measured against.

    Reads a file; makes no request. A mismatch is expected as Wispr Flow
    updates and means the endpoint table has not been re-checked since.
    """
    version = _live_app_version()
    if version is None:
        pytest.skip("no local Wispr Flow installation to check")

    assert version == CLIENT_PIN.app_version, (
        f"app is {version}, pin says {CLIENT_PIN.app_version}. Re-probe with "
        "`wispr-export schema --source cloud --candidates` and refresh "
        "CLIENT_PIN; MAINTENANCE.md has the procedure."
    )


def test_the_recorded_app_version_matches_the_bundle() -> None:
    """``prefs.version`` and Info.plist must agree, or one of them is stale."""
    version = _live_app_version()
    plist = Path("/Applications/Wispr Flow.app/Contents/Info.plist")
    if version is None or not plist.exists():
        pytest.skip("no local Wispr Flow installation to check")
    text = plist.read_text(encoding="utf-8", errors="replace")

    assert f"<string>{version}</string>" in text
