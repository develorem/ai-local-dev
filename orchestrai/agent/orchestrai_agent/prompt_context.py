"""Render project documents + available secret NAMES for agent prompts.

SECURITY: secret VALUES are never placed in prompts — only names/descriptions,
so the model knows what it may request at runtime (via the audited
/secrets/{name}/value endpoint, after declaring the name in the task's
payload.secrets_needed). Document content IS injected (it's project context),
but bounded so it can't blow the 16K context budget.
"""


def documents_block(documents: list | None, budget: int = 2500) -> str:
    docs = [d for d in (documents or [])
            if (d.get("content_md") or "").strip() or d.get("title")]
    if not docs:
        return "(no project documents)"
    out: list[str] = []
    used = 0
    for d in docs:
        title = d.get("title") or "(untitled)"
        body = (d.get("content_md") or "").strip()
        header = f"## {title}\n"
        remaining = budget - used - len(header)
        if remaining <= 0:
            out.append(f"## {title}\n(omitted — context budget reached)")
            break
        if len(body) > remaining:
            body = body[:remaining].rstrip() + "\n…(truncated)"
        out.append(header + body)
        used += len(header) + len(body)
    return "\n\n".join(out)


def secrets_block(secret_names: list | None) -> str:
    items = secret_names or []
    if not items:
        return "(no secrets available)"
    lines = []
    for s in items:
        scope = str(s.get("scope") or "global")
        tag = "project" if scope.startswith("project:") else "global"
        desc = (s.get("description") or "").strip()
        lines.append(f"  - {s.get('name')}" + (f" — {desc}" if desc else "") + f" ({tag})")
    return "\n".join(lines)
