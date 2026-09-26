# Maintaining the remote backends

The local backend reads files Wispr Flow wrote to your disk. If it breaks, the
schema moved, and `wispr-export schema` will tell you how.

The two remote backends are different in kind. Most of this document is about
the REST one, which is the more fragile; the MCP backend has its own section at
the end.

The cloud backend talks to `api.wisprflow.ai`, which is the desktop app's own
private interface: undocumented, unversioned, with no changelog, no deprecation
policy and no promise that any of it still exists tomorrow. **This document is
the price of reaching data that is otherwise unreachable.** Read it when
something stops working, or after Wispr Flow updates.

Everything below was measured against **app 1.6.897** on macOS, and every
endpoint status was measured again on **1.6.957**, unchanged. Nothing in it is
inferred from documentation, because there is none.

---

## The check that has to be run by a person

Nothing automated will tell you the declaration has gone stale. The four tests
that compare this tool against a live installation —
`test_pin_matches_the_live_database`, `test_expected_covers_the_live_database`,
`test_the_pin_matches_the_installed_app` and
`test_the_recorded_app_version_matches_the_bundle` — all call `pytest.skip`
when Wispr Flow is not installed, and CI runs on `ubuntu-latest` and
`macos-latest`, where it never is. **A green CI badge says nothing about schema
drift.** The weekly cron is a CVE canary; it is not a schema canary, and it
cannot be made into one without a runner that has the app on it.

So the cadence is manual, and it is short because the drift is not:

```bash
uv run pytest -q -m live           # the four that only run here
uv run wispr-export schema          # all three backends, read-only
```

Run it **after every Wispr Flow update, and before every release.** Wispr Flow
ships roughly twenty migrations a month, so "when something breaks" is too late
a trigger — by then the pin is months behind and the diff is no longer one
migration you can reason about.

That is not hypothetical. This document previously said 1.6.721 at migration
149; the check was not run for three weeks, and by then the app was at 1.6.897
at migration 152, with a new `Folders` table, a new `Meetings.recordedMs`
column, and an MCP tool swapped for another one. Nothing was lost — the reader
is `PRAGMA`-driven and archived all of it — but every drift report in that
window was measured against a declaration that no longer described anything.

---

## When something breaks, in order

### 1. Check the pin

```bash
uv run pytest -q -m live
```

`test_the_pin_matches_the_installed_app` compares `prefs.version` in
`~/Library/Application Support/Wispr Flow/config.json` against `CLIENT_PIN` in
`src/wispr_flow_exporter/cloud_schema.py`. It skips when Wispr Flow is not
installed, so a green CI run means nothing here — run it locally.

A mismatch is expected. Wispr Flow updates often and the pin only moves when
somebody re-checks. It tells you the endpoint table has not been validated
against this build, not that anything is wrong yet.

`test_the_declared_pin_describes_the_declared_table` needs nothing installed and
does run in CI: it fails if an endpoint was added or edited without refreshing
`CLIENT_PIN`.

### 2. Ask the API what it thinks

```bash
uv run wispr-export schema --source cloud
uv run wispr-export schema --source cloud --candidates --json   # wider, machine-readable
```

This is the cloud half of `schema`. One paced `GET` per declared endpoint, no
writes — not to the archive, not to Wispr Flow. It is the safe first move.

Read the `drift` line:

| verdict | meaning | what to do |
| --- | --- | --- |
| `ok` | every endpoint answered as recorded | nothing |
| `additive` | new or retyped fields, or an endpoint that started answering | note it; adopt the new data if it is worth archiving |
| `breaking` | a field vanished, or an endpoint stopped answering | go to step 3 — but note the archive still completed |
| `stale_source` | the installed app is *older* than the pin | you downgraded, or you are on a machine behind the pinned build |

Fields are compared by path — `items[].title` — and a field counts as vanished
only on evidence: the record that held it came back without it, and it was
never optional. A list that came back empty, or a value that is null today, is
not a removal. An endpoint that could not be asked this run — no network, a
`401`, `403`, `408`, `429` or `5xx` — is listed as *not answered this run* and
fails the run, but is not drift: nothing about the interface was learned. A
`204` is listed as *no content*. A failure never replaces what was recorded,
so the next answer is compared against the last one that arrived.

`schema --json` exits with the same code as the text form. Through 0.4.1 it
always exited 0.

**`breaking` does not mean the run failed.** Everything reachable is still
archived verbatim before anything is interpreted. Failing loud must never mean
failing closed.

