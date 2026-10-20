r"""Client that calls the tools of the Vissim MCP server (`python -m vissim_mcp`, http://127.0.0.1:<port>/mcp) directly over HTTP.

A substitute for when the Vissim MCP connection of an MCP client session has dropped (the server is still alive in the tray): the same tools can be called from the command line.

  python tools/mcp_call.py list                                   # tool names, descriptions, arguments
  python tools/mcp_call.py call vissim_status
  python tools/mcp_call.py call get_attributes --args "{\"type\": \"Link\", \"no\": 1, \"attrs\": [\"Length2D\"]}"
  python tools/mcp_call.py call --file calls.json                 # several calls in order

- The address is fixed to 127.0.0.1; the port is `--port` -> `port` in `config/vissim_mcp.json` -> 8766.
- `--file` format: `[["tool", {args}], ...]` or `[{"tool": "tool", "args": {args}}, ...]`. A call with `ok=false` does not
  stop the next ones (`--stop-on-error` stops).
- Output: UTF-8, exactly the text the tool returned (the server's JSON response). Exit code 0 = every call succeeded
  (the response is JSON and `ok` is not false), 1 = a failure response occurred, 2 = argument error, 3 = could not
  reach the server.
- Write scope: none (this script writes no files). What changes is decided by the tool called - writing tools such as
  `save_network` are passed through unchanged, so call only what the user asked for, as in a conversation. The server's
  protected list and save rules still apply on the server side.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Sequence

HOST = "127.0.0.1"           # the server is fixed to 127.0.0.1 - never send to another host
DEFAULT_PORT = 8766
CONFIG = Path(__file__).resolve().parents[1] / "config" / "vissim_mcp.json"


class CallArgError(ValueError):
    """Argument or call-file format error (exit code 2)."""


def server_url(port: int | None = None, config: Path = CONFIG) -> str:
    """http://127.0.0.1:<port>/mcp - port: argument -> port in the config file -> 8766."""
    if port is None:
        try:
            port = int(json.loads(config.read_text(encoding="utf-8")).get("port", DEFAULT_PORT))
        except (OSError, ValueError):
            port = DEFAULT_PORT
    if not 0 < int(port) < 65536:
        raise CallArgError(f"Port must be 1-65535: {port}")
    return f"http://{HOST}:{int(port)}/mcp"


def parse_args_json(s: str | None) -> dict[str, Any]:
    """--args JSON string -> dict (empty dict if absent)."""
    if not s:
        return {}
    try:
        v = json.loads(s)
    except json.JSONDecodeError as e:
        raise CallArgError(f"--args is not JSON: {e}") from None
    if not isinstance(v, dict):
        raise CallArgError("--args must be a JSON object ({...})")
    return v


def parse_calls(data: Any) -> list[tuple[str, dict[str, Any]]]:
    """Call file content -> [(tool, args)]. Accepts both [[name, args]] and [{"tool", "args"}]."""
    if not isinstance(data, list) or not data:
        raise CallArgError("The call file must be a non-empty list")
    out: list[tuple[str, dict[str, Any]]] = []
    for i, c in enumerate(data):
        if isinstance(c, list) and len(c) in (1, 2) and isinstance(c[0], str):
            name, args = c[0], (c[1] if len(c) == 2 else {})
        elif isinstance(c, dict) and isinstance(c.get("tool"), str):
            name, args = c["tool"], c.get("args", {})
        else:
            raise CallArgError(f"Call {i}: not of the form [\"tool\", {{args}}] or {{\"tool\": ..., \"args\": ...}}: {c!r}")
        if not isinstance(args, dict):
            raise CallArgError(f"Call {i} ({name}): args must be an object")
        out.append((name, args))
    return out


def response_ok(text: str) -> bool:
    """Whether the tool response text is not a failure (JSON ok=false or isError)."""
    try:
        v = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return True
    return not (isinstance(v, dict) and v.get("ok") is False)


async def _run(url: str, calls: list[tuple[str, dict[str, Any]]] | None, stop_on_error: bool) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    failed = False
    async with streamable_http_client(url) as (read, write):     # the installed mcp returns two values, (read, write)
        async with ClientSession(read, write) as s:
            await s.initialize()
            if calls is None:
                for t in (await s.list_tools()).tools:
                    print(f"{t.name} — {(t.description or '').strip()}")
                    props = (t.input_schema or {}).get("properties", {})   # the Tool schema attribute is named input_schema
                    req = set((t.input_schema or {}).get("required", []))
                    for k, v in props.items():
                        print(f"    {k}{'*' if k in req else ''}: {json.dumps(v, ensure_ascii=False)}")
                return 0
            for name, args in calls:
                res = await s.call_tool(name, args)
                texts = [getattr(c, "text", None) or str(c) for c in res.content]
                ok = not getattr(res, "isError", False) and all(response_ok(t) for t in texts)
                print(f"== {name} {json.dumps(args, ensure_ascii=False)} → {'ok' if ok else 'FAILED'}")
                for t in texts:
                    print(t)
                if not ok:
                    failed = True
                    if stop_on_error:
                        break
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Call Vissim MCP server tools directly over HTTP (fixed to 127.0.0.1).")
    ap.add_argument("--port", type=int, help=f"port (default: port in config/vissim_mcp.json, otherwise {DEFAULT_PORT})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list tools")
    c = sub.add_parser("call", help="call a tool")
    c.add_argument("tool", nargs="?", help="tool name (omit with --file)")
    c.add_argument("--args", help="arguments as a JSON object")
    c.add_argument("--file", type=Path, help="JSON file with several calls (UTF-8)")
    c.add_argument("--stop-on-error", action="store_true", help="stop at the first failure response")
    a = ap.parse_args(argv)
    try:
        url = server_url(a.port)
        calls = None
        if a.cmd == "call":
            if a.file:
                if a.tool or a.args:
                    raise CallArgError("--file cannot be combined with a tool name or --args")
                calls = parse_calls(json.loads(a.file.read_text(encoding="utf-8")))
            elif a.tool:
                calls = [(a.tool, parse_args_json(a.args))]
            else:
                raise CallArgError("A tool name or --file is required")
    except (CallArgError, OSError, json.JSONDecodeError) as e:
        print(f"[argument error] {e}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run(url, calls, getattr(a, "stop_on_error", False)))
    except Exception as e:  # noqa: BLE001 - a connection failure arrives in several forms (ConnectError, ExceptionGroup)
        print(f"[connection/call failed] {url}: {type(e).__name__}: {e}\n  Is the server running: python -m vissim_mcp status",
              file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
