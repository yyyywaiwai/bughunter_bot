from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional


class CommandError(RuntimeError):
    def __init__(self, message: str, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr

    def __str__(self) -> str:
        base = super().__str__()
        detail = (self.stderr or self.stdout).strip()
        if detail:
            return f"{base}\n{detail}"
        return base


def run_cmd(cmd: List[str], cwd: Path, env: Optional[dict] = None) -> str:
    if not cmd:
        raise ValueError("Empty command")
    if cmd[0] not in {"git", "gh"}:
        raise CommandError(f"Command not allowed: {cmd[0]}")
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CommandError(
            f"Command failed: {' '.join(cmd)}",
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return result.stdout.strip()


def git_pull(repo_path: Path) -> None:
    run_cmd(["git", "-C", str(repo_path), "pull", "--ff-only"], cwd=repo_path)


def git_is_clean(repo_path: Path) -> bool:
    output = run_cmd(["git", "-C", str(repo_path), "status", "--porcelain"], cwd=repo_path)
    return output.strip() == ""


def git_fetch(repo_path: Path) -> None:
    run_cmd(["git", "-C", str(repo_path), "fetch", "--all"], cwd=repo_path)


def git_worktree_add(repo_path: Path, worktree_path: Path, branch: str, base_branch: str) -> None:
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(
        [
            "git",
            "-C",
            str(repo_path),
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree_path),
            base_branch,
        ],
        cwd=repo_path,
    )


def git_worktree_remove(repo_path: Path, worktree_path: Path) -> None:
    run_cmd(
        ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(worktree_path)],
        cwd=repo_path,
    )


def git_commit_all(worktree_path: Path, message: str) -> None:
    run_cmd(["git", "-C", str(worktree_path), "add", "-A"], cwd=worktree_path)
    run_cmd(["git", "-C", str(worktree_path), "commit", "-m", message], cwd=worktree_path)


def git_push(worktree_path: Path, branch: str) -> None:
    run_cmd(["git", "-C", str(worktree_path), "push", "-u", "origin", branch], cwd=worktree_path)


def git_current_branch(worktree_path: Path) -> str:
    return run_cmd(
        ["git", "-C", str(worktree_path), "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=worktree_path,
    )


def gh_pr_create(repo_path: Path, title: str, body: str, head: str, base: str) -> str:
    output = run_cmd(
        [
            "gh",
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--head",
            head,
            "--base",
            base,
        ],
        cwd=repo_path,
    )
    return output