### 3. If the token is being rejected

Every endpoint returning `401` almost always means the access token expired.
**Open Wispr Flow and re-run.** This tool deliberately cannot refresh it — see
*The rules that must not be relaxed* below.

If the token is fresh and everything is still `401`, check the header format
first. The service requires:

```
Authorization: <raw access token>
```

with **no `Bearer` prefix**. Sending the RFC 6750 form returns
`401 {"detail":"Invalid or expired token"}` for a token that works bare. This
was the original defect: the backend shipped sending `Bearer` and could never
have worked. `Credential.header()` in `cloud_auth.py` is the only place this is
decided.

### 4. Rediscover the endpoints

Wispr Flow ships as an Electron app; every path is a string in
`/Applications/Wispr Flow.app/Contents/Resources/app.asar` (~220 MB).

**Do not use the obvious grep.** This one:

```bash
strings app.asar | grep -oE '/api/v1/[a-z0-9_/-]+' | sort -u
```

is how the original endpoint table was built and it is why four of its nine
entries were wrong. It fails three ways:

- **It misses whole path families.** Everything outside `/api/v1/` is invisible
  to it — `/history/*`, `/llm/*`, `/geo*`, `/warmup`, `/marketing/*`. Dictation
  upload lives at `/history/upload`, so the grep that was supposed to find the
  dictation surface could not see it.
- **It cannot tell a read from a write.** `/api/v1/notes/sync` and
  `/api/v1/user/profile` look identical to it. The first answers only to a write
  method; a `GET` returns `405`. This is the property that decides whether this
  tool may call a path at all.
- **It invents paths that do not exist.** `/api/v1/meetings/` is not a route —
  it is the common prefix of `/api/v1/meetings/<id>/status` and friends, and a
  `GET` returns `404`. Truncating at the last matched character manufactures an
  endpoint.

Use this instead. It matches the app's own request helper, so it yields the
method and the path together:

