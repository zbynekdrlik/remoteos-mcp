"""The MCP tools by area. Importing this package registers all of them on ``remoteos.app.mcp``.

The import order below IS the order of ``tools/list`` (``tests/golden/tools_list.json``).
"""

# isort: off
from remoteos.tools import desktop, windows, shell, files, system, network, media, tasks  # noqa: F401
# isort: on
