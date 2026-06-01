"""Outline large files: keep structure, elide bodies the task doesn't need.

For pass-2, the LLM needs to see file contents so it can produce a diff or a
full rewrite. Sending a 5,000-line file when the task only touches one
function wastes most of the 16 K window. This module compacts a file to:

  - all imports / top-level statements / decorators
  - every class declaration
  - every def / async def SIGNATURE (and docstring if short)
  - the FULL body of any def/method whose name matches a task keyword
  - "# (body elided: N lines)" placeholder where bodies were dropped

It tolerates syntax errors (line-based heuristic, not the `ast` module) so
work-in-progress files are still summarisable.

The prompt instructs the LLM that elided bodies mean it MUST rewrite via
`files[]` rather than `diff` — a diff against fictional context lines would
fail to apply.
"""

import re
from typing import Iterable

# Threshold below which we just send the whole file. Picked so test files,
# small modules, configs, and templates fly through unchanged.
DEFAULT_FULL_THRESHOLD = 2500

_DEF_RE = re.compile(
    r"^(?P<indent>\s*)"                                    # leading indent
    r"(?:async\s+)?"                                       # optional async
    r"(?P<kw>def|class)\s+"                                # def / class
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"                   # symbol name
)


def _line_indent(line: str) -> int:
    stripped = line.lstrip(" \t")
    return len(line) - len(stripped)


def outline_python(text: str, keep_names: Iterable[str]) -> tuple[str, int]:
    """Return (outlined_text, elided_line_count). `keep_names` is a set of
    lowercase symbol names whose bodies should NOT be elided.

    Algorithm: line-based scan. Whenever we see `def`/`async def`/`class`,
    check the name; if not in keep_names AND it's not the top-level class
    container (we want to keep class skeletons), elide the body — every
    subsequent line indented STRICTLY MORE than the def/class header,
    skipping blanks — and emit a one-line placeholder in its place.

    Classes themselves are NEVER elided wholesale; we recurse INTO them so
    method signatures stay visible. Only function/method *bodies* get
    replaced.
    """
    keep_lower = {n.lower() for n in (keep_names or [])}
    lines = text.splitlines()
    out: list[str] = []
    elided_total = 0
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = _DEF_RE.match(line)
        if not m:
            out.append(line)
            i += 1
            continue

        kw = m.group("kw")
        name = m.group("name")
        indent = len(m.group("indent"))

        # Class headers stay as-is and we keep descending; method bodies
        # below will be examined on their own iterations.
        if kw == "class":
            out.append(line)
            i += 1
            continue

        # def / async def — keep the body if ANY keyword token appears in
        # the function name. We use substring match (not equality) so
        # `list_users` is kept when keywords are {"users", "list"}.
        name_lower = name.lower()
        if any(tok in name_lower for tok in keep_lower):
            out.append(line)
            i += 1
            continue

        # Elide this def's body. Find end-of-body: next non-blank line whose
        # indent <= the def's indent.
        out.append(line)
        j = i + 1
        body_start = j
        while j < n:
            nxt = lines[j]
            if nxt.strip() == "":
                j += 1
                continue
            if _line_indent(nxt) <= indent:
                break
            j += 1
        body_len = j - body_start  # includes blanks (visually faithful)
        if body_len > 0:
            placeholder_indent = " " * (indent + 4)
            out.append(
                f"{placeholder_indent}...  # body elided ({body_len} lines) — "
                f"rewrite via files[], not diff"
            )
            elided_total += body_len
        i = j
    return "\n".join(out), elided_total


def _head_tail(text: str, keep_chars: int) -> str:
    """Non-Python large-file fallback: keep head and tail, elide the middle."""
    if len(text) <= keep_chars:
        return text
    half = keep_chars // 2 - 40
    head = text[:half]
    tail = text[-half:]
    elided_chars = len(text) - len(head) - len(tail)
    return f"{head}\n# ... ({elided_chars} chars elided — middle of file) ...\n{tail}"


def maybe_outline(path: str, content: str, keep_names: Iterable[str],
                  full_threshold: int = DEFAULT_FULL_THRESHOLD) -> tuple[str, dict]:
    """Return (compacted_content, info). info: {kind, original_chars, final_chars, elided_lines}."""
    orig = len(content)
    info = {"kind": "full", "original_chars": orig, "final_chars": orig,
            "elided_lines": 0}
    if orig <= full_threshold:
        return content, info
    lower_path = path.lower()
    if lower_path.endswith(".py"):
        outlined, elided = outline_python(content, keep_names)
        info.update(kind="outlined_py", final_chars=len(outlined), elided_lines=elided)
        return outlined, info
    # Fallback for non-Python large files: head+tail snippet
    snippet = _head_tail(content, keep_chars=full_threshold)
    info.update(kind="head_tail", final_chars=len(snippet))
    return snippet, info
