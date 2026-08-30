"""What this tool expects Wispr Flow's database to look like.

Declarations only, no behavior. ``normalize.py`` holds the parsers and
``sqlite_source.py`` holds the reader; this module is the spec they consult.
Keeping them apart matters because they are read for different reasons: one is
a table you scan to answer "what does column X mean", the other is logic you
read to answer "what happens to it".

Nothing here decides what gets *archived*. The raw path is driven by
``PRAGMA table_info`` at runtime, so a column added upstream is captured on the
next run with no code change. ``EXPECTED`` exists to detect and report the
difference, and to say which columns feed a renderer, which are pure churn,
and which are sensitive enough to require an explicit flag.

That distinction is load-bearing. Wispr Flow shipped 11 migrations in August,
21 in July and 25 in June -- roughly twenty a month. Drift is continuous, not
exceptional, so a design that had to be updated before it could read a new
column would be permanently out of date, and an archive that stopped when it
fell behind would be useless on the one day it was needed.

Column lists were read from a live installation of Wispr Flow v1.6.721 at
migration 149.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from .normalize import TimestampKind


class Layout(StrEnum):
    """How a table's records are laid out on disk.

    Attributes:
        ENTITY: A directory per record, for records with many artifacts.
        DOCUMENT: A file per record.
        SHARD: Date-sharded NDJSON, for append-mostly tables that can grow
            without bound.
        SNAPSHOT: One NDJSON for the whole table, for small mutable tables.
    """

    ENTITY = "entity"
    DOCUMENT = "document"
    SHARD = "shard"
    SNAPSHOT = "snapshot"


class DriftClass(StrEnum):
    """How far a live source has moved from what this tool declares.

    Shared vocabulary for both backends. The local backend classifies a SQLite
    schema against ``MIGRATION_PIN``; the cloud backend classifies response
    shapes against ``CLIENT_PIN``. Reporting the same four words for both is
    what lets one habit cover both.

    Attributes:
        OK: The pin matches exactly.
        ADDITIVE: New migrations, tables, columns or response fields, with
            everything previously known still present. The export completes.
        BREAKING: Something declared is gone -- a required column, an expected
            table, a primary key, or an endpoint that stopped answering. Raw
            archiving still completes; only interpretation is skipped.
        STALE_SOURCE: The source is older than the declaration -- fewer
            migrations than pinned, or an app build behind the pinned one.
    """

    OK = "ok"
    ADDITIVE = "additive"
    BREAKING = "breaking"
    STALE_SOURCE = "stale_source"


@dataclass(frozen=True, slots=True)
class SchemaPin:
    """A fingerprint of the migrations applied to a database.

    Attributes:
        count: Number of rows in ``SequelizeMeta``.
        latest: Lexicographically greatest migration name. Names are
            date-prefixed and therefore monotonic, so this is a real version.
        sha256: Digest over every migration name, sorted and newline-joined.
            This catches the case the other two miss -- a migration replaced
            without changing either the count or the maximum.
    """

    count: int
    latest: str
    sha256: str


def pin_from_migrations(names: Iterable[str]) -> SchemaPin:
    """Compute a :class:`SchemaPin` from a database's migration names.

    Args:
        names: Every value of ``SequelizeMeta.name``.

    Returns:
        The fingerprint of that migration set.
    """
    ordered = sorted(names)
    digest = hashlib.sha256("\n".join(ordered).encode("utf-8")).hexdigest()
    return SchemaPin(
        count=len(ordered), latest=ordered[-1] if ordered else "", sha256=digest
    )


# Read from a live installation. A mismatch is not an error -- see the drift
# classification in sqlite_source -- but it is always reported.
MIGRATION_PIN = SchemaPin(
    count=149,
    latest="20260821120001-add-meetings-calendar-occurrence-start-index.js",
    sha256="bbc0c3726dacd8031a7f2ca875078b43d3378908883301b99a08e8d24377ee6a",
)


@dataclass(frozen=True, slots=True)
class TableSpec:
    """Everything declared about one source table.

    Attributes:
        pk: Primary key column.
        layout: How records are written to disk.
        columns: Columns present at ``MIGRATION_PIN``. Used only to report
            drift; the reader discovers columns at runtime.
        required: Columns a renderer depends on. Losing one is breaking drift.
        volatile: Columns that change without the record changing. Excluded
            from the content digest so an unchanged record is not rewritten,
            but still archived verbatim.
        timestamps: Column to encoding, for the ``normalize`` dispatcher.
        soft_delete: Columns that tombstone a row in place when true.
        date_column: Column that decides which date shard a record lands in.
        json_columns: TEXT columns holding JSON.
        blobs: Columns holding binary data, written as sidecar files rather
            than inlined as base64.
        screen_context: Columns holding captures of the user's screen. These
            require an explicit flag and are excluded by name from the
            projection rather than filtered out of a ``SELECT *``.
        credentials: Columns whose value grants access to something and must
            be redacted rather than archived.
    """

    pk: str
    layout: Layout
    columns: tuple[str, ...]
    required: frozenset[str] = frozenset()
    volatile: frozenset[str] = frozenset()
    timestamps: Mapping[str, TimestampKind] = field(default_factory=dict)
    soft_delete: tuple[str, ...] = ()
    date_column: str | None = None
    json_columns: frozenset[str] = frozenset()
    blobs: frozenset[str] = frozenset()
    screen_context: frozenset[str] = frozenset()
    credentials: frozenset[str] = frozenset()

    def projection(
        self, available: Sequence[str], *, include_screen_context: bool
    ) -> tuple[str, ...]:
        """Choose the columns to select from a table.

        Screen-context columns are removed **by name** from the discovered
        column list. The failure mode of an upstream schema change is
        therefore a missing field rather than a silent screenshot dump: a new
        column is captured, but a new *screen-capture* column is only captured
        once it has been added to ``screen_context`` deliberately.

        Args:
            available: Columns the database actually has, from
                ``PRAGMA table_info``.
            include_screen_context: Whether the operator opted in.

        Returns:
            The columns to read, in the order the database reports them.
        """
        if include_screen_context:
            return tuple(available)
        return tuple(name for name in available if name not in self.screen_context)

    def is_soft_deleted(self, row: Mapping[str, object]) -> bool:
        """Report whether a row is tombstoned upstream.

        Driven by the declaration rather than a hardcoded ``isDeleted``:
        ``Todos`` tombstones with either ``isDeleted`` or ``isArchived``,
        ``History`` uses ``isArchived`` alone, and ``NotetakerChats`` uses a
        nullable ``deletedAt`` timestamp. A truthiness test covers all three,
        since a non-null timestamp and a ``1`` are both truthy and an unset
        flag is ``0`` or ``None``.

        Note that a soft-deleted record is still archived and still rendered.
        The flag records what upstream thinks, and never causes a deletion
        here.

        Args:
            row: The record, keyed by column name.

        Returns:
            ``True`` when any declared tombstone column is set.
        """
        return any(bool(row.get(column)) for column in self.soft_delete)


# Columns that flip without the record's content changing. Sequelize writes
# most of these as part of its own sync bookkeeping; hashing them would mean
# rewriting every file in the archive on every run.
_PUSH_FLAGS = frozenset(
    {
        "synced",
        "needsUploading",
        "liveTranscriptPendingPush",
        "speakerMapPendingPush",
        "notesPendingPush",
        "summaryPendingPush",
    }
)

# The modification timestamps are the *signal* that something changed, not the
# change itself. Sequelize bumps modifiedAt whenever it touches a row, so a
# background sync that only flips `synced` also moves modifiedAt -- and if the
# digest counted it, every such sync would rewrite the record even though not
# one word of its content moved. The watermark still reads these columns; the
# content digest deliberately does not.
_TOUCH_TIMES = frozenset({"modifiedAt", "updatedAt", "syncedAt"})

_CHURN = _PUSH_FLAGS | _TOUCH_TIMES


_MEETING_TIMESTAMPS: Mapping[str, TimestampKind] = {
    "createdAt": TimestampKind.SEQUELIZE,
    "modifiedAt": TimestampKind.SEQUELIZE,
    "transcriptDeletedAt": TimestampKind.SEQUELIZE,
    "endedAt": TimestampKind.EPOCH_MS,
    "calendarOccurrenceStartAtUtc": TimestampKind.EPOCH_MS,
    "submitAcceptedAt": TimestampKind.EPOCH_MS,
    "audioUploadedAt": TimestampKind.EPOCH_MS,
    "speakerArtifactUploadedAt": TimestampKind.EPOCH_MS,
    "liveTranscriptUploadedAt": TimestampKind.EPOCH_MS,
    "serverRefinedUploadedAt": TimestampKind.BARE_ISO,
    "refinedFetchedThroughAt": TimestampKind.BARE_ISO,
}

# Sequelize-managed DATETIME columns are all written the same way, so the
# default for a createdAt/modifiedAt/updatedAt pair is SEQUELIZE unless a
# table was observed to differ (CalendarEvents is the one that does).
_SEQUELIZE_TIMES: Mapping[str, TimestampKind] = {
    "createdAt": TimestampKind.SEQUELIZE,
    "modifiedAt": TimestampKind.SEQUELIZE,
    "updatedAt": TimestampKind.SEQUELIZE,
}


def _spec(
    *,
    columns: tuple[str, ...],
    volatile: Iterable[str] = (),
    timestamps: Mapping[str, TimestampKind] | None = None,
    **rest: object,
) -> TableSpec:
    """Build a spec, narrowing convention-based sets to columns that exist.

    ``volatile`` and ``timestamps`` are written from shared vocabularies --
    Sequelize's push flags and its ``createdAt``/``modifiedAt``/``updatedAt``
    convention -- which apply to most tables but not every column of every
    table. Narrowing them here keeps the declarations terse and still accurate,
    so the self-consistency test can stay strict about the sets where a stray
    name really is a bug: ``required``, ``blobs``, ``screen_context`` and
    ``credentials`` each assert something about one specific table.

    Args:
        columns: The table's columns at ``MIGRATION_PIN``.
        volatile: Candidate churn columns, narrowed to those present.
        timestamps: Candidate encodings, narrowed to columns present.
        **rest: Passed to :class:`TableSpec` unchanged.

    Returns:
        The narrowed spec.
    """
    present = set(columns)
    return TableSpec(
        columns=columns,
        volatile=frozenset(volatile) & present,
        timestamps={
            column: kind
            for column, kind in (timestamps or {}).items()
            if column in present
        },
        **rest,  # type: ignore[arg-type]
    )


EXPECTED: Mapping[str, TableSpec] = {
    "Meetings": _spec(
        pk="id",
        layout=Layout.ENTITY,
        columns=(
            "id", "title", "createdAt", "modifiedAt", "synced", "isDeleted",
            "finalized", "liveTranscriptPendingPush", "refineRetries",
            "submitAcceptedAt", "audioUploadedAt", "calendarEventExternalId",
            "avatarPick", "speakerArtifactUploadedAt", "speakerArtifactRetries",
            "endedAt", "transcriptDeletedAt", "importSource",
            "liveTranscriptUploadedAt", "liveTranscriptRetries",
            "participantNames", "uploadDeferred", "encodeRetries", "notes",
            "summary", "speakerMap", "speakerMapPendingPush",
            "notesPendingPush", "summaryPendingPush", "refineStatus",
            "latestRecordingStopSeq", "refinePipelineRanThroughStopSeq",
            "isTourDemo", "serverRefinedUploadedAt", "refinedFetchedThroughAt",
            "shareSlug", "shareVisibility", "refinedFetchRetries",
            "refineUploadFailureReason", "calendarOccurrenceStartAtUtc",
        ),
        required=frozenset({"id", "title", "createdAt", "modifiedAt"}),
        volatile=_CHURN
        | {
            "refineRetries",
            "speakerArtifactRetries",
            "liveTranscriptRetries",
            "refinedFetchRetries",
            "encodeRetries",
            "uploadDeferred",
            "latestRecordingStopSeq",
            "refinePipelineRanThroughStopSeq",
            "audioUploadedAt",
            "submitAcceptedAt",
            "speakerArtifactUploadedAt",
            "liveTranscriptUploadedAt",
            "serverRefinedUploadedAt",
            "refinedFetchedThroughAt",
        },
        timestamps=_MEETING_TIMESTAMPS,
        soft_delete=("isDeleted",),
        date_column="createdAt",
        json_columns=frozenset({"participantNames", "speakerMap", "avatarPick"}),
    ),
    "Notes": _spec(
        pk="id",
        layout=Layout.DOCUMENT,
        columns=(
            "id", "title", "contentPreview", "content", "createdAt",
            "modifiedAt", "synced", "isDeleted", "finalized", "pinned",
            "searchableContent",
        ),
        required=frozenset({"id", "title", "content", "createdAt", "modifiedAt"}),
        # searchableContent is a derived index of content; archiving both
        # doubles the text and makes every content edit look like two.
        volatile=_CHURN | {"searchableContent", "contentPreview"},
        timestamps=_SEQUELIZE_TIMES,
        soft_delete=("isDeleted",),
        date_column="createdAt",
    ),
    "CalendarEvents": _spec(
        pk="externalId",
        layout=Layout.DOCUMENT,
        columns=(
            "externalId", "title", "startAtUtc", "endAtUtc", "conferenceUrl",
            "status", "updatedAt", "syncedAt", "notifiedAt", "prereadTitle",
            "prereadContent", "prereadSummary", "participantNames",
            "participants", "colorId", "selfResponseStatus",
            "prereadDispatchedAt", "connectorKey",
        ),
        required=frozenset({"externalId", "title", "startAtUtc"}),
        volatile=frozenset({"syncedAt", "notifiedAt", "prereadDispatchedAt"}),
        timestamps={
            "startAtUtc": TimestampKind.EPOCH_MS,
            "endAtUtc": TimestampKind.EPOCH_MS,
            "notifiedAt": TimestampKind.EPOCH_MS,
            "prereadDispatchedAt": TimestampKind.EPOCH_MS,
            # The only table observed to use ISO-with-Z rather than Sequelize's
            # space-separated offset.
            "updatedAt": TimestampKind.ISO_Z,
            "syncedAt": TimestampKind.ISO_Z,
        },
        date_column="startAtUtc",
        json_columns=frozenset({"participantNames", "participants"}),
    ),
    "Dictionary": _spec(
        pk="id",
        layout=Layout.SNAPSHOT,
        columns=(
            "id", "phrase", "replacement", "teamDictionaryId", "lastUsed",
            "frequencyUsed", "remoteFrequencyUsed", "manualEntry", "createdAt",
            "modifiedAt", "isDeleted", "source", "isSnippet", "observedSource",
            "isStarred", "replacementHtml",
        ),
        required=frozenset({"id", "phrase"}),
        # Usage counters tick on every dictation; they say nothing about the
        # entry itself.
        volatile=frozenset({"lastUsed", "frequencyUsed", "remoteFrequencyUsed"}),
        timestamps=_SEQUELIZE_TIMES | {"lastUsed": TimestampKind.SEQUELIZE},
        soft_delete=("isDeleted",),
    ),
    "Todos": _spec(
        pk="id",
        layout=Layout.SNAPSHOT,
        columns=(
            "id", "meetingId", "title", "status", "isDeleted", "isArchived",
            "createdAt", "modifiedAt", "synced", "sortOrder",
        ),
        required=frozenset({"id", "title"}),
        volatile=_CHURN | {"sortOrder"},
        timestamps=_SEQUELIZE_TIMES,
        soft_delete=("isDeleted", "isArchived"),
        date_column="createdAt",
    ),
    "History": _spec(
        pk="transcriptEntityId",
        layout=Layout.SHARD,
        columns=(
            "transcriptEntityId", "asrText", "formattedText", "editedText",
            "timestamp", "audio", "screenshot", "additionalContext", "status",
            "app", "url", "e2eLatency", "needsUploading", "duration",
            "numWords", "shareType", "textboxContents", "appVersion",
            "editedTextStatus", "editedTextAttempts", "toneMatchedText",
            "toneMatchPairs", "feedback", "language", "isArchived",
            "micDevice", "conversationId", "builtInAudio",
            "formattingDivergenceScore", "pastedText", "defaultAsrText",
            "fallbackAsrText", "defaultFormattedText", "fallbackFormattedText",
            "fallbackAsrDivergenceScore",
            "fallbackFormattingDivergenceScore", "detectedLanguage",
            "averageLogProb", "hasRevertedAI", "axText", "userEditMetaData",
            "axHTML", "opusChunks", "usedFallbackAsr",
            "usedFallbackFormatting", "desiredAsr", "desiredFormatted",
            "calledExternalAsr", "transcriptOrigin", "platform",
            "transcriptCommand", "personalizationStyleSettings",
            "clientNetworkLatency", "fallbackLevel", "speechDuration",
            "timezoneOffsetMinutes", "numWordsCorrected",
            "numDictionaryReplacements", "serverFinalizedText",
            "contentObservationEndReason",
            "contentObservationEndLastKeystroke", "editDistanceToDictated",
            "editedTextUnbounded",
        ),
        required=frozenset({"transcriptEntityId", "timestamp"}),
        volatile=_CHURN | {"editedTextStatus", "editedTextAttempts"},
        timestamps={"timestamp": TimestampKind.SEQUELIZE},
        soft_delete=("isArchived",),
        date_column="timestamp",
        json_columns=frozenset(
            {
                "additionalContext",
                "toneMatchPairs",
                "opusChunks",
                "userEditMetaData",
                "personalizationStyleSettings",
            }
        ),
        blobs=frozenset({"audio", "builtInAudio", "screenshot"}),
        # A bitmap and an accessibility-tree capture of whatever had focus
        # when the user spoke. That can be a password manager or a banking
        # session, so it takes two explicit flags to include.
        screen_context=frozenset(
            {"screenshot", "axText", "axHTML", "textboxContents", "pastedText"}
        ),
    ),
    "FlowLensHistory": _spec(
        pk="id",
        layout=Layout.SHARD,
        columns=(
            "id", "conversationId", "userId", "userEmail", "role", "content",
            "messageNumber", "app", "url", "tools", "screenshot", "axText",
            "axHTML", "needsUploading", "createdAt", "updatedAt",
        ),
        volatile=_CHURN,
        timestamps=_SEQUELIZE_TIMES,
        date_column="createdAt",
        json_columns=frozenset({"content", "tools"}),
        blobs=frozenset({"screenshot"}),
        # The table a tiering rule written only against History would miss.
        screen_context=frozenset({"screenshot", "axText", "axHTML"}),
    ),
    "NoteImages": _spec(
        pk="id",
        layout=Layout.SNAPSHOT,
        columns=(
            "id", "noteId", "data", "width", "height", "sizeBytes",
            "createdAt", "s3Key", "contentType", "uploadedToS3",
            "presignedGetUrl", "urlExpiresAt",
        ),
        volatile=frozenset({"presignedGetUrl", "urlExpiresAt", "uploadedToS3"}),
        timestamps=_SEQUELIZE_TIMES,
        date_column="createdAt",
        blobs=frozenset({"data"}),
        # A signed object URL grants read access to anyone holding it until it
        # expires. That is a bearer credential in a TEXT column, not metadata.
        credentials=frozenset({"presignedGetUrl"}),
    ),
    "Polish": _spec(
        pk="id",
        layout=Layout.SHARD,
        columns=(
            "id", "polishInitialText", "polishedText",
            "polishInitialWordCount", "polishedWordCount", "app",
            "processingTime", "status", "polishUndone", "instruction",
            "needsUploading", "createdAt", "updatedAt", "modelVersion",
            "diffCount", "feedback", "usedProvider", "promptName",
            "shortcutKey", "instructHistoryId",
        ),
        volatile=_CHURN,
        timestamps=_SEQUELIZE_TIMES,
        date_column="createdAt",
    ),
    "Links": _spec(
        pk="url",
        layout=Layout.SNAPSHOT,
        columns=(
            "url", "domain", "firstCopiedAt", "lastCopiedAt", "copyCount",
            "pinned", "title", "title_source", "anchor_text_raw",
            "fetched_html_title", "enrichment_attempted", "enrichment_status",
        ),
        volatile=frozenset(
            {"lastCopiedAt", "copyCount", "enrichment_attempted", "enrichment_status"}
        ),
        timestamps={
            "firstCopiedAt": TimestampKind.SEQUELIZE,
            "lastCopiedAt": TimestampKind.SEQUELIZE,
        },
    ),
    "InstructHistory": _spec(
        pk="id",
        layout=Layout.SHARD,
        columns=(
            "id", "transcriptEntityId", "polishId", "wasAutoRouted",
            "classificationMode", "shortCircuitRoute", "routesPayload",
            "contextPayload", "configPayload", "classifierRouteData",
            "classifierModelVersion", "classifierUsedProvider",
            "classifierLatencyMs", "classifierStatus", "routeStatus",
            "routeError", "toolCalls", "appVersion", "needsUploading",
            "createdAt", "updatedAt", "instructSessionId", "sessionTurnIndex",
            "feedback", "editArtifact",
        ),
        volatile=_CHURN,
        timestamps=_SEQUELIZE_TIMES,
        date_column="createdAt",
        json_columns=frozenset(
            {"routesPayload", "contextPayload", "configPayload", "toolCalls"}
        ),
    ),
    "SharedNotes": _spec(
        pk="slug",
        layout=Layout.SNAPSHOT,
        columns=(
            "slug", "title", "notes", "summary", "searchableContent",
            "ownerEmail", "ownerFirstName", "ownerLastName", "ownerAvatarUrl",
            "callerRole", "createdAt", "modifiedAt",
        ),
        volatile=frozenset({"searchableContent"}),
        timestamps=_SEQUELIZE_TIMES,
    ),
    "NoteVersions": _spec(
        pk="id",
        layout=Layout.SNAPSHOT,
        columns=(
            "id", "noteId", "content", "source", "transformId",
            "transformPrompt", "createdAt",
        ),
        timestamps=_SEQUELIZE_TIMES,
        date_column="createdAt",
    ),
    "MeetingVersions": _spec(
        pk="id",
        layout=Layout.SNAPSHOT,
        columns=(
            "id", "meetingId", "content", "source", "transformId",
            "transformPrompt", "createdAt",
        ),
        timestamps=_SEQUELIZE_TIMES,
        date_column="createdAt",
    ),
    "TranscriptCorrections": _spec(
        pk="id",
        layout=Layout.SNAPSHOT,
        columns=(
            "id", "meetingId", "artifactRevision", "artifactDigest", "entryId",
            "originalText", "editedText", "createdAt", "updatedAt",
        ),
        timestamps=_SEQUELIZE_TIMES,
    ),
    "NotetakerChats": _spec(
        pk="id",
        layout=Layout.SNAPSHOT,
        columns=(
            "id", "userId", "kind", "meetingId", "title", "uploadState",
            "deletedAt", "createdAt", "modifiedAt", "lastAccessedAt",
            "shareNote",
        ),
        volatile=frozenset({"uploadState", "lastAccessedAt"}),
        timestamps=_SEQUELIZE_TIMES,
        # The only table observed to tombstone with a nullable timestamp
        # rather than a boolean.
        soft_delete=("deletedAt",),
    ),
    "NotetakerChatMessages": _spec(
        pk="id",
        layout=Layout.SNAPSHOT,
        columns=(
            "id", "chatId", "userId", "turnId", "serverSequence", "localOrder",
            "role", "status", "content", "metadata", "feedback", "createdAt",
            "modifiedAt",
        ),
        timestamps=_SEQUELIZE_TIMES,
        json_columns=frozenset({"content", "metadata"}),
    ),
    "NotetakerChatAgentStates": _spec(
        pk="chatId",
        layout=Layout.SNAPSHOT,
        columns=(
            "chatId", "userId", "trajectory", "displaySequenceIndex",
            "revision", "updatedAt",
        ),
        timestamps=_SEQUELIZE_TIMES,
        json_columns=frozenset({"trajectory"}),
    ),
    "UserContext": _spec(
        pk="id",
        layout=Layout.SNAPSHOT,
        columns=("id", "writingSamples", "polishPrompts", "modifiedAt"),
        timestamps=_SEQUELIZE_TIMES,
        json_columns=frozenset({"writingSamples", "polishPrompts"}),
    ),
    "UserVoicePreferences": _spec(
        pk="id",
        layout=Layout.SNAPSHOT,
        columns=("id", "preference", "filter", "needsUploading", "createdAt", "updatedAt"),
        volatile=_CHURN,
        timestamps=_SEQUELIZE_TIMES,
    ),
    "Automations": _spec(
        pk="id",
        layout=Layout.SNAPSHOT,
        columns=("id", "entityKind", "entityId", "kind", "payload", "createdAt"),
        timestamps=_SEQUELIZE_TIMES,
        json_columns=frozenset({"payload"}),
    ),
    "RemoteNotifications": _spec(
        pk="id",
        layout=Layout.SNAPSHOT,
        columns=(
            "id", "type", "key", "title", "text", "isArchived", "isRead",
            "createdAt", "updatedAt", "synced",
        ),
        volatile=_CHURN | {"isRead"},
        timestamps=_SEQUELIZE_TIMES,
        soft_delete=("isArchived",),
    ),
    "InstructChatSession": _spec(
        pk="id",
        layout=Layout.SNAPSHOT,
        columns=("id", "title", "needsUploading", "createdAt", "updatedAt"),
        volatile=_CHURN,
        timestamps=_SEQUELIZE_TIMES,
    ),
    "GranolaImportRun": _spec(
        pk="id",
        layout=Layout.SNAPSHOT,
        columns=(
            "id", "wisprUserId", "granolaAccountHash", "snapshotJson",
            "manifestJson", "cancelRequested", "createdAt", "updatedAt",
        ),
        timestamps=_SEQUELIZE_TIMES,
        json_columns=frozenset({"snapshotJson", "manifestJson"}),
    ),
    "GranolaTranscriptQueue": _spec(
        pk="id",
        layout=Layout.SNAPSHOT,
        columns=(
            "id", "granolaDocumentId", "wisprMeetingId", "wisprUserId",
            "granolaAccountHash", "state", "attempts", "lastErrorCode",
            "createdAt", "updatedAt",
        ),
        volatile=frozenset({"attempts", "state", "lastErrorCode"}),
        timestamps=_SEQUELIZE_TIMES,
    ),
    "SequelizeMeta": _spec(
        pk="name",
        layout=Layout.SNAPSHOT,
        columns=("name",),
    ),
}

# Tables whose records are rendered to Markdown, in the order a sync pass
# walks them. Everything else is archived as NDJSON without a renderer.
RENDERED = ("Meetings", "Notes", "CalendarEvents", "Dictionary", "History")
