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
from conftest import MEETING_A

from wispr_flow_exporter import paths
from wispr_flow_exporter.cloud_api import ENDPOINTS, Endpoint, EndpointResult
from wispr_flow_exporter.cloud_schema import (
    CLIENT_PIN,
    ClientPin,
    CloudDrift,
    detect_cloud_drift,
    field_names,
    fingerprint,
    observe,
    pin_from_endpoints,
    skeleton,
)
from wispr_flow_exporter.local_config import read_config
from wispr_flow_exporter.schema import DriftClass

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
    """Bool subclasses int in Python, so the naive check erases the difference."""
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


def test_a_failed_endpoint_records_a_status_and_no_fields() -> None:
    """A 405 is a fact about the endpoint and belongs in the ledger."""
    ledger = observe({"gone": _result("gone", 405)})

    assert ledger["gone"]["status"] == 405
    assert ledger["gone"]["fields"] == {}


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


# --- what counts as drift -------------------------------------------------


def _drift_after(
    before: object, after: object, *, between: tuple[int | None, ...] = ()
) -> CloudDrift:
    """Observe one answer, then optional failures, then classify another.

    Args:
        before: The first payload.
        after: The payload classified.
        between: Statuses of failed answers observed in between.

    Returns:
        The drift.
    """
    ledger = observe({"good": _result("good", 200, before)})
    for status in between:
        ledger = observe({"good": _result("good", status)}, ledger)
    return detect_cloud_drift(
        {"good": _result("good", 200, after)}, ledger, _TABLE, "1.0.0", _PIN
    )


def test_a_failure_between_two_observations_does_not_erase_the_baseline() -> None:
    """Measured on 0.4.1: a, b, c; a timeout; a, b -- reported as additive.

    The failed answer recorded no fields, so the next answer was compared
    against nothing and the removal of c was never reported.
    """
    drift = _drift_after({"a": 1, "b": 2, "c": 3}, {"a": 1, "b": 2}, between=(None,))

    assert drift.kind is DriftClass.BREAKING
    assert drift.missing_fields == {"good": ("c",)}


@pytest.mark.parametrize("status", [None, 401, 403, 408, 429, 500, 503])
def test_an_endpoint_that_could_not_be_asked_is_not_drift(status: int | None) -> None:
    """Measured on 0.4.1: each of these was breaking drift, exit 4.

    A lapsed token, a timeout, a rate limit or a server having a bad minute
    says nothing about the interface. It is a failure of the run, reported as
    one, and not a change a maintainer has to chase.
    """
    ledger = observe({"good": _result("good", 200, {"a": 1})})

    drift = detect_cloud_drift(
        {"good": _result("good", status)}, ledger, _TABLE, "1.0.0", _PIN
    )

    assert drift.kind is DriftClass.OK
    assert drift.unasked == ("good",)
    assert "not answered this run: good" in drift.summary()


def test_a_no_content_answer_is_reported_but_not_breaking() -> None:
    """A 204 has nothing to compare, which is not the same as broken."""
    ledger = observe({"good": _result("good", 200, {"a": 1})})

    drift = detect_cloud_drift(
        {"good": _result("good", 204)}, ledger, _TABLE, "1.0.0", _PIN
    )

    assert drift.kind is DriftClass.OK
    assert drift.empty == ("good",)


def test_a_record_list_that_empties_is_not_a_removal() -> None:
    """Measured on 0.4.1: an emptied list reported every field gone, breaking."""
    drift = _drift_after([{"id": "x", "title": "t"}], [])

    assert drift.kind is not DriftClass.BREAKING
    assert drift.missing_fields == {}


def test_a_null_that_becomes_a_value_is_not_drift() -> None:
    """Measured on 0.4.1: it was a moved shape, additive, every time it flipped."""
    assert _drift_after({"a": None}, {"a": "x"}).kind is DriftClass.OK
    assert _drift_after({"a": "x"}, {"a": None}).kind is DriftClass.OK


def test_a_field_removed_inside_a_record_list_is_breaking() -> None:
    """Measured on 0.4.1: invisible -- only top-level names were compared."""
    drift = _drift_after({"items": [{"id": "x", "title": "t"}]}, {"items": [{"id": "x"}]})

    assert drift.kind is DriftClass.BREAKING
    assert drift.missing_fields == {"good": ("items[].title",)}


def test_an_optional_field_absent_from_some_records_is_not_a_removal() -> None:
    """A field some records never had is not missing from the ones without it."""
    drift = _drift_after(
        {"items": [{"id": "x", "title": "t"}, {"id": "y"}]}, {"items": [{"id": "z"}]}
    )

    assert drift.kind is not DriftClass.BREAKING


def test_a_retyped_field_is_reported() -> None:
    """A field that changes kind is named, not folded into "shape moved"."""
    drift = _drift_after({"count": 3}, {"count": "three"})

    assert drift.retyped == {"good": ("count",)}
    assert "fields retyped on good: count" in drift.summary()


def test_a_ledger_written_before_the_upgrade_is_compared_without_false_alarms() -> None:
    """0.4.x recorded top-level names only; nested fields must not look new."""
    legacy = {
        "good": {
            "status": 200,
            "shape": "0123456789ab",
            "keys": ["items", "total"],
            "observed_at": "2026-09-01T00:00:00+00:00",
        }
    }
    same = {"good": _result("good", 200, {"items": [{"id": "x"}], "total": 1})}
    less = {"good": _result("good", 200, {"items": [{"id": "x"}]})}

    assert detect_cloud_drift(same, legacy, _TABLE, "1.0.0", _PIN).kind is DriftClass.OK
    gone = detect_cloud_drift(less, legacy, _TABLE, "1.0.0", _PIN)
    assert gone.kind is DriftClass.BREAKING
    assert gone.missing_fields == {"good": ("total",)}
    assert "fields" in observe(same, legacy)["good"]


def test_an_identifier_shaped_key_never_reaches_the_ledger() -> None:
    """A map keyed by id records its shape, never an id."""
    long_id = "a" * 181
    payload = {"by_id": {MEETING_A: {"title": "t"}, long_id: {"title": "u"}}}

    fields = observe({"good": _result("good", 200, payload)})["good"]["fields"]

    assert all(MEETING_A not in path and long_id not in path for path in fields)
    assert "$.by_id.<dynamic>.title" in fields


def test_a_body_that_is_not_json_is_breaking() -> None:
    """A 200 that cannot be read is the interface changing, not the network."""
    ledger = observe({"good": _result("good", 200, {"a": 1})})
    broken = EndpointResult(
        name="good", path="/api/v1/x", status=200, reason="response was not JSON"
    )

    drift = detect_cloud_drift({"good": broken}, ledger, _TABLE, "1.0.0", _PIN)

    assert drift.kind is DriftClass.BREAKING
    assert drift.broke == ("good",)


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


@pytest.mark.live
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


@pytest.mark.live
def test_the_recorded_app_version_matches_the_bundle() -> None:
    """``prefs.version`` and Info.plist must agree, or one of them is stale."""
    version = _live_app_version()
    plist = Path("/Applications/Wispr Flow.app/Contents/Info.plist")
    if version is None or not plist.exists():
        pytest.skip("no local Wispr Flow installation to check")
    text = plist.read_text(encoding="utf-8", errors="replace")

    assert f"<string>{version}</string>" in text
