"""remoteos-mcp — CLI entry point: the server's start-up and its subcommands."""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys as _sys

import click
from click.core import ParameterSource
from starlette.middleware import Middleware

import remoteos.tools  # noqa: F401  (registers every tool on mcp)
from remoteos import __version__, app
from remoteos.app import log, mcp
from remoteos.config import discover_config_path, load_config
from remoteos.platform import is_linux, is_macos, is_windows
from remoteos.security import IPAllowlistMiddleware, check_bind_exposure, parse_ip_allowlist
from remoteos.taskmanager import manager as task_manager
from remoteos.tiers import (
    ALL_TOOLS,
    LINUX_ALWAYS_EXCLUDED_TOOLS,
    LINUX_EXCLUDED_TOOLS,
    get_tier_names,
    linux_platform_excludes,
    parse_tool_csv,
    resolve_enabled_tools,
)

# ====================== Apply task manager wrapping ========================


def _get_registered_tools() -> dict[str, object]:
    """Return tool-name -> tool object map across fastmcp 2.x/3.x internals."""
    # fastmcp 2.x
    tool_mgr = getattr(mcp, "_tool_manager", None)
    tools = getattr(tool_mgr, "_tools", None)
    if isinstance(tools, dict):
        return tools

    # fastmcp 3.x
    provider = getattr(mcp, "_local_provider", None)
    components = getattr(provider, "_components", None)
    if isinstance(components, dict):
        out: dict[str, object] = {}
        for comp_key, comp in components.items():
            if not isinstance(comp_key, str) or not comp_key.startswith("tool:"):
                continue
            name = getattr(comp, "name", None)
            if not isinstance(name, str) or not name:
                name = comp_key.split(":", 1)[1].split("@", 1)[0]
            out[name] = comp
        return out

    raise RuntimeError("Unsupported fastmcp internals: cannot locate registered tools")


def _remove_tool(name: str) -> None:
    """Remove a tool by name across fastmcp 2.x/3.x internals."""
    # fastmcp 2.x
    tool_mgr = getattr(mcp, "_tool_manager", None)
    tools = getattr(tool_mgr, "_tools", None)
    if isinstance(tools, dict):
        tools.pop(name, None)
        return

    # fastmcp 3.x
    provider = getattr(mcp, "_local_provider", None)
    components = getattr(provider, "_components", None)
    if isinstance(components, dict):
        keys_to_remove = [
            k
            for k, v in components.items()
            if isinstance(k, str)
            and k.startswith("tool:")
            and ((getattr(v, "name", None) == name) or k.split(":", 1)[1].split("@", 1)[0] == name)
        ]
        for k in keys_to_remove:
            components.pop(k, None)
        return


def _wrap_all_tools():
    """Wrap all registered MCP tools with task manager for error resilience + concurrency."""
    # Skip wrapping the task management tools themselves
    skip = {"CancelTask", "GetTaskStatus", "GetRunningTasks"}
    for name, tool in _get_registered_tools().items():
        if name in skip:
            continue
        original_fn = getattr(tool, "fn", None)
        if callable(original_fn):
            tool.fn = task_manager.wrap_sync_tool(name, original_fn)


_wrap_all_tools()

if is_macos():
    _remove_tool("ReconnectSession")


def _param_explicit(ctx: click.Context, name: str) -> bool:
    src = ctx.get_parameter_source(name)
    return src in {ParameterSource.COMMANDLINE, ParameterSource.ENVIRONMENT}


def _choose_value(ctx: click.Context, name: str, cli_value, config_value, default_value):
    if _param_explicit(ctx, name):
        return cli_value
    if config_value is not None:
        return config_value
    return default_value


def _apply_tool_filter(enabled_tools: set[str]) -> None:
    for tool_name in list(_get_registered_tools().keys()):
        if tool_name not in enabled_tools:
            _remove_tool(tool_name)


