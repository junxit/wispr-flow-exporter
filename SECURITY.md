# Security Policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability):
open the repository's **Security** tab and choose **Report a vulnerability**.

Please do not open a public issue for an undisclosed vulnerability.

## Supported versions

This is a personal tool. Only the current `main` branch is supported.

## Scope and threat model

`wispr-flow-exporter` is a read-only client for three Wispr Flow backends: the
app's own store at `~/Library/Application Support/Wispr Flow/`, the
undocumented sync API at `api.wisprflow.ai`, and the remote MCP server at
`api.wisprflow.ai/connect/mcp`. It writes transcripts and dictation
history to disk, so the corpus it handles — rather than any protocol — is the
thing worth defending.

That corpus is unusually broad. It is not only your meetings; it is every
sentence this machine has dictated, the other people in every recorded room,
and — in two tables — a capture of whatever was on screen at the moment you
spoke.

- **Local disclosure.** The archive can contain the most sensitive data on the
  machine. Archive files are written `0600` and directories `0700` so they are
  readable only by the owning user, rather than inheriting a typically
  world-readable umask. Note that Wispr Flow's own `config.json` and
  `session.json` are mode `0666`; `shutil.copy2` would preserve that, so
  archived copies are written through a helper that forces `0600` instead.

- **The plaintext session token.** `session.json` is a bare, unencrypted
  Supabase GoTrue session — a bearer `access_token`, a refresh token, and the
  account's email, id and full name — with no keychain entry guarding it. Its
  outer JSON holds a single key whose *value is itself a JSON string*, so the
  credential sits one parse deeper than it looks and a redactor keyed on
  top-level shape would miss it entirely.

  Only the cloud backend reads that file, lazily at the call site. The local
  backend never opens it, the MCP backend cannot reach it at all, and there
  are regression tests asserting both.

  **Invariant:** no byte originating in `session.json` is ever written to a file
  this tool creates, printed to a stream it writes, or included in an exception
  message or traceback. The only place the token may appear is an outbound
  `Authorization` header; it is held in a local and never stored.

  This extends to the cloud backend's drift machinery. The per-endpoint
  response fingerprints in `.sync-state.json` are digests of *structure* —
  field names and types, with every value discarded and dictionary keys that do
  not look like field names collapsed to `<dynamic>`, so a response keyed by
  UUID cannot put an id into the state file. That is a privacy property first
  and a stability property second.

- **The tool never refreshes the *borrowed* session.** Supabase GoTrue rotates
  refresh tokens and detects reuse, so a second client calling the refresh
  endpoint with the desktop app's token either revokes the app's session or
  races it. This tool therefore reads the existing access token and **never
  calls `/auth/v1/token`**. The honest cost is that cloud sync only works when
  the app has refreshed recently; when the token has expired the correct
  behavior is to stop and tell you to open Wispr Flow and re-run. No refresh
  code path exists to be reached by accident.

  The token is sent bare, as `Authorization: <token>`, with no `Bearer` scheme —
  that is what the REST service accepts, measured against it. The redactor does
  not depend on the scheme: it matches the JWT shape itself, so a bare token in
  a log line or an exception is redacted exactly as a prefixed one was.

- **One credential is minted, and it is not the app's.** The MCP backend is the
  exception to "this tool never mints a credential", and the distinction is
  worth stating precisely because it is what keeps the rule above intact.

  `api.wisprflow.ai/connect/mcp` is an OAuth 2.0 protected resource whose
  authorization server is `mcp-auth.wisprflow.com` — **a different issuer from
  the desktop app's Supabase project**. Measured: it answers the borrowed token
  with `401 invalid_token`, bare and as a Bearer. There is nothing to borrow, so
  this backend registers a client of its own (dynamic registration, public
  client, no secret) and holds its own token.

  Refreshing *that* token cannot disturb the desktop app's session, because it
  was never the app's session. So the invariant is restated rather than
  abandoned:

  > Never refresh a borrowed credential. A credential this tool minted for
  > itself is its own to manage.

  Enforced by test on both sides: `cloud_auth.py` still may not contain
  `grant_type` or `/auth/v1/token`, and the four MCP modules may not reference
  `read_access_token`, `cloud_auth`, the Supabase issuer, or `session.json` —
  so the minting path cannot reach the borrowed one at all.

