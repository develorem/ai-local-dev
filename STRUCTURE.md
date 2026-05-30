# Repository structure

This is a **monorepo** holding several independent apps plus shared research.
Each top-level app is self-contained and deployable on its own.

```
.
├── orchestrai/        # THE PLATFORM (multiple services, one compose)
│   ├── server/        #   hub: FastAPI + WebSocket + REST API + auth/billing/tenancy
│   ├── ui/            #   hub web UI (vanilla JS SPA served by the hub)
│   ├── agent/         #   worker agent (pure API client; runs tasks)
│   ├── mcp/           #   MCP integration assets
│   ├── scripts/       #   orchestrai-serve etc.
│   ├── docker-compose.yml, Dockerfile.hub, Dockerfile.agent, requirements.txt
│   └── README.md
├── landing/           # MARKETING SITE (standalone static app — see landing/README.md)
├── test-harness/      # original model-benchmark harness (research)
├── docs/              # research write-ups (model recommendation, findings)
├── README.md          # research-repo overview (model decision)
└── OVERNIGHT_PROGRESS.md   # in-flight build log + assumptions
```

## Apps

| App | What it is | How to run |
|---|---|---|
| **orchestrai** | The local-first agentic coding platform (hub + agent + UI + MCP). | `cd orchestrai && docker compose up -d --build` |
| **landing** | Product marketing page. Pure static (HTML/CSS), no build. | open `landing/index.html`, or serve the folder with any static host |
| **test-harness** | Benchmark harness behind the model recommendation. | see `test-harness/README.md` |

## Conventions for adding a new app
- Each app is a **top-level directory** with its own README, build, and deploy.
- Apps communicate over HTTP APIs, not by importing each other's code.
- Shared cross-app concerns (e.g. a future shared design system) get their own
  top-level dir (e.g. `packages/`), not buried inside one app.

## Notes / planned cleanup
- `orchestrai/` currently bundles the hub (server+ui), the agent, and MCP under
  one compose + one requirements.txt. Hub and agent are already separable
  (separate Dockerfiles); if they diverge further, split into
  `orchestrai/hub/` and `orchestrai/agent/` with independent requirements.
- The root `README.md` still documents the original model-research project; the
  product overview lives in `orchestrai/README.md`.
