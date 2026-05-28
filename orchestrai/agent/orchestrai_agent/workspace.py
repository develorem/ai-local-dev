"""Workspace management.

For now, every project gets a directory under /workspace/<project_slug>/.
The directory is git-initialized so `git apply` works on diffs. Workspaces
are transient — destroyed when the agent container is recycled, recreated
on demand the next time a task in that project runs.

Future (when repos are wired up): if task.repo_id is set, the workspace
is a clone of that repo with the relevant branch checked out.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from orchestrai_agent.subprocess_util import run

log = logging.getLogger("orchestrai-agent.workspace")

WORKSPACES_ROOT = Path(os.environ.get("WORKSPACES_ROOT", "/workspace"))


def _slug_dir(project_slug: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (project_slug or "default"))
    return WORKSPACES_ROOT / safe


async def ensure_workspace(project_slug: str) -> Path:
    """Make sure /workspace/<slug>/ exists and is a git repo. Returns the path."""
    WORKSPACES_ROOT.mkdir(parents=True, exist_ok=True)
    path = _slug_dir(project_slug)
    path.mkdir(parents=True, exist_ok=True)

    if not (path / ".git").exists():
        log.info("init git repo at %s", path)
        await run(["git", "init", "-q", "-b", "main"], cwd=str(path), timeout_sec=20)
        await run(["git", "config", "user.email", "agent@orchestrai.local"],
                  cwd=str(path), timeout_sec=10)
        await run(["git", "config", "user.name", "OrchestrAi Agent"],
                  cwd=str(path), timeout_sec=10)
    return path


_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".venv",
              "dist", "build", ".mypy_cache", ".ruff_cache", "coverage"}


def _walk_paths(workspace: Path) -> list[str]:
    """Return all non-skipped file paths in the workspace, POSIX-style."""
    out: list[str] = []
    for root, dirs, fnames in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in sorted(fnames):
            rel = os.path.relpath(os.path.join(root, f), workspace).replace("\\", "/")
            out.append(rel)
    return out


def list_tree(workspace: Path, max_files: int = 200) -> str:
    """Unfiltered tree dump. Kept for handlers that don't have a task context
    (ci_fix). New code should prefer `list_tree_relevant`."""
    files = _walk_paths(workspace)
    if not files:
        return "(empty workspace)"
    if len(files) > max_files:
        return "\n".join(files[:max_files]) + f"\n... ({len(files)}+ files, list truncated)"
    return "\n".join(files)


# Files that are nearly always relevant to ANY task — they're how a human
# orients on a fresh project, and they're small enough not to crowd things.
_ENTRY_POINT_BASENAMES = {
    "readme.md", "readme.rst", "readme",
    "requirements.txt", "requirements.in", "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "package-lock.json", "tsconfig.json",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml", "makefile",
    "app.py", "main.py", "__main__.py", "__init__.py", "server.py", "wsgi.py",
    "index.html", "index.js", "index.ts",
}


def _tokenize(text: str) -> set[str]:
    """Lowercase tokens of length >= 3 from camelCase / snake_case / kebab-case."""
    import re
    if not text:
        return set()
    parts = re.split(r"[^a-zA-Z0-9]+", text)
    # split camelCase: "MyClass" -> ["My", "Class"]
    out: set[str] = set()
    for p in parts:
        for sub in re.findall(r"[A-Za-z][a-z0-9]+|[A-Z]+(?=[A-Z]|$)|\d+", p):
            s = sub.lower()
            if len(s) >= 3:
                out.add(s)
    return out


def _score_path(path: str, *, keyword_tokens: set[str],
                pinned: set[str]) -> int:
    score = 0
    if path in pinned:
        score += 200  # files we know matter — show them first
    base = path.rsplit("/", 1)[-1].lower()
    if base in _ENTRY_POINT_BASENAMES:
        score += 60
    # Match path stem tokens against task keywords. Cheap substring is fine.
    p_lower = path.lower()
    for tok in keyword_tokens:
        if tok in p_lower:
            score += 25
    # Slight bonus for files at the root (more likely to be relevant entry points)
    if "/" not in path:
        score += 10
    return score


def list_tree_relevant(workspace: Path, task: Optional[dict] = None,
                       max_chars: int = 1200) -> str:
    """Return a relevance-ranked textual tree, capped to `max_chars`.

    Scores each path by:
      - +200 if pinned (acceptance_criteria file_exists, or prior attempt's
        files_written/files_modified)
      - +60 if the basename is a well-known entry point (README, app.py, ...)
      - +25 per task-keyword token that appears in the path
      - +10 if the file lives at the workspace root
    Then takes top-scored paths until we hit `max_chars`, and footers the rest.
    """
    paths = _walk_paths(workspace)
    if not paths:
        return "(empty workspace)"

    keyword_tokens: set[str] = set()
    pinned: set[str] = set()
    if task:
        keyword_tokens |= _tokenize(task.get("title") or "")
        keyword_tokens |= _tokenize(task.get("description_md") or "")
        for crit in (task.get("acceptance_criteria") or []):
            if isinstance(crit, dict) and crit.get("kind") == "file_exists":
                p = (crit.get("path") or "").strip().lstrip("/")
                if p:
                    pinned.add(p)
        result = task.get("result") or {}
        for k in ("files_written", "files_modified"):
            for p in (result.get(k) or []):
                if isinstance(p, str):
                    pinned.add(p)

    # Score, sort by (-score, path) for stable ordering
    scored = [(p, _score_path(p, keyword_tokens=keyword_tokens, pinned=pinned))
              for p in paths]
    scored.sort(key=lambda x: (-x[1], x[0]))

    out_lines: list[str] = []
    used = 0
    shown = 0
    for path, _score in scored:
        line_cost = len(path) + 1  # newline
        if used + line_cost > max_chars:
            break
        out_lines.append(path)
        used += line_cost
        shown += 1
    remaining = len(paths) - shown
    if remaining > 0:
        out_lines.append(f"... ({remaining} more files; ask in pass-1 if you need any)")
    return "\n".join(out_lines)


async def write_files(workspace: Path, files: list[dict]) -> tuple[bool, str, list[str]]:
    """Write a list of {path, content} entries directly into the workspace.

    Refuses traversal outside the workspace. Creates parent dirs as needed.
    Returns (ok, error_msg, written_paths).
    """
    written: list[str] = []
    ws_real = workspace.resolve()
    for f in files or []:
        if not isinstance(f, dict):
            return False, f"file entry not a dict: {type(f).__name__}", written
        path = f.get("path") or ""
        content = f.get("content")
        if not path or content is None:
            return False, f"missing path or content in entry: {f}", written
        if path.startswith("/") or ".." in path.split("/"):
            return False, f"refusing absolute or traversal path: {path}", written
        target = (workspace / path).resolve()
        try:
            target.relative_to(ws_real)
        except ValueError:
            return False, f"path escapes workspace: {path}", written
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        written.append(path)
    return True, "ok", written


async def apply_diff(workspace: Path, diff: str) -> tuple[bool, str]:
    """Apply a unified diff via `git apply`. Returns (ok, stderr_tail).

    Strips a leading code-fence wrapper if the model accidentally included one.
    """
    diff = diff.strip()
    # Be tolerant of an embedded fence
    if diff.startswith("```"):
        lines = diff.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        diff = "\n".join(lines)

    # Ensure trailing newline (git apply is finicky)
    if not diff.endswith("\n"):
        diff += "\n"

    async def _try_apply(candidate: str) -> tuple[bool, str]:
        diff_path = workspace / ".orchestrai_pending.diff"
        diff_path.write_text(candidate, encoding="utf-8")
        try:
            check = await run(
                ["git", "apply", "--check", "--whitespace=nowarn", str(diff_path)],
                cwd=str(workspace), timeout_sec=30,
            )
            if check.exit_code != 0:
                return False, f"git apply --check failed:\n{check.stderr}"
            applied = await run(
                ["git", "apply", "--whitespace=nowarn", str(diff_path)],
                cwd=str(workspace), timeout_sec=30,
            )
            if applied.exit_code != 0:
                return False, f"git apply failed:\n{applied.stderr}"
            return True, "ok"
        finally:
            try:
                diff_path.unlink()
            except OSError:
                pass

    ok, err = await _try_apply(diff)
    if ok:
        return True, "ok"

    # Auto-repair: try fixing common LLM diff mistakes and retry once.
    repaired = _autorepair_diff(diff)
    if repaired and repaired != diff:
        ok2, err2 = await _try_apply(repaired)
        if ok2:
            return True, "ok (auto-repaired)"
        return False, f"{err}\n(auto-repair attempted but also failed: {err2})"
    return False, err


def _autorepair_diff(diff: str) -> Optional[str]:
    """Best-effort fix for the most common LLM unified-diff mistakes.

    Heuristic 1: any line inside a hunk body that doesn't start with one of
    `+`, `-`, ` `, or `\\` is a context line that lost its leading space.
    Re-prefix it with a space so `git apply` can parse the hunk.

    Heuristic 2: rebuild the `@@ -a,b +c,d @@` line counts from the actual
    body, so a wrong `b` or `d` doesn't reject the patch.

    Returns None if nothing repairable found, or the repaired diff string.
    """
    import re

    lines = diff.splitlines(keepends=False)
    out: list[str] = []
    i = 0
    repaired_any = False
    HUNK_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(.*)$")
    BODY_PREFIXES = ("+", "-", " ", "\\")

    while i < len(lines):
        line = lines[i]
        m = HUNK_RE.match(line)
        if not m:
            out.append(line)
            i += 1
            continue

        old_start = int(m.group(1))
        new_start = int(m.group(3))
        trailing = m.group(5) or ""

        # Collect the hunk body
        body: list[str] = []
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            # Body ends at the next file/hunk header or top-level boundary
            if (nxt.startswith("@@ ") or nxt.startswith("diff --git ") or
                nxt.startswith("--- ") or nxt.startswith("+++ ")):
                break
            body.append(nxt)
            j += 1

        # Heuristic 1: re-prefix orphan context lines
        repaired_body: list[str] = []
        for b in body:
            if b == "":
                repaired_body.append(" ")  # blank context line
                repaired_any = True
                continue
            if b.startswith(BODY_PREFIXES):
                repaired_body.append(b)
            else:
                repaired_body.append(" " + b)
                repaired_any = True
        # Drop trailing blank-context padding lines (LLMs sometimes pad with empties)
        while repaired_body and repaired_body[-1].strip() == "":
            repaired_body.pop()

        # Heuristic 2: recount
        old_count = sum(1 for b in repaired_body if b.startswith((" ", "-")))
        new_count = sum(1 for b in repaired_body if b.startswith((" ", "+")))
        new_header = f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{trailing}"
        if new_header != line:
            repaired_any = True

        out.append(new_header)
        out.extend(repaired_body)
        i = j

    if not repaired_any:
        return None
    return "\n".join(out) + "\n"


async def commit_all(workspace: Path, message: str) -> Optional[str]:
    """Add + commit all changes. Returns the new commit SHA or None if nothing to commit."""
    await run(["git", "add", "-A"], cwd=str(workspace), timeout_sec=15)
    diff_check = await run(["git", "diff", "--cached", "--quiet"], cwd=str(workspace), timeout_sec=10)
    if diff_check.exit_code == 0:
        return None  # nothing staged
    res = await run(["git", "commit", "-q", "-m", message], cwd=str(workspace), timeout_sec=15)
    if res.exit_code != 0:
        return None
    sha_res = await run(["git", "rev-parse", "HEAD"], cwd=str(workspace), timeout_sec=10)
    return sha_res.stdout.strip()


def read_files(workspace: Path, paths: list[str], max_chars: int = 30_000) -> tuple[dict, list[str]]:
    """Read the requested files from the workspace.

    Returns (contents_dict, missing_list). Caps total bytes at max_chars to keep
    the next-pass prompt within budget.
    """
    contents: dict = {}
    missing: list[str] = []
    total = 0
    for p in paths or []:
        full = (workspace / p).resolve()
        # Refuse traversal outside workspace
        try:
            full.relative_to(workspace.resolve())
        except ValueError:
            missing.append(p)
            continue
        if not full.exists() or not full.is_file():
            missing.append(p)
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except Exception:
            missing.append(p)
            continue
        if total + len(text) > max_chars:
            remaining = max_chars - total
            if remaining > 0:
                text = text[:remaining] + "\n... (truncated)"
            else:
                contents[p] = "(skipped: prompt budget exceeded)"
                continue
        contents[p] = text
        total += len(text)
    return contents, missing
