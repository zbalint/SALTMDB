"""Synchronous access to one long-lived MCP stdio adapter process."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import sys
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client

logger = logging.getLogger(__name__)


class ToolRunnerError(RuntimeError):
    """A runner operation could not be completed by the adapter process."""

    def __init__(self, kind: Literal["unavailable", "timeout"], message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class _Request:
    operation: Literal["list", "call", "close"]
    name: str | None
    arguments: dict[str, Any] | None
    result: concurrent.futures.Future[Any]


class ToolRunner:
    """Own one MCP adapter and expose blocking list/call methods to viewer threads."""

    def __init__(
        self,
        db_path: str | Callable[[], str],
        command: Sequence[str] | str | None = None,
        env: dict[str, str] | None = None,
        call_timeout_s: float = 60.0,
        idle_timeout_s: float = 300.0,
    ) -> None:
        if call_timeout_s <= 0:
            raise ValueError("call_timeout_s must be positive")
        if idle_timeout_s < 0:
            raise ValueError("idle_timeout_s must not be negative")
        self._db_path_source = db_path
        self.db_path = ""
        self.call_timeout_s = call_timeout_s
        self.idle_timeout_s = idle_timeout_s
        command_parts = [sys.executable, "-m", "saltmdb"] if command is None else command
        if isinstance(command_parts, str):
            command_parts = [command_parts]
        self._command = list(command_parts)
        if not self._command:
            raise ValueError("command must not be empty")
        self._env_overrides = dict(env or {})
        self._env: dict[str, str] = {}
        self._cwd = ""
        if isinstance(db_path, str):
            self._configure_process()

        self._state_lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[_Request] | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def list_tools(self) -> list[types.Tool]:
        """Return the tools currently exposed by the adapter."""
        return self._submit("list", None, None)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """Call one adapter tool and return its raw MCP result."""
        return self._submit("call", name, arguments)

    def close(self) -> None:
        """Close the adapter and permanently refuse future starts."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            ready = self._ready
        if thread is None:
            return
        if not ready.wait(timeout=self._wait_timeout()):
            logger.warning("MCP runner owner did not publish its loop during close")
            return

        result: concurrent.futures.Future[Any] = concurrent.futures.Future()
        request = _Request("close", None, None, result)
        try:
            with self._state_lock:
                loop = self._loop
                queue = self._queue
                if loop is None or queue is None:
                    return
                enqueue = asyncio.run_coroutine_threadsafe(queue.put(request), loop)
            enqueue.result(timeout=self._wait_timeout())
            result.result(timeout=self._wait_timeout())
        except (RuntimeError, concurrent.futures.TimeoutError) as exc:
            logger.warning("MCP runner close could not be queued: %s", exc)
        thread.join(timeout=self._wait_timeout())

    def _submit(
        self,
        operation: Literal["list", "call"],
        name: str | None,
        arguments: dict[str, Any] | None,
    ) -> Any:
        if not self._operation_lock.acquire(blocking=False):
            raise ToolRunnerError("unavailable", "busy")
        try:
            self._ensure_thread()
            result: concurrent.futures.Future[Any] = concurrent.futures.Future()
            request = _Request(operation, name, arguments, result)
            with self._state_lock:
                if self._closed:
                    raise ToolRunnerError("unavailable", "tool runner is closed")
                loop = self._loop
                queue = self._queue
                if loop is None or queue is None:
                    raise ToolRunnerError("unavailable", "tool runner owner is unavailable")
                enqueue = asyncio.run_coroutine_threadsafe(queue.put(request), loop)
            try:
                enqueue.result(timeout=self._wait_timeout())
                return result.result(timeout=self._wait_timeout())
            except concurrent.futures.TimeoutError as exc:
                raise ToolRunnerError(
                    "unavailable", "tool runner request did not complete"
                ) from exc
            except ToolRunnerError:
                raise
            except RuntimeError as exc:
                raise ToolRunnerError("unavailable", "tool runner owner stopped") from exc
        finally:
            self._operation_lock.release()

    def _ensure_thread(self) -> None:
        with self._state_lock:
            if self._closed:
                raise ToolRunnerError("unavailable", "tool runner is closed")
            if self._thread is not None and self._thread.is_alive():
                thread = self._thread
            else:
                self._ready.clear()
                thread = threading.Thread(
                    target=self._thread_main, name="saltmdb-viewer-tools", daemon=True
                )
                self._thread = thread
                thread.start()
        if not self._ready.wait(timeout=self._wait_timeout()):
            raise ToolRunnerError("unavailable", "tool runner owner did not start")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        queue: asyncio.Queue[_Request] = asyncio.Queue()
        with self._state_lock:
            self._loop = loop
            self._queue = queue
            self._ready.set()
        try:
            loop.run_until_complete(self._owner(queue))
        except Exception:
            logger.exception("MCP tool runner owner stopped unexpectedly")
        finally:
            with self._state_lock:
                self._loop = None
                self._queue = None
            loop.close()

    async def _owner(self, queue: asyncio.Queue[_Request]) -> None:
        stack: AsyncExitStack | None = None
        session: ClientSession | None = None
        try:
            while True:
                try:
                    request = await asyncio.wait_for(
                        queue.get(), timeout=self.idle_timeout_s if session is not None else None
                    )
                except asyncio.TimeoutError:
                    await self._close_transport(stack)
                    stack = None
                    session = None
                    continue
                try:
                    if request.operation == "close":
                        await self._close_transport(stack)
                        self._set_result(request.result, None)
                        return
                    stack, session = await self._run_request(request, stack, session)
                except BaseException as exc:
                    self._set_exception(request.result, exc)
                finally:
                    queue.task_done()
        finally:
            await self._close_transport(stack)

    async def _run_request(
        self,
        request: _Request,
        stack: AsyncExitStack | None,
        session: ClientSession | None,
    ) -> tuple[AsyncExitStack | None, ClientSession | None]:
        operation_error: BaseException | None = None
        for attempt in range(2):
            try:
                if session is None:
                    stack, session = await self._open_transport()
                if request.operation == "list":
                    list_result = await session.list_tools()
                    self._set_result(request.result, list_result.tools)
                else:
                    if request.name is None:
                        raise ToolRunnerError("unavailable", "tool call name is required")
                    call_result = await self._call_with_timeout(
                        session, request.name, request.arguments or {}
                    )
                    self._set_result(request.result, call_result)
                return stack, session
            except ToolRunnerError as exc:
                operation_error = exc
                await self._close_transport(stack)
                stack = None
                session = None
                if exc.kind == "timeout" or attempt:
                    break
            except Exception as exc:
                operation_error = ToolRunnerError(
                    "unavailable", f"MCP adapter operation failed: {exc}"
                )
                await self._close_transport(stack)
                stack = None
                session = None
                if attempt:
                    break
        if operation_error is None:
            operation_error = ToolRunnerError("unavailable", "MCP adapter operation failed")
        self._set_exception(request.result, operation_error)
        return stack, session

    def _configure_process(self) -> None:
        raw_db_path = (
            self._db_path_source() if callable(self._db_path_source) else self._db_path_source
        )
        self.db_path = str(Path(raw_db_path).expanduser().resolve())
        self._env = dict(os.environ)
        self._env.update(self._env_overrides)
        self._env["SALTMDB_AGENT_ID"] = "viewer"
        self._env["SALTMDB_DB_PATH"] = self.db_path
        self._cwd = str(Path(self.db_path).parent)

    async def _open_transport(self) -> tuple[AsyncExitStack, ClientSession]:
        stack = AsyncExitStack()
        try:
            self._configure_process()
            transport = StdioServerParameters(
                command=self._command[0],
                args=self._command[1:],
                env=self._env,
                cwd=self._cwd,
            )
            read_stream, write_stream = await stack.enter_async_context(stdio_client(transport))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            return stack, session
        except asyncio.CancelledError:
            await stack.aclose()
            raise
        except Exception as exc:
            await stack.aclose()
            raise ToolRunnerError("unavailable", f"could not start MCP adapter: {exc}") from exc

    async def _call_with_timeout(
        self, session: ClientSession, name: str, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        try:
            return await asyncio.wait_for(
                session.call_tool(name, arguments), timeout=self.call_timeout_s
            )
        except asyncio.TimeoutError as exc:
            raise ToolRunnerError(
                "timeout", f"tool call timed out after {self.call_timeout_s:g}s"
            ) from exc
        except ToolRunnerError:
            raise
        except Exception as exc:
            raise ToolRunnerError("unavailable", f"MCP adapter call failed: {exc}") from exc

    @staticmethod
    async def _close_transport(stack: AsyncExitStack | None) -> None:
        if stack is None:
            return
        try:
            await stack.aclose()
        except asyncio.CancelledError as exc:
            logger.warning("MCP adapter shutdown was cancelled: %s", exc)
        except Exception as exc:
            logger.warning("MCP adapter shutdown failed: %s", exc)

    @staticmethod
    def _set_result(future: concurrent.futures.Future[Any], value: Any) -> None:
        if not future.done():
            future.set_result(value)

    @staticmethod
    def _set_exception(future: concurrent.futures.Future[Any], exc: BaseException) -> None:
        if not future.done():
            future.set_exception(exc)

    def _wait_timeout(self) -> float:
        return max(5.0, self.call_timeout_s + 5.0)
