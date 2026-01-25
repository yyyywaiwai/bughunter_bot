from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

import inspect

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLINotFoundError,
    CLIJSONDecodeError,
    ProcessError,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    query,
)


class ClaudeRunError(RuntimeError):
    pass


ProgressCallback = Callable[[str], Awaitable[None] | None]


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
) -> str:
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        max_turns=max_turns,
        model=model,
        system_prompt=system_prompt or "",
    )
    chunks: List[str] = []
    tool_name_by_id: Dict[str, str] = {}
    try:
        async for message in query(prompt=prompt, options=options):
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
    except (CLINotFoundError, ProcessError, CLIJSONDecodeError) as exc:
        raise ClaudeRunError(str(exc)) from exc
    return "".join(chunks).strip()
