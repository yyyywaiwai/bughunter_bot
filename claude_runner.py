from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import inspect

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    CLINotFoundError,
    CLIJSONDecodeError,
    ProcessError,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    query,
)


class ClaudeRunError(RuntimeError):
    pass


ProgressCallback = Callable[[str], Awaitable[None] | None]


async def _collect_messages(
    messages: Any,
    on_progress: Optional[ProgressCallback],
) -> str:
    chunks: List[str] = []
    tool_name_by_id: Dict[str, str] = {}
    async for message in messages:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_name_by_id[block.id] = block.name
                    summary = _summarize_tool_input(block.name, block.input)
                    label = f"ツール開始: {block.name}"
                    if summary:
                        label = f"{label} ({summary})"
                    await _emit_progress(on_progress, label)
                elif isinstance(block, ToolResultBlock):
                    tool_name = tool_name_by_id.get(block.tool_use_id, block.tool_use_id)
                    status = "失敗" if block.is_error else "完了"
                    await _emit_progress(on_progress, f"ツール{status}: {tool_name}")
        elif isinstance(message, ResultMessage):
            break
    return "".join(chunks).strip()


class ClaudeSession:
    def __init__(self, session_id: str, options: ClaudeAgentOptions) -> None:
        self.session_id = session_id
        self.options = options
        self.client = ClaudeSDKClient(options)
        self._connected = False
        self._lock = asyncio.Lock()
        self.last_activity = 0.0

    def touch(self) -> None:
        self.last_activity = asyncio.get_running_loop().time()

    async def run(self, prompt: str, on_progress: Optional[ProgressCallback]) -> str:
        async with self._lock:
            self.touch()
            if not self._connected:
                await self.client.connect()
                self._connected = True
            await self.client.query(prompt, session_id=self.session_id)
            return await _collect_messages(self.client.receive_response(), on_progress)

    async def close(self) -> None:
        if self._connected:
            await self.client.disconnect()
            self._connected = False


class ClaudeSessionManager:
    def __init__(self, idle_ttl_seconds: int = 12 * 60 * 60) -> None:
        self._sessions: Dict[str, ClaudeSession] = {}
        self._idle_tasks: Dict[str, asyncio.Task] = {}
        self._idle_ttl_seconds = idle_ttl_seconds

    def get(self, session_id: str, options: ClaudeAgentOptions) -> ClaudeSession:
        session = self._sessions.get(session_id)
        if session is None:
            session = ClaudeSession(session_id, options)
            self._sessions[session_id] = session
        session.touch()
        self._schedule_idle_close(session_id)
        return session

    async def close(self, session_id: str) -> None:
        idle_task = self._idle_tasks.pop(session_id, None)
        if idle_task:
            idle_task.cancel()
        session = self._sessions.pop(session_id, None)
        if session is not None:
            await session.close()

    def touch(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.touch()
        self._schedule_idle_close(session_id)

    def _schedule_idle_close(self, session_id: str) -> None:
        existing = self._idle_tasks.pop(session_id, None)
        if existing:
            existing.cancel()
        self._idle_tasks[session_id] = asyncio.create_task(self._idle_watch(session_id))

    async def _idle_watch(self, session_id: str) -> None:
        try:
            while True:
                session = self._sessions.get(session_id)
                if session is None:
                    return
                now = asyncio.get_running_loop().time()
                remaining = session.last_activity + self._idle_ttl_seconds - now
                if remaining <= 0:
                    await self.close(session_id)
                    return
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            return


_session_manager = ClaudeSessionManager()


async def _emit_progress(callback: Optional[ProgressCallback], message: str) -> None:
    if not callback:
        return
    result = callback(message)
    if inspect.isawaitable(result):
        await result


def _summarize_tool_input(tool_name: str, tool_input: dict) -> Optional[str]:
    summary: Optional[str] = None
    if tool_name in {"Bash", "Shell"}:
        summary = tool_input.get("command")
    elif tool_name in {"Read", "Write", "Edit"}:
        summary = tool_input.get("path")
    elif tool_name in {"Grep", "Glob"}:
        summary = tool_input.get("pattern")
    elif tool_name == "Task":
        summary = tool_input.get("description")
    if summary:
        summary = str(summary).strip().replace("\n", " ")
        if len(summary) > 120:
            summary = summary[:117] + "..."
    return summary


async def run_claude(
    *,
    prompt: str,
    cwd: Path,
    allowed_tools: List[str],
    permission_mode: Optional[str],
    max_turns: int,
    model: Optional[str],
    system_prompt: Optional[str],
    on_progress: Optional[ProgressCallback] = None,
    reuse_session: bool = False,
    session_id: Optional[str] = None,
) -> str:
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        max_turns=max_turns,
        model=model,
        system_prompt=system_prompt or "",
    )
    try:
        if reuse_session:
            if not session_id:
                raise ClaudeRunError("session_id is required when reuse_session=True")
            session = _session_manager.get(session_id, options)
            result = await session.run(prompt, on_progress)
            _session_manager.touch(session_id)
            return result
        return await _collect_messages(query(prompt=prompt, options=options), on_progress)
    except (CLIConnectionError, CLINotFoundError, ProcessError, CLIJSONDecodeError) as exc:
        raise ClaudeRunError(str(exc)) from exc


async def close_claude_session(session_id: str) -> None:
    await _session_manager.close(session_id)


def touch_claude_session(session_id: str) -> None:
    _session_manager.touch(session_id)
