"""Render the project-document INDEX + available secret NAMES for agent prompts.

The index is a SEEK structure, not a content dump: each entry is a title, a
one-line purpose (when to consult), and the doc's section headings. The agent
decides which docs it actually needs and asks for them by title in pass 1; the
full text of only those is fetched and shown in pass 2. This keeps the standing
context tiny (16K budget) while the authoritative content is one fetch away.

SECURITY: secret VALUES are never placed in prompts — only names/descriptions,
so the model knows what it may request at runtime (via the audited
/secrets/{name}/value endpoint, after declaring the name in the task's
payload.secrets_needed).
"""

_MAX_INDEX_HEADINGS = 12


def documents_index_block(documents: list | None) -> str:
    """The document index: title + purpose + section headings. No bodies."""
    docs = documents or []
    if not docs:
        return "(no project documents)"
    lines: list[str] = []
    for d in docs:
        title = d.get("title") or "(untitled)"
        purpose = (d.get("purpose") or "").strip()
        headings = d.get("headings") or []
        tag = " [repo]" if d.get("source") == "repo" else ""
        lines.append(f'  - "{title}"{tag} — ' + (purpose or "(indexing…)"))
        if headings:
            lines.append("    sections: " + ", ".join(headings[:_MAX_INDEX_HEADINGS]))
    return "\n".join(lines)


def requested_documents_block(contents: dict | None) -> str:
    """Full text of the docs the agent asked to read (keyed by title)."""
    contents = contents or {}
    if not contents:
        return "(none requested)"
    out: list[str] = []
    for title, body in contents.items():
        out.append(f"## {title}\n{(body or '').strip()}")
    return "\n\n".join(out)


# Back-compat: a couple of call sites still import documents_block. It now just
# renders the index (no bodies) so nothing injects full content unasked-for.
def documents_block(documents: list | None, budget: int = 2500) -> str:
    return documents_index_block(documents)


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