- **Where the minted token lives.** `~/.config/wispr-flow-exporter/`
  (`XDG_CONFIG_HOME` when set), file `0600` in a directory `0700`. Deliberately
  **outside the archive**: an archive is the thing people copy to a backup drive
  or hand to somebody else, and the property that it carries no credential is
  the one that mattered when there was no token store at all. `wispr-export
  logout` removes it. This is the only state this tool keeps outside an archive.

  The login flow binds a one-shot HTTP listener on `127.0.0.1` — not `0.0.0.0` —
  for the length of one browser round trip, and checks the `state` parameter
  before using the code it receives.

  Note that the two services want opposite header forms, and both were measured
  rather than assumed: the REST API rejects `Bearer` and takes the token bare,
  while the MCP resource advertises `bearer_methods_supported: ["header"]` and
  takes only a Bearer.

- **An endpoint denylist, asserted rather than intended.** The borrowed
  credential is the account's own, so it is entitled to do things this tool must
  never do. `DENIED` in `cloud_api.py` names path prefixes no endpoint may begin
  with, and a test enforces it against both the archived table and the candidate
  table:

  - `/api/v1/support/` — **account deletion**. A `DELETE` here removes the
    account. The `GET`-only rule already makes it unreachable; the denylist means
    a future maintainer cannot make it reachable by widening one method.
  - `/api/v1/sandbox-user/` — creates and destroys accounts.
  - `/api/v1/enterprise/` — organization administration: members, invitations,
    join requests, settings.
  - `/api/v1/contacts`, `/api/v1/teams/` — other people.

  One further endpoint answers, is not on the denylist, and is still deliberately
  not archived: `/api/v1/referral/` returns the names of people this account
  referred. It is third-party data with no archival value here, and the reason is
  recorded next to the decision in `CANDIDATES`.

- **Silent truncation, again, at the endpoint boundary.** Four cloud endpoints
  paginate. This tool does not page them, so it detects the markers
  (`has_more`, `next_cursor`, `nextCursor`) and reports loudly when a response
  says it withheld records. An archive holding one page and saying nothing would
  be indistinguishable from a complete one.

- **Redaction is at the sink, not the source.** Every diagnostic stream — log
  lines, `--verbose` output, exception messages, the run summary — passes
  through one `redact()` before it is emitted, replacing JWTs,
  `sb-<ref>-auth-token` keys and `X-Amz-(Signature|Credential)` parameters.
  Redacting at the sink rather than at each call site is deliberate: a new code
  path cannot forget to do it.

- **Presigned URLs are credentials, not metadata.** `NoteImages.presignedGetUrl`
  is a signed object URL that grants read access to anyone holding it until
  `urlExpiresAt`. It is a bearer credential living in a `TEXT` column, and it is
  excluded from `raw.json` and redacted from output, on the same footing as the
  session token.

- **Screen context is excluded by default.** `History` and `FlowLensHistory`
  each carry `screenshot`, `axText` and `axHTML`: a bitmap and a full
  accessibility-tree capture of whatever application had focus when you spoke.
  That can be a password manager, a banking session, a private message, or
  someone else's screen on a shared call. `FlowLensHistory` additionally carries
  a `userEmail` column, so a tiering rule written only against `History` would
  miss it.

  Meeting audio (`meetings/<uuid>/upload.ogg`) *is* archived by default, because
  Wispr Flow garbage-collects it — on the machine this was developed against,
  only one of three meetings still had its audio on disk. Dictation audio blobs
  and note images are opt-in. Screen context requires both
  `--include-screen-context` and `--i-understand`.

  **Invariant:** screen-context columns are excluded **by name in the `SELECT`**,
  not filtered out of a `SELECT *`. A column added upstream is excluded until it
  is deliberately added to the projection, so the failure mode of a Wispr Flow
  schema change is a missing field rather than a silent screenshot dump.

