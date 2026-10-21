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

That's it. The script will:

1. Check/install Python 3.10+
2. Install remoteos-mcp via pip
3. Keep the PC's auth key, or generate a random one
4. Create a start script and auto-start scheduled task
5. Open the firewall port
6. Start the server immediately
7. Print the `claude mcp add` command to run on your Linux machine

On **macOS**: `curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install.sh | bash`.

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
  its key in `~/.claude/mcp-configs/remoteos-mcp.mcp.json`.
- **Per-consumer tokens, registered by hash only.** The gateway checks the consumer's
  `Authorization: Bearer <token>`. It then forwards the request with the machine's own key,
  read from the master MCP config (never copied). The consumer token is never forwarded.
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

## Powered by

- [WinRemote MCP](https://github.com/dddabtc/winremote-mcp) - the actual MCP server (40+ tools)
- [Model Context Protocol](https://modelcontextprotocol.io) - the transport layer
