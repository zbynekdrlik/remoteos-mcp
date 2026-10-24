# WinRemote MCP Setup

One-line installer for [WinRemote MCP](https://github.com/dddabtc/winremote-mcp) on Windows machines, enabling remote control from Claude Code on Linux.

## What it does

Installs and configures WinRemote MCP server on Windows so Claude Code (running on Linux) can:

- Execute PowerShell/CMD commands as the desktop user
- Take screenshots
- Control mouse/keyboard and interact with GUI apps
- Access files, registry, services, and processes

No more SSH quoting hell or headless session limitations.

## Install

On the Windows machine, open PowerShell **as Administrator** and run:

```powershell
irm https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install.ps1 | iex
```

The installer needs an admin PowerShell and checks it before it changes anything. From a normal
window it relaunches itself elevated (one UAC prompt). It stops with
`nothing was changed on this box` instead when it runs as a non-admin account over SSH, from a
program with no console input (e.g. an agent's Shell tool), with an explicit
`REMOTEOS_MCP_AUTH_KEY` (it never crosses the elevation), or when even the relaunch is not
elevated (UAC turned off for a standard user): open PowerShell as Administrator (or use an admin
account) and run it again. Over SSH, or elevated as another account, it starts the server through
the scheduled task, in the logged-in desktop user's session (a server started from the SSH
session would die with it). Elevated as another account, Python must be installed for all users:
a per-user Python of that account is refused before anything changes.

It installs ONE commit: `REMOTEOS_MCP_REF` (a branch, tag or commit; default `main`, as on Linux)
is resolved through the GitHub API to a commit sha, and both the code and its `constraints.txt`
come from that sha. The sha is printed and written to
`C:\Users\<user>\.remoteos-mcp\installed-ref.txt`.

**Download first, install offline, roll back (#64).** Before anything on the PC changes, the
installer downloads every wheel the install needs (and builds one for every source archive, the
remoteos archive itself included) into `C:\Users\<user>\.remoteos-mcp\wheelhouse\<sha>`
(about 60 MB; the folder is owned by Administrators, the desktop user may only read it, because an
elevated pip installs from it). A failed download prints `[X] could not download ... -- nothing was changed on
this box` and the running server is untouched. Only then is the server stopped, and the install
runs with `--no-index` from that folder. A failure after the stop (pip, the import check, the
task, the firewall, the start, the health check) reinstalls the previous commit's wheelhouse
(the one `installed-ref.txt` names), restarts the server, proves its health and prints
`[X] ... -- rolled back to <old version>`; the first install with wheelhouses has nothing to go
back to and says so. After a proven install the current and the previous wheelhouse are kept and
older ones deleted. A re-run of the same commit reuses its wheelhouse (no download).

That's it. The script will:

1. Check/install Python 3.11+
2. Download remoteos-mcp and its dependencies into the commit's wheelhouse
3. Install remoteos-mcp offline from it
4. Keep the PC's auth key, or generate a random one
5. Create a start script and auto-start scheduled task
6. Open the firewall port
7. Start the server immediately, and roll back if it does not prove itself
8. Print the `claude mcp add` command to run on your Linux machine

On **macOS**: `curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install.sh | bash`.
Pin a source with `| REMOTEOS_MCP_REF=<full 40-hex commit sha | tag | branch> bash` (default `main`).

On **Linux** (systemd): `curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install-linux.sh | sudo bash`.
Pin a source with `| sudo REMOTEOS_MCP_REF=<commit sha | tag | branch> bash` (default `main`).
To install a given key, never put it on sudo's command line (`ps` shows it): `read -rs k`,
then `… | REMOTEOS_MCP_AUTH_KEY="$k" sudo --preserve-env=REMOTEOS_MCP_AUTH_KEY bash` (the key
reaches sudo's environment only; nothing is exported to curl or the rest of the shell).

### Where the installers put things (#12)

- **The auth key is never on a command line, in a service file or in the installer's output.**
  The server reads it from `REMOTEOS_AUTH_KEY`:
  - Linux: 0600 `/etc/remoteos-mcp/config.json` and the unit's 0600 `EnvironmentFile`
    `/etc/remoteos-mcp/remoteos-mcp.env`;
  - macOS: the LaunchAgent plist's `EnvironmentVariables` (plist and
    `~/.remoteos-mcp/config.json` mode 600);
  - Windows: `start-remoteos.bat` sets it before starting the server; the `.bat` and
    `config.json` in `C:\Users\<user>\.remoteos-mcp\` are readable by that user, SYSTEM and
    Administrators only.
- **A re-run keeps the key**: an explicit `REMOTEOS_MCP_AUTH_KEY`, else the key the running
  service uses, else `config.json` (Windows: then `remoteos.toml`), else a new one.
- **Linux runs a dedicated venv**, the layout camera-box's provisioning uses too:
  `/opt/remoteos-mcp-venv` (a link to the current build in `/opt/remoteos-mcp-venvs/`) and
  `remoteos-mcp.service` (`User=` the desktop user with `DISPLAY` on a desktop box, headless
  otherwise). A new source is built next to the running one and swapped in; the new install must
  answer `/health` with its version, accept the key and refuse a request without it, or the box
  goes back to the previous proven install (or, with none, is stopped and disabled: an unproven
  agent never stays up). The key goes only to the unit's own process on :8092 (the check needs
  `ss`, from iproute2). An unchanged source changes nothing.
- **Upgrading a Linux box from the old system-python install** moves it to the venv with the
  same key. The old copy in the system python is removed only after the venv has proved itself.
  The old unit showed that key to every local account (`systemctl cat`, `ps`), and the old
  installers printed it: on a box with other local accounts, or whose earlier install output was
  logged, rotate it with `REMOTEOS_MCP_AUTH_KEY` and update the clients. The same holds for a
  migrated mac (the old plist's ProgramArguments) and Windows box (the old `.bat`'s
  `--auth-key`).
- **Run the installers over SSH**, never through the agent's own Shell: on Linux that run is
  inside `remoteos-mcp.service`, whose restart would kill it, so it is refused.

## Connect from Claude Code

After install, the script prints a command like:

```bash
claude mcp add --transport http win-mypc \
  http://192.168.1.100:8092/mcp \
  --header "Authorization: Bearer <the auth key>"
```

The key itself is not printed. Read it on the machine: `Get-Content $HOME\.remoteos-mcp\config.json`
(Windows), `cat ~/.remoteos-mcp/config.json` (macOS), `sudo cat /etc/remoteos-mcp/config.json`
(Linux). Run the command with it on your Linux machine, restart Claude Code, and you're connected.

**The server names its machine at connect (#59).** The name you give it (`win-mypc`) is only a
label and can go stale (a box reinstalled from Windows to Ubuntu keeps it). The MCP
`initialize` result's `instructions` say which machine it really is and what Shell runs, e.g.
`Remote OS MCP Server on stream-snv — Ubuntu 24.04.3 LTS, x86_64. The Shell tool runs /bin/bash -c.`
(Windows: `Windows 11 (build 10.0.26100)`, a Server `Windows Server (build …)`, and
`PowerShell (powershell -NoProfile -Command, …)`; macOS: `macOS 15.1` and `/bin/zsh -c`).
`/health` carries the same `os` and `hostname` next to `status` and `version`:
`{"status":"ok","version":"…","os":"Ubuntu 24.04.3 LTS","hostname":"stream-snv"}`. `/health`
needs no key, so whoever can reach the port sees the hostname and OS.

## Restrict access per project

To limit which Claude session controls which Windows machine, use project-scoped config.

Create `.mcp.json` in your project directory:

```json
{
  "mcpServers": {
    "win-strih": {
      "type": "http",
      "url": "http://192.168.1.10:8092/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_KEY"
      }
    }
  }
}
```

Claude Code only loads MCP servers from the current project's `.mcp.json`, so different projects = different Windows machines.

## Screen coordinates (Snapshot → Click)

Snapshot and AnnotatedSnapshot shrink the screenshot to `max_width` (default 1280 px). The
input tools use the **same pixels as that image**, so a point read off the screenshot can be
passed straight to Click / Move / Scroll / Type.

- **The text starts with the coordinate contract**, for example:

  ```
  **Coordinates:** Image 1280x800 = screen 2560x1600 at origin (0,0), scale 2.000; input tools take IMAGE pixels of this frame (monitor=0, max_width=1280)
  ```

  All window rects and element centers in the text are in image pixels too.
- **The server maps every point back to the screen:** `screen = origin + round(image × scale)`,
  clamped to the captured area. The mapping is stateless: it is recomputed from `monitor` +
  `max_width` at click time. If you used a non-default `monitor` / `max_width` for the
  Snapshot, pass the same values to the input tool. The tool's reply shows the mapping:
  `[image (640,400) → screen (1280,800); scale 2.000]`.
- **`coords="screen"`** passes raw OS coordinates through unchanged. Those are physical pixels
  on Windows and Linux, and points on macOS.
- **`monitor=0`** means different things per OS:
  - Windows: the whole virtual screen. Its origin is negative when a monitor sits left of
    or above the primary.
  - Linux: the X root, all heads, sized by `xrandr --current`. Needs `x11-xserver-utils`; the
    installer adds it.
  - macOS: the main display.

  `monitor=N` is one display: `EnumDisplayMonitors` on Windows, `xrandr --listmonitors`
  on Linux, `screencapture -D N` on macOS.
- **Windows runs per-monitor-v2 DPI aware**, set before pyautogui loads. That way screenshots,
  window rects and the cursor all use physical pixels. The startup log prints
  `Windows DPI awareness: …`.
- **OCR and ScreenRecord regions are still in screen coordinates.**

## Scroll units

`Scroll(amount=3, direction="down")` turns the mouse wheel 3 **clicks** (notches), with the
same unit on every OS. `direction` is `up` / `down` / `left` / `right`, and `amount` is
clamped to 1..100.

- **Windows:** one `SendInput` wheel event of `clicks × 120`. Vertical uses
  `MOUSEEVENTF_WHEEL`, horizontal uses `MOUSEEVENTF_HWHEEL`.
- **macOS:** Quartz line events, one line per click, in events of at most 10 lines.
- **Linux:** xdotool wheel buttons 4/5/6/7, once per click.
- **Legacy calls still work:** a signed `amount` (positive = up/right, negative = down/left)
  with `horizontal=true` for left/right. An explicit `direction` wins over both.

## Typing text (Unicode)

`Type("Žltý kôň ďakuje – €")` types any Unicode text exactly, whatever the keyboard layout:
diacritics, symbols and emoji. `\n` (also `\r\n`) presses Enter and `\t` presses Tab.

- **Windows:** `SendInput` Unicode packets (`KEYEVENTF_UNICODE`), one down/up per UTF-16
  code unit, so emoji go as surrogate pairs.
- **macOS:** Quartz keyboard events that carry the text, at most 20 UTF-16 units per event.
  The process needs the Accessibility permission (System Settings › Privacy & Security).
- **Linux:** `xdotool type`.
- **The reply counts what was handed to the OS.** `Typed N chars` means N characters were
  handed over.
  - **Windows:** if `SendInput` refuses input part-way (a locked or disconnected session),
    the reply is an error that says how many got through.
  - **macOS:** refuses up front, when the process may not post keyboard events. Nothing is
    then clicked, typed or pasted.
  - **Windows and macOS:** a locked screen is refused before anything is typed (see
    [Tool errors](#tool-errors-and-desktop-diagnoses)). On Linux X11, typed input reaches
    the screen locker.
- **`method="paste"` (opt-in):** for an app that ignores typed keystrokes.
  - It saves the clipboard text, pastes with Ctrl+V (Command+V on macOS), then restores the
    clipboard.
  - Only text can be restored. When the clipboard held none, the reply says so.
  - The reply means the paste keys were pressed; it does not confirm the app took the text.
  - Not for secrets: clipboard history and sync keep a copy.

## Tool errors and desktop diagnoses

A tool that fails returns an MCP **error result** (`isError: true`), never a successful
reply with error text in it. The client shows it as a tool error. The text says what is
wrong, and for a tool call it keeps the task id, e.g.
`[task:1a2b3c4d5e6f] Shell error: working directory not found: C:\no such dir`.

Before a desktop tool touches the screen, it checks that the desktop can be used:

- **Screen tools** (Snapshot, AnnotatedSnapshot, OCR, ScreenRecord) and **input tools**
  (Click, Move, Type, Scroll, Shortcut, MinimizeAll).
- **Windows:**
  - the server runs in session 0: start it with `schtasks /Run /TN RemoteOSMCP`;
  - the server runs in a non-interactive window station;
  - the session is disconnected: run **ReconnectSession**. It attaches the server's own
    session to the console, then checks it worked. The screen and input tools refuse a
    disconnected session instead of moving it, because a reconnect puts the user's desktop
    on the machine's physical screen;
  - the screen is locked, or a secure desktop (UAC prompt, Ctrl+Alt+Del) is showing.
- **macOS:** no GUI session, the session is not on the screen (fast user switching), or the
  screen is locked. Also a missing permission: **Screen Recording** for the screen tools,
  **Accessibility** for the input tools. The message names the Python binary to allow.
- **Linux:** no X11 session. The headless/Wayland stub text itself is unchanged, and
  Snapshot, ScreenRecord and OCR now return the bare stub instead of `<Tool> error: <stub>`.
  A locked X11 screen is not refused: the screenshot shows the locker, and typed input
  reaches it.
- `Snapshot(use_vision=False)` captures nothing, so it needs only the user's desktop, locked
  or not. Its window list works on a disconnected or locked session too. It is refused only
  in these cases: session 0, a non-interactive window station, no macOS GUI session, a macOS
  session that is not on the screen, or no X11 session.
- **It never refuses a working desktop.** A check that cannot run (an older OS, a missing
  API, a probe error) counts as "usable", and the tool goes ahead.

**Console output on Windows.** The output of PowerShell comes back as exact Unicode on every
Windows code page: `Žltý kôň ďakuje – €` stays exactly that, error text (`Write-Error`) and
parse errors included. This covers Shell, the service, task and EventLog helpers, app launch,
notifications and Windows OCR. On macOS and Linux, output is read in the locale's encoding
(UTF-8 on the fleet).

- Each command runs in its own hidden console. So a `chcp` inside one Shell command cannot
  change how later replies are decoded.
- That console is switched to UTF-8 BEFORE PowerShell starts, by a small `python.exe` launcher
  from the server's own Python folder. Your command reaches PowerShell unchanged: no code is
  added to it, so error messages, exit codes, `param(...)` at the start of a script, and
  Constrained Language Mode behave exactly as in plain PowerShell.
- Programs started from the command share that console, so a program that writes through the
  console (`cmd /c echo Žltý kôň`) is exact too.
- Text that PowerShell pipes INTO a native program (`"čaká" | findstr "."`) uses Windows
  PowerShell's default, ASCII: each non-ASCII character arrives as `?`.
- A line that is not UTF-8 is read on its own in the system's OEM code page, so it is exact
  only for the characters that page holds. Example: a native program that writes the OEM page
  whatever the console's page is.
- Such a native program's output loses its non-ASCII text when PowerShell itself captures it,
  in a pipeline, an assignment or `| Out-String`. PowerShell decodes it as UTF-8 then.
- The UTF-8 page also applies to programs started from the command. For example, cmd.exe reads
  a `.bat` file in the console's code page, which is UTF-8.
- A command that times out is stopped together with the processes it started (its process
  tree on Windows, its process group elsewhere; best effort).
- A command never reads the server's own input: its stdin is empty.
- The launcher takes about 700 characters, plus the working directory, of Windows'
  32 767-character command line, so the longest possible command is that much shorter than
  before.
- If PowerShell cannot be started at all, the tool reports an error, never a command result.
- Ping runs without PowerShell and is read in the OEM code page. `tscon` also runs without
  PowerShell; it is read UTF-8 first, and a line that is not UTF-8 in the OEM code page.
- Real Windows PowerShell 5.1 checks in CI (`tests_windows`, an en-US runner whose OEM code
  page is 437): exact output, `cmd /c echo`, `Write-Error` text, exit codes, `param(...)`, a parse
  error, and that a timed-out PowerShell is gone. The other points above are verified on the
  fleet only.

**Shell on Linux: the user's session.** The Linux service is a systemd system unit, which has no
login-session environment. A Shell command still gets the session of the user the service runs
as, so `systemctl --user`, `journalctl --user` and GUI programs work through MCP (no SSH needed):

- `XDG_RUNTIME_DIR`, when `/run/user/<uid>` exists (any login or linger);
- `DBUS_SESSION_BUS_ADDRESS`, when its `bus` socket exists too;
- `DISPLAY` and `XAUTHORITY` of that same user's graphical session (the one found when the
  service started) when its X socket exists, plus `WAYLAND_DISPLAY` in a Wayland session. The
  service starts at boot, so after a reboot a login that came later is picked up only when the
  service restarts.

A variable the service already has with a value is never changed; an empty one is filled in. A
service running as root gets none of them: it never borrows another user's session. (One known
exception, older than this feature: a Shell call made at the very moment an AnnotatedSnapshot
sets up its accessibility connection can copy that session's variables.)

Start a GUI program detached, so it outlives the Shell call: `setsid -f obs >/dev/null 2>&1`, or
`systemd-run --user obs`. A plain `obs &` keeps the call's output open: the call waits until its
timeout and is then stopped together with everything it started, the program included.

With `DISPLAY` set, a command that would prompt (an `ssh` host-key question or passphrase,
`sudo -A`) may open a dialog on the screen (`ssh-askpass`) and wait there until the timeout,
instead of failing at once. Run such commands non-interactively: `ssh -o BatchMode=yes`, or
`SSH_ASKPASS_REQUIRE=never` before the command. The session bus can do the same: a program that
asks a locked keyring for a secret (git's credential helper, `secret-tool`) makes the keyring
show its unlock dialog on the screen. Run it with `env -u DBUS_SESSION_BUS_ADDRESS`, or with a
short `timeout`.

## MCP gateway (dev1)

The machines are reachable only from boxes with a LAN route to them. `remoteos-gateway`
runs on dev1, which has that route, and lets a **remote fleet consumer** reach a machine's
MCP. Example consumer: `david3@subdev` → `win-pjsnvs-pokladna`. The machine key never
leaves dev1.

- **Listens on dev1's tailscale address only:** `100.104.8.125:8796`. It refuses
  `0.0.0.0` / `::`.
- **One URL per machine:** `http://100.104.8.125:8796/<machine>/mcp`. The machine name is
  its name in the key store, `~/.config/remoteos-gateway/machines.json` (#51).
- **Per-consumer tokens, registered by hash only.** The gateway checks the consumer's
  `Authorization: Bearer <token>`. It then forwards the request with the machine's own key,
  read from the key store (never copied). The consumer token is never forwarded.
- **Every project of the owner goes through it too (#51):** each project's `.mcp.json` is
  GENERATED from one manifest, so a machine key lives in ONE place. See
  [Every project through the gateway](#every-project-through-the-gateway-51).
- **Transparent streaming proxy.** JSON and SSE responses stream back unchanged, including
  `Mcp-Session-Id`.

| Answer | Meaning |
|---|---|
| 401 | missing token, or its sha256 is not registered |
| 403 | consumer known, but not allowed that machine |
| 404 | machine not exposed (or unknown path) |
| 503 + JSON-RPC `-32000` | the machine is down / unreachable, or missing from the master config |

### Run it

Install into a **dedicated venv** with the pinned `constraints.txt`. Never install into the
user's shared site-packages: that would move `fastmcp` / `mcp` for every other tool of that
account.

```bash
python3 -m venv ~/.local/share/remoteos-gateway/venv
~/.local/share/remoteos-gateway/venv/bin/pip install \
  -c https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/constraints.txt \
  "git+https://github.com/zbynekdrlik/remoteos-mcp.git@main"
ln -sf ~/.local/share/remoteos-gateway/venv/bin/remoteos-gateway ~/.local/bin/remoteos-gateway
cp deploy/remoteos-gateway.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now remoteos-gateway
loginctl enable-linger "$USER"
curl -s http://100.104.8.125:8796/healthz      # {"status":"ok","version":"..."}
```

To upgrade, re-run the `pip install` line with `--force-reinstall` added. pip does not
re-fetch a git URL whose version is already installed. Then run
`systemctl --user restart remoteos-gateway` and check that `/healthz` shows the new version.

### Add a consumer

1. **The consumer**, on its own box, generates a random token and keeps it `0600` in its
   home. It sends **only the sha256** of the token (through a ticket).
   - The hash is over the exact token bytes, with no trailing newline:

     ```bash
     umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > ~/.remoteos-gateway.token
     sha256sum < ~/.remoteos-gateway.token        # 64 hex chars — this is what you send
     ```

2. **On dev1**, register it and grant a machine. `consumer add` refuses anything that is not
   64 hex characters, so a raw token can never be registered by mistake.

   ```bash
   remoteos-gateway consumer add david3@subdev --sha256 <64-hex>
   remoteos-gateway expose win-pjsnvs-pokladna          # once per machine
   remoteos-gateway allow david3@subdev win-pjsnvs-pokladna
   ```

3. **The consumer's** `.mcp.json`:

   ```json
   "win-pjsnvs-pokladna": {"type": "http",
     "url": "http://100.104.8.125:8796/win-pjsnvs-pokladna/mcp",
     "headers": {"Authorization": "Bearer <its own token>"}}
   ```

### Revoke, inspect

```bash
remoteos-gateway deny david3@subdev win-pjsnvs-pokladna    # take one machine away
remoteos-gateway consumer revoke david3@subdev             # remove the consumer entirely
remoteos-gateway consumer revoke david3@subdev --sha256 <hex>   # retire one token (rotation)
remoteos-gateway unexpose win-pjsnvs-pokladna              # kill switch for everyone
remoteos-gateway list                                      # machines, consumers, hash prefixes
remoteos-gateway log --consumer david3@subdev --tail 50    # who called what, when
```

Every edit is written atomically to `~/.config/remoteos-gateway/registry.json` (`0600`). The
running gateway applies it on the next request, so no restart is needed. To force a re-read,
run `systemctl --user reload remoteos-gateway` (SIGHUP).

A registry that turns invalid is never applied: the last good copy stays and an error is
logged. A deleted registry denies everything.

**Logs:**

- `journalctl --user -u remoteos-gateway` — operational log: decisions, reloads, errors.
- `~/.local/state/remoteos-gateway/access.jsonl` — one line per request: time, consumer,
  machine, JSON-RPC method, tool name, status, duration. It never holds a token, a key, or
  tool arguments. It rotates to `.1` at 20 MiB.

### Every project through the gateway (#51)

Before #51, every project config held its own copy of each machine key. A key change made
every copy stale, and every running Claude Code session kept sending the old key until the
owner reconnected it. Now:

- **One key store** — `~/.config/remoteos-gateway/machines.json` (`0600`, dir `0700`), on
  dev1: `{"<machine>": {"url": "http://<box>:8092/mcp", "key": "<key>"}}`. It is the
  gateway's machine source (`serve --machines`, the default).
- **One manifest** — `~/.config/remoteos-gateway/projects.toml`: per project, where its
  config lives and which machines it may reach.
- **Generated configs** — `remoteos-gateway sync-configs` writes each project's config. Each
  fleet entry keeps its server NAME (so `mcp__win-stream-snv__Shell` stays) and points at the
  gateway with the project's own token. Every other entry (n8n, playwright, …) stays byte-equal.
- **A drift watchdog** — `remoteos-gateway check`, every 10 minutes (a user timer).

| Task | Do this |
|---|---|
| A machine's key changed | `remoteos-gateway machine set-key <machine>` with the key on stdin. Every project routed through the gateway gets it on its next call: no config change, no reconnect. The direct remoteos-mcp copy is refreshed by `remoteos-gateway sync-configs`. |
| Add a machine to a project | Add it to the project's `machines` in `projects.toml`, then `remoteos-gateway sync-configs`. |
| Add a NEW machine | `remoteos-gateway machine set-key <machine> --url http://<box>:8092/mcp` (key on stdin), then the step above. |
| Add a project | One `[projects.<name>]` table in `projects.toml`, then `remoteos-gateway sync-configs`. |
| See what would change | `remoteos-gateway sync-configs --dry-run` (every key and token masked). |
| See the machines | `remoteos-gateway machine list` (keys masked) and `~/.local/state/remoteos-gateway/machines-status.json`. |

The manifest:

```toml
[gateway]
url = "http://100.104.8.125:8796"

[projects.camera-box]
host = "dev1"                      # where the project's sessions run (dev1 | dev2)
account = "newlevel"               # whose ~/.claude it is
config = "~/.claude/mcp-configs/camera-box.mcp.json"
machines = ["win-stream-snv", "linux-cam1", "linux-cam2"]

[projects.remoteos-mcp]            # keeps DIRECT entries: it debugs machines when the gateway is down
host = "dev1"
account = "newlevel"
config = "~/.claude/mcp-configs/remoteos-mcp.mcp.json"
mode = "direct"
machines = ["win-stream-snv", "linux-cam1", "linux-cam2"]
```

**How `sync-configs` behaves:**

- **The consumer.** For each project it keeps a consumer `<project>@<host>` in the registry,
  marked `"managed_by": "sync-configs"`.
  - Its token is minted once and lives only in the generated file.
  - Its allow-list is EXACTLY the manifest's `machines`: a machine removed there leaves the
    allow-list AND the config.
  - A managed consumer whose project left the manifest loses every machine but keeps its token,
    so putting the project back needs no new token and no restart. `sync-configs --prune`
    removes such consumers. A manifest with no projects at all is refused.
  - A hand-made consumer of the same name is never taken over.
  - A write that fails after it may have landed (an ssh drop) keeps the last confirmed AND the
    new token registered; the next run keeps whichever the file holds. However many writes
    fail, the token the file holds stays registered and no more than two are.
- **What it writes.** It writes only fleet entries, and only when an entry changes: a config
  whose entries are already right keeps its own layout, byte for byte. It first backs the file
  up to `.backup-<date>/` next to it, then writes atomically with mode `0600`. A second run
  changes nothing. The backups hold the OLD keys and tokens: delete them once the migration
  is verified.
- **What it leaves alone.** An entry it did not generate stays as it is, even when its name
  is in the key store, and it is reported (`! … not in the manifest`). For example, a direct
  entry for a machine that dev1 cannot reach.
- **Where it writes.**
  - dev2 configs: over `ssh -o BatchMode=yes <account>@dev2`.
  - Another account's configs: over `sudo -n -u <account>`.
  - The file content travels on stdin only. When that access is missing, the command prints
    the exact one-time step (`ssh-copy-id …`, or the sudoers line) and syncs the other
    projects.
- **Exposure.** It never re-exposes a machine (`unexpose` stays a kill switch); it warns
  instead. `machine import` and `machine set-key --url` expose only machines NEW to the key
  store, and never one a consumer already allows while it is unexposed (a kill switch engaged
  before #51); the import's dry run lists what it would expose.
- **Fail closed.** A manifest machine missing from the key store, an invalid or empty manifest
  or key store, or `mode = "direct"` for any account but the gateway's own on dev1 refuses
  the whole run before anything changes. An unparseable config is never overwritten.
- After a project's config changed, its RUNNING sessions need ONE restart (Claude Code reads
  `.mcp.json` only at start). After that, a key change never reaches a session again.

**`check` (the watchdog):**

- **What it probes.** Every key-store machine directly with its stored key (`initialize`),
  plus the gateway's `/healthz`.
- **What it records.** `ok` / `401` / `unreachable` / `error`, with `since`, in
  `machines-status.json`.
- **Notifications.** It announces each state change ONCE, through
  `~/devel/airuleset/airuleset.py notify --body` (owner from
  `REMOTEOS_GATEWAY_NOTIFY_OWNER`). Without airuleset it writes a log line instead. A
  notification that did not go out (airuleset did not print `sent`) is retried on the next
  run. A check that cannot run at all (an invalid key store) is announced once too, exits 1,
  and its recovery is announced.
- **Auto-repair of a 401** happens only for a machine with an explicit recipe:
  `remoteos-gateway machine repair-recipe linux-cam2 --ssh root@cam2.lan [--sudo]`.
  - The recipe reads the box's installer `config.json` (`/etc/remoteos-mcp/config.json`)
    over ssh.
  - It stores the key only if the machine accepts it.
  - Without a recipe, a 401 is only reported, never guessed.

```bash
cp deploy/remoteos-gateway-check.service deploy/remoteos-gateway-check.timer ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now remoteos-gateway-check.timer
journalctl --user -u remoteos-gateway-check          # each run's table
```

**Migrating from hand-written configs (once, on dev1):**

1. Upgrade the gateway venv (the `pip install … --force-reinstall` line above). Do NOT restart
   it yet: the new version reads the key store by default.
2. Write `projects.toml` from today's configs (one table per project, `machines` = its fleet
   entries).
3. Build the key store: `remoteos-gateway machine import --from-manifest --dry-run`, then
   without `--dry-run`. For each machine, the copy the machine accepts wins; every conflict
   and every machine it could not verify is listed. A machine the key store already has
   keeps its entry unless a copy AT ITS URL verifies (a copy at another url needs
   `--accept-url-change`). An unreadable `--from` file, or no machine at all, writes nothing
   and exits 1.
4. `systemctl --user restart remoteos-gateway` (it now reads the key store), then check
   `/healthz`.
5. `remoteos-gateway sync-configs --dry-run`, review, then `remoteos-gateway sync-configs`.
6. Install the check timer, and restart each project's sessions once.

**Rolling back** to a gateway older than 0.7.0.dev33 after `sync-configs` has run: the old
version rejects the registry's `managed_by` field (an unknown key) and refuses to start. Drop
the field from `~/.config/remoteos-gateway/registry.json` first, and point `--machines` back
at `~/.claude/mcp-configs/remoteos-mcp.mcp.json` (the old default).

## Uninstall

```powershell
irm https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/uninstall.ps1 | iex
```

## Security

- Auth key is required for all connections
- The key is kept in owner-only files and reaches the server through `REMOTEOS_AUTH_KEY`, never
  its command line (`ps`, Task Manager), a unit file or the installer output. The server drops
  it from its environment at start, so no tool child inherits it; its initial process
  environment (`/proc/<pid>/environ`) still holds it, readable by root and by the account the
  server runs as (on a desktop box: the desktop user's own programs)
- The Windows firewall rule allows inbound TCP 8092 on all profiles (it survives network
  drift); the server binds 0.0.0.0, so keep the machines on a trusted network
- On LAN this is fine over HTTP
- For internet exposure, use TLS reverse proxy or SSH tunnel
- **Bind guard (#39):** a non-loopback bind (`0.0.0.0`, a LAN or tailnet address) with no auth
  key refuses to start (exit 2) while any Tier 3 tool (Shell, files, services) is enabled. With
  Tier 1/2 only it starts with a warning. `--enable-all` with a key, the way every installer
  starts, is unaffected
- **Caller values never become code (#39):** a value put into a PowerShell script (service and
  task names, EventLog's log name, App's path and arguments) is a single-quoted literal; the
  toast title and message reach PowerShell in environment variables. macOS AppleScript strings
  escape `\` before `"`
- **Scrape (#39):** http(s) only, at most 5 MiB read (a larger body is an error; a redirect's own
  body is never read). LAN and private
  addresses are allowed; `--scrape-block-private` (or `scrape_block_private = true` under
  `[security]` in `remoteos.toml`) refuses them, every resolved address and redirect checked
- **OAuth (optional, off unless `--oauth-client-id`/`--oauth-client-secret`, #39):** PKCE S256
  only; redirect URIs must be `https://` or loopback `http://`; a configured client secret is
  required at the token endpoint and never returned by registration. The authorize endpoint
  approves every client, so OAuth WITHOUT `--oauth-client-secret` lets anyone obtain a token
  (accepted next to the auth key): never turn it on without a secret (the server warns at start)
- **App (Windows):** the name and the arguments are taken literally since #39: `$env:VAR` or
  `$(...)` in them is no longer expanded by PowerShell
- **`remoteos.toml` booleans** are real TOML booleans or the strings `true`/`false`/`1`/`0`/
  `yes`/`no`; anything else stops the server with an error naming the key (#39)
- **Dependency audit (#39):** CI runs `pip-audit -r constraints.txt` (Python 3.12 on Linux,
  Windows and macOS, and 3.11 on Linux) with no ignore list; a finding is fixed by upgrading the
  pin. Python 3.11 is the floor since #64 (Pillow 13, the fix of its CVEs, needs it), so every
  supported pin set is audited

## Powered by

- [WinRemote MCP](https://github.com/dddabtc/winremote-mcp) - the actual MCP server (40+ tools)
- [Model Context Protocol](https://modelcontextprotocol.io) - the transport layer
