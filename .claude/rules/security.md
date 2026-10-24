---
paths:
  - "src/remoteos/platform/win/psquote.py"
  - "src/remoteos/platform/win/services.py"
  - "src/remoteos/platform/win/desktop.py"
  - "src/remoteos/platform/mac/desktop.py"
  - "src/remoteos/platform/mac/services.py"
  - "src/remoteos/ocr.py"
  - "src/remoteos/oauth.py"
  - "src/remoteos/auth.py"
  - "src/remoteos/scrape.py"
  - "src/remoteos/security.py"
  - "src/remoteos/config.py"
  - "tests/test_injection_quoting.py"
  - "tests/test_server_hardening.py"
  - "tests_windows/test_injection_powershell.py"
  - ".github/workflows/ci.yml"
  - "constraints.txt"
---

# Security boundaries (#39) — gotchas

- **A caller value in PowerShell source = `ps_literal(value)`** (`platform/win/psquote.py`). Never a
  double-quoted string or an `@" "@` here-string: both are EXPANDABLE, so `$(...)` runs. A tier-1
  tool that splices text into PowerShell is a way around the tier-3 Shell gate (the old toast).
  - PowerShell treats U+2018/U+2019/U+201A/U+201B exactly like `'` (tokenizer `IsSingleQuote`):
    `ps_literal` doubles them too. A helper that only doubles `'` lets `’; calc; ’` out.
  - Multi-line free text goes in an environment variable read as `$env:NAME`, never into the
    script (`desktop.toast_environment`, `run_command(env=)` = the child's WHOLE environment).
  - XML built from caller text: a fixed template plus `CreateTextNode`, never string splicing.
  - A native argument from PowerShell 5.1 (`schtasks /TR '…'`) is passed WITHOUT escaping an
    embedded `"`: argument injection INTO that program stays possible (tier 3 only).
- **AppleScript strings = `applescript_literal`**: `\` escaped FIRST, then `"`. Quote-only escaping
  turns `\"` into `\\"`, which ends the string.
- **urllib's redirect handler reads the 30x body with a bare `fp.read()`** after `redirect_request`
  returns (CPython `http_error_302`). Any byte cap must close `fp` inside `redirect_request`
  (`scrape._RefusePrivateRedirects`). `urlopen` also opens `file://`/`ftp://`: check the scheme on
  the URL AND every redirect.
- **Scrape is excluded on Linux** (`tiers.LINUX_ALWAYS_EXCLUDED_TOOLS`): a real-server test on the
  Linux runner cannot call it. `test_startup_flags` reads the configured policy through a probe
  that replaces only `mcp.run`.
- **Bind guard** (`security.check_bind_exposure`): non-loopback + no auth key + any tier-3 tool =
  exit 2. OAuth is no substitute for the key: `/oauth/authorize` approves every client, so OAuth
  without `--oauth-client-secret` hands a token to anyone (start-up WARNING).
- **TOML booleans go through `config._boolean`**: `bool("false")` is True. Its error text is fixed
  and never echoes the value.
- **pip-audit evaluates markers for its own interpreter.** `pip-audit -r constraints.txt` on one
  Python/OS audits one slice; the CI matrix is py3.12 on Linux/Windows/macOS + py3.11 on Linux.
  Python 3.11 is the floor since #64 (decided on #39: drops the unaudited Pillow 12.3.0 3.10
  set). No ignore list without the owner. Regenerate pins with the uv command at the
  top of `constraints.txt`, after editing `pyproject.toml`; `--upgrade-package <name>` per pin.
- **FastMCP 4.1 logs an input-free `{error_count, error_types}` for invalid arguments**;
  `errors.RedactInvalidArguments` keeps those two keys (4.0.3 logged pydantic errors WITH inputs).
- **Tests:** a literal secret-looking string in a test (`client_secret="..."`) is blocked by the
  staging hook — build it at run time (`"op-" + secrets.token_hex(8)`). Real-PowerShell injection
  payloads write a file `x` into the working directory: assert it never appears.
