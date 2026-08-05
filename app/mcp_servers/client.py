"""The MCP client the graph nodes call their tools through.

Two things have to be reconciled here. The MCP SDK is async and its
sessions are scoped to an `async with` block; LangGraph nodes (see
app/graph/nodes.py) are plain synchronous functions. So this module owns
a single background thread running one asyncio event loop, and one
long-lived stdio session per server on it, and exposes
`call_tool(...)` / `list_tools(...)` as ordinary blocking calls.

Sessions are started on first use and reused for the rest of the
process. Spawning a fresh server subprocess per call would be simpler
and more isolated, but the retrieval server loads an embedding model on
startup, which would cost considerably more than the query itself -- an
investigation makes a retrieval call on every pass of the graph.

Requests for a given server are handled by that server's own task, one
at a time, and the session is opened and closed by that same task. That
matters: the SDK's transports are anyio context managers, which have to
be exited by the task that entered them, so the session can't be parked
in a shared exit stack and torn down from somewhere else later.

Everything is lazy. Importing this module starts no thread and spawns no
process, so code paths that never touch a tool (and test collection)
don't pay for it.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from mcp import Client, StdioServerParameters, stdio_client
from mcp.types import CallToolResult

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

POSTGRES_SERVER = "postgres"
RETRIEVAL_SERVER = "retrieval"

SERVER_MODULES = {
    POSTGRES_SERVER: "app.mcp_servers.postgres_server",
    RETRIEVAL_SERVER: "app.mcp_servers.retrieval_server",
}

# Variables whose value in this process must be reproduced exactly in
# the server process, including when they're *unset* here.
#
# Both servers import modules that call load_dotenv() at import time, and
# python-dotenv only sets a variable that isn't already in the
# environment. So a key the parent deliberately cleared (tests/conftest.py
# does this for every test) would come back to life from .env inside the
# child, and the retrieval server would then embed its queries with
# Gemini while the index was built with the local ONNX model -- a
# mismatch that surfaces as garbage results rather than a clean error.
# Passing the variable through as "" keeps it present-but-empty, which
# blocks the .env fallback and reads as "not configured" everywhere.
MIRRORED_ENV_VARS = ("GOOGLE_API_KEY", "GROQ_API_KEY", "LLM_PROVIDER")

# Generous, because a cold start can include loading the local embedding
# model, and a call can include an embedding round-trip to a remote API.
STARTUP_TIMEOUT_SECONDS = 180.0
CALL_TIMEOUT_SECONDS = 180.0
SHUTDOWN_TIMEOUT_SECONDS = 15.0


class MCPToolError(RuntimeError):
    """A tool call reached the server, and the server reported an error.

    Distinct from a transport/startup failure, which surfaces as
    whatever the SDK raised: this one means the tool ran and refused
    (e.g. query_table asked to filter on a column that doesn't exist).
    """


# An operation to run against a connected client, on the event loop
# thread. Parameterized this way so `call_tool` and `list_tools` share
# one queue, one session and one shutdown path.
Operation = Callable[[Client], Awaitable[Any]]


@dataclass
class _Channel:
    """A running server session and the queue feeding it work."""

    queue: asyncio.Queue
    task: concurrent.futures.Future


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in MIRRORED_ENV_VARS:
        environment[name] = os.environ.get(name, "")
    return environment


def _server_parameters(server: str) -> StdioServerParameters:
    """How to launch one of our servers as a subprocess.

    Uses this interpreter (so the server runs in the same virtualenv)
    and the project root as the working directory (so `-m app...`
    resolves).
    """
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", SERVER_MODULES[server]],
        cwd=str(PROJECT_ROOT),
        env=_child_environment(),
    )


def _result_text(result: CallToolResult) -> str:
    return "".join(
        block.text for block in result.content if getattr(block, "text", None)
    )


def _tool_payload(tool_name: str, result: CallToolResult) -> Any:
    """Unwraps a tool result into the plain dict the tool returned.

    Every tool in app/mcp_servers/ returns a single JSON object, which
    the protocol carries as one text block, so parsing that text is the
    whole job. `structured_content` is only consulted as a fallback --
    it's the newer of the two representations, and preferring the text
    keeps this working the same way across SDK versions.
    """
    if result.is_error:
        raise MCPToolError(
            f"{tool_name} failed: {_result_text(result) or 'no detail reported'}"
        )

    text = _result_text(result)
    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    if isinstance(result.structured_content, dict):
        return result.structured_content
    raise MCPToolError(
        f"{tool_name} returned no JSON payload (content={result.content!r})"
    )


class _SessionPool:
    """Owns the event-loop thread and one session per server."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._channels: dict[str, _Channel] = {}

    def submit(self, server: str, operation: Operation) -> Any:
        """Runs `operation` against `server`'s session and returns its
        result, blocking the calling thread until it's done."""
        if server not in SERVER_MODULES:
            raise KeyError(
                f"Unknown MCP server {server!r}. Known: {', '.join(SERVER_MODULES)}."
            )
        channel, loop = self._channel(server)
        future: concurrent.futures.Future = concurrent.futures.Future()
        loop.call_soon_threadsafe(channel.queue.put_nowait, (operation, future))
        return future.result(timeout=CALL_TIMEOUT_SECONDS)

    def close(self) -> None:
        """Stops every session (terminating the server subprocesses) and
        shuts the event loop down. Registered with atexit, and safe to
        call more than once."""
        with self._lock:
            channels, self._channels = self._channels, {}
            loop, self._loop = self._loop, None
            thread, self._thread = self._thread, None

        if loop is None:
            return

        for channel in channels.values():
            loop.call_soon_threadsafe(channel.queue.put_nowait, None)
        for server, channel in channels.items():
            try:
                channel.task.result(timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except Exception:  # pragma: no cover - best-effort teardown
                logger.debug("MCP %s server did not shut down cleanly", server)

        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)

    def _channel(self, server: str) -> tuple[_Channel, asyncio.AbstractEventLoop]:
        with self._lock:
            loop = self._ensure_loop()
            channel = self._channels.get(server)
            if channel is None:
                ready: concurrent.futures.Future = concurrent.futures.Future()
                task = asyncio.run_coroutine_threadsafe(
                    self._run_server(server, ready), loop
                )
                # Propagates a startup failure (bad module, missing
                # dependency, unusable warehouse) to the caller instead
                # of leaving it to time out on the first call.
                queue = ready.result(timeout=STARTUP_TIMEOUT_SECONDS)
                channel = _Channel(queue=queue, task=task)
                self._channels[server] = channel
            return channel, loop

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever,
                name="mcp-client-loop",
                daemon=True,
            )
            self._thread.start()
            atexit.register(self.close)
        return self._loop

    async def _run_server(
        self, server: str, ready: concurrent.futures.Future
    ) -> None:
        """Opens a session to `server` and serves its queue until asked
        to stop. Runs as one task so the session is entered and exited
        by the same task, as anyio requires."""
        queue: asyncio.Queue = asyncio.Queue()
        try:
            async with Client(stdio_client(_server_parameters(server))) as client:
                logger.debug("MCP %s server session established", server)
                ready.set_result(queue)
                await self._serve(client, queue)
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            if not ready.done():
                ready.set_exception(exc)
            else:
                logger.warning("MCP %s server session ended: %s", server, exc)
            _fail_pending(queue, exc)
            raise

    @staticmethod
    async def _serve(client: Client, queue: asyncio.Queue) -> None:
        while True:
            item = await queue.get()
            if item is None:
                return
            operation, future = item
            try:
                future.set_result(await operation(client))
            except BaseException as exc:  # noqa: BLE001 - handed to the caller
                future.set_exception(exc)


