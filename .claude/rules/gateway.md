---
paths:
  - "src/remoteos/gateway/**"
  - "tests/test_gateway*.py"
  - "tests/gateway_harness.py"
  - "tests/conftest.py"
  - "tests/fleet_harness.py"
  - "src/remoteos/release_channel.py"
  - "tests/test_release_channel.py"
  - "scripts/create-release.sh"
  - "deploy/remoteos-gateway*"
  - "installers/enroll-*/**"
  - "installers/enroll-values.conf"
  - "enroll.ps1"
  - "enroll-linux.sh"
  - "enroll.sh"
  - "tests/test_enroll_wrappers.py"
  - "tests_windows/test_enroll_ps1.py"
---

# remoteos-gateway (dev1 MCP gateway, #25) — gotchas

- **HTTP client is `httpx2`, not `httpx`.** `httpx2` is what fastmcp and mcp use at runtime,
  and it is pinned directly. Plain `httpx` exists only in the `test` extra.
  - `client.send(httpx2.Request(...), stream=True)` sends the request AS-IS. No client default
    headers are added (no `Accept-Encoding`, no cookies), which is what keeps the proxy
    transparent. Do not switch to `client.build_request()`.
  - Relay the body with `aiter_raw()`, never `aiter_bytes()`. The body must pass through raw,
    so `Content-Encoding` and `Content-Length` stay valid.
- **`httpx2.ASGITransport` buffers the whole response body.** A test through it cannot prove
  streaming. Drive the ASGI app directly with a custom `send` (see
  `test_sse_is_streamed_chunk_by_chunk_not_buffered`): the upstream yields part 2 only after
  the consumer has received part 1, so a buffering proxy deadlocks into the 5 s timeout.
- **uvicorn advertises ASGI spec_version 2.3.** So Starlette's `StreamingResponse` runs
  `listen_for_disconnect` and cancels the stream on disconnect.
  - Its `background=` task does NOT run when the stream raises. That is why
    `_UpstreamResponse.__call__` closes the upstream and writes the log line in a `finally`.
  - `test_upstream_is_closed_even_when_the_consumer_vanishes_before_headers` locks this in.
