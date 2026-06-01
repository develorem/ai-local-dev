"""Extract JSON from an LLM response that's supposed to be a single fenced JSON block."""

import json
import re
from typing import Any, Optional

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(response: str) -> Optional[Any]:
    """Return the parsed JSON object if found, else None."""
    if not response:
        return None
    s = response.strip()

    # Try a fenced block first
    m = _FENCE_RE.search(s)
    if m:
        candidate = m.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # Fall back to matched-brace scan
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
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
                try:
                    return json.loads(s[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None