```bash
python3 - <<'PY'
import mmap, re
path = "/Applications/Wispr Flow.app/Contents/Resources/app.asar"
with open(path, "rb") as handle:
    data = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)[:]
pattern = re.compile(
    rb'\.request\(\s*"(get|post|put|patch|delete)"\s*,\s*[`"]([^`"]{1,120})[`"]'
)
seen = {}
for match in pattern.finditer(data):
    method, route = match.group(1).decode(), match.group(2).decode()
    seen.setdefault(route, set()).add(method.upper())
for route in sorted(seen):
    print(f"{','.join(sorted(seen[route])):16} {route}")
PY
```

Only `GET` rows are candidates. Add promising ones to `CANDIDATES` in
`cloud_api.py`, probe with `schema --source cloud --candidates`, and promote the
ones that answer into `ENDPOINTS`.

### 5. Tell a readable endpoint from an upload-only one

A path answering `GET` in the bundle is necessary but not sufficient. The
app's sync coordinator sorts every resource into two lists, and that is the
authoritative answer:

```bash
# The pull list: each entry has a fetch(). These are readable.
# The push list: each entry has only push(). These are upload-only.
python3 -c "
import mmap
p='/Applications/Wispr Flow.app/Contents/Resources/app.asar'
d=mmap.mmap(open(p,'rb').fileno(),0,access=mmap.ACCESS_READ)[:]
i=d.find(b'{name:\"subscription\",fetch:')
print(d[i-200:i+3000].decode('utf-8','replace'))
"
```

On 1.6.897 the split was:

- **pull** (`fetch()`) — `subscription`, `preferences`, `notifications`, `notes`,
  `meetings`, `meetings_shared`, `todos`, `calendar`, `agentic_prereads`,
  `automations`
- **push only** (`push()`) — `history`, `polish`, `instructHistory`,
  `userVoicePreferences`, `todos`, `notetakerChats`

Being in the pull list does not make a resource reachable *by this tool*: the
app pulls `meetings`, `notes` and `todos` through write methods, which this tool
does not issue. See *What cannot be reached* below.

### 6. Re-baseline

Once the probe is clean and you have adopted whatever changed:

```bash
uv run python -c "
from wispr_flow_exporter.cloud_api import ENDPOINTS
from wispr_flow_exporter.cloud_schema import pin_from_endpoints
print(pin_from_endpoints(ENDPOINTS, '<new app version>'))
"
```

Paste `app_version`, `count` and `sha256` into `CLIENT_PIN` in `cloud_schema.py`.
Then run `wispr-export sync --source cloud` twice and confirm the second run
reports `0 written` — the per-endpoint shape ledger in `.sync-state.json`
re-baselines itself on the first run.

---

## What cannot be reached, and why

Do not spend an afternoon rediscovering these.

### Dictation history has no read endpoint

This is the finding that matters most, because reaching dictation history is the
reason the cloud backend was built.

`history`, `polish` and `instructHistory` are in the coordinator's **push-only**
list. Dictation leaves the machine through `POST /history/upload` and
`POST /api/v1/instruct_history/upload`. There is no counterpart. Searching the
bundle for `downloadHistory`, `fetchHistory`, `getHistory(` and `pullHistory`
returns nothing.

So when `localDataPolicy = never_store`, **the dictation text is gone** — not
withheld, not behind a flag, absent from every surface this tool can reach. The
tool's honest answer is: change the preference in Wispr Flow, and dictation is
archivable from that day forward. Retroactively, there is nothing to get.

What *is* readable is aggregate: `/history/stats`, `/history/context-stats`,
`/api/v1/insights`, `/api/v1/insights/heatmap` and `/llm/voice_profile/latest`
give word counts, durations, per-day activity, streaks and a derived profile.
All five are archived. None of them contain a sentence you dictated.

### Meetings, notes and todos cannot be enumerated

The app pulls all three through write methods:
`/api/v1/meetings/sync`, `/api/v1/notes/sync` and `/api/v1/todos/sync` are
bidirectional push-pull — you send your local records and receive
`{acked, pull, sync_time}`. This tool issues `GET` only, so all three return
`405`, and `/api/v1/meetings/` returns `404` because it is not a route.

The local backend is the only route to meetings, notes and todos. That is not a
limitation to work around; it is why the local backend is the primary one.

### `syncCoordinator.timestamps` is not an inventory

It is tempting to read `config.json` → `syncCoordinator.timestamps` as a list of
what the account holds. It is not. It is a client-side validator map sent as
query parameters to `GET /api/v1/sync/check`, which replies
`{changed, unchanged, timestamps}`; the client advances an entry only when the
server hands one back.

On the machine this was measured against, **seven of eleven entries sat at
`1970-01-01T00:00:00Z`, including `notes` and `todos`, which have real data.**
They are still driven by the app's older per-entity intervals, which keep their
own watermarks elsewhere in the same file — `prefs.user.lastNoteSyncTime` was
non-zero, proving notes had synced despite the coordinator showing the epoch.

Reading that map as a taxonomy would understate the account by seven entities
out of eleven.

### Cursors exist and are deliberately not sent

`GET /api/v1/calendar/sync` accepts `since` and `cursor`; `/api/v1/insights/heatmap`
accepts `since`; `/api/v1/notetaker-chats` accepts `cursor`. The client sends
none of them, and the parameter names are recorded in `ENDPOINTS` so the choice
stays visible.

The reason is the archive layout: one verbatim snapshot per endpoint at
`cloud/<name>.json`. A `since`-filtered delta would **overwrite a whole snapshot
with a partial one**. Incremental cursors and one-file-per-endpoint verbatim
archiving are incompatible, and the zero-bytes invariant already makes a full
re-fetch cost nothing on disk.

If you ever do want incremental cloud sync, it needs a different layout —
append-only NDJSON per endpoint — not a `params=` argument.

### Four endpoints paginate

`meetings_shared` (`has_more`, `next_cursor`), `calendar` (`nextCursor`),
`calendar_prereads` (`nextCursor`) and `notetaker_chats` (`next_cursor`) can
withhold records. Each returned a single complete page on the account this was
measured against, which is an account-shaped fact and not a guarantee.

`sync_cloud.truncated()` detects the markers and the run prints
`cloud: MORE RECORDS EXIST upstream than archived — …`. **If you ever see that
line, the archive is short and paging needs implementing.** Silent truncation is
the one failure an archival tool must never have.

---

## The rules that must not be relaxed

Each of these is asserted by a test. If you find yourself editing the test to
make a change pass, stop.

- **Never call the refresh endpoint.** Supabase GoTrue rotates refresh tokens
  and detects reuse, so refreshing would invalidate Wispr Flow's own session and
  sign the user out of the app being backed up. On an expired token, stop and
  say "open Wispr Flow and re-run".
  `test_no_refresh_endpoint_appears_anywhere_in_the_backend` greps all three
  cloud modules for `grant_type` and `/auth/v1/token`. It scans a hardcoded list
  of module names — **add any new cloud module to that list.**
- **`GET` only.** `test_every_declared_endpoint_is_read_only` scans
  `cloud_api.py` as raw text for the four write-method call forms, so even a
  comment containing one fails it. This tool must never write to Wispr Flow's
  servers.
- **The denylist.** `DENIED` in `cloud_api.py` names path prefixes no endpoint
  may ever start with, asserted by `test_no_endpoint_reaches_a_denied_path`.
  `/api/v1/support/` is account deletion, which the borrowed credential is
  perfectly entitled to call. The rest are other people's data.
- **A run says which backends it will contact before it contacts any.** The
  default, `all`, is local plus every remote backend that has a credential;
  `auto` and `local` never touch the network. `auto` once silently sent nine
  requests to an undocumented private API nobody had asked for, which is why
  it no longer reaches the cloud and why the default announces itself.
- **Never write to `session.json`,** never copy it into the archive, never log a
  token. `redact()` in `local_config.py` runs at the output sink so a new code
  path cannot forget it.
- **Stay a quiet client.** 4 req/s (`MIN_INTERVAL = 0.25` in `transport.py`,
  which both remote clients share). Do not raise it. `Retry-After` is honored
  up to 60 s, as seconds or as a date; a value that cannot be used means the
  backoff ladder, never zero. The archive is never urgent.
- **The zero-bytes invariant.** A second sync with nothing changed upstream must
  write no byte and no mtime anywhere, for both backends. If a new endpoint
  breaks it, the cause is almost certainly a self-moving field — add it to
  `VOLATILE_FIELDS` in `sync_cloud.py`, as `serverTime` already is.

---

## Measured endpoint table (app 1.6.897, unchanged on 1.6.957)

Archived. Every status observed against the live service.

| name | path | status |
| --- | --- | --- |
| `user_profile` | `/api/v1/user/profile` | 200 |
| `user_preferences` | `/api/v1/user/preferences` | 200 |
| `meetings` | `/api/v1/meetings/` | **404** — not a route |
| `meetings_shared` | `/api/v1/meetings/shared` | 200 |
| `notes` | `/api/v1/notes/sync` | **405** — write method only |
| `todos` | `/api/v1/todos/sync` | **405** — write method only |
| `calendar` | `/api/v1/calendar/sync` | 200 |
| `calendar_prereads` | `/api/v1/calendar/prereads/agentic_sync` | 200 |
| `dictionary_personal` | `/api/v1/dictionary/personal` | 200 |
| `dictionary_shared` | `/api/v1/dictionary/shared` | 200 |
| `dictionary_team` | `/api/v1/dictionary/team` | 200 |
| `notetaker_chats` | `/api/v1/notetaker-chats` | 200 |
| `notifications` | `/api/v1/notification` | 200 |
| `insights` | `/api/v1/insights` | 200 |
| `insights_heatmap` | `/api/v1/insights/heatmap` | 200 |
| `history_stats` | `/history/stats` | 200 |
| `history_context_stats` | `/history/context-stats` | 200 |
| `voice_profile` | `/llm/voice_profile/latest` | 200 |

Probed and deliberately **not** archived, with the reason recorded in
`CANDIDATES`:

| path | status | why not |
| --- | --- | --- |
| `/api/v1/sync/check` | 200 | degenerate without the client's timestamp map, which the local backend already archives from `config.json` |
| `/api/v1/meetings/weekly-quota` | 200 | a counter that resets weekly; would rewrite a file to record nothing |
| `/api/v1/user/registered_devices` | 200 | account trivia |
| `/api/v1/referral/` | 200 | carries the names of people this account referred — third-party data with no archival value |
| `/api/v1/calendar/events/` | 404 | not a route |
| `/api/v1/calendar/events/batch` | 422 | needs a request body |
| `/api/v1/user_context` | 204 | empty |
| `/api/v1/me/active-cost-center` | 404 | not a route |


---

# The MCP backend

`api.wisprflow.ai/connect/mcp` is Wispr Flow's remote MCP server. Unlike the
REST API it is a *product surface*: documented, versioned, and it declares its
own capabilities. That makes it easier to maintain, and the runbook is shorter.

It is also the only backend that holds a credential, which is the part worth
being careful with.

## When something breaks, in order

### 1. Check the authorization

```bash
uv run wispr-export schema --source mcp
```

A `401` in the middle of a run is renewed once from the stored refresh token;
if that fails too, the grant is gone and the message says so. Run
`wispr-export login` again. Everything else the server says is passed through,
including the reason, with control characters shown as escapes.

If the token store is confusing you, look at it — it is one small JSON file:

```bash
cat ~/.config/wispr-flow-exporter/mcp-token.json | python3 -c \
  'import json,sys; print(sorted(json.load(sys.stdin)))'
```

It holds `client_id`, `issuer`, `resource`, `access_token`, `refresh_token`
and `expires_at`. The tokens are bound to `resource` and `issuer`: a run
against any other endpoint refuses them, before any request, and says to log
in. **One login is kept at a time** — logging in against another endpoint
replaces it. A store written before 0.5.0 has no `resource`; it is adopted for
the shipped endpoint and issuer, and anything else asks for a login.
`wispr-export logout` deletes it; the next `login` re-registers. The
`mcp-token.json.lock` beside it serializes refreshes between runs and can be
ignored.

### 2. Re-check the OAuth topology

Every endpoint is discovered, never hardcoded — and checked before it is
followed. The resource document must name the configured endpoint exactly, the
shipped endpoint must name the issuer in `SHIPPED_ISSUERS` (`mcp_auth.py`), and
every endpoint that issuer advertises must be https on its own host. A server
that moves an endpoint within its host is followed automatically. One that
moves to another issuer stops login and refresh with a message naming both
hosts: check the new one as below, then update `DEFAULT_ISSUER` — or set
`WISPR_ALLOW_ENDPOINT_OVERRIDE=1` to try it first. To see what it is
advertising today:

```bash
# What resource, and which authorization server?
curl -s https://api.wisprflow.ai/.well-known/oauth-protected-resource | python3 -m json.tool

# What grants, and is dynamic registration still open?
curl -s https://mcp-auth.wisprflow.com/.well-known/oauth-authorization-server | python3 -m json.tool
```

Measured on the build this was written against:

| fact | value |
| --- | --- |
| resource | `https://api.wisprflow.ai/connect/mcp`, at both well-known URLs |
| authorization server | `https://mcp-auth.wisprflow.com`, the only one named |
| issuer's endpoints | authorization, token, registration and device all on `mcp-auth.wisprflow.com` |
| RFC 9207 `iss` on the redirect | not advertised, so not required |
| token lifetime | `expires_in` of seven days; refresh tokens rotate |
| `401` | `WWW-Authenticate: Bearer resource_metadata=…` |
| scopes | `openid offline_access` |
| bearer method | header — and it takes **only** `Bearer`, unlike the REST API which takes only the bare token |
| registration | dynamic, public client, no secret |
| grant used | authorization code + PKCE S256, loopback redirect |

**The device grant does not work, and the metadata says it does.**
`grant_types_supported` advertises
`urn:ietf:params:oauth:grant-type:device_code`, but registration refuses it —
*"each value in grant_types must be one of the following values:
authorization_code, refresh_token"* — and calling the device endpoint with a
registered client answers *"Device authorization is not enabled for this
application."* Both measured. If you are wondering why a command-line tool
listens on a loopback port instead of printing a code, that is why. Re-test it
occasionally; if it ever starts working, the listener can go.

### 3. Re-check the tools

`wispr-export schema --source mcp` prints every advertised tool, marks the ones
this backend calls (`use`) and the rest of the allowlist (`ok`), and classifies
drift the same four ways the other backends do. What it compares is each tool's
input schema reduced to its **constraints** — types, required lists, ranges,
enumerations — so a reworded description is not drift and a retyped argument
is.

Severity is about what this tool needs, not the server's inventory. Each tool
the sync pass calls declares the arguments it sends, in `McpTool.sends` in
`mcp_api.py`; a tool with `sends` is *used*. `breaking` means a used tool is
gone, or its *contract* moved: an argument it sends was renamed, retyped or
narrowed, or a new argument became required beside them. Everything else — a
new tool, a new optional argument, any change to an allowlisted tool nothing
calls — is `additive`. **When the pass starts sending a new argument, add it to
`sends`**; a test fails if the pass sends anything a tool does not declare.

To re-baseline, paste the values from `--json` into `MCP_PIN` in
`mcp_schema.py`, including `algorithm`: version 2 digests constraints, and a
pin records the algorithm it was taken with so it is always compared the same
way.

### 4. The allowlist

`READ_TOOLS` in `mcp_api.py` is the read-only guarantee. The client refuses to
call anything not in it, and refuses to send any JSON-RPC method outside
`initialize`, `notifications/initialized`, `tools/list` and `tools/call`. Both
asserted by test, and a third runs a whole pass against a fake server and
checks every request that reaches it.

MCP is JSON-RPC over POST, so the REST backend's GET-only test cannot extend
here — and should not be made to. What that test protects is *cannot mutate*,
and the allowlist is the MCP-shaped form of it. If the server grows a write
tool, absence from the table is what keeps it unreachable.

### 5. A listing that ends early

`search_meetings` lists most recently modified first, and `since` and `until`
bound when a meeting *started* — both measured, both published. So a first
run, or `--full`, lists everything, and an incremental run lists from the top
and stops at the first page older than the watermark less `recheck_days`. It
checks the order as it reads, and if the server ever stops listing most
recently modified first it reads to the end and says so.

`mcp: meetings listing incomplete: …` after a run means the pass did not see
every meeting, and it says why: a page could not be fetched (the call's own
failure is printed too, with the server's reason), a page flagged more records
without a cursor, or the server repeated a cursor. A query the server capped
with `truncated: true` — a thousand results — is narrowed rather than given up
on: split by start time, a year at a time and then in halves, for at most 32
queries. The watermark does not move until a listing completes, so the next
run asks again. A tool's own error — `isError` in the result — is always a
failure with its reason, never archived as data.

### 6. Transcripts and notes, a range at a time

`get_meeting` returns at most 40,000 characters of a transcript or of a
meeting's notes per request. Measured, a transcript range arrives inside an
envelope — a `<<<…>>>` line before it and `<<<END TRANSCRIPT>>>` after — and a
range that is not the last ends with a marker:

```
(...truncated, 24383 chars remaining; continue with view_transcript.start_char=1...)
```

The next request always uses the offset that marker names, and neither the
envelope nor the marker reaches the text. Every range is archived verbatim
under the offset it was asked for, and the manifest records each range's
length in code points and in UTF-16 units beside the offset the server named
next. Assembly stops, and says why, when a full range arrives with no marker
(the marker's format has changed: update `_MARKER` in `sync_mcp.py`), when the
envelope is missing, or when an offset does not move forward. An offset that
disagrees with the length of the range before it, in both units, is rendered
with a warning but not recorded as recovered.

Recoveries carry `assembly: 2`. One without it was assembled by 0.4.x's
splicer, which lost characters at every seam, and is fetched again when a
run next lists its meeting. An incremental run lists back only to its
watermark less the recheck window, and at least one page of 200, so on a
larger account an old recovery waits for `sync --full`, which fetches every
recovered transcript again and is the repair path whenever one is in doubt.

## What cannot be reached

**Dictation, again.** The MCP server does not expose it, and Wispr Flow says so
in its own settings copy in every locale: *"Wispr MCP has no access to your
dictation."* That is now the third independent confirmation, after the app's
push/pull resource lists and the REST probe. Do not go looking a fourth time.

## The rules that must not be relaxed

- **The two credentials must not meet.** `cloud_auth` borrows the app's Supabase
  token and must never refresh it. The MCP backend mints its own against a
  different issuer, where refreshing is safe *because it is not the app's
  session*. A test asserts the MCP modules cannot reference `read_access_token`,
  `cloud_auth`, the Supabase issuer or `session.json` at all, and another that
  importing them in a fresh interpreter loads no cloud module — so the minting
  path cannot reach the borrowed one.
- **A minted token goes only where it was minted for.** Never loosen the
  resource and issuer binding in the token store, and never follow an
  advertised endpoint off the issuer's host. Both decide who receives a
  refresh token.
- **The token never enters an archive.** It lives in `~/.config/`, and an
  archive stays copyable without carrying a credential.
- **Local wins on transcripts.** MCP returns normalized plaintext with no
  speaker attribution and no timestamps. The gap-fill gate reads the archive's
  own NDJSON from disk rather than trusting `index.json`, and a meeting with any
  local transcript — refined *or* live — is left alone.
- **The two ownership rules.** The MCP pass never creates a key under
  `entities["meetings"]` and writes exactly one field into an existing one, the
  reserved `"mcp"` sub-key; and every file it writes carries an `mcp` path
  component or an `.mcp.md` suffix. Together these keep `_archive_meeting`'s
  up-to-date check invariant under the MCP pass, which is what stops the two
  backends rewriting each other's work forever. Asserted by
  `test_the_pass_adds_no_meetings_key_and_only_the_mcp_subkey`.
- **Upstream-only meetings stay under `mcp/`.** Writing them into `meetings/`
  would make `verify` count them against the database and report a mismatch on
  every run, forever, on a healthy archive.
