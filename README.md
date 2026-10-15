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
2. Install winremote-mcp via pip
3. Generate a random auth key
4. Create a start script and auto-start scheduled task
5. Open the firewall port
6. Start the server immediately
7. Print the exact `claude mcp add` command to run on your Linux machine

## Connect from Claude Code

After install, the script prints a command like:

```bash
claude mcp add --transport http win-mypc \
  http://192.168.1.100:8092/mcp \
  --header "Authorization: Bearer abc123..."
```

Run it on your Linux machine, restart Claude Code, and you're connected.

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
- **Console output** (Shell, the service and task helpers, Ping, Windows OCR) is read in
  the console's own code page, so on Windows `č` stays `č`.
- **It never refuses a working desktop.** A check that cannot run (an older OS, a missing
  API, a probe error) counts as "usable", and the tool goes ahead.

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
- Firewall rule is scoped to private/domain networks only (not public)
- On LAN this is fine over HTTP
- For internet exposure, use TLS reverse proxy or SSH tunnel

## Powered by

- [WinRemote MCP](https://github.com/dddabtc/winremote-mcp) - the actual MCP server (40+ tools)
- [Model Context Protocol](https://modelcontextprotocol.io) - the transport layer
