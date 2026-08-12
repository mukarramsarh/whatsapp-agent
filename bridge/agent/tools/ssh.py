"""SSH tool — execute commands on configured remote servers."""
from __future__ import annotations

import json
import logging

from agent.tools.base import Tool, ToolResult

logger = logging.getLogger("bridge.tool.ssh")


class SSHTool(Tool):
    name = "execute_ssh_command"
    display_name = "SSH — Remote Command Execution"
    description = (
        "Execute a shell command on a configured remote server via SSH. "
        "Use for server management, log inspection, service control, or deployment tasks. "
        "Only servers listed in the tool configuration can be accessed."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
                "description": "Server alias as configured in the tool settings",
            },
            "command": {
                "type": "string",
                "description": "Shell command to execute (keep it safe and non-destructive)",
            },
        },
        "required": ["server", "command"],
    }

    async def run(self, server: str, command: str, **_) -> ToolResult:
        servers: dict = self.config.get("servers", {})
        if not servers:
            return ToolResult(success=False, error="No SSH servers configured. Add servers to the tool configuration.")
        if server not in servers:
            available = ", ".join(servers.keys())
            return ToolResult(success=False, error=f"Server '{server}' not found. Available: {available}")

        srv = servers[server]
        try:
            import asyncssh

            connect_kwargs = {
                "host": srv["host"],
                "port": int(srv.get("port", 22)),
                "username": srv["username"],
                "known_hosts": None,
            }
            if "password" in srv:
                connect_kwargs["password"] = srv["password"]
            elif "key_path" in srv:
                connect_kwargs["client_keys"] = [srv["key_path"]]

            async with asyncssh.connect(**connect_kwargs) as conn:
                result = await conn.run(command, timeout=30)
                output = (result.stdout or "") + (result.stderr or "")
                return ToolResult(
                    success=result.returncode == 0,
                    output=output.strip() or "(no output)",
                    error="" if result.returncode == 0 else f"Exit code {result.returncode}",
                )
        except ImportError:
            return ToolResult(success=False, error="asyncssh package not installed.")
        except Exception as exc:
            logger.error("SSH error on %s: %s", server, exc)
            return ToolResult(success=False, error=str(exc))
