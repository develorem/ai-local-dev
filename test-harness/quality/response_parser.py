"""Extract a single Python code block from a model response.

The model is asked to reply with code only, but we still defensively handle:
    - ```python ... ``` fenced blocks
    - ``` ... ``` fenced blocks (unlabelled)
    - "Here is the function:" preambles
    - Bare code with no fences
"""

import re

_FENCE_RE = re.compile(
    r"```(?:python|py)?\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def extract_code(response: str) -> str:
    if not response:
        return ""
    m = _FENCE_RE.search(response)
    if m:
        return m.group(1).strip()
    return response.strip()


def extract_json(response: str) -> str:
    """Extract a JSON object from a response, tolerating fences and preamble."""
    if not response:
        return ""
    s = response.strip()
    # strip ```json ... ``` or ``` ... ```
    fence = re.search(r"```(?:json)?\s*\n?(.*?)```", s, re.DOTALL | re.IGNORECASE)
    if fence:
        s = fence.group(1).strip()
    # take the substring from the first { to the matching close
    start = s.find("{")
    if start < 0:
        return s
    depth = 0
    end = -1
    in_string = False
    escape = False
    for i in range(start, len(s)):
        c = s[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return s[start:end] if end > 0 else s[start:]
