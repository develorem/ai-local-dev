# OrchestrAi — UI

The browser app served by the Hub at `:8080`. Five primary screens plus a global notification area. Left nav for top-level navigation, deep links into details, live updates via WebSocket.

## Frame

```
┌──────────────┬──────────────────────────────────────────────────┐
│ OrchestrAi   │  <breadcrumb>                       🔔 3 pending │
│              ├──────────────────────────────────────────────────┤
│ ▸ Agents     │                                                  │
│ ▸ Projects   │                                                  │
│ ▸ Vault      │                  main content                    │
│              │                                                  │
│ ─── status   │                                                  │
│ Hub: ok      │                                                  │
│ Ollama: ok   │                                                  │
│ 1 agent      │                                                  │
└──────────────┴──────────────────────────────────────────────────┘
```

- Left rail: three nav items + persistent status indicators (Hub, Ollama, agent count)
- Top bar: contextual breadcrumb + notification bell (count of pending questions across the system)
- Main: the active screen

All updates push live via WebSocket — no page refreshes. Agent transitions from `idle` → `busy`, task counts ticking up, new questions appearing — all animated in.

## Screen 1 — Agents

The landing screen. What's working right now.

```
Agents                                                    [Add Agent…]

┌───────────────────────────────────────────────────────────────────┐
│ ● agent@steven-desktop      busy        for 3m 12s                │
│   host: steven-desktop      v0.1.0      registered: 2h ago        │
│   ─────────────────────────────────────────────────────────────── │
│   Current: implement "Add /signup endpoint"                       │
│   Project: locate2u-microservices  ·  Repo: user-service          │
│   Branch:  feature/signup-flow                                    │
│   Next likely: review "Add /signup endpoint"  (1 ready, 2 blocked)│
└───────────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────────┐
│ ○ agent@laptop-2           idle                                   │
│   host: laptop-2           v0.1.0       last seen: 12s ago        │
│   No tasks ready that this agent can pick up                      │
└───────────────────────────────────────────────────────────────────┘
```

Each card shows:
- Status indicator (`●` busy / `○` idle / `✕` lost / `▢` released)
- Name + host + version + last heartbeat
- For busy agents: current task title (clickable), project, repo, branch, time on task
- "Next likely" — a peek at the highest-priority claimable task this agent would pick up next (queue depth indicator)
- Click anywhere on the card → Screen 2 (Agent detail)

Actions per agent (kebab menu):
- Restart (sends a release signal; the supervisor restarts the container)
- Drop lease (forcibly reclaim the task even if not yet expired)
- Mark lost (admin action; flips status without waiting for heartbeat timeout)

## Screen 2 — Agent detail

Everything we know about one agent.

```
Agents › agent@steven-desktop                            [Restart] [Release]

Status      busy  ●                                  Last heartbeat: 2s ago
Host        steven-desktop                           Registered: 2h ago
Version     0.1.0                                    Capabilities: gpu,
                                                                   docker-cli,
                                                                   linux, node, python

─── Current task ───────────────────────────────────────────────────────────
implement: Add /signup endpoint   ·  feature/signup-flow @ user-service
Started 3m 12s ago · attempt 1/3 · lease expires in 18s (extends on heartbeat)
[Open task detail →]

─── Live activity ─────────────────────────────────────────────────────────
14:23:08  task.claimed                              (agent@steven-desktop)
14:23:08  llm.call.started        prompt: planner_v3, ctx=16384
14:23:14  llm.call.completed      gen=82 t/s, prompt=3450 tok, gen=612 tok
14:23:14  task.progress           step: implementer_pass_1
14:23:21  workspace.checkout      user-service @ feature/signup-flow
14:23:22  subprocess.started      git fetch origin
14:23:24  subprocess.completed    exit=0  (1.2s)
…

─── Recent tasks (last 5) ─────────────────────────────────────────────────
✓ implement: scaffold FastAPI app          2h ago    user-service       ⟶
✓ implement: add user model                1h ago    user-service       ⟶
✗ implement: add migrations                1h ago    failed (retry 3/3) ⟶
✓ review:    add user model                45m ago   user-service       ⟶
… (more)                                                          [View all]
```

The live activity feed is the event stream filtered to this agent's events, oldest → newest. Useful for "what is it doing right now?"

Recent tasks summary lets you quickly inspect what this agent has been doing. Click a row → task detail.