def _fail_pending(queue: asyncio.Queue, exc: BaseException) -> None:
    """Fails anything already queued when a session dies, so a caller
    gets the real error instead of waiting out CALL_TIMEOUT_SECONDS."""
    while not queue.empty():
        item = queue.get_nowait()
        if item is None:
            continue
        _operation, future = item
        if not future.done():
            future.set_exception(exc)


_POOL = _SessionPool()


def call_tool(
    server: str, tool_name: str, arguments: Optional[dict[str, Any]] = None
) -> Any:
    """Calls `tool_name` on `server` and returns the JSON payload the
    tool produced (a dict, for every tool in app/mcp_servers/).

    Raises MCPToolError if the tool itself reported a failure.
    """
    result = _POOL.submit(
        server, lambda client: client.call_tool(tool_name, arguments or {})
    )
    return _tool_payload(tool_name, result)


def list_tools(server: str) -> list[dict[str, Any]]:
    """The tools `server` advertises: name, description (the tool
    function's docstring) and JSON input schema. Used by the tests to
    check the published contract, and handy for confirming by hand what
    an MCP client would see."""
    result = _POOL.submit(server, lambda client: client.list_tools())
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for tool in result.tools
    ]


def close() -> None:
    """Shuts down every server session. Called automatically at
    interpreter exit; exposed for tests and long-lived hosts that want
    to release the subprocesses sooner."""
    _POOL.close()
