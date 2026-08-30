# Maintaining the cloud backend

The local backend reads files Wispr Flow wrote to your disk. If it breaks, the
schema moved, and `wispr-export schema` will tell you how.

The cloud backend is different in kind. It talks to `api.wisprflow.ai`, which is
the desktop app's own private interface: undocumented, unversioned, with no
changelog, no deprecation policy and no promise that any of it still exists
tomorrow. **This document is the price of reaching data that is otherwise
unreachable.** Read it when something stops working, or after Wispr Flow
updates.

Everything below was measured against **app 1.6.721** on macOS. Nothing in it is
inferred from documentation, because there is none.

---

## When something breaks, in order

### 1. Check the pin

```bash
uv run pytest -q -k pin
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
| `additive` | new fields, or an endpoint that started answering | note it; adopt the new data if it is worth archiving |
| `breaking` | a field vanished, or an endpoint stopped answering | go to step 3 — but note the archive still completed |
| `stale_source` | the installed app is *older* than the pin | you downgraded, or you are on a machine behind the pinned build |

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

On 1.6.721 the split was:

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
- **`--source auto` stays local-only.** Cloud is reached on an explicit
  `--source cloud` or `--source both` and never otherwise. This was changed
  after an `auto` run silently sent nine requests to an undocumented private API
  nobody had asked for.
- **Never write to `session.json`,** never copy it into the archive, never log a
  token. `redact()` in `local_config.py` runs at the output sink so a new code
  path cannot forget it.
- **Stay a quiet client.** 4 req/s (`MIN_INTERVAL = 0.25`), `Retry-After`
  honoured up to 60s. Do not raise it. The archive is never urgent.
- **The zero-bytes invariant.** A second sync with nothing changed upstream must
  write no byte and no mtime anywhere, for both backends. If a new endpoint
  breaks it, the cause is almost certainly a self-moving field — add it to
  `VOLATILE_FIELDS` in `sync_cloud.py`, as `serverTime` already is.

---

## Measured endpoint table (app 1.6.721)

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