## Screen 3 — Projects (list and detail)

The product / system view.

```
Projects                                                  [Add Project…]

┌───────────────────────────────────────────────────────────────────┐
│  locate2u-microservices                                            │
│  3 repos · 5 active goals · 2 tasks running · 1 needs your answer  │
│  [View →]                                                          │
└───────────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────────┐
│  weekend-side-project                                              │
│  1 repo · 1 active goal · 0 tasks running                          │
│  [View →]                                                          │
└───────────────────────────────────────────────────────────────────┘
```

Clicking into a project:

```
Projects › locate2u-microservices                         [Edit] [Archive]

Description
  Locate2u's split-microservices stack. FastAPI services, Postgres, …

Stack         python 3.12, fastapi, postgres, redis, react+ts
Conventions   snake_case (py), camelCase (ts), tests/ next to source
              shared user model from libs/auth
                                                            [Edit context]

─── Repos ──────────────────────────────────────────────────────────
api-gateway      service    main · 1 active branch  feature/routing-X    ⟶
user-service     service    main · 2 active branches                     ⟶
billing-service  service    main · idle                                  ⟶
libs/auth        shared-lib main · idle                          [Add repo]

─── Goals ──────────────────────────────────────────────────────────
► Add user authentication       active       7/12 tasks done             ⟶
  Add billing subscription      planning     awaiting your approval  🔔  ⟶
✓ Migrate to Python 3.12        done                                     ⟶

─── Tasks ──────────────────────────────────────────────────────────
[All] [Ready] [In progress] [Blocked on human] [Done] [Failed]

🟢 implement  Add /signup endpoint        in_progress  agent@steven-desktop ⟶
🟡 review     Add /signup endpoint        ready                            ⟶
⏸  implement  Add /login endpoint         blocked_on_dep (depends ↑)       ⟶
🛑 implement  Migrate auth tables          blocked_on_human  🔔             ⟶
✓  implement  Add user model              done                              ⟶

─── Discussions ───────────────────────────────────────────────────
💬 "Should we use Redis for sessions?"     open · 4 msgs                  ⟶
💬 "Subscription pricing tiers"            open · 12 msgs                 ⟶

```

The "Edit context" button opens a modal where you edit the project's `context_md` — the description agents read when picking up tasks. The UI shows a token-count estimate so you keep it tight (see `PROMPTS.md` for guidance).

Tasks tab supports filtering, search, and sorting. Real-time updates: as agents pick tasks up, the indicators move from yellow (ready) → green (in_progress) → ✓ (done).

## Screen 4 — Task detail

Reachable from anywhere a task title appears.

```
Projects › locate2u-microservices › Tasks › Add /signup endpoint

State          in_progress  🟢                  Priority    normal
Type           implement                        Attempt     1 / 3
Started        3m 12s ago                       Lease       expires 18s
Agent          agent@steven-desktop →           Repo        user-service
Branch         feature/signup-flow              Goal        Add user authentication →

─── Description ─────────────────────────────────────────────────────────
Add a POST /signup endpoint that accepts {email, password}, hashes the
password with argon2, creates a User row, and returns {user_id}. Returns
409 if email already exists.

─── Acceptance criteria ─────────────────────────────────────────────────
  ☐ tests/test_signup.py passes
  ☐ /signup returns 201 with {user_id}
  ☐ /signup returns 409 on duplicate email
  ☐ Password is argon2-hashed, never stored plaintext

─── Live progress ───────────────────────────────────────────────────────
14:23:14  Pass 1: planning files to read/write
          → wants: src/main.py, src/models.py, src/security.py
          → will create: src/routes/signup.py, tests/test_signup.py
14:23:19  Pass 2: generating diff
          (612 tokens · 82 t/s · 7.4s)
14:23:21  Applying diff
14:23:22  Running: pytest tests/test_signup.py -q
…

─── Diff preview ────────────────────────────────────────────────────────
[Live-updating as the agent works; final diff settles when task transitions]

─── History ─────────────────────────────────────────────────────────────
14:18:00  created                                  (planner@agent)
14:23:08  status_changed     ready → in_progress
14:23:08  claimed            agent@steven-desktop

─── Open questions ──────────────────────────────────────────────────────
(none currently)

[Cancel task]  [Open discussion]  [Edit task]
```

