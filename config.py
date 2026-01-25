from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set


@dataclass(frozen=True)
class Config:
    bot_root: Path
    repo_root: Path
    discord_token: str
    owner_ids: Set[int]
    forum_repo_map: Dict[int, Path]
    repo_base_branch_map: Dict[int, str]
    default_base_branch: str
    worktree_root: Path
    discord_guild_id: Optional[int]
    force_command_sync: bool
    claude_model: Optional[str]
    claude_allowed_tools: List[str]
    claude_permission_mode: Optional[str]
    claude_max_turns: int
    claude_system_prompt: Optional[str]


def _parse_csv_ints(value: str) -> Set[int]:
    return {int(v.strip()) for v in value.split(",") if v.strip()}


def _parse_json_map(value: str) -> Dict[str, str]:
    data = json.loads(value)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    return {str(k): str(v) for k, v in data.items()}


def _normalize_paths(base: Path, mapping: Dict[str, str]) -> Dict[int, Path]:
    normalized: Dict[int, Path] = {}
    for key, raw_path in mapping.items():
        channel_id = int(key)
        path = Path(raw_path)
        if not path.is_absolute():
            path = (base / path).resolve()
        normalized[channel_id] = path
    return normalized


def _parse_allowed_tools(value: Optional[str]) -> List[str]:
    if not value:
        return ["Read", "Write", "Edit"]
    return [v.strip() for v in value.split(",") if v.strip()]


def load_config() -> Config:
    bot_root = Path(__file__).resolve().parent
    repo_root_raw = os.environ.get("REPO_ROOT")
    if repo_root_raw:
        repo_root = Path(repo_root_raw)
        if not repo_root.is_absolute():
            repo_root = (bot_root / repo_root).resolve()
        else:
            repo_root = repo_root.resolve()
    else:
        repo_root = bot_root
        if not (repo_root / "repos").exists() and (bot_root.parent / "repos").exists():
            repo_root = bot_root.parent

    discord_token = os.environ.get("DISCORD_BOT_TOKEN")
    if not discord_token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required")

    owner_ids_raw = os.environ.get("OWNER_IDS", "")
    owner_ids = _parse_csv_ints(owner_ids_raw)
    if not owner_ids:
        raise RuntimeError("OWNER_IDS is required")

    forum_repo_raw = os.environ.get("FORUM_REPO_MAP")
    if not forum_repo_raw:
        raise RuntimeError("FORUM_REPO_MAP is required")
    forum_repo_map = _normalize_paths(repo_root, _parse_json_map(forum_repo_raw))

    repo_branch_map_raw = os.environ.get("REPO_BASE_BRANCH_MAP", "{}")
    repo_base_branch_map: Dict[int, str] = {}
    try:
        repo_base_branch_map = {int(k): str(v) for k, v in _parse_json_map(repo_branch_map_raw).items()}
    except json.JSONDecodeError:
        repo_base_branch_map = {}

    default_base_branch = os.environ.get("DEFAULT_BASE_BRANCH", "main")

    worktree_root = Path(os.environ.get("WORKTREE_ROOT", str(bot_root / "worktrees")))
    if not worktree_root.is_absolute():
        worktree_root = (bot_root / worktree_root).resolve()

    guild_id_raw = os.environ.get("DISCORD_GUILD_ID")
    discord_guild_id = int(guild_id_raw) if guild_id_raw else None
    force_command_sync = os.environ.get("DISCORD_FORCE_COMMAND_SYNC", "").lower() in {"1", "true", "yes", "on"}

    claude_model = os.environ.get("CLAUDE_MODEL") or None
    claude_allowed_tools = _parse_allowed_tools(os.environ.get("CLAUDE_ALLOWED_TOOLS"))
    claude_permission_mode = os.environ.get("CLAUDE_PERMISSION_MODE") or None
    claude_max_turns = int(os.environ.get("CLAUDE_MAX_TURNS", "20"))
    claude_system_prompt = os.environ.get("CLAUDE_SYSTEM_PROMPT") or None

    return Config(
        bot_root=bot_root,
        repo_root=repo_root,
        discord_token=discord_token,
        owner_ids=owner_ids,
        forum_repo_map=forum_repo_map,
        repo_base_branch_map=repo_base_branch_map,
        default_base_branch=default_base_branch,
        worktree_root=worktree_root,
        discord_guild_id=discord_guild_id,
        force_command_sync=force_command_sync,
        claude_model=claude_model,
        claude_allowed_tools=claude_allowed_tools,
        claude_permission_mode=claude_permission_mode,
        claude_max_turns=claude_max_turns,
        claude_system_prompt=claude_system_prompt,
    )