- **Untrusted local input.** The NDJSON transcripts and the JSON-in-`TEXT`
  columns (`Meetings.speakerMap`, `Meetings.participantNames`,
  `CalendarEvents.participants`, `History.additionalContext`,
  `History.toneMatchPairs`, `History.opusChunks`) are parsed as untrusted data.
  They are read a line at a time under a per-line byte cap and a per-file line
  cap; parsed with `json.loads` only, never `eval` and never `pickle`; and
  type-checked before use, so a `speakerMap` that is a list where a mapping was
  expected is treated as absent rather than raising from four frames deep. A
  malformed line is skipped rather than aborting the file — but it is
  **counted**, and the count is reported.

- **Silent truncation is a security failure.** For an archival tool, quietly
  archiving less than exists is worse than failing. Row counts read from SQLite
  are reconciled against records written, and any mismatch is surfaced in the run
  summary rather than absorbed.

- **Path traversal from titles and ids.** Meeting titles are user- and
  calendar-supplied and become directory-name slugs; UUIDs become path
  components. Ids are validated against a canonical lowercase UUID pattern before
  they are used to build a path, titles are slugified and length-capped, and
  every archive path is verified to resolve inside the archive root.

  **Invariant:** the validated UUID, not the slug, is what makes a directory name
  unique and safe. A title that slugifies to the empty string, to `..`, or to 300
  characters still yields a valid, contained directory. Regression tests cover
  traversal at depths 1 through 7, absolute paths, and the empty slug.

  `CalendarEvents.externalId` — the primary key — is 181 characters of base32 in
  practice, so it cannot be a path component at all and truncating it is not
  injective. Calendar filenames use `sha256(externalId)[:12]` instead.

- **Markdown frontmatter injection.** The archive targets Obsidian, so files open
  with YAML frontmatter. A meeting title containing a newline and `---` would
  otherwise inject arbitrary frontmatter keys into a file other tools then parse.
  Every emitted scalar is quoted and escaped; frontmatter is never built by
  string interpolation of raw values.

- **Reading a live WAL database.** `flow.sqlite` is open and being written by the
  Wispr Flow app while this tool runs. It is opened `file:...?mode=ro` with
  `PRAGMA query_only = 1`, and a single deferred read transaction spans the whole
  export so every table comes from one consistent snapshot. It never issues
  `PRAGMA journal_mode`, never checkpoints, and never writes.

  **Invariant:** the tool cannot leave Wispr Flow's own database in a state the
  app would not recognize. It also never reads `backups/backup-*.sqlite`
  implicitly — that is the app's rolling copy, and silently archiving from it
  would produce an archive whose provenance nobody can explain.

- **Local index tampering.** `index.json` is treated as untrusted input; entries
  resolving outside the archive root are refused rather than followed. A
  corrupted or edited index cannot redirect a write, or a rename, outside the
  archive.

- **Third parties who never consented.** This is the risk most specific to this
  tool. A meeting archive contains other people's voices and words, recorded
  under whatever expectation they had at the time — which was almost certainly
  not "kept verbatim, indefinitely, in a folder that gets backed up." Recording
  consent is jurisdiction-dependent and this tool cannot evaluate it for you.

  What the tool does: writes owner-only, keeps the archive out of git by default,
  and keeps screen context behind a flag. What it cannot do: make sharing an
  archive safe. Publishing, syncing or handing over an archive is a disclosure
  decision about people who are not in the room, and it is yours. Treat an export
  the way you would treat the recording it came from.

Out of scope: the security of the Wispr Flow service itself, and anything
requiring an attacker who already has code execution as your user — who can
simply read `~/Library/Application Support/Wispr Flow/` directly.

## Dependencies

Dependencies are pinned in `uv.lock` and scanned against the OSV database by CI
on every push and pull request, plus weekly on a schedule, so a newly published
advisory surfaces even when nothing changes. Dependabot opens update pull
requests weekly.

The dependency set is deliberately small. The store is SQLite and NDJSON, both of
which the standard library reads, so the local export path pulls in nothing that
parses untrusted bytes in C beyond the `sqlite3` module Python already ships.
`httpx` is imported lazily and only by the two remote backends; the MCP client
is JSON-RPC written against it directly rather than an SDK, so no protocol
implementation joins the audit surface.