Key UX bits:
- "Agent" field is clickable — jumps to Screen 2 for that agent
- "Goal" field is clickable — jumps to the goal-level view
- "Repo" field is clickable — jumps to the repo view inside the project
- Live diff preview streams in as the agent works
- Once done, the task detail shows the final diff and command outputs (stdout/stderr per command)
- "Open discussion" creates a discussion attached to this task at `critical` priority

When the task is `blocked_on_human`, the question(s) render inline with answer controls:

```
─── Question (clarification) ────────────────────────────────────────────
The existing /users endpoint uses snake_case (user_id) but the new spec
suggests camelCase (userId). Which should I use for new endpoints?

  ◯ snake_case      (matches existing)
  ◯ camelCase       (matches new spec)
  ◯ Other / Discuss

[Answer]  or  [Open discussion]
```

## Screen 5 — Key Vault

```
Key Vault                                                  [Add Secret…]

┌───────────────────────────────────────────────────────────────────┐
│ GITHUB_TOKEN                                            [Rotate] [⋯]│
│ scope: global · last used 12s ago · 47 accesses (last 7d)         │
│ description: GitHub access for clone, push, gh CLI                │
│ [View audit log →]                                                │
└───────────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────────┐
│ ANTHROPIC_API_KEY                                       [Rotate] [⋯]│
│ scope: global · never used                                        │
│ description: For future cloud-model fallback                      │
└───────────────────────────────────────────────────────────────────┘
```

Click "View audit log":

```
GITHUB_TOKEN  ·  audit log

When                Result    Agent                      Task
14:23:21            issued    agent@steven-desktop       Add /signup endpoint  ⟶
13:48:02            issued    agent@steven-desktop       Add user model        ⟶
12:30:14            denied    agent@laptop-2             (no_active_task)
…
```

Add Secret modal:

```
Name         [GITHUB_TOKEN              ]   (UPPER_SNAKE_CASE)
Value        [••••••••••••••••••••••••••]   write-only; cannot be read back
Description  [GitHub access for clone, push, gh CLI                  ]
Scope        ◉ Global  ◯ Project ▼  ◯ Repo ▼

[Save]
```

Once saved, value is gone from the UI forever. To change it, "Rotate" (which is just another write-only entry).

## Global notification bell

Top-right, always visible. Shows count of:
- Pending questions
- Pending proposed-actions
- Failed tasks awaiting human

Click to drop down a list:

```
🔔 3 items need your attention

  ❓ Add /signup endpoint  →  Which case style?
  📋 Plan ready: "Add billing subscription"
  ❗ Task failed (3 retries): Migrate auth tables
```

Each item is clickable, taking you to the relevant task/plan/question.

## Navigation flow

```
Agents ── click agent ──► Agent detail
   │                        │
   │ click current task     │ click recent task
   ▼                        ▼
Task detail ◄───── click task ───── Projects › Project view
   │                                            │
   │ click "Goal"                               │ click goal
   ▼                                            ▼
Goal view ◄──────────────────────── Project view
   │
   │ click "Agent"
   ▼
Agent detail
```

Everything cross-links. No dead ends.

## State for the UI

The UI is a thin shell over the REST + WebSocket API. State management:

- On load: fetch initial state (`/api/agents`, `/api/projects` etc.) per current screen
- Subscribe to `/api/events` WebSocket with `?since=<latest-event-id>` to fill the gap
- Apply incoming events to local state (reducer pattern)
- For screens not currently visible, lazy-fetch on navigate

This keeps the UI cheap (only one WebSocket regardless of how many screens are open in browser tabs) and self-healing (the `since` parameter recovers from brief disconnects).

## Tech (recommendation, not commitment)

- Vanilla JS + small reactivity library (Alpine, htmx, or Svelte) — no build pipeline required
- Tailwind utility CSS for the layout
- WebSocket via native `WebSocket` API
- Diff rendering: `diff2html` for unified-diff display
- Markdown: `marked` for descriptions / plans / messages
- Code editing (for task descriptions, project context): `CodeMirror 6`

All loaded from CDN initially; bundled later if we feel the latency.

## What we don't build in v1

- Themes / dark mode (single style; can revisit)
- Mobile responsive (designed for desktop browser primarily)
- Real-time presence ("user X is editing this") — single user, doesn't apply
- Inline file editing (the UI views, the agent writes; user does file edits in their IDE)
- Replay/scrubbing of historical events (just lists them in order; no scrubber)