def _enable_debug_logging() -> None:
    """``--debug`` (upstream winremote pull 65): the ``remoteos`` loggers at DEBUG on stderr.

    Only our own logger tree. The root logger and FastMCP stay at their levels:
    at DEBUG, FastMCP and sse_starlette log every tool call's arguments and reply
    (typed text, clipboard, Shell commands, FileUpload data) — #31 review. stderr,
    because in stdio mode stdout is the JSON-RPC stream.
    """
    handler = logging.StreamHandler(_sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    ours = logging.getLogger("remoteos")
    ours.addHandler(handler)
    ours.setLevel(logging.DEBUG)
    log.debug("debug logging on")


# ================================== CLI ====================================


@click.group(invoke_without_command=True)
@click.option("--transport", default="streamable-http", type=click.Choice(["stdio", "streamable-http"]))
@click.option("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1; use 0.0.0.0 for remote access)")
@click.option("--port", default=8092, type=int)
@click.option(
    "--reload",
    is_flag=True,
    default=False,
    help="No effect, kept so existing command lines still start: uvicorn cannot hot-reload the in-process server",
)
@click.option(
    "--debug",
    is_flag=True,
    default=False,
    help=(
        "Debug logging on stderr for remoteos and uvicorn "
        "(never typed text, clipboard, Shell commands, file data or tool replies)"
    ),
)
@click.option("--auth-key", default=None, envvar="REMOTEOS_AUTH_KEY", help="API key for authentication")
@click.option("--config", default=None, help="Path to remoteos.toml config file")
@click.option(
    "--enable-all",
    is_flag=True,
    default=False,
    help="Enable all tools including high-risk Tier 3 tools (backward-compatible)",
)
@click.option("--enable-tier3", is_flag=True, default=False, help="Enable tier3 destructive tools")
@click.option("--disable-tier2", is_flag=True, default=False, help="Disable tier2 interactive tools")
@click.option("--tools", default="", help="Comma-separated tools to enable (highest precedence)")
@click.option("--exclude-tools", default="", help="Comma-separated tools to disable")
@click.option("--ip-allowlist", default="", help="Comma-separated IPs/CIDRs allowed to access HTTP transport")
@click.option("--ssl-certfile", default=None, help="Path to SSL certificate file for HTTPS")
@click.option("--ssl-keyfile", default=None, help="Path to SSL private key file for HTTPS")
@click.option("--oauth-client-id", default=None, envvar="REMOTEOS_OAUTH_CLIENT_ID", help="OAuth client ID whitelist")
@click.option("--oauth-client-secret", default=None, envvar="REMOTEOS_OAUTH_CLIENT_SECRET", help="OAuth client secret")
@click.option(
    "--scrape-block-private",
    is_flag=True,
    default=False,
    help="Scrape refuses private/LAN/loopback addresses (every resolved address checked); default: allowed",
)
@click.pass_context
def cli(
    ctx,
    transport: str,
    host: str,
    port: int,
    reload: bool,
    debug: bool,
    auth_key: str | None,
    config: str | None,
    enable_all: bool,
    enable_tier3: bool,
    disable_tier2: bool,
    tools: str,
    exclude_tools: str,
    ip_allowlist: str,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
    oauth_client_id: str | None,
    oauth_client_secret: str | None,
    scrape_block_private: bool,
):
    """Start the remoteos MCP server."""
    # The installers hand the key over in REMOTEOS_AUTH_KEY, never an argv (#12). click has read
    # it (and the OAuth secret) by now: drop both, so no child (Shell, App, ...) inherits them.
    # (The process's initial environment, /proc/<pid>/environ, keeps them: root and this
    # account can read that, as they can read the 0600 key files.)
    for secret in ("REMOTEOS_AUTH_KEY", "REMOTEOS_OAUTH_CLIENT_SECRET"):
        os.environ.pop(secret, None)
    if ctx.invoked_subcommand is not None:
        return  # subcommand will handle it

    if debug:
        _enable_debug_logging()
    if reload:
        # FastMCP serves an app OBJECT through uvicorn.Server; uvicorn reloads only an
        # import-string app via uvicorn.run (#31), so passing reload would do nothing.
        click.echo(
            "  WARNING: --reload has no effect: uvicorn cannot hot-reload the in-process server. "
            "Restart the service to load new code.",
            err=True,
        )

    config_path = discover_config_path(config)
    cfg = load_config(config_path)

    host = _choose_value(ctx, "host", host, cfg.server.host, "127.0.0.1")
    port = int(_choose_value(ctx, "port", port, cfg.server.port, 8092))
    auth_key = _choose_value(ctx, "auth_key", auth_key, cfg.server.auth_key, None)
    ssl_certfile = _choose_value(ctx, "ssl_certfile", ssl_certfile, cfg.server.ssl_certfile, None)
    ssl_keyfile = _choose_value(ctx, "ssl_keyfile", ssl_keyfile, cfg.server.ssl_keyfile, None)
    if bool(ssl_certfile) != bool(ssl_keyfile):
        # one of the pair used to drop TLS silently and serve plain HTTP (#31)
        raise click.UsageError("--ssl-certfile and --ssl-keyfile must be given together (CLI or remoteos.toml)")
    oauth_client_id = _choose_value(ctx, "oauth_client_id", oauth_client_id, cfg.security.oauth_client_id, None)
    oauth_client_secret = _choose_value(
        ctx,
        "oauth_client_secret",
        oauth_client_secret,
        cfg.security.oauth_client_secret,
        None,
    )

    enable_tier3 = bool(_choose_value(ctx, "enable_tier3", enable_tier3, cfg.security.enable_tier3, False))
    disable_tier2 = bool(_choose_value(ctx, "disable_tier2", disable_tier2, cfg.security.disable_tier2, False))
    scrape_block_private = bool(
        _choose_value(ctx, "scrape_block_private", scrape_block_private, cfg.security.scrape_block_private, False)
    )
    from remoteos import scrape as _scrape

    _scrape.configure(scrape_block_private)

    cli_tools = parse_tool_csv(tools)
    cli_excluded = parse_tool_csv(exclude_tools)
    cli_allowlist = parse_tool_csv(ip_allowlist)

    selected_tools = cli_tools if _param_explicit(ctx, "tools") else cfg.tools.enable
    excluded_tools = cli_excluded if _param_explicit(ctx, "exclude_tools") else cfg.tools.exclude
    allowlist_entries = cli_allowlist if _param_explicit(ctx, "ip_allowlist") else cfg.security.ip_allowlist

    # On Linux, gate the display-dependent tools on a runtime session check:
    # a machine with an active X11 session (e.g. imag-nb) gets the full desktop
    # tool set; a headless box (cam1..4) keeps them excluded — zero regression;
    # on Wayland they stay and say what is missing (#38).
    # Status lines go to stderr: in stdio mode stdout is the JSON-RPC stream (#31 review).
    platform_excludes = None
    if is_windows():
        click.echo(f"  Windows DPI awareness: {app._WIN_DPI_MODE}", err=True)
    if is_linux():
        platform_excludes = set(LINUX_ALWAYS_EXCLUDED_TOOLS)
        try:
            from remoteos.platform.linux import desktop as _linux_desktop

            session = _linux_desktop.get_session()
            platform_excludes = linux_platform_excludes(session.mode)
            click.echo(
                f"  Linux session: {session.mode}" + (f" (display={session.display})" if session.mode == "x11" else ""),
                err=True,
            )
        except Exception as exc:  # pragma: no cover - defensive
            click.echo(f"  Linux session detection failed ({exc}); excluding display tools", err=True)
            platform_excludes = set(LINUX_EXCLUDED_TOOLS)
    enabled_tools = resolve_enabled_tools(
        enable_tier3=enable_tier3,
        disable_tier2=disable_tier2,
        enable_all=enable_all,
        explicit_tools=selected_tools,
        exclude_tools=excluded_tools,
        platform_excludes=platform_excludes,
    )
    _apply_tool_filter(enabled_tools)
    enabled_tiers = get_tier_names(enabled_tools)

    # #39 (upstream PR #76): an unauthenticated non-loopback bind with Tier 3 (Shell, files,
    # services) would be an open full shell on the network, so it is refused; Tier 1/2 warns.
    bind_warning = None
    if transport != "stdio":
        try:
            bind_warning = check_bind_exposure(host, auth_key, tier3_enabled="3" in enabled_tiers)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from None

    # ---- OAuth setup ----
    oauth_store = None
    oauth_validator = None
    use_oauth = bool(oauth_client_id or oauth_client_secret)
    if use_oauth and not oauth_client_secret and transport != "stdio":
        # #39 review: authorize approves every client, so without a secret a token is free for
        # anyone, and AuthKeyMiddleware accepts it next to the key
        click.echo(
            "  WARNING: OAuth is on without --oauth-client-secret: anyone can obtain a token, "
            "which is accepted next to the auth key. Set a client secret.",
            err=True,
        )

    if use_oauth and transport != "stdio":
        from remoteos.oauth import OAuthStore, build_oauth_routes, validate_oauth_token

        oauth_store = OAuthStore()
        scheme = "https" if (ssl_certfile and ssl_keyfile) else "http"
        issuer = f"{scheme}://{host}:{port}"

        routes = build_oauth_routes(
            store=oauth_store,
            issuer=issuer,
            configured_client_id=oauth_client_id,
            configured_client_secret=oauth_client_secret,
        )
        for path, (handler, methods) in routes.items():
            mcp.custom_route(path, methods=methods)(handler)

        oauth_validator = lambda tok: validate_oauth_token(oauth_store, tok)  # noqa: E731

    # ---- Middleware ----
    middleware: list[Middleware] = []

    if allowlist_entries:
        allowlist_networks = parse_ip_allowlist(allowlist_entries)
        middleware.append(Middleware(IPAllowlistMiddleware, allowlist=allowlist_networks))

    if auth_key:
        from remoteos.auth import AuthKeyMiddleware

        middleware.append(Middleware(AuthKeyMiddleware, auth_key=auth_key, oauth_validator=oauth_validator))
    elif oauth_validator:
        from remoteos.auth import OAuthOnlyMiddleware

        middleware.append(Middleware(OAuthOnlyMiddleware, oauth_validator=oauth_validator))

    class BannerFilter(logging.Filter):
        """Inject our banner after uvicorn's 'Application startup complete' log."""

        _shown = False

        def filter(self, record):
            if not self._shown and "Application startup complete" in record.getMessage():
                self._shown = True
                auth_line = "[auth ON]" if auth_key else "[no auth]"
                ssl_line = "[https ON]" if (ssl_certfile and ssl_keyfile) else ""
                oauth_line = "[oauth ON]" if use_oauth else ""
                bind_line = f"[{host}:{port}]"
                tiers_line = f"[tiers: {','.join(enabled_tiers)}]"
                tools_line = f"[tools: {len(enabled_tools)}/{len(ALL_TOOLS)}]"
                pad = " " * 10  # align with uvicorn log text
                ver_line = f"remoteos-mcp v{__version__}"
                lines = [
                    f"{pad}+----------------------------------+",
                    f"{pad}|  {ver_line:<32s}|",
                    f"{pad}|  by dddabtc                      |",
                    f"{pad}|  github.com/dddabtc              |",
                    f"{pad}|  {auth_line:<32s}|",
                    *([f"{pad}|  {ssl_line:<32s}|"] if ssl_line else []),
                    *([f"{pad}|  {oauth_line:<32s}|"] if oauth_line else []),
                    f"{pad}|  {bind_line:<32s}|",
                    f"{pad}|  {tiers_line:<16s}{tools_line:<16s}|",
                    f"{pad}+----------------------------------+",
                ]
                if bind_warning:
                    lines.append(f"{pad}  WARNING: {bind_warning}")
                if enable_all:
                    lines.append(f"{pad}  INFO: High-risk Tier 3 tools enabled!")
                print("\n" + "\n".join(lines) + "\n", flush=True)
            return True

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        logging.getLogger("uvicorn.error").addFilter(BannerFilter())
        run_kwargs = dict(transport="streamable-http", host=host, port=port, stateless_http=True)
        if middleware:
            run_kwargs["middleware"] = middleware
        if platform.system() == "Windows":
            os.environ.setdefault("NO_COLOR", "1")
        # FastMCP 4.x run_http_async takes uvicorn_config, never uvicorn_args (#31; still so in 4.1.2).
        # Debug goes to uvicorn only, not run_http_async(log_level=...): that would
        # also turn FastMCP to DEBUG, which logs every tool call's arguments.
        uvicorn_config: dict[str, object] = {}
        if debug:
            uvicorn_config["log_level"] = "debug"
        if ssl_certfile and ssl_keyfile:
            uvicorn_config.update(ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile)
        if uvicorn_config:
            run_kwargs["uvicorn_config"] = uvicorn_config
        mcp.run(**run_kwargs)


@cli.command()
def install():
    """Create a Windows scheduled task for auto-start."""
    import getpass
    import os

    username = getpass.getuser()

    # Create start_mcp.bat for Chinese Windows compatibility
    python_exe = subprocess.run(["where", "python"], capture_output=True, text=True).stdout.strip().split("\n")[0]
    bat_content = f"""@echo off
rem remoteos-mcp startup script with UTF-8 encoding for Chinese Windows
set PYTHONIOENCODING=utf-8
"{python_exe}" -m remoteos %*
"""

    # Write batch file to user's profile directory
    user_profile = os.environ.get("USERPROFILE", ".")
    bat_path = os.path.join(user_profile, "start_mcp.bat")

    try:
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(bat_content)
        click.echo(f"[OK] Created startup script: {bat_path}")
    except Exception as e:
        click.echo(f"[ERROR] Failed to create startup script: {e}")
        return

    # Create scheduled task using the batch file
    task_cmd = f'schtasks /Create /SC ONSTART /TN "RemoteOSMCP" /TR "{bat_path}" /RU {username} /F'
    try:
        result = subprocess.run(task_cmd, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            click.echo("[OK] Scheduled task 'RemoteOSMCP' created for auto-start.")
            click.echo("The server will start automatically on system boot.")
            click.echo("Note: Uses start_mcp.bat for Chinese Windows compatibility.")
        else:
            click.echo(f"[ERROR] Failed to create task:\n{result.stderr or result.stdout}")
    except Exception as e:
        click.echo(f"[ERROR] Error: {e}")


@cli.command()
def uninstall():
    """Remove the RemoteOSMCP scheduled task."""
    import os

    task_cmd = 'schtasks /Delete /TN "RemoteOSMCP" /F'
    try:
        result = subprocess.run(task_cmd, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            click.echo("[OK] Scheduled task 'RemoteOSMCP' removed.")
        else:
            click.echo(f"[ERROR] Failed to remove task:\n{result.stderr or result.stdout}")
    except Exception as e:
        click.echo(f"[ERROR] Error: {e}")

    # Also remove the batch file
    user_profile = os.environ.get("USERPROFILE", ".")
    bat_path = os.path.join(user_profile, "start_mcp.bat")
    try:
        if os.path.exists(bat_path):
            os.remove(bat_path)
            click.echo(f"[OK] Removed startup script: {bat_path}")
    except Exception as e:
        click.echo(f"[ERROR] Failed to remove startup script: {e}")


@cli.command()
def health():
    """Print health status JSON."""
    import json

    click.echo(json.dumps({"status": "ok", "version": __version__}))


if __name__ == "__main__":
    cli()
