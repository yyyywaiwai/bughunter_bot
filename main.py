from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import discord
from discord import app_commands
from dotenv import load_dotenv

from claude_runner import ClaudeRunError, run_claude
from config import Config, load_config
from repo_ops import (
    CommandError,
    git_commit_all,
    git_current_branch,
    git_fetch,
    git_is_clean,
    git_pull,
    git_push,
    git_worktree_add,
    gh_pr_create,
)
from storage import Job, approve_job, create_job, get_job, get_job_by_thread, init_db, update_job_status


MAX_MESSAGE_LEN = 1900
DEFAULT_SYSTEM_PROMPT = (
    "You are a senior software engineer. Follow instructions carefully, "
    "make minimal changes, and explain reasoning succinctly."
)


class BughunterBot(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.config = config
        self.tree = app_commands.CommandTree(self, fallback_to_global=False)
        self.tree.error(self._on_app_command_error)
        self.db_path = self.config.bot_root / "data" / "bot.sqlite"
        init_db(self.db_path)
        self._tasks: Dict[int, asyncio.Task] = {}

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.config.discord_guild_id) if self.config.discord_guild_id else None

        if self.config.force_command_sync:
            # Clear remote commands first, then re-add and sync.
            self.tree.clear_commands(guild=None)
            cleared = await self.tree.sync(guild=None)
            logging.info("Cleared global commands: %s", [cmd.name for cmd in cleared])
            if guild is not None:
                self.tree.clear_commands(guild=guild)
                cleared_guild = await self.tree.sync(guild=guild)
                logging.info("Cleared guild commands: %s", [cmd.name for cmd in cleared_guild])

        if guild is not None:
            self.tree.add_command(self.approve_job, guild=guild)
            self.tree.add_command(self.instruct_job, guild=guild)
            logging.info("Local guild commands: %s", [cmd.name for cmd in self.tree.get_commands(guild=guild)])
        else:
            self.tree.add_command(self.approve_job)
            self.tree.add_command(self.instruct_job)
            logging.info("Local global commands: %s", [cmd.name for cmd in self.tree.get_commands()])
        if guild is not None:
            synced = await self.tree.sync(guild=guild)
            logging.info("Synced guild commands: %s", [cmd.name for cmd in synced])
        else:
            synced = await self.tree.sync(guild=None)
            logging.info("Synced global commands: %s", [cmd.name for cmd in synced])

    async def on_ready(self) -> None:
        logging.info("Logged in as %s", self.user)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        # Fallback handler to bypass signature mismatch issues for app commands.
        try:
            if (
                interaction.type == discord.InteractionType.application_command
                and isinstance(interaction.data, dict)
            ):
                name = interaction.data.get("name")
                options = interaction.data.get("options", [])
                if name in {"approve_job", "approve"}:
                    job_id = _extract_option_int(options, "job_id")
                    await self._handle_approve(interaction, job_id)
                    return
                if name == "instruct_job":
                    job_id = _extract_option_int(options, "job_id")
                    instruction = _extract_option_str(options, "instruction") or ""
                    await self._handle_instruct(interaction, instruction, job_id)
                    return
        except Exception as exc:
            logging.exception("Failed to handle interaction: %s", exc)
        return

    async def on_thread_create(self, thread: discord.Thread) -> None:
        parent = thread.parent
        if parent is None:
            parent_id = getattr(thread, "parent_id", None)
            if parent_id:
                try:
                    parent = await self.fetch_channel(parent_id)
                except discord.NotFound:
                    parent = None
        if not isinstance(parent, discord.ForumChannel):
            return
        repo_path = self.config.forum_repo_map.get(parent.id)
        if not repo_path:
            await self._safe_send(
                thread,
                "このフォーラムに対応するリポジトリが設定されていません。",
            )
            return
        existing = get_job_by_thread(self.db_path, thread.id)
        if existing:
            return
        try:
            job = create_job(self.db_path, thread.id, parent.id, str(repo_path))
        except Exception as exc:
            logging.exception("Failed to create job: %s", exc)
            return

        await self._safe_send(
            thread,
            (
                f"Job ID: {job.id}"
            ),
        )

        if job.id in self._tasks:
            return
        task = asyncio.create_task(self._process_job(job))
        self._tasks[job.id] = task

    @app_commands.command(name="approve_job", description="承認済みジョブを開始します")
    @app_commands.describe(job_id="承認するJob ID（スレ内なら省略可）")
    async def approve_job(self, interaction: discord.Interaction, job_id: Optional[int] = None) -> None:
        await self._handle_approve(interaction, job_id)

    @app_commands.command(name="instruct_job", description="ジョブに追加指示を送ります")
    @app_commands.describe(instruction="追加指示", job_id="対象Job ID（スレ内なら省略可）")
    async def instruct_job(
        self,
        interaction: discord.Interaction,
        instruction: str,
        job_id: Optional[int] = None,
    ) -> None:
        await self._handle_instruct(interaction, instruction, job_id)

    async def _handle_approve(self, interaction: discord.Interaction, job_id: Optional[int]) -> None:
        if interaction.user.id not in self.config.owner_ids:
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return

        job = None
        if job_id is None and isinstance(interaction.channel, discord.Thread):
            job = get_job_by_thread(self.db_path, interaction.channel.id)
        elif job_id is not None:
            job = get_job(self.db_path, job_id)

        if not job:
            await interaction.response.send_message("対象ジョブが見つかりません。", ephemeral=True)
            return
        if job.status not in {"pending_approval", "failed"}:
            await interaction.response.send_message(
                f"ジョブは既に {job.status} です。",
                ephemeral=True,
            )
            return

        approved = approve_job(self.db_path, job.id, interaction.user.id)
        if not approved:
            await interaction.response.send_message("承認に失敗しました。", ephemeral=True)
            return

        await interaction.response.send_message(
            f"承認しました。Job ID: {approved.id} を開始します。",
            ephemeral=True,
        )

        if approved.id in self._tasks:
            return
        task = asyncio.create_task(self._process_job(approved))
        self._tasks[approved.id] = task

    async def _handle_instruct(
        self,
        interaction: discord.Interaction,
        instruction: str,
        job_id: Optional[int],
    ) -> None:
        if interaction.user.id not in self.config.owner_ids:
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        if not instruction.strip():
            await interaction.response.send_message("追加指示が空です。", ephemeral=True)
            return

        job = None
        if job_id is None and isinstance(interaction.channel, discord.Thread):
            job = get_job_by_thread(self.db_path, interaction.channel.id)
        elif job_id is not None:
            job = get_job(self.db_path, job_id)

        if not job:
            await interaction.response.send_message("対象ジョブが見つかりません。", ephemeral=True)
            return
        if job.id in self._tasks:
            await interaction.response.send_message("ジョブは既に実行中です。", ephemeral=True)
            return
        if not job.worktree_path:
            await interaction.response.send_message("worktreeが作成されていません。", ephemeral=True)
            return
        worktree_path = Path(job.worktree_path).resolve()
        if not worktree_path.exists():
            await interaction.response.send_message("worktreeが存在しません。", ephemeral=True)
            return

        await interaction.response.send_message(
            f"追加指示を受け付けました。Job ID: {job.id}",
            ephemeral=True,
        )
        task = asyncio.create_task(self._process_job_instruction(job, instruction))
        self._tasks[job.id] = task

    async def _process_job(self, job: Job) -> None:
        thread = await self._fetch_thread(int(job.thread_id))
        if not thread:
            update_job_status(self.db_path, job.id, "failed", error="Thread not found")
            return

        status = StatusTracker(thread)
        try:
            update_job_status(self.db_path, job.id, "running")
            repo_path = Path(job.repo_path).resolve()
            if not repo_path.exists():
                mapped_path = self.config.forum_repo_map.get(int(job.forum_id))
                if mapped_path:
                    repo_path = mapped_path.resolve()
            if not self._is_safe_repo_path(repo_path):
                raise RuntimeError("リポジトリパスが安全ではありません")
            if not repo_path.exists():
                raise RuntimeError("リポジトリが存在しません")

            await status.add("リポジトリを更新します。")
            git_fetch(repo_path)
            git_pull(repo_path)

            if not git_is_clean(repo_path):
                raise RuntimeError("リポジトリに未コミット変更があります")

            base_branch = self.config.repo_base_branch_map.get(
                int(job.forum_id),
                self.config.default_base_branch,
            )
            branch = f"bughunter/thread-{job.thread_id}"
            worktree_path = self._worktree_path(repo_path.name, job.thread_id)
            if worktree_path.exists():
                raise RuntimeError("worktreeが既に存在します")

            await status.add(f"worktreeを作成します: {worktree_path}")
            git_worktree_add(repo_path, worktree_path, branch, base_branch)
            update_job_status(
                self.db_path,
                job.id,
                "running",
                worktree_path=str(worktree_path),
                branch=branch,
            )

            context = await self._build_thread_context(thread)
            prompt = self._build_prompt(context)
            await status.add("Claudeに解析と実装を依頼します。")

            last_progress_at = 0.0
            last_progress_msg: Optional[str] = None

            async def on_progress(message: str) -> None:
                nonlocal last_progress_at, last_progress_msg
                now = asyncio.get_running_loop().time()
                if last_progress_msg == message and now - last_progress_at < 2.0:
                    return
                if now - last_progress_at < 0.8:
                    return
                last_progress_msg = message
                last_progress_at = now
                await status.add(f"Claude: {message}")

            claude_output = await run_claude(
                prompt=prompt,
                cwd=worktree_path,
                allowed_tools=self.config.claude_allowed_tools,
                permission_mode=self.config.claude_permission_mode,
                max_turns=self.config.claude_max_turns,
                model=self.config.claude_model,
                system_prompt=self.config.claude_system_prompt or DEFAULT_SYSTEM_PROMPT,
                on_progress=on_progress,
            )

            pr_title = _extract_section(claude_output, "PRタイトル") or f"fix: thread {job.thread_id}"
            pr_body = _extract_section(claude_output, "PR本文") or "Auto-generated by Bughunterbot."

            await status.add("変更をコミットします。")
            if not git_is_clean(worktree_path):
                git_commit_all(worktree_path, pr_title)
                git_push(worktree_path, branch)
            else:
                await status.add("変更が見つかりませんでした。PR作成をスキップします。")
                updated = update_job_status(self.db_path, job.id, "completed")
                await self._send_completion_embed(
                    thread,
                    updated or job,
                    pr_url=None,
                    error=None,
                )
                await status.delete()
                await self._post_result(thread, claude_output, None)
                return

            await status.add("PRを作成します。")
            pr_url = gh_pr_create(repo_path, pr_title, pr_body, branch, base_branch)

            updated = update_job_status(self.db_path, job.id, "completed", pr_url=pr_url)
            await self._send_completion_embed(
                thread,
                updated or job,
                pr_url=pr_url,
                error=None,
            )
            await status.delete()
            await self._post_result(thread, claude_output, pr_url)
        except (RuntimeError, CommandError, ClaudeRunError) as exc:
            logging.exception("Job failed: %s", exc)
            updated = update_job_status(self.db_path, job.id, "failed", error=str(exc))
            await self._send_completion_embed(
                thread,
                updated or job,
                pr_url=None,
                error=str(exc),
            )
            await status.delete()
        finally:
            self._tasks.pop(job.id, None)

    async def _process_job_instruction(self, job: Job, instruction: str) -> None:
        thread = await self._fetch_thread(int(job.thread_id))
        if not thread:
            update_job_status(self.db_path, job.id, "failed", error="Thread not found")
            return

        status = StatusTracker(thread)
        try:
            update_job_status(self.db_path, job.id, "running")
            repo_path = Path(job.repo_path).resolve()
            if not repo_path.exists():
                mapped_path = self.config.forum_repo_map.get(int(job.forum_id))
                if mapped_path:
                    repo_path = mapped_path.resolve()
            if not self._is_safe_repo_path(repo_path):
                raise RuntimeError("リポジトリパスが安全ではありません")
            if not repo_path.exists():
                raise RuntimeError("リポジトリが存在しません")

            worktree_path = Path(job.worktree_path or "").resolve()
            if not worktree_path.exists():
                raise RuntimeError("worktreeが存在しません")

            base_branch = self.config.repo_base_branch_map.get(
                int(job.forum_id),
                self.config.default_base_branch,
            )
            branch = job.branch or git_current_branch(worktree_path)

            context = await self._build_thread_context(thread)
            prompt = self._build_prompt(context, extra_instructions=instruction)
            await status.add("Claudeに追加指示を送信します。")

            last_progress_at = 0.0
            last_progress_msg: Optional[str] = None

            async def on_progress(message: str) -> None:
                nonlocal last_progress_at, last_progress_msg
                now = asyncio.get_running_loop().time()
                if last_progress_msg == message and now - last_progress_at < 2.0:
                    return
                if now - last_progress_at < 0.8:
                    return
                last_progress_msg = message
                last_progress_at = now
                await status.add(f"Claude: {message}")

            claude_output = await run_claude(
                prompt=prompt,
                cwd=worktree_path,
                allowed_tools=self.config.claude_allowed_tools,
                permission_mode=self.config.claude_permission_mode,
                max_turns=self.config.claude_max_turns,
                model=self.config.claude_model,
                system_prompt=self.config.claude_system_prompt or DEFAULT_SYSTEM_PROMPT,
                on_progress=on_progress,
            )

            pr_title = _extract_section(claude_output, "PRタイトル") or f"fix: thread {job.thread_id}"
            pr_body = _extract_section(claude_output, "PR本文") or "Auto-generated by Bughunterbot."

            await status.add("変更をコミットします。")
            if not git_is_clean(worktree_path):
                git_commit_all(worktree_path, pr_title)
                git_push(worktree_path, branch)
            else:
                await status.add("変更が見つかりませんでした。PR作成をスキップします。")
                updated = update_job_status(self.db_path, job.id, "completed")
                await self._send_completion_embed(
                    thread,
                    updated or job,
                    pr_url=(updated.pr_url if updated else job.pr_url),
                    error=None,
                )
                await status.delete()
                await self._post_result(thread, claude_output, updated.pr_url if updated else job.pr_url)
                return

            pr_url = job.pr_url
            if not pr_url:
                await status.add("PRを作成します。")
                pr_url = gh_pr_create(repo_path, pr_title, pr_body, branch, base_branch)

            updated = update_job_status(self.db_path, job.id, "completed", pr_url=pr_url)
            await self._send_completion_embed(
                thread,
                updated or job,
                pr_url=pr_url,
                error=None,
            )
            await status.delete()
            await self._post_result(thread, claude_output, pr_url)
        except (RuntimeError, CommandError, ClaudeRunError) as exc:
            logging.exception("Job failed: %s", exc)
            updated = update_job_status(self.db_path, job.id, "failed", error=str(exc))
            await self._send_completion_embed(
                thread,
                updated or job,
                pr_url=updated.pr_url if updated else job.pr_url,
                error=str(exc),
            )
            await status.delete()
        finally:
            self._tasks.pop(job.id, None)

    async def _fetch_thread(self, thread_id: int) -> Optional[discord.Thread]:
        channel = self.get_channel(thread_id)
        if isinstance(channel, discord.Thread):
            return channel
        try:
            fetched = await self.fetch_channel(thread_id)
        except discord.NotFound:
            return None
        return fetched if isinstance(fetched, discord.Thread) else None

    async def _build_thread_context(self, thread: discord.Thread) -> str:
        tags = []
        if isinstance(thread.parent, discord.ForumChannel):
            tags = [tag.name for tag in thread.applied_tags]

        starter = None
        try:
            starter = await thread.fetch_message(thread.id)
        except discord.NotFound:
            starter = None

        parts = [f"タイトル: {thread.name}"]
        if tags:
            parts.append(f"タグ: {', '.join(tags)}")
        if starter:
            parts.append(f"投稿者: {starter.author} (id={starter.author.id})")
            parts.append("本文:")
            parts.append(starter.content or "(本文なし)")
            if starter.attachments:
                attachment_urls = [a.url for a in starter.attachments]
                parts.append("添付:")
                parts.extend(attachment_urls)
        parts.append(f"スレッドURL: {thread.jump_url}")
        return "\n".join(parts)

    def _build_prompt(self, context: str, extra_instructions: Optional[str] = None) -> str:
        extra = ""
        if extra_instructions:
            extra = f"\n追加指示:\n{extra_instructions}\n"
        return (
            "以下はDiscordフォーラムの新規スレッドです。\n\n"
            f"{context}\n\n"
            f"{extra}\n"
            "要件:\n"
            "1. 原因を特定し、簡潔に説明する。\n"
            "2. 修正/実装の仕様をMarkdownで書く（箇条書き可）。\n"
            "3. 必要なコード変更を実装する。\n"
            "4. PRタイトルとPR本文を提案する。\n\n"
            "制約:\n"
            "- git操作や外部コマンドは実行しない。\n"
            "- 変更は最小限にし、既存設計を尊重する。\n\n"
            "出力フォーマット:\n"
            "## 原因\n"
            "...\n\n"
            "## 仕様\n"
            "...\n\n"
            "## 変更概要\n"
            "...\n\n"
            "## PRタイトル\n"
            "...\n\n"
            "## PR本文\n"
            "...\n"
        )

    def _worktree_path(self, repo_name: str, thread_id: str) -> Path:
        return self.config.worktree_root / repo_name / str(thread_id)

    async def _post_result(self, thread: discord.Thread, claude_output: str, pr_url: Optional[str]) -> None:
        if pr_url:
            await self._safe_send(thread, f"PR: {pr_url}")
        output = claude_output.strip()
        if not output:
            await self._safe_send(thread, "```(本文なし)```")
            return
        for chunk in _chunk_codeblock(output):
            await self._safe_send(thread, f"```\n{chunk}\n```")

    async def _safe_send(self, thread: discord.Thread, content: str) -> None:
        try:
            await thread.send(content)
        except discord.HTTPException as exc:
            logging.warning("Failed to send message: %s", exc)

    def _is_safe_repo_path(self, repo_path: Path) -> bool:
        try:
            repo_path.resolve().relative_to(self.config.repo_root.resolve())
            return True
        except ValueError:
            return False

    async def _send_completion_embed(
        self,
        thread: discord.Thread,
        job: Job,
        *,
        pr_url: Optional[str],
        error: Optional[str],
    ) -> None:
        success = error is None
        title = "完了" if success else "失敗"
        color = 0x57F287 if success else 0xED4245
        embed = discord.Embed(
            title=title,
            description="処理が完了しました。" if success else "処理に失敗しました。",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Job ID", value=str(job.id), inline=True)
        embed.add_field(name="Thread ID", value=str(job.thread_id), inline=True)
        if job.branch:
            embed.add_field(name="Branch", value=job.branch, inline=True)
        if pr_url:
            embed.add_field(name="PR", value=pr_url, inline=False)
        if error:
            embed.add_field(name="Error", value=_truncate_text(error, 1000), inline=False)
        await thread.send(embed=embed)

    async def _on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, (app_commands.CommandSignatureMismatch, app_commands.CommandNotFound)):
            return
        logging.error("App command error: %s", error, exc_info=error)


