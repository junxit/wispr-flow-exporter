"""Shared fixtures and the fictional cast every test draws on.

Two rules govern everything in this file, and ``tests/test_privacy.py``
enforces both.

Fixtures are Python literals. No ``.ndjson``, ``.ogg``, ``.png`` or ``.sqlite``
fixture is ever committed, which is what lets ``.gitignore`` blanket-ignore
those extensions with no exceptions to erode. A test needing a database builds
one in ``tmp_path`` from DDL held here as a string; a test needing audio
synthesizes bytes, because no code path decodes audio -- it only copies it.

Every proper noun comes from the cast below. Not "a made-up name" but *the*
made-up names, so there is one table to audit. Real content is never
anonymized into a fixture either: swapping names out of a real transcript
leaves its cadence, topic and structure behind. The content here is invented to
exercise a parser.

The theme is sounds that are not quite speech, and the running subject is the
quarterly whisper budget.
"""

from __future__ import annotations

# --- cast -----------------------------------------------------------------
# example.invalid is reserved by RFC 2606 and can never resolve or route. This
# is a deliberate divergence from granola-exporter, whose fixtures use a real
# vendor domain with live MX records.
OWNER = "Murmur Pike"
OWNER_EMAIL = "murmur@example.invalid"
SECOND = "Hush Delgado"
SECOND_EMAIL = "hush@example.invalid"
THIRD = "Static Vance"
THIRD_EMAIL = "static@example.invalid"
LATE = "Rumble Osei"
LATE_EMAIL = "rumble@example.invalid"
UNIDENTIFIED = "Speaker 2"

# --- identifiers ----------------------------------------------------------
# Every UUID-shaped string anywhere in src/ or tests/ must appear here.
# test_privacy.py asserts that, which is what stops a real meeting id from ever
# reaching the repository: there is no other way for one to get in.
MEETING_A = "0f2b6cf1-6d3a-4a5c-9d21-2f4e7b8c0a11"
MEETING_B = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
NOTE_A = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
HISTORY_A = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"

# Rejected on purpose: the archive-key validator demands canonical lowercase.
UUID_UPPER = "0F2B6CF1-6D3A-4A5C-9D21-2F4E7B8C0A11"
UUID_SHORT = "0f2b6cf1-6d3a-4a5c-9d21-2f4e7b8c0a1"

APPROVED_UUIDS = frozenset(
    {MEETING_A, MEETING_B, NOTE_A, HISTORY_A, UUID_UPPER.lower(), UUID_SHORT}
)

# --- credentials ----------------------------------------------------------
# Structurally valid and cryptographically meaningless: header and payload are
# base64 of {"alg":"HS256"} and {"sub":"murmur"}, and the signature segment is
# the literal text "signature-placeholder". The eyJ prefix trips the
# pre-publication audit grep on purpose; test_privacy.py allow-lists this exact
# value rather than relaxing the pattern.
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJtdXJtdXIifQ.c2lnbmF0dXJlLXBsYWNlaG9sZGVy"
FAKE_SESSION_KEY = "sb-aaaaaaaaaaaaaaaaaaaa-auth-token"

# --- adversarial titles ---------------------------------------------------
# Each one is a path or frontmatter failure the renderer must survive.
TITLE_PLAIN = "Quarterly whisper budget: review"
TITLE_AMPERSAND = "Murmur & Hush weekly"
TITLE_EMPTY = ""
TITLE_TRAVERSAL = "../../../../etc/passwd"
TITLE_FRONTMATTER = "  \n---\ntitle: injected\n---  "