- **Upstream keep-alive (#25 reopened).** `UPSTREAM_LIMITS.keepalive_expiry` (2.0 s) MUST stay
  well below the machines' uvicorn `timeout_keep_alive` (5 s). With equal values, a call after
  ~4.9 s idle reused a connection the machine was closing, and the consumer got 503
  `RemoteProtocolError: Server disconnected without sending a response`.
  - `Gateway._send_upstream` retries ONCE, only for `RemoteProtocolError`/`ReadError`/`WriteError`
    raised by `send()` (pre-headers), and only when that attempt REUSED a pooled connection
    (httpcore `trace` extension: no `connection.connect_*` event). Never timeouts, connect errors,
    a freshly dialled connection, or mid-stream.
  - The retry goes through `retry_client` (`max_keepalive_connections=0`), so it always dials. The
    main pool would hand it the next stale connection after a machine restart.
  - Double-run safety rests on two machine properties: uvicorn discards bytes racing its
    keep-alive close, and machines answer `tools/call` as SSE (headers before the tool runs).
    Never start a machine with fastmcp `json_response` on (`FASTMCP_JSON_RESPONSE`).
  - Pool tests run a REAL loopback server against the production clients
    (`tests/test_gateway_upstream_pool.py`). Pin a long expiry (`long_keepalive`) in tests that
    are about the retry, so a stalled runner cannot expire a connection between calls.
- **`UPSTREAM_TIMEOUT.read` = 3700 s (#67):** a running tool sends no bytes, so this is the
  longest tool call through the gateway: Shell's one hour, the client's 60 s, 40 s spare. A value
  test pins it (`test_cli_put_and_shell_fields.py`); no test waits that long. Only a POST gets it:
  `upstream_extensions` gives anything else (the idle GET stream) the old 600 s through the
  request's `timeout` extension, because a vanished box would hold a slot of the fleet's shared
  pool for an hour. A bare `httpx2.Request` otherwise takes the client's timeout.
- **Decision order:** malformed machine name → 404 (before auth, so the raw value never
  reaches a log), then 401 → 403 → 404 (the design order), then 413 (body over the cap).
  - The 403 comes before the 404 so an authenticated consumer cannot probe which other
    machines are exposed.
  - The body is read only after the allow-list and exposure checks pass.
- **Consumer-supplied strings** (`rpc_method`, `tool`) go into operational log lines only via
  `%r`. Machine and consumer names are `fullmatch`-validated: `$` would accept a trailing
  `\n`.
- **Never log or forward the consumer token, the machine key, or tool arguments.**
  - `Upstream.authorization` is `repr=False`.
  - The access log keeps only `rpc_method` and the `tools/call` `params.name`.
  - Tests assert all three never appear.
- **Fail closed.** A deleted registry is an empty registry (deny all). A broken registry or
  machine source at reload keeps the last good copy. At startup, `serve` refuses to start
  on a broken file.
- **Test fixtures:** `block-sensitive-staging.sh` flags `TOKEN = ...` in tests even though the
  values come from `secrets.token_hex`. Append `# airuleset:secret-ok <reason>` to BOTH the
  `git add` and the `git commit`.
- **Rollout (dev1, owner account):** follow the README section "MCP gateway (dev1)".
  - Install into a DEDICATED venv, `~/.local/share/remoteos-gateway/venv`, with
    `-c constraints.txt`. That venv is the unit's `ExecStart`.
  - NEVER `pip install --user`: it would upgrade `fastmcp`/`mcp` in the account's shared user
    site, which is the #12 fastmcp-slim collision class.
  - Upgrade = the same `pip install` line with `--force-reinstall`, then
    `systemctl --user restart remoteos-gateway`, then check the version on `/healthz`.
  - After install: `remoteos-gateway expose <machine>`, then verify from another tailnet node:
    200 tools/list, 401 wrong token, 403 not allowed.
- **Unit hardening:** only `NoNewPrivileges`, `RestrictAddressFamilies` and `MemoryMax` are
  enforced for a `--user` unit on dev1.
  - `ProtectSystem`/`ProtectHome`/`PrivateTmp` are VERIFIED-INERT there:
    `apparmor_restrict_unprivileged_userns=1` blocks the mount namespace they need.
  - Never claim them as isolation the gateway has.

## Every project through the gateway (#51): key store, manifest, sync-configs, check

- **Modules:** `keystore.py` (machines.json I/O, `set_key`, the import), `manifest.py`
  (projects.toml), `sync.py` (the generator), `targets.py` (local / `sudo -n -u` /
  `ssh -o BatchMode=yes`), `fileops.py` (atomic write + backup + lock), `probe.py` (the
  `initialize` probe), `check.py` (the watchdog), `cli_fleet.py` (the commands, registered on
  `cli.main`). The dependency direction is one way: `registry` ← `keystore` ← `sync` /
  `check` ← `cli_fleet` ← `cli`.
- **The key-store shape lives in `registry.py`** (`parse_keystore`, `MachineEntry`,
  `RepairRecipe`), because `load_machines` must read it; `keystore.py` builds on it.
  - `load_machines` tells the shapes apart by the `mcpServers` key: the legacy one skips bad
    entries with a warning (a shared file), the key store refuses the WHOLE file on one bad
    entry (the gateway keeps its last good copy).
  - `--machines` defaults to the key store, so after an upgrade the gateway must not restart
    before `machine import` ran (README migration order).
- **`fileops.py` is stdlib-only, py3.8+, with no package imports:** `targets.py` runs its
  SOURCE as `python3 -c` under sudo or over ssh, so the three writers share one tested
  implementation.
  - `write` takes the byte count (never a secret) and refuses a stdin that ended early or ran
    long: an ssh that died mid-transfer must not install a truncated config (review round 6).
  - A config inside a git work tree (a `.git` entry upward) is refused at READ and at write:
    a token there, or its `.backup-*/<name>.1` copies, could be committed. Refusing at read
    means no token is minted for a file that can never be written.
  - A new token keeps exactly ONE previous hash next to it (`sync._apply`): the one the file
    HOLDS right now (`ProjectPlan.held_hashes`), else index 0 (what the last CONFIRMED write
    left — a confirmed write prunes to exactly its own hash). So the file's token stays known
    however many writes fail (even after a write that landed but reported failure), and failed
    mints only rotate the second slot. Never `[-1:]`: that keeps the previous FAILED mint and
    drops the file's token (review round 7).
  - File contents go on stdin/stdout only. An argv holds the source, an op and a path.
  - `~` is the account's passwd home, never `$HOME` (sudo may keep the caller's).
  - A symlink is written through (`realpath`): a project's `.mcp.json` symlink stays one.
  - A same-day backup is never overwritten (`name.1`, `.2`, ...).
- **What sync owns:** an entry whose url is one sync writes (`<gateway>/<name>/mcp`, or in
  direct mode the key store's url), or sync's gateway SHAPE (`.../<name>/mcp`) at another url
  carrying this project's registered token: an entry left at the gateway's OLD url after a
  `[gateway] url` change (review round 8). A hand-made alias (another name) with the token
  copied into it is NOT owned. A manifest machine is always (re)written. An owned entry whose
  machine left the manifest is removed. ANY other entry is kept, even when its name is in the
  key store: dev2's `linux-presenter` / `win-marek-nb` may not be reachable from dev1, and
  deleting them would break those projects.
  - **Direct entries after their machine left the key store (#109):** `direct_record.py` keeps
    `sync-direct.json` next to the registry (`{project: {machine: url}}`, urls only). `owned()`
    also owns a direct entry whose name + url is RECORDED. `machine remove` / `rename` write the
    urls they retire into it FIRST (`remember_retired`), so even a removal before any record existed,
    or one whose later step / sync write failed, is finished by a plain sync (review round 1). Never "any sync-shaped
    entry not in the key store": that deletes hand-made entries. Record = exactly `direct_urls`
    after a confirmed write or an unchanged config; the UNION with the old record (or the seed)
    after a failed write; the old record for a project that failed before planning. No record /
    unreadable = SEED from entries at the key-store url (removes nothing extra), and an unusable
    record falls back to seeding (keeps, never deletes wrongly). Written under the registry lock,
    never in a dry run (which says it would); a failed record write fails the run (exit 1).
- **A lone surrogate escape (`\ud800`) anywhere in a config** fails that project in
  `parse_config` (it would break hashing, output and the UTF-8 write-back), before any token.
- **Byte-equal non-fleet entries:** `sync.parse_config` walks `mcpServers` with
  `JSONDecoder.raw_decode` and keeps each member's source slice (key as written, colon spacing,
  value). `render_config` re-joins the slices; only generated entries are serialised
  (`json.dumps(indent=2)` re-indented to the entry's column) and only the whitespace BETWEEN
  entries is rendered. Never "load + json.dump" a project config: that rewrote
  `reaperiem.mcp.json` (not in indent-2 form).
- **Token order (no window with an unknown token):** register the new hash (keep the old) →
  save → write the file → prune to `[hash]` → save. A write that REPORTS failure may still have
  landed (an ssh drop after the remote finished, a lost reply, an fsync error after the
  rename), so it is NEVER rolled back (review round 1): the last confirmed hash and the new
  one stay, and the next run reuses whichever token the file holds. A failing project's
  allow-list only ever NARROWS. A token is reused only when its hash is registered for THIS
  managed consumer; an unregistered token in a file is never adopted.
- **`managed_by: "sync-configs"`** marks a consumer that sync owns. A managed consumer whose
  project left the manifest keeps its hash with `machines = []` (never broader than the
  manifest; undoing a mistaken edit needs no new token and no restart); `--prune` deletes it.
  An empty manifest is refused outright. A same-name consumer sync did not create is refused.
  `Consumer.to_json` omits the field when it is empty, so hand-made consumers keep the #25 shape.
- **Exposure is a kill switch sync never undoes:** sync warns about an unexposed manifest
  machine. `machine import` and `machine set-key --url` expose only machines NEW to the key store.
- **Import never downgrades the key store:** when no copy of a machine verifies, the key
  store's current entry is kept (a set-key made while the box was down), never the most
  common config copy. For a machine already in the key store, only copies at ITS url count:
  a 200 from another url only proves the key fits that url (a stale IP reused by another box).
  Other-url copies are listed, never probed (the key is not sent there), unless
  `--accept-url-change`. `apply_import(snapshot=...)` leaves a machine whose entry changed
  while the import probed.
- **`consumer add` / `allow` refuse a sync-managed consumer** (`Registry._refuse_if_managed`):
  its allow-list must stay the manifest's. `deny` / `revoke` (narrowing) still work.
- **Org per machine (#105, owner 2026-11-17: Slovnormal apart from nlmedia):**
  - `MachineEntry.org`, key store field `"org"`, closed set `registry.ORGS` (`nlmedia`, `slovnormal`);
    `validate_org` errors never repeat the value (length only). An entry with no org still PARSES:
    the gateway and `check` read the same file, so a strict parse would take every project offline
    over a metadata field. The loud refusal is in `sync.validate_run` (a manifest machine with no org,
    or a project spanning two orgs, refuses the whole run) and in `machine list` (`org MISSING` +
    warning). No default org, ever.
  - The only cross-org project is `sync.CROSS_ORG_PROJECTS` (`remoteos-mcp`), a code constant on
    purpose: a manifest flag would let any project opt out.
  - Every rewrite of an entry goes through `dataclasses.replace` (repair-recipe, check's repair,
    set_key, apply_import), never `MachineEntry(url=, key=, repair=)`: that silently dropped the org.
  - `set_org` is all or nothing (an unknown name refuses the call). `set_key` needs `org` for a NEW
    machine; `enroll` / `enroll-finish` take `--org` and refuse a project holding another org's
    machines in `_check_request`, before any box or vault is touched.
  - `fleet-down` stays ONE text (the one-per-kind ruling), lines grouped under an org header
    (Slovnormal, nlmedia, bez organizácie); `check` writes `org` into each status record and the
    fingerprint is `[name, state, org-or-""]`, but only a changed BOX SET re-sends (#109, below): an
    org change alone, or the fingerprint format change of an upgrade, is stored silently. `_fit_grouped` cuts BOX lines only, never leaves a header
    without a box, and the "… a ďalších N (Slovnormal 3, nlmedia 2)" line counts boxes per org.
  - A known machine's org is changed ONLY by `set-org` (set_key / enroll refuse a different org;
    `set-org` warns about every project it would mix). `enroll` checks the named projects AND every
    project already holding the machine.
  - Deploy order: install → restart the gateway → `set-org`. An OLDER gateway refuses a key store
    carrying `org` (unknown key): at reload it keeps the last good copy, at a start it refuses.
- **`edit_keystore` saves only when the content changed:** every save makes the running
  gateway reload, and a no-op (the repair race's early return) must not.
- **`check`:**
  - `notified: false` survives a failed notification, so the next run retries it.
  - **`airuleset.py notify --body` exits 0 even when the Discord post failed** (review round 2):
    it PRINTS `send()`'s status (`sent` / `dedup` / `dry-run` / `no-config` / `error`). Only a
    printed `sent` / `dedup` / `suppressed` is delivered (see below). Every attempt passes its own `--dedup-key`, so a
    retry is never deduped away. The test fake prints a status and exits 0, like the real tool.
  - A check that cannot run because of an invalid key store is announced once per distinct
    failure, and its recovery is announced too. An UNWRITABLE status file is announced on every
    run: there is nowhere to remember that it was. Validation messages never repeat a value (a
    key pasted into `url` would reach Discord): length and scheme only.
  - `urlsplit` RAISES `ValueError` on some strings (`http://[bad/mcp`, an invalid IPv6 literal),
    and its `.port` raises on a bad port (`:99999`, `:80a`). `registry.is_http_url` evaluates
    both inside one `try` and requires a hostname, so such a url is just "not http(s)" in the
    key store, the gateway reload and import (review rounds 3-4: each crashed check silently).
    Second line of defence: `probe.gather_probes` turns any probe that still raises into an
    `error` result for that machine alone.
  - **`suppressed` counts as delivered** (#63): airuleset's owner policy (`notify.OWNER_PING_ALLOW`)
    posts nothing for owner zbynek unless the dedup key is one of its alarm classes, and prints
    `suppressed`. Retrying could never change that, and airuleset itself counts it as delivered.
    So on dev1 these messages do NOT reach Discord: the text is in the journal. Never borrow an
    alarm key to get around the owner's policy.
  - airuleset CUTS a body at 1900 characters (mention included) and still prints `sent`: a
    fleet-wide change goes out as several messages of at most `CHUNK_LIMIT` (1700,
    `messages.split_to_fit`, shared by every owner message), and only the
    machines of a DELIVERED message count as notified. While an announcement is still owed, the
    record keeps the detail of the change (`repaired: ...`), so a retry still says what happened.
- **Owner notices (`owner_notice.py`, #102, airuleset #1300):**
  - `Notifier.per_condition` = the airuleset has `--owner-notice` (`notify --help`, probed ONCE per
    notifier = per run, `cached_property`) AND `--notify-project-dir` is a git root that is not
    `$HOME`. Never send `--owner-notice` otherwise: airuleset exits 2 (unknown flag) or 1 (bad
    `--cwd`), and the notice would be retried forever. A plain callable notifier (tests) is never
    per condition.
  - **ONE U item per condition KIND, never per box** (owner ruling 2026-11-16, after 17 unreachable
    boxes became 17 `state-<box>` items): `fleet_notice.py` builds `fleet-down` (every box not ok,
    with its state), `fleet-stale` (drift) and `fleet-no-admin` from the FINAL status, each ONE text
    (`fleet_notice.fit`: what does not fit `CHUNK_LIMIT` becomes "… a ďalších N"; an unreachable
    box's boilerplate detail is left out so ~20 boxes fit). A FINGERPRINT per kind (down: name +
    state; stale: name + version + stable tag; no-admin: names) is kept in the status file
    `fleet_notices` once delivered: a different BOX SET re-sends the same slug (replaces; see the
    #109 bullet below), an undelivered send keeps the old one (retried). A text must hold nothing the fingerprint does not
    (no day count: "pozadu od <date>"), or the unchanged U item goes stale. An empty kind is
    cleared by `_settle` from the book; one in U but NOT in the book (its book write failed) by
    `fleet_notice.clear_unbooked` before the status write (a failed clear stays `[]` = owed, retried).
    Each kind is built on its own in `reconcile`: one that raises never blocks the others. In per-kind mode
    `check` sends no per-box message for a box going down (`fleet-down` marks it notified), and
    `drift.update` / `no_admin.update` run with `announce=False`.
  - **Only a changed BOX SET re-sends a kind (#109, owner 2026-11-17):** `reconcile` compares
    `_box_set(delivered fingerprint)` (item[0] of each item, any format) with `notice.names`. The
    same set with another state / version / org, or an older fingerprint format after an upgrade,
    stores the new fingerprint silently: an acknowledged item never comes back for the same boxes.
    `[]` (an owed clear) or a hand-edited value has no box set and is sent again. Accepted cost:
    the U text keeps its old per-box detail while the set holds.
  - `gateway-health`, `check-failed` stay their own slugs (`owner_notice.slug`; an upper-case or too long name
    gets a sha1 tail). A change TO `ok` goes out plain: it is news, not an open condition. The
    recovery title ("opäť beží") goes out plain too.
  - Migration: an old per-box slug (`state-`/`stale-`/`no-admin-<box>`) left in the book is never
    in `open_notices`, so `settle` clears it once and removes it (clearing a slug U no longer
    has is harmless).
  - Exit 1 with `--owner-notice` = not recorded = NOT delivered (retried like any other).
  - The book `owner-notices.json` (state dir) holds what is in U. `check` writes it (several
    check runs can overlap), so every send-with-slug + add and every clear + remove holds
    `NoticeBook.locked()`; `settle` re-reads the entry under the lock and skips one re-sent
    meanwhile. A failed clear stays in the book (retried); `settle` runs after drift/no-admin and
    never costs the run its status write.
  - `check._settle` judges what is open from the FINAL status (`open_notices`): a box gone from the
    key store has no open condition, so its notices are cleared. It runs AFTER the status write
    (clears can be slow; the unit has `TimeoutStartSec=300`), never on the failure path.
  - `restart-<project>` is never sent any more (#109) and is never in `open_notices`, so an old one
    in the book is cleared once and forgotten, like the old per-box slugs.
  - The fake `airuleset.py` in `fleet_harness` keeps a fake U (`footer_u`) only with
    `FAKE_NOTIFY_OWNER_NOTICE=1`; without it the flags are an argparse error, like the real one
    before #1300. `--help` is not written to the notify log.
- **Version drift (`drift.py`, #63, resolver `remoteos/release_channel.py`):**
  - `release_channel.py` is stdlib only (the per-box updater uses it too), so `version_key` is a
    small regex for `1.2.3[.devN]`, not `packaging`. Anything else is "not comparable" and is
    never judged.
  - "Newest" is by PUBLISH date, never by version. GitHub's `prerelease` flag means HELD, not
    "a dev version": every release of this repo is a `.devN`.
  - Behind since = when the OLDEST release newer than the box finished its soak (`behind_since`).
    It needs no history, and a new stable does not restart the count.
  - A re-run of an older merge publishes nothing (`create-release.sh`, `is-newest`): the resolver
    goes by publish date, so a late older release would become "newest". CI never cancels a run
    on main: each merge has its own concurrency group, because a shared group's pending run is
    cancelled by the next one, and that merge would get no release.
  - Drift can never cost the run its status write (`drift.update` catches everything, logged): a
    skipped write would re-send every state notice on every run.
  - A stable tag `version_key` refuses skips drift for the run (it must not read as "caught up").
  - Only a version read in THIS run is judged. No answer, or no release list, keeps the record's
    `drift` as it is. Reported once per episode: the record stays until a fresh version shows the
    box is not behind.
  - `probe_version` reads the public `/health` without a key. A version that is not plain
    `[0-9A-Za-z.+_-]` is dropped: it would reach the status file and Discord.
  - `check` tests never reach GitHub: `Check.releases_url` defaults to a closed port.
- **No admin (`no_admin.py`, #57):** like drift, once per episode, by name, retried while
  undelivered, kept when this run read no value. An older server's `/health` has no `admin` key:
  `probe_version` gives `None`, never `False`, so the fleet is not "reported" before its upgrade.
- **Boxes that sleep after hours (`sleep_rule.py`, #118, owner 2026-11-17):**
  - `MachineEntry.may_sleep` (bool, key store `"may_sleep"`, written ONLY when true: off = the key's
    absence, the shape older readers know). `machine set-sleep NAME on|off` (`keystore.set_sleep`, a
    no-op writes nothing), `machine list` prints `sleep on|off`. A non-bool is refused at parse.
  - Deploy order like `org`: install -> restart the gateway -> `set-sleep`. An older gateway refuses the
    unknown key (keeps its last good copy at reload, refuses at a start).
  - `check` (`sleep_rule.apply`, right after the org loop): `last_ok` on EVERY record (this run's stamp
    when ok, else carried; a pre-#118 record that was ok carries the previous `checked_at`, unless that
    run FAILED: `_report_failure` rewrites `checked_at` over old records, so it is passed as None),
    `may_sleep: true` on a flagged one, `asleep: true` while EXCUSED. Excused = not ok AND (local time <
    10:00 OR `last_ok` >= local midnight). Local = `ZoneInfo("Europe/Bratislava")`; midnight from the
    LOCAL date (`datetime.combine`), never UTC date or now-24h (DST days are tested).
  - `fleet_notice._is_down` skips `asleep is True`, so `fleet-down` and `open_slugs` drop the box; the
    plain path drops it from `pending` (its `notified` stays false, so it goes out the run it stops
    being excused). The state itself is never changed: `check` output shows `asleep (may_sleep)`.
  - Wake-up after an excused night (previous record `asleep` and not `notified`) is marked notified:
    not news, keyed on the PREVIOUS record so it holds after `set-sleep off` too. A box reported at
    10:00 announces its recovery normally. A box that stays dead leaves `fleet-down` at midnight and is
    re-sent at 10:00 (the rule is per local day; the owner's wording, a known daily churn).
  - Tests inject the clock: `run_check(now=...)`, or `monkeypatch` `check.utc_now` for the CLI
    (`tests/test_gateway_may_sleep.py`).
  - **`occasional` (#130, owner 2026-11-19, notebooks off for weeks):** `MachineEntry.occasional` (bool,
    key store `"occasional"`, written ONLY when true, and refused at parse without `may_sleep: true`);
    `MachineEntry.sleep` = `off|on|occasional` (`registry.SLEEP_MODES`) is what `set-sleep`,
    `machine list` and `check` use. `keystore.set_sleep(path, name, mode)` writes both keys through
    `replace`. `sleep_rule.excused(..., occasional=True)` = excused whenever `unreachable` (off); `401`/`error`
    (on but broken, news) fall through to the #118 rule; `apply` takes the
    mode per machine and writes `occasional: true` on the record. Wake-up of a box whose previous record
    was asleep AND occasional is never news, even if it was `notified` (reported before the switch).
    `check` output: `asleep (occasional)`. Same deploy order and older-gateway refusal as `may_sleep`.
    Tests: `tests/test_gateway_occasional_sleep.py`.
- **Restart list (`live_sessions.py` + `session_scan.py`, #66; print-only since #109):**
  - Owner ROZHODNUTÉ 2026-11-17 (#60): `report_restarts` echoes + `logger.info`s ONE text and
    notifies nobody; `sync-configs` has no notify options (`--airuleset` etc. are a usage error);
    `enroll-finish` keeps them for its `check` only. No session tracking (`start`, `--alive`,
    `still_running` are gone): never re-add a notice for this without a new owner ruling.
  - `session_scan.py` is like `fileops.py`: stdlib only, py3.8+, no package imports, its SOURCE runs
    through `Target.run_helper` as the project's account. Only that account can read
    `/proc/<pid>/cwd` and `environ`. Of `environ` it takes `TMUX` + `TMUX_PANE` only.
  - A session = comm `claude` + a `.mcp.json` in its cwd or a parent resolving to the config.
  - It runs AFTER the registry lock is released, in a `finally`: a file already written needs its
    list even if the closing registry save raised.
  - It runs only for a file really written (a dry run: would be), never for a registry-only change.
  - A failed scan is echoed and never counts as a failed project.
  - Real-process test: a shebang script named `claude` has comm `claude`
    (`tests/test_gateway_live_sessions.py`).
    Each `LiveSession` writes its OWN script (mkdtemp) and returns only after the script prints
    `ready` (#108): a shared script rewritten by the next session made the earlier shell run nothing
    and end (comm still `claude`, cwd gone, so the scan skipped it), and comm is set at exec start
    while `/proc/<pid>/environ` is filled only at its end. Never wait on comm alone.
- **Fleet server (`fleet.py`, #51 Phase 2):** `/fleet/mcp`, tools `ListMachines` / `MachineTools` /
  `Call`, FastMCP mounted by `Route(FLEET_MCP_PATH, FleetEndpoint)` before the machine route.
  - `FLEET_SERVER` is reserved: `is_valid_machine_name("fleet")` is False, so a key store or
    manifest with it is refused, and `fleet` is never in an allow-list.
  - **No second access path:** `Call` / `MachineTools` are a fastmcp `Client` over
    `httpx2.ASGITransport` to `Gateway.handle`, with the consumer's own `Authorization`. Never
    give it a client of its own to the machines: that would be a second place for the allow-list,
    key swap, retry and audit. `_admit` only gives a clear error + an audit line.
    `FleetEndpoint.__call__` reuses `authenticate` and `read_rpc`, so 401 and 413 are the same.
  - The token is re-read per tool call (`get_http_request`), so a stateless session never
    outlives a revocation. Refused machines: "not allowed" for an unknown one too (no probing),
    `record_refusal` writes the access-log line a fleet 200 would otherwise hide.
  - `ASGITransport` buffers, and a reply peaks at ~10x its wire size (measured), so `_ReplyBudget`
    caps one reply (`MAX_REPLY_BYTES`, 20 MB) and all in flight (`MAX_BUFFERED_BYTES`, 32 MB) against
    `MemoryMax=512M`. FileDownload has no cap of its own. `/<machine>/mcp` still streams.
    - A reply's bytes stay reserved until the OUTER response has gone out (`_Reservation`, via the
      ASGI scope): the parsed and re-serialised copies outlive the machine's reply. Releasing when
      the inner app returned let stalled readers reach 815 MB.
    - `SEND_TIMEOUT_SECONDS` drops a consumer that stops reading, so it cannot pin the budget.
    - Simultaneous big replies can all be refused (each holds a part): safe, and "try again" works.
  - `allow_large_replies` (`mcp_sse.py`, shared with `remoteos-call`) is needed by every in-repo
    MCP client: the SDK refuses an SSE event over 1 MiB, and its error reads like a dying box
    ("SSE stream ended without a response"). A test must use a REAL reply over 1 MiB.
  - `Call` uses `session.send_request`, never `call_tool_mcp`: the SDK validates by a `tools/list`
    AFTER the tool ran, so a box failing that turns a finished call into an error and a retry.
  - **`--log-level debug` prints payloads:** `mcp`, `fastmcp`, `httpcore2` and `sse_starlette` log
    whole messages (arguments, results, for the plain proxy too). `_PAYLOAD_LOGGERS` holds them at
    INFO; a new dependency that logs bodies goes there, the sentinel test catches it.
  - `ListMachines` reads the public `/health` only (no key) in parallel, each cut off at
    `PROBE_BOUND_SECONDS`. Any HTTP answer is `reachable`; a stale key shows on `Call`.
  - sync: `fleet` joins `desired` in gateway mode only, so a direct project gets none and `owned()`
    removes it when a project turns direct. An entry named `fleet` stays only with a url not
    ending in `/fleet/mcp`: the shape alone decides, because an old-url entry's token may be dead.
  - Tests: `RealMachine` + `LoopbackServer` (`fleet_harness.py`) are real FastMCP / uvicorn on
    loopback, in-process so caplog sees them. uvicorn keeps a connection open through
    `force_exit`, so a dying box is simulated by raising after the headers (`die_at_tool_answer`).
    Match `"content"`: `"result"` is in the initialize reply too, and the box would die there.
- **Dry run = one choke point:** `sync._Saver(dry_run=True).commit()` never writes. Registry
  edits of a dry run (narrowing, withdrawing, `--prune`) happen in memory only. Never add a
  second save path around it.
- **Import writes no empty key store** (the gateway would then serve nothing, where with no
  key store it refuses to start): zero machines, or an unreadable `--from`, exits 1. A NEW
  machine a consumer already allows but that is not exposed (a kill switch from before #51)
  stays unexposed, with a warning; the dry run lists what it would expose.
  - A machine first seen `ok` is not announced.
  - A repair stores the re-read key only if the machine accepts it AND the stored url + key did
    not change meanwhile (a concurrent `set-key` wins).
  - Nothing can make the watchdog silent (review round 6): any unexpected exception after the
    key store loads goes through `_report_failure` (announced, exit 1), and a status record
    that is not what check writes (a hand edit) is treated as unknown.
  - No single machine can blind the run (review rounds 4-5): a probe that raises
    (`gather_probes`), a key store the repair cannot write (`OSError`), a key file nested too
    deep (`RecursionError`), or any other repair exception (the guard in `observe`) becomes a
    result / 401 detail for THAT machine; the status is still written and the rest announced.
  - The notification (Slovak, phone-readable) goes out through
    `airuleset.py notify --body [--owner-name] [--owner-notice <slug> --cwd <dir>]` (#102). The unit sets
    `REMOTEOS_GATEWAY_NOTIFY_OWNER`: a headless caller has no tmux pane for airuleset to find
    the owner from.
- **Tests:** `tests/fleet_harness.py`.
  - `FakeFleetMachine` is a REAL `ThreadingHTTPServer` on 127.0.0.1, so CLI commands that run
    their own `asyncio.run` can probe it from any thread.
  - The fake `ssh` / `sudo` / `airuleset.py` log their argv. Assert that no secret is in those
    argvs: that is the "contents on stdin only" proof.
  - A CLI call from inside a running loop (the live-reload test) goes through
    `asyncio.to_thread`.

## Remove / rename (#109): `cli_machine_edit.py`, `manifest.remove_machines` / `rename_machine`

- Order: validate everything first (key store names, manifest edit computed with `dry_run=True`,
  the registry rename on a loaded copy), then direct record (`remember_retired`) -> manifest -> key
  store -> registry -> `sync_configs`. The registry is saved only when it changed (a save makes the
  gateway reload).
- **Resumable, never half-silent:** `OSError` is caught too; a failure after the first write prints
  `done: <steps> — rerun ...`. Before the registry step is done the SAME command finishes it; after
  it, every edit is in place and the advice is `sync-configs` (the record holds the retired urls).
  `remove` accepts a name the key store already lost while the manifest or registry still holds it;
  `manifest.rename_machine` is a no-op when NEW is there and OLD gone; `rename` counts as RESUMED
  (NEW in the key store, OLD gone) only with evidence of the earlier run — OLD still in the registry,
  or the url `_retire_first` recorded — so a typo for OLD next to an existing NEW is refused.
  `Registry.rename_machine(fresh=False)` is a no-op when nothing carries OLD and never refuses (a sync
  between the runs may have put NEW into a managed allow-list); a FRESH rename always runs the
  stale-NEW check, even for an OLD that nothing carries (review rounds 2-3). When nothing carries
  OLD the registry step is counted done BEFORE it runs (it still rechecks under the lock and saves
  only on a change), so any later failure points to `sync-configs`; `_retire_first` takes no lock
  when there is no direct project to record. Known edge: a key-store write that LANDED but reported an error, for an OLD
  nothing carries and no direct project, leaves no evidence; the rerun is refused and the owner
  finishes with `sync-configs`.
- `remove` = `Registry.forget_machine` (unexpose + out of EVERY allow-list, hand-made ones too:
  only narrows); refuses when a project would be left empty (the manifest forbids `[]`).
- `rename` = `Registry.rename_machine`: exposure follows (a kill-switched OLD stays unexposed as
  NEW), every consumer's OLD becomes NEW in place (bypasses `allow()`'s managed refusal on purpose:
  same box, not a widening). The key-store entry moves as is; NEW must be valid (`fleet` is not)
  and absent from key store AND manifest AND registry: a NEW left exposed / allowed by a hand removal
  is refused (carrying OLD over would hand the box to that consumer; review round 1).
- `manifest._edit_machines` edits TEXT in reverse order: a value alone on its line (+ its comma and
  trailing comment) loses the line; inline, the following comma (and the blank before a line end),
  else the preceding one; CRLF files work. `add_machine` writes through the same `_commit_edit`. Anything
  odd is caught by `_commit_edit`'s parse-check (must equal the expected manifest), never written.
- A dry run edits nothing, not even `.projects.lock` (`_manifest_lock` is a nullcontext): registry
  changes on a loaded copy, the sync dry on the edited in-memory manifest + key store.
- Fault tests use REAL failures: a lock file mode 000 (`locked_out`), a read-only config dir.
- `check` needs nothing: its status and fleet notices come from the key store, so the next run
  forgets the box (`test_after_remove_check_forgets_the_box_in_the_status_and_the_fleet_down_item`).
- Tests (`tests/test_gateway_machine_remove.py`) never touch a live machine: temp copies only.

## Enrollment (#26): `enroll.py`, `cli_enroll.py`, `manifest.add_machine`

- **The public drop lane cannot take a box's POST.** `drop-dev1` sits behind Cloudflare Access: only
  `/healthz` has a bypass, so `POST /<token>/` gets a 302 from the edge, valid token or not (measured
  2026-10-31). Hence `secret request --private` (binds tailscale) + a quick tunnel. Python's default
  user agent also gets 403 from Cloudflare: name one (`USER_AGENT`).
- **`cloudflared tunnel --config <empty file>`, always:** with `~/.cloudflared/config.yml` present (dev1
  has spinbike's) a quick tunnel answers 404 to everything, from that file's catch-all.
- **A quick tunnel's name resolves ~10 s after the log prints it, and an earlier lookup is cached
  negative:** `Tunnel.wait_for_host` sleeps `DNS_SETTLE_S` before its first probe, and a URL is printed
  only after `/healthz` answered 204 through it. `GET /` is 501 there: not a route.
- **The endpoint is airuleset's `vault_server.py`, never a receiver here:** a third credential endpoint
  would need its hardening. It stores the raw body, so the installer's JSON is one vault value and
  reaches `enroll-finish` on stdin through `secret exec --stdin` (the key is in no argv and no env).
- **Whoever holds the URL can post first** (the vault's stated threat model): the name and projects
  come from dev1's command line, the body picks neither. The key goes only to private/tailscale IPv4
  literals (`_private_ipv4`), never a public or loopback one; `--url` is the operator's override.
- `enroll-finish` stores the key and exposes a new machine before it edits the manifest, so a retry
  needs `--replace` (the printed line carries it) and still finds the machine exposed.
- TERM and HUP raise `SystemExit` while the tunnel is up (`_exit_on_signals`): python's default skips
  `finally`, and the public tunnel outlived the run.
- `add_machine` edits TEXT (comments are the owner's), through `fileops` (lock, dated backup, a
  symlink stays one), and writes only when the result parses to the same manifest plus that machine.
- Tests: the child of the fake `secret exec` is a real process, so a monkeypatch does not reach
  `enroll-finish`: machines on 127.0.0.1 go through `--url`.

- **Enroll a box behind the sites' NAT with `--url`.** A box posts the addresses it sees: strih-pp is 10.77.8.202 on its own NIC and 10.76.8.202 from dev1, so the posted address is unreachable and the finish fails (live, #26). The registration stays in the vault for an hour: rerun the printed finish command with `--url http://<name>.lan:8092/mcp`. A name must start with `win-`, `linux-` or `mac-`.

## Permanent enrollment (#101): `enroll_listen.py`, `enroll_queue.py`, `cli_enroll_queue.py`, `installers/enroll-*`

- **One fixed README line per OS** (`enroll.ps1`, `enroll-linux.sh`, `enroll.sh`, generated; the endpoint +
  dev1's public SSH key come from `installers/enroll-values.conf` through `@@NAME@@` placeholders, a
  missing value fails the build). They run the normal installer with `REMOTEOS_ENROLL` = the permanent
  URL, so the installers' enrollment code (#26) is unchanged: the URL must keep passing `enroll_url_ok` /
  `Test-RemoteOSEnrollUrl` (one path segment + `/`: `/enroll/`) and the listener must answer exactly `ok`.
- The values are PUBLIC constants (the endpoint only queues), so enroll.ps1 relaunches itself for UAC
  (`-EncodedCommand`, the ref inside) — unlike #26's one-time URL, which never crosses the elevation.
  The installer runs as a CHILD powershell (`-File`: no pause, its `exit` cannot end the wrapper); a
  piped wrapper never `exit`s (it would close the person's console). The bash child gets `</dev/null`:
  the rest of the piped script must not reach it.
- **A failed install posts its log tail** (`POST /enroll/log`, text, `X-Remoteos-Host/Os/Exit` headers,
  no JSON escaping in bash/PS); the server validates the headers (`log_meta`) and keeps the newest
  `MAX_LOGS`. `enroll-log` reads them. PowerShell: a reply with no text content type is `byte[]`.
- **The listener** binds loopback only (`_loopback_only`), `proxy_headers=False`; the poster is
  `CF-Connecting-IP` only when the peer is loopback (cloudflared). Caps: 16 KiB registration / 256 KiB
  log, enforced while streaming (a missing or lying Content-Length too; `isascii()` before `isdigit()`),
  per-poster + global sliding window keyed by `source_group` (IPv4 address, IPv6 /64: one host holds a
  /64), registrations and logs on SEPARATE limiters, `MAX_PENDING` + `MAX_PER_SOURCE` in the queue
  (review round 1: 5 addresses filled the queue for 24 h). `enroll-reject --source <ip>` / `--all` clean
  a flood. No `--log-level debug` (uvicorn would log bodies). Errors name the field
  (`parse_registration`), never the value.
- **The queue:** one 0600 file per registration (temp + rename), 0700 dirs, 24 h expiry (the key goes
  with the file), a repost of the same hostname + key is the same entry, ids are 8 hex checked before
  any path, every file is re-validated on read (a tampered one is skipped). The folders are listed
  with `os.scandir`, never `Path.glob` (it answers "none" for a mode-000 folder): an unreadable queue
  RAISES, so `check` keeps the last list instead of clearing the notice. Expiry runs inside `pending()`,
  so `check`'s 10-minute timer is the expiry clock. `enroll-log` prints control characters as `?`
  (`printable`): the text is anyone's, an ESC sequence could rewrite the operator's screen.
- **`enroll-accept`** = `finish_registration` (shared with `enroll-finish`) with `check_hostname`: the
  public `/health` of each candidate must report the posted hostname (short names compared,
  case-insensitive; a server that reports none does NOT match) BEFORE the key is sent there. Candidates
  are private/tailscale only (`candidate_urls`), `--url` the override. A failure BEFORE the key is
  stored keeps the entry; `after_store` drops it once the key store + manifest hold the machine (the
  retry is then `sync-configs`/`check`), so the closing `check` no longer lists it. **`ctx.invoke`
  fills a parameter's DEFAULT, never its envvar**: `finish_registration` passes `enroll_dir` (and
  `releases_url`) itself, else the check read dev1's real queue from the tests.
- The PowerShell relaunch command ends `$enrollCode = $LASTEXITCODE; try { Read-Host } catch {}; exit
  $enrollCode`: a window's exit code is its LAST statement's, and the Read-Host made every failure a 0.
- **The owner notice** is a fourth fleet kind, `enroll-pending`, built from `status["enroll_pending"]`
  (`[[id, hostname]]`, written by `check` from the queue; an unreadable queue keeps the last list, never
  clears). Like the other kinds: ONE item, only a changed id set re-sends, an empty queue clears it.
- **The public hostname** (`enroll-dev1.newlevel.media` -> a named tunnel -> 127.0.0.1:8817) is an
  account-level Cloudflare change: airuleset's (#1241), never minted from this project. No Access app
  covers `*.newlevel.media` (checked 2026-11-17), so the hostname needs no Access bypass.
- Tests: `test_gateway_enroll_listen.py` (real HTTP through uvicorn + the real command as a child),
  `test_gateway_enroll_accept.py`, `test_enroll_wrappers.py` (the generated bash wrappers piped into bash
  with a fake curl; also under bash 3.2 in the shell job), `tests_windows/test_enroll_ps1.py`.