class StatusTracker:
    def __init__(self, thread: discord.Thread) -> None:
        self.thread = thread
        self.lines: List[str] = []
        self.message: Optional[discord.Message] = None

    async def add(self, line: str) -> None:
        self.lines.append(line)
        content = _format_status_message(self.lines)
        try:
            if self.message:
                await self.message.edit(content=content)
            else:
                self.message = await self.thread.send(content)
        except discord.HTTPException as exc:
            logging.warning("Failed to update status message: %s", exc)

    async def delete(self) -> None:
        if not self.message:
            return
        try:
            await self.message.delete()
        except discord.HTTPException as exc:
            logging.warning("Failed to delete status message: %s", exc)


def _chunk_message(text: str) -> List[str]:
    chunks: List[str] = []
    current = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > MAX_MESSAGE_LEN and current:
            chunks.append("".join(current))
            current = []
            size = 0
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


def _chunk_codeblock(text: str) -> List[str]:
    safe = text.replace("```", "`` `")
    max_body = MAX_MESSAGE_LEN - 6
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for line in safe.splitlines(keepends=True):
        if size + len(line) > max_body and current:
            chunks.append("".join(current).rstrip("\n"))
            current = []
            size = 0
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current).rstrip("\n"))
    return chunks


def _format_status_message(lines: List[str]) -> str:
    safe_lines = [line.replace("```", "`` `") for line in lines]
    body = "\n".join(safe_lines)
    max_body = MAX_MESSAGE_LEN - 6  # code block overhead
    if len(body) > max_body:
        trimmed: List[str] = []
        size = 0
        for line in reversed(safe_lines):
            extra = len(line) + 1
            if size + extra > max_body - 4:
                break
            trimmed.append(line)
            size += extra
        trimmed.reverse()
        body = "...\n" + "\n".join(trimmed) if trimmed else body[-max_body:]
    return f"```\n{body}\n```"


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _extract_section(text: str, heading: str) -> Optional[str]:
    marker = f"## {heading}"
    if marker not in text:
        return None
    lines = text.splitlines()
    capture = False
    collected: List[str] = []
    for line in lines:
        if line.strip() == marker:
            capture = True
            continue
        if capture and line.startswith("## "):
            break
        if capture:
            collected.append(line)
    result = "\n".join(collected).strip()
    return result or None


def _extract_option_int(options: List[dict], name: str) -> Optional[int]:
    for opt in options:
        if opt.get("name") == name:
            try:
                return int(opt.get("value"))
            except (TypeError, ValueError):
                return None
    return None


def _extract_option_str(options: List[dict], name: str) -> Optional[str]:
    for opt in options:
        if opt.get("name") == name:
            value = opt.get("value")
            if value is None:
                return None
            return str(value)
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    load_dotenv()
    config = load_config()
    bot = BughunterBot(config)
    bot.run(config.discord_token)


if __name__ == "__main__":
    main()
