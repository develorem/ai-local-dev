// OrchestrAi UI — vanilla JS, hash routes, WebSocket live updates.

const API = '/api';
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// ---- Auth: the operator token (if the hub requires one) ------------------
let AUTH_TOKEN = localStorage.getItem('orchestrai_token') || '';
let _loginShown = false;

function showLogin() {
  if (_loginShown) return;
  _loginShown = true;
  const c = document.getElementById('content');
  c.innerHTML = `
    <h1>Sign in</h1>
    <p class="muted">This OrchestrAi hub requires an operator token.</p>
    <div style="display:flex;gap:8px;align-items:center;max-width:420px;">
      <input id="op-token" type="password" placeholder="Operator token"
             style="flex:1;" />
      <button id="op-login">Sign in</button>
    </div>`;
  const submit = () => {
    const t = document.getElementById('op-token').value.trim();
    if (!t) return;
    localStorage.setItem('orchestrai_token', t);
    location.reload();
  };
  document.getElementById('op-login').onclick = submit;
  document.getElementById('op-token').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submit();
  });
}

function signOut() {
  localStorage.removeItem('orchestrai_token');
  location.reload();
}

// ---- Toast notifications -------------------------------------------------
function toast(msg, kind = 'info', ttlMs = 3500) {
  let host = document.getElementById('toast-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toast-host';
    host.style.cssText = 'position:fixed;top:60px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:8px;';
    document.body.appendChild(host);
  }
  const t = document.createElement('div');
  const bg = kind === 'error' ? '#5a1f1f' : kind === 'success' ? '#1f4a26' : '#1f3a52';
  const border = kind === 'error' ? '#f85149' : kind === 'success' ? '#3fb950' : '#4f9eff';
  t.style.cssText = `background:${bg};border:1px solid ${border};color:#fff;padding:10px 14px;border-radius:6px;` +
                    `min-width:240px;max-width:420px;box-shadow:0 4px 12px rgba(0,0,0,0.4);font-size:13px;` +
                    `animation:fadeIn 0.2s;cursor:pointer;`;
  t.textContent = msg;
  t.onclick = () => t.remove();
  host.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity 0.3s'; }, ttlMs - 300);
  setTimeout(() => t.remove(), ttlMs);
}

const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
};

const pill = (status) => el('span', { class: `pill pill-${status}` }, status);

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (AUTH_TOKEN) headers['Authorization'] = `Bearer ${AUTH_TOKEN}`;
  const res = await fetch(API + path, {
    ...opts,
    headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) {
    showLogin();
    throw new Error('401: unauthorized');
  }
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`${res.status}: ${txt}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

const fmtTime = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString();
};

const fmtDuration = (iso) => {
  if (!iso) return '—';
  const sec = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec/60)}m ${sec%60}s`;
  if (sec < 86400) return `${Math.floor(sec/3600)}h ${Math.floor((sec%3600)/60)}m`;
  return `${Math.floor(sec/86400)}d`;
};

// -------- Live state from WebSocket -------------------------------------
let ws = null;
const wsListeners = new Set();
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const q = AUTH_TOKEN ? `?token=${encodeURIComponent(AUTH_TOKEN)}` : '';
  ws = new WebSocket(`${proto}://${location.host}/api/events${q}`);
  ws.onopen = () => console.log('[ws] connected');
  ws.onmessage = (msg) => {
    try {
      const frame = JSON.parse(msg.data);
      if (frame.type === 'event' && frame.event) {
        wsListeners.forEach(fn => { try { fn(frame.event); } catch (e) { console.error(e); } });
      }
    } catch {}
  };
  ws.onclose = () => {
    console.log('[ws] disconnected — reconnecting in 2s');
    setTimeout(connectWS, 2000);
  };
  ws.onerror = (e) => console.error('[ws] error', e);
}
function onEvent(fn) { wsListeners.add(fn); return () => wsListeners.delete(fn); }

// -------- Periodic health refresh ---------------------------------------
async function refreshHealth() {
  try {
    const h = await api('/health');
    $('#hub-status').textContent = h.status;
    $('#hub-status').className = h.status === 'ok' ? 'pill pill-active' : 'pill pill-failed';
    $('#ollama-status').textContent = h.ollama.reachable ? 'ok' : 'down';
    $('#ollama-status').className = h.ollama.reachable ? 'pill pill-active' : 'pill pill-failed';
    $('#agent-count').textContent = `${h.agents.connected} (${h.agents.busy} busy, ${h.agents.lost} lost)`;
  } catch (e) {
    $('#hub-status').textContent = 'unreachable';
    $('#hub-status').className = 'pill pill-failed';
  }
}

async function refreshNotifications() {
  try {
    const data = await api('/questions?status=pending&limit=200');
    $('#notif-count').textContent = data.items.length;
    window._pendingQuestions = data.items;
  } catch {}
}

// ---- Notification dropdown -----------------------------------------------
function closeNotifDropdown() {
  const d = document.getElementById('notif-dropdown');
  if (d) d.remove();
}
async function toggleNotifDropdown() {
  if (document.getElementById('notif-dropdown')) { closeNotifDropdown(); return; }
  await refreshNotifications();
  const items = window._pendingQuestions || [];
  const dd = document.createElement('div');
  dd.id = 'notif-dropdown';
  dd.style.cssText = 'position:fixed;top:48px;right:24px;width:380px;max-height:60vh;' +
    'overflow-y:auto;background:var(--surface);border:1px solid var(--border);' +
    'border-radius:6px;box-shadow:0 4px 16px rgba(0,0,0,0.4);z-index:9000;padding:8px;';
  dd.onclick = (e) => e.stopPropagation();
  if (items.length === 0) {
    dd.appendChild(el('p', { class: 'muted', style: 'padding:8px;margin:0;' }, 'No pending questions.'));
  } else {
    items.forEach(q => {
      const row = el('div', { style: 'padding:8px;border-bottom:1px solid var(--border);cursor:pointer;',
        onClick: () => {
          location.hash = `#/tasks/${q.task_id}`;
          closeNotifDropdown();
        }},
        el('div', { style: 'font-size:11px;color:var(--muted);text-transform:uppercase;' }, q.kind),
        el('div', { style: 'margin:4px 0;font-size:13px;' }, q.prompt_md.substring(0, 200)),
        q.task_title ? el('div', { class: 'muted', style: 'font-size:11px;' },
          `${q.task_title}${q.goal_title ? ' · ' + q.goal_title : ''}`) : null);
      dd.appendChild(row);
    });
  }
  document.body.appendChild(dd);
}

// -------- Routing -------------------------------------------------------
const routes = {};
function route(path, fn) { routes[path] = fn; }

function matchRoute(hash) {
  const path = hash.replace(/^#?\/?/, '/');
  for (const [pattern, fn] of Object.entries(routes)) {
    const regex = new RegExp('^' + pattern.replace(/:[a-z_]+/g, '([^/]+)') + '$');
    const m = path.match(regex);
    if (m) {
      const params = {};
      const names = (pattern.match(/:[a-z_]+/g) || []).map(s => s.slice(1));
      names.forEach((n, i) => { params[n] = m[i + 1]; });
      return { fn, params };
    }
  }
  return null;
}

function setActiveNav(name) {
  $$('.sidebar nav a').forEach(a => {
    a.classList.toggle('active', a.dataset.nav === name);
  });
}

function setBreadcrumb(parts) {
  $('#breadcrumb').innerHTML = '';
  parts.forEach((p, i) => {
    if (i > 0) $('#breadcrumb').appendChild(document.createTextNode(' › '));
    if (p.href) {
      const a = el('a', { href: p.href }, p.label);
      a.style.color = 'inherit';
      a.style.textDecoration = 'none';
      $('#breadcrumb').appendChild(a);
    } else {
      $('#breadcrumb').appendChild(document.createTextNode(p.label));
    }
  });
}

async function render() {
  const m = matchRoute(location.hash);
  if (!m) {
    location.hash = '#/agents';
    return;
  }
  try {
    await m.fn(m.params);
  } catch (e) {
    $('#content').innerHTML = `<p class="danger">Error: ${e.message}</p>`;
  }
}

window.addEventListener('hashchange', render);

// -------- Screens -------------------------------------------------------

route('/agents', async () => {
  setActiveNav('agents');
  setBreadcrumb([{ label: 'Agents' }]);
  const content = $('#content');
  content.innerHTML = '<h1>Agents</h1>';

  const data = await api('/agents');
  if (data.items.length === 0) {
    content.appendChild(el('div', { class: 'card muted' },
      'No agents registered yet. The first Agent container will appear here when it boots.'));
    return;
  }
  for (const a of data.items) {
    const currentRow = a.current_task_id
      ? el('div', { class: 'muted', style: 'margin-top:4px;' },
          'current: ',
          el('a', { href: `#/tasks/${a.current_task_id}` },
            a.current_task_title || `task ${a.current_task_id.slice(-6)}`),
          a.current_task_type
            ? el('span', { class: 'muted' }, ` (${a.current_task_type})`)
            : null)
      : el('div', { class: 'muted', style: 'margin-top:4px;' }, 'no current task');

    content.appendChild(el('div', { class: 'card' },
      el('div', { class: 'card-row' },
        pill(a.status),
        el('div', { class: 'grow' },
          el('div', {},
            el('a', { href: `#/agents/${a.id}` },
              el('strong', {}, a.name)),
            ' · ', a.host || 'no host'),
          el('div', { class: 'muted' },
            `v${a.version} · last heartbeat ${fmtTime(a.last_heartbeat_at)}`),
          currentRow))));
  }
});

route('/agents/:agent_id', async ({ agent_id }) => {
  setActiveNav('agents');
  const data = await api(`/agents/${agent_id}`);
  const a = data.agent;
  setBreadcrumb([{ label: 'Agents', href: '#/agents' }, { label: a.name }]);
  const content = $('#content');
  content.innerHTML = '';
  content.appendChild(el('h1', {}, a.name));

  // Separate `port:NNNN:proto` advertisements from raw capabilities so we
  // can render the ports as clickable links to whatever the agent is hosting.
  const allCaps = a.capabilities || [];
  const portCaps = allCaps.filter(c => /^port:\d+(:|$)/.test(c));
  const otherCaps = allCaps.filter(c => !/^port:\d+(:|$)/.test(c));
  const portsCell = portCaps.length
    ? el('div', { class: 'agent-ports' },
        ...portCaps.flatMap((c, i) => {
          const m = c.match(/^port:(\d+)(?::(\w+))?/);
          const port = m[1];
          const proto = (m[2] || 'http').toLowerCase();
          const url = `${proto.startsWith('http') ? proto : 'http'}://localhost:${port}`;
          const link = el('a', { href: url, target: '_blank', rel: 'noopener' }, url);
          return i === 0 ? [link] : [' · ', link];
        }))
    : el('div', { class: 'muted' }, '—');
  const kvs = el('div', { class: 'kvs' });
  for (const [k, v] of [
    ['Status', pill(a.status)],
    ['Host', a.host || '—'],
    ['Version', a.version],
    ['Hosting ports', portsCell],
    ['Capabilities', otherCaps.join(', ') || '—'],
    ['Registered', fmtTime(a.registered_at)],
    ['Last heartbeat', fmtTime(a.last_heartbeat_at)],
  ]) {
    kvs.appendChild(el('div', { class: 'k' }, k));
    kvs.appendChild(typeof v === 'string' ? el('div', {}, v) : el('div', {}, v));
  }
  content.appendChild(el('div', { class: 'card' }, kvs));

  // ---- External agents: connection config, project access, delete ----
  if (a.kind === 'external') {
    const origin = location.origin;

    const cmdPre = el('pre', { style: _CODE_STYLE }, '(click "Show config")');
    const copyBtn = el('button', { style: 'display:none;' }, 'Copy command');
    const dlBtn = el('button', { style: 'display:none;margin-left:6px;' }, 'Download .mcp.json');
    const showBtn = el('button', {
      onClick: async () => {
        let cfg;
        try { cfg = await api(`/agents/${agent_id}/config`); }
        catch (e) { toast('Could not load config', 'error'); return; }
        const cmd = `claude mcp add --transport http --header "Authorization: Bearer ${cfg.token}" orchestrai ${origin}/mcp`;
        cmdPre.textContent = cmd;
        copyBtn.style.display = ''; dlBtn.style.display = '';
        copyBtn.onclick = () => copyText(cmd, 'Command copied');
        dlBtn.onclick = () => downloadText('.mcp.json', mcpJsonForAgent(origin, cfg.token));
      } }, 'Show config');
    content.appendChild(el('div', { class: 'card' },
      el('h3', {}, 'Connection'),
      el('div', { class: 'muted', style: 'font-size:12px;margin-bottom:6px;' },
        'Re-download this agent\'s MCP config (carries its token).'),
      el('div', {}, showBtn), cmdPre, el('div', {}, copyBtn, dlBtn)));

    const accHost = el('div', {});
    content.appendChild(el('div', { class: 'card' },
      el('h3', {}, 'Project access'), accHost));
    const renderAcc = async () => {
      accHost.innerHTML = '';
      let items = [];
      try { items = (await api(`/agents/${agent_id}/projects`)).items || []; } catch (e) { /* */ }
      if (!items.length) {
        accHost.appendChild(el('div', { class: 'muted' },
          "Not granted to any project. Grant it from a project's Access section."));
        return;
      }
      for (const it of items) {
        accHost.appendChild(el('div', { style: 'display:flex;align-items:center;gap:8px;margin:3px 0;' },
          el('a', { href: `#/projects/${it.id}` }, it.name),
          it.via === 'kind' ? el('span', { class: 'muted', style: 'font-size:11px;' }, '(via kind grant)') : null,
          it.via === 'agent' ? el('button', {
            onClick: async () => {
              await api(`/projects/${it.id}/agents`, {
                method: 'DELETE', body: { grantee_type: 'agent', grantee: agent_id } });
              toast('Access revoked', 'success'); renderAcc();
            } }, 'Revoke') : null));
      }
    };
    await renderAcc();

    content.appendChild(el('div', { style: 'margin:14px 0;' },
      el('button', {
        onClick: async () => {
          if (!confirm(`Delete agent "${a.name}"? Its token will stop working.`)) return;
          await api(`/agents/${agent_id}`, { method: 'DELETE' });
          toast('Agent deleted', 'success'); location.hash = '#/agents';
        } }, 'Delete agent')));
  }

  if (data.current_task) {
    const t = data.current_task;
    content.appendChild(el('h2', {}, 'Current task'));
    content.appendChild(el('div', { class: 'card' },
      el('div', {}, el('a', { href: `#/tasks/${t.id}` }, t.title)),
      el('div', { class: 'muted' },
        `${t.type} · ${t.branch_name || 'no branch'} · started ${fmtTime(t.started_at)} (${fmtDuration(t.started_at)})`)));
  }

  content.appendChild(el('h2', {}, 'Recent tasks'));
  if ((data.recent_tasks || []).length === 0) {
    content.appendChild(el('p', { class: 'muted' }, 'No tasks yet.'));
  } else {
    const tbl = el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, 'Title'), el('th', {}, 'Type'),
        el('th', {}, 'Status'), el('th', {}, 'Branch'),
        el('th', {}, 'Finished'))),
      el('tbody', {}, ...data.recent_tasks.map(t => el('tr', {},
        el('td', {}, el('a', { href: `#/tasks/${t.id}` }, t.title)),
        el('td', {}, t.type),
        el('td', {}, pill(t.status)),
        el('td', {}, t.branch_name || '—'),
        el('td', { class: 'muted' }, fmtTime(t.finished_at))))));
    content.appendChild(tbl);
  }

  content.appendChild(el('h2', {}, 'Recent events'));
  const hist = el('div', { class: 'history' });
  (data.recent_events || []).forEach(e => {
    hist.appendChild(el('div', {},
      el('span', { class: 'ts' }, fmtTime(e.ts)),
      ' ',
      el('span', { class: 'kind' }, e.kind),
      ' ',
      el('span', { class: 'muted' }, JSON.stringify(e.detail || {}).slice(0, 200))));
  });
  content.appendChild(hist);
});

route('/projects', async () => {
  setActiveNav('projects');
  setBreadcrumb([{ label: 'Projects' }]);
  const content = $('#content');
  content.innerHTML = `
    <h1>Projects <button id="add-project">+ Add Project</button></h1>
    <div id="projects-list"></div>
  `;

  $('#add-project').onclick = () => openProjectModal();

  await refreshProjects();
});

async function refreshProjects() {
  const data = await api('/projects?limit=200');
  const list = $('#projects-list');
  if (!list) return;
  list.innerHTML = '';
  if (data.items.length === 0) {
    list.appendChild(el('p', { class: 'muted' }, 'No projects yet.'));
    return;
  }
  for (const p of data.items) {
    const s = p.stats || {};
    list.appendChild(el('div', { class: 'card' },
      el('div', { class: 'card-row' },
        el('div', { class: 'grow' },
          el('div', {},
            el('a', { href: `#/projects/${p.id}` }, el('strong', {}, p.name)),
            ' · ', el('span', { class: 'muted' }, p.slug)),
          el('div', { class: 'muted' },
            `${s.goals_active || 0} active goals · ${s.tasks_in_progress || 0} running · ` +
            `${s.tasks_ready || 0} ready · ${s.open_questions || 0} need answer`))
      )));
  }
}

function openProjectModal() {
  // Render the form INLINE above the list rather than replacing the page —
  // gives the user context + a fast path back if they cancel.
  let host = document.getElementById('inline-form-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'inline-form-host';
    const list = $('#projects-list');
    if (list && list.parentNode) list.parentNode.insertBefore(host, list);
    else $('#content').appendChild(host);
  }
  host.innerHTML = '';
  const card = el('div', { class: 'card', style: 'max-width:680px;' },
    el('h2', {}, 'New Project'),
    el('form', { class: 'form-grid', onSubmit: async (e) => {
      e.preventDefault();
      const f = e.target;
      const btn = f.querySelector('button[type=submit]');
      btn.disabled = true; btn.textContent = 'Creating…';
      try {
        const result = await api('/projects', { method: 'POST', body: {
          name: f.name.value.trim(),
          slug: f.slug.value.trim(),
          description_md: f.description_md.value,
          context_md: f.context_md.value,
        }});
        toast(`Project "${result.name}" created`, 'success');
        host.innerHTML = '';
        await refreshProjects();
      } catch (err) {
        toast(`Create failed: ${err.message}`, 'error', 6000);
        btn.disabled = false; btn.textContent = 'Create';
      }
    }},
      el('label', {}, 'Name', el('input', { name: 'name', type: 'text', required: true })),
      el('label', {}, 'Slug', el('input', { name: 'slug', type: 'text', required: true,
                                              pattern: '[a-z0-9-]+',
                                              placeholder: 'lowercase-with-dashes' })),
      el('label', {}, 'Description', el('textarea', { name: 'description_md' })),
      el('label', {},
        el('span', {}, 'Context (terse bullets — token-efficient format)'),
        el('textarea', { name: 'context_md',
          placeholder: 'Stack: python 3.12, fastapi\nConventions: snake_case, pytest, tests/ next to source' })),
      el('div', { style: 'display:flex;gap:8px;' },
        el('button', { type: 'submit' }, 'Create'),
        el('button', { type: 'button', class: 'secondary',
          onClick: () => { host.innerHTML = ''; } }, 'Cancel'))));
  host.appendChild(card);
  card.querySelector('input[name=name]').focus();
}

route('/projects/:project_id', async ({ project_id }) => {
  setActiveNav('projects');
  const data = await api(`/projects/${project_id}`);
  const p = data.project;
  setBreadcrumb([{ label: 'Projects', href: '#/projects' }, { label: p.name }]);
  const content = $('#content');
  content.innerHTML = '';
  content.appendChild(el('h1', {}, p.name));

  if (p.description_md) {
    content.appendChild(el('div', { class: 'card' }, p.description_md));
  }
  if (p.context_md) {
    content.appendChild(el('div', { class: 'card' },
      el('h3', {}, 'Context'),
      el('pre', { style: 'white-space:pre-wrap;font-family:monospace;font-size:12px;margin:0;' }, p.context_md)));
  }

  // ---- Required tools (populated by the planner; agent installs at claim) ---
  const tools = p.tools || {};
  const pyPkgs = tools.python_packages || [];
  const nodePkgs = tools.node_packages || [];
  if (pyPkgs.length || nodePkgs.length) {
    const card = el('div', { class: 'card' }, el('h3', {}, 'Required tools'));
    if (pyPkgs.length) {
      card.appendChild(el('div', { class: 'muted', style: 'margin-top:4px;' }, 'Python:'));
      const list = el('div', { class: 'tool-list' });
      pyPkgs.forEach(pkg => list.appendChild(el('span', { class: 'pill' }, pkg)));
      card.appendChild(list);
    }
    if (nodePkgs.length) {
      card.appendChild(el('div', { class: 'muted', style: 'margin-top:8px;' }, 'Node:'));
      const list = el('div', { class: 'tool-list' });
      nodePkgs.forEach(pkg => list.appendChild(el('span', { class: 'pill' }, pkg)));
      card.appendChild(list);
    }
    card.appendChild(el('div', { class: 'muted', style: 'margin-top:6px;font-size:11px;' },
      'Agents pip/npm-install anything missing before each task runs.'));
    content.appendChild(card);
  }

  // ---- Access (which agents may pick up this project's tasks) ---------
  const accessCard = el('div', { class: 'card' },
    el('h3', {}, 'Access'),
    el('div', { class: 'muted', style: 'margin-bottom:6px;font-size:12px;' },
      "Which agents may pick up this project's tasks. No grants = nobody picks it up."));
  const grantsHost = el('div', {});
  accessCard.appendChild(grantsHost);
  content.appendChild(accessCard);

  const renderGrants = async () => {
    grantsHost.innerHTML = '';
    let grants = [], agents = [];
    try { grants = (await api(`/projects/${project_id}/agents`)).items || []; } catch (e) { /* */ }
    try { agents = (await api('/agents')).items || []; } catch (e) { /* */ }
    if (!grants.length) grantsHost.appendChild(el('div', { class: 'muted' }, 'No agents granted.'));
    for (const g of grants) {
      const label = g.grantee_type === 'kind'
        ? (g.grantee === 'worker' ? 'OrchestrAi worker (all instances)' : `kind: ${g.grantee}`)
        : (g.agent_name || g.grantee);
      grantsHost.appendChild(el('div', { style: 'display:flex;align-items:center;gap:8px;margin:3px 0;' },
        el('span', {}, label),
        (g.grantee_type === 'agent' && g.agent_status) ? pill(g.agent_status) : null,
        el('button', {
          onClick: async () => {
            await api(`/projects/${project_id}/agents`, {
              method: 'DELETE', body: { grantee_type: g.grantee_type, grantee: g.grantee } });
            toast('Access revoked', 'success'); renderGrants();
          } }, 'Revoke')));
    }
    const opts = [el('option', { value: 'kind:worker' }, 'OrchestrAi worker')];
    for (const a of agents.filter((a) => a.kind === 'external')) {
      opts.push(el('option', { value: `agent:${a.id}` }, `${a.name} (external)`));
    }
    const addSel = el('select', {}, ...opts);
    grantsHost.appendChild(el('div', { style: 'margin-top:8px;' }, addSel,
      el('button', { style: 'margin-left:6px;',
        onClick: async () => {
          const v = addSel.value; const i = v.indexOf(':');
          await api(`/projects/${project_id}/agents`, {
            method: 'POST', body: { grantee_type: v.slice(0, i), grantee: v.slice(i + 1) } });
          toast('Access granted', 'success'); renderGrants();
        } }, '+ Grant access')));
  };
  await renderGrants();

  // ---- Goals (the most important entry point — keep at top) -----------
  content.appendChild(el('h2', {}, `Goals (${data.goals.length})`,
    el('button', { style: 'margin-left:12px;',
      onClick: () => openGoalForm(project_id) }, '+ Add Goal')));
  content.appendChild(el('div', { id: 'goal-form-host' }));
  if (data.goals.length === 0) {
    content.appendChild(el('p', { class: 'muted' }, 'No goals yet. Click + Add Goal to give the agent something to work on.'));
  } else {
    const tbl = el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, 'Title'), el('th', {}, 'Status'),
        el('th', {}, 'Priority'), el('th', {}, 'Created'))),
      el('tbody', {}, ...data.goals.map(g => el('tr', {},
        el('td', {}, g.title), el('td', {}, pill(g.status)),
        el('td', {}, g.priority),
        el('td', { class: 'muted' }, fmtTime(g.created_at))))));
    content.appendChild(tbl);
  }

  // ---- Repos ----------------------------------------------------------
  content.appendChild(el('h2', {}, `Repos (${data.repos.length})`,
    el('button', { class: 'secondary', style: 'margin-left:12px;',
      onClick: () => openRepoForm(project_id) }, '+ Add Repo')));
  content.appendChild(el('div', { id: 'repo-form-host' }));
  if (data.repos.length === 0) {
    content.appendChild(el('p', { class: 'muted' }, 'No repos yet.'));
  } else {
    const tbl = el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, 'Name'), el('th', {}, 'Role'),
        el('th', {}, 'URL'), el('th', {}, 'Branch'))),
      el('tbody', {}, ...data.repos.map(r => el('tr', {},
        el('td', {}, r.name), el('td', {}, r.role || '—'),
        el('td', { class: 'muted' }, r.url),
        el('td', {}, r.default_branch)))));
    content.appendChild(tbl);
  }

  // ---- Tasks -----------------------------------------------------------
  content.appendChild(el('h2', {}, `Tasks (${data.tasks.length})`,
    el('button', { class: 'secondary', style: 'margin-left:12px;',
      onClick: () => openTaskForm(project_id) }, '+ Add Task (manual)')));
  content.appendChild(el('div', { id: 'task-form-host' }));
  if (data.tasks.length === 0) {
    content.appendChild(el('p', { class: 'muted' }, 'No tasks yet. Tasks usually appear automatically after you add a goal and approve its plan.'));
  } else {
    const tbl = el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, 'Title'), el('th', {}, 'Type'),
        el('th', {}, 'Status'), el('th', {}, 'Branch'),
        el('th', {}, 'Agent'), el('th', {}, 'Created'))),
      el('tbody', {}, ...data.tasks.map(t => el('tr', {},
        el('td', {}, el('a', { href: `#/tasks/${t.id}` }, t.title)),
        el('td', {}, t.type),
        el('td', {}, pill(t.status)),
        el('td', {}, t.branch_name || '—'),
        el('td', { class: 'muted' }, t.assigned_agent_id ? t.assigned_agent_id.slice(-6) : '—'),
        el('td', { class: 'muted' }, fmtTime(t.created_at))))));
    content.appendChild(tbl);
  }
});

function openGoalForm(project_id) {
  const host = $('#goal-form-host');
  if (!host) return;
  host.innerHTML = '';
  const card = el('div', { class: 'card', style: 'max-width:680px;' },
    el('h3', {}, 'New Goal'),
    el('p', { class: 'muted', style: 'margin:0 0 8px 0;' },
      'A planner task is auto-created. Approve the resulting plan to instantiate the implementation tasks.'),
    el('form', { class: 'form-grid', onSubmit: async (e) => {
      e.preventDefault();
      const f = e.target;
      const btn = f.querySelector('button[type=submit]');
      btn.disabled = true; btn.textContent = 'Submitting…';
      try {
        const result = await api('/goals', { method: 'POST', body: {
          project_id,
          title: f.title.value.trim(),
          description_md: f.description_md.value,
          priority: f.priority.value,
        }});
        toast(`Goal submitted — planner task queued`, 'success');
        host.innerHTML = '';
        render();
      } catch (err) {
        toast(`Submit failed: ${err.message}`, 'error', 6000);
        btn.disabled = false; btn.textContent = 'Submit';
      }
    }},
      el('label', {}, 'Title',
        el('input', { name: 'title', type: 'text', required: true,
          placeholder: 'e.g. Add a /health endpoint with passing pytest test' })),
      el('label', {}, 'Description (what the agent should do)',
        el('textarea', { name: 'description_md', required: true, rows: 5,
          placeholder: 'Describe the feature. Be specific about acceptance criteria so the planner can produce structured tests/checks.' })),
      el('label', {}, 'Priority',
        el('select', { name: 'priority' },
          el('option', { value: 'low' }, 'low'),
          el('option', { value: 'normal', selected: true }, 'normal'),
          el('option', { value: 'high' }, 'high'),
          el('option', { value: 'critical' }, 'critical'))),
      el('div', { style: 'display:flex;gap:8px;' },
        el('button', { type: 'submit' }, 'Submit'),
        el('button', { type: 'button', class: 'secondary',
          onClick: () => { host.innerHTML = ''; } }, 'Cancel'))));
  host.appendChild(card);
  card.querySelector('input[name=title]').focus();
}

function openRepoForm(project_id) {
  const host = $('#repo-form-host');
  if (!host) return;
  host.innerHTML = '';
  const card = el('div', { class: 'card', style: 'max-width:680px;' },
    el('h3', {}, 'New Repo'),
    el('p', { class: 'muted', style: 'margin:0 0 8px 0;' },
      'Repos are git remotes the agent can clone into its workspace. Cloning is wired in for future task types; v1 implement tasks use a scratch workspace.'),
    el('form', { class: 'form-grid', onSubmit: async (e) => {
      e.preventDefault();
      const f = e.target;
      const btn = f.querySelector('button[type=submit]');
      btn.disabled = true; btn.textContent = 'Saving…';
      try {
        await api(`/projects/${project_id}/repos`, { method: 'POST', body: {
          name: f.name.value.trim(),
          role: f.role.value.trim() || null,
          url: f.url.value.trim(),
          default_branch: f.default_branch.value.trim() || 'main',
          description_md: f.description_md.value,
        }});
        toast('Repo added', 'success');
        host.innerHTML = '';
        render();
      } catch (err) {
        toast(`Add failed: ${err.message}`, 'error', 6000);
        btn.disabled = false; btn.textContent = 'Save';
      }
    }},
      el('label', {}, 'Name', el('input', { name: 'name', type: 'text', required: true,
        placeholder: 'e.g. api-gateway' })),
      el('label', {}, 'Role (optional — e.g. service, frontend, infra)',
        el('input', { name: 'role', type: 'text' })),
      el('label', {}, 'Git URL',
        el('input', { name: 'url', type: 'text', required: true,
          placeholder: 'https://github.com/org/repo.git' })),
      el('label', {}, 'Default branch',
        el('input', { name: 'default_branch', type: 'text', value: 'main' })),
      el('label', {}, 'Description (1-2 sentences)',
        el('textarea', { name: 'description_md' })),
      el('div', { style: 'display:flex;gap:8px;' },
        el('button', { type: 'submit' }, 'Save'),
        el('button', { type: 'button', class: 'secondary',
          onClick: () => { host.innerHTML = ''; } }, 'Cancel'))));
  host.appendChild(card);
  card.querySelector('input[name=name]').focus();
}

function openTaskForm(project_id) {
  const host = $('#task-form-host');
  if (!host) return;
  host.innerHTML = '';
  const card = el('div', { class: 'card', style: 'max-width:680px;' },
    el('h3', {}, 'New Task (manual)'),
    el('p', { class: 'muted', style: 'margin:0 0 8px 0;' },
      'Most tasks are created automatically by the planner. Use this for ad-hoc work outside a planned goal — e.g. a one-off implement, review, or discuss task.'),
    el('form', { class: 'form-grid', onSubmit: async (e) => {
      e.preventDefault();
      const f = e.target;
      const btn = f.querySelector('button[type=submit]');
      btn.disabled = true; btn.textContent = 'Creating…';
      try {
        await api('/tasks', { method: 'POST', body: {
          project_id,
          type: f.type.value,
          title: f.title.value.trim(),
          description_md: f.description_md.value,
          priority: f.priority.value,
          status: 'ready',
        }});
        toast(`Task created`, 'success');
        host.innerHTML = '';
        render();
      } catch (err) {
        toast(`Create failed: ${err.message}`, 'error', 6000);
        btn.disabled = false; btn.textContent = 'Create';
      }
    }},
      el('label', {}, 'Type',
        el('select', { name: 'type' },
          el('option', { value: 'implement', selected: true }, 'implement'),
          el('option', { value: 'review' }, 'review'),
          el('option', { value: 'discuss' }, 'discuss'),
          el('option', { value: 'review_pr' }, 'review_pr'),
          el('option', { value: 'respond_to_ci_failure' }, 'respond_to_ci_failure'))),
      el('label', {}, 'Title', el('input', { name: 'title', type: 'text', required: true })),
      el('label', {}, 'Description', el('textarea', { name: 'description_md', rows: 4 })),
      el('label', {}, 'Priority',
        el('select', { name: 'priority' },
          el('option', { value: 'low' }, 'low'),
          el('option', { value: 'normal', selected: true }, 'normal'),
          el('option', { value: 'high' }, 'high'),
          el('option', { value: 'critical' }, 'critical'))),
      el('div', { style: 'display:flex;gap:8px;' },
        el('button', { type: 'submit' }, 'Create'),
        el('button', { type: 'button', class: 'secondary',
          onClick: () => { host.innerHTML = ''; } }, 'Cancel'))));
  host.appendChild(card);
  card.querySelector('input[name=title]').focus();
}

route('/tasks/:task_id', async ({ task_id }) => {
  setActiveNav('projects');
  const data = await api(`/tasks/${task_id}`);
  const t = data.task;
  setBreadcrumb([
    { label: 'Tasks' },
    { label: t.title },
  ]);
  const content = $('#content');
  content.innerHTML = '';
  content.appendChild(el('h1', {}, t.title));

  const kvs = el('div', { class: 'kvs' });
  for (const [k, v] of [
    ['Type', t.type],
    ['Status', pill(t.status)],
    ['Priority', t.priority],
    ['Project', t.project_id],
    ['Goal', t.goal_id || '—'],
    ['Repo', t.repo_id || '—'],
    ['Branch', t.branch_name || '—'],
    ['Agent', data.agent ? el('a', { href: `#/agents/${data.agent.id}` }, data.agent.name) : '—'],
    ['Attempts', `${t.attempt_count} / ${t.max_attempts}`],
    ['Created', fmtTime(t.created_at)],
    ['Started', fmtTime(t.started_at)],
    ['Finished', fmtTime(t.finished_at)],
  ]) {
    kvs.appendChild(el('div', { class: 'k' }, k));
    kvs.appendChild(typeof v === 'string' ? el('div', {}, v) : el('div', {}, v));
  }
  content.appendChild(el('div', { class: 'card' }, kvs));

  // Action row — what the human can do with this task right now
  const isTerminal = ['done', 'failed', 'cancelled'].includes(t.status);
  const isFailed = t.status === 'failed';
  const actions = el('div', { class: 'card', style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center;' });
  actions.appendChild(el('span', { class: 'muted', style: 'margin-right:8px;' }, 'Actions:'));

  if (isFailed) {
    actions.appendChild(el('button', {
      onClick: async () => {
        if (!confirm(`Retry "${t.title}"?\nResets attempts to 0 and re-queues for the agent.`)) return;
        try {
          await api(`/tasks/${t.id}/retry`, { method: 'POST', body: {} });
          toast('Task re-queued', 'success');
          render();
        } catch (err) { toast(`Retry failed: ${err.message}`, 'error', 6000); }
      }
    }, 'Retry'));
  }

  if (!isTerminal) {
    actions.appendChild(el('button', { class: 'secondary',
      onClick: async () => {
        if (!confirm(`Cancel "${t.title}"?\nAlso cancels any descendant tasks. This is permanent.`)) return;
        try {
          await api(`/tasks/${t.id}/cancel`, { method: 'POST', body: {} });
          toast('Task cancelled', 'success');
          render();
        } catch (err) { toast(`Cancel failed: ${err.message}`, 'error', 6000); }
      }}, 'Cancel'));
  }

  actions.appendChild(el('button', { class: 'secondary',
    onClick: async () => {
      const note = prompt('Add a note to this task (visible to the agent on next attempt):');
      if (!note) return;
      try {
        await api(`/tasks/${t.id}/notes`, { method: 'POST', body: { note_md: note } });
        toast('Note added', 'success');
        render();
      } catch (err) { toast(`Add note failed: ${err.message}`, 'error', 6000); }
    }}, 'Add note'));

  content.appendChild(actions);

  // Failure / error surface — show prominently when status=failed so the user
  // doesn't have to dig through history to understand what went wrong.
  if (isFailed || (t.error && t.error.trim())) {
    content.appendChild(el('h2', { style: 'color:var(--red);' }, 'Failure details'));
    const errCard = el('div', { class: 'card', style: 'border-color:var(--red);' });
    if (t.error && t.error.trim()) {
      errCard.appendChild(el('div', { class: 'muted', style: 'font-size:12px;' }, 'error'));
      errCard.appendChild(el('pre', {
        style: 'white-space:pre-wrap;font-family:monospace;font-size:12px;margin:4px 0;'
      }, t.error));
    }
    if (t.result && Object.keys(t.result).length > 0) {
      errCard.appendChild(el('div', { class: 'muted', style: 'font-size:12px;margin-top:8px;' }, 'last result'));
      errCard.appendChild(el('pre', {
        style: 'white-space:pre-wrap;font-family:monospace;font-size:11px;margin:4px 0;' +
               'max-height:300px;overflow:auto;background:var(--bg);padding:8px;border-radius:4px;'
      }, JSON.stringify(t.result, null, 2)));
    }
    content.appendChild(errCard);
  }

  // Accumulated notes (from agent retries + human additions) — always shown if present
  if (t.notes && t.notes.trim()) {
    content.appendChild(el('h2', {}, 'Notes'));
    content.appendChild(el('div', { class: 'card' },
      el('pre', {
        style: 'white-space:pre-wrap;font-family:monospace;font-size:12px;margin:0;'
      }, t.notes)));
  }

  if (t.description_md) {
    content.appendChild(el('h2', {}, 'Description'));
    content.appendChild(el('div', { class: 'card' },
      el('pre', { style: 'white-space:pre-wrap;margin:0;font-family:inherit;' }, t.description_md)));
  }

  if ((t.acceptance_criteria || []).length > 0) {
    content.appendChild(el('h2', {}, 'Acceptance criteria'));
    const ul = el('ul', {});
    t.acceptance_criteria.forEach(c => ul.appendChild(el('li', {},
      typeof c === 'string' ? c : JSON.stringify(c))));
    content.appendChild(ul);
  }

  if ((data.questions || []).length > 0) {
    content.appendChild(el('h2', {}, 'Open questions'));
    data.questions.forEach(q => {
      if (q.status !== 'pending') return;
      const card = el('div', { class: 'card' },
        el('div', { class: 'muted' }, q.kind),
        el('pre', { style: 'white-space:pre-wrap;font-family:inherit;margin:8px 0;' }, q.prompt_md));

      // For plan_approval questions, render the actual plan content + task outline
      // so the human knows exactly what they are approving.
      if (q.kind === 'plan_approval' && (data.plans || []).length > 0) {
        const draft = data.plans.find(p => p.status === 'draft') || data.plans[0];
        if (draft) {
          card.appendChild(el('h3', { style: 'margin-top:16px;' },
            `Plan v${draft.version}`,
            el('span', { class: 'muted', style: 'margin-left:8px;font-weight:normal;font-size:12px;' },
              draft.status)));
          if (draft.content_md) {
            card.appendChild(el('pre', {
              style: 'background:var(--bg);padding:12px;border-radius:4px;' +
                     'white-space:pre-wrap;font-family:inherit;font-size:13px;' +
                     'max-height:400px;overflow:auto;border:1px solid var(--border);'
            }, draft.content_md));
          }
          if ((draft.task_outline || []).length > 0) {
            card.appendChild(el('h4', { style: 'margin:12px 0 6px 0;' },
              `Proposed tasks (${draft.task_outline.length})`));
            const list = el('ol', { style: 'margin:0 0 12px 20px;padding:0;' });
            draft.task_outline.forEach(t => {
              const li = el('li', { style: 'margin:10px 0;' },
                el('div', {},
                  el('span', { class: 'pill',
                    style: 'background:var(--surface-2);color:var(--accent);margin-right:6px;' },
                    t.type || 'implement'),
                  el('strong', {}, t.title || '(no title)')));
              if (t.description_md) {
                li.appendChild(el('div', { class: 'muted',
                  style: 'font-size:12px;margin:4px 0;' }, t.description_md));
              }
              if ((t.acceptance_criteria || []).length > 0) {
                const ul = el('ul', { style: 'margin:4px 0;font-size:12px;' });
                t.acceptance_criteria.forEach(c => {
                  const label = typeof c === 'string'
                    ? c
                    : (c.kind === 'test'
                        ? `test: \`${c.cmd}\` exits ${c.expect_exit ?? 0}`
                        : c.kind === 'file_exists'
                        ? `file exists: \`${c.path}\``
                        : JSON.stringify(c));
                  ul.appendChild(el('li', {}, label));
                });
                li.appendChild(ul);
              }
              if ((t.depends_on_titles || []).length > 0) {
                li.appendChild(el('div', { class: 'muted',
                  style: 'font-size:11px;margin-top:4px;' },
                  '↳ depends on: ' + t.depends_on_titles.join(', ')));
              }
              list.appendChild(li);
            });
            card.appendChild(list);
          }
        }
      }

      const form = el('form', { class: 'form-grid', onSubmit: async (e) => {
        e.preventDefault();
        const f = e.target;
        try {
          await api(`/questions/${q.id}/answer`, { method: 'POST', body: {
            answer_md: f.answer_md.value || null,
            answer_value: f.answer_value.value || null,
          }});
          const lbl = f.answer_value.value
            ? f.answer_value.options[f.answer_value.selectedIndex]?.text
            : 'answered';
          toast(`Answer recorded: ${lbl || f.answer_value.value || 'submitted'}`, 'success');
          render();
        } catch (err) {
          toast(`Answer failed: ${err.message}`, 'error', 6000);
        }
      }},
        (q.options || []).length > 0
          ? el('label', {}, 'Choice',
              el('select', { name: 'answer_value' },
                el('option', { value: '' }, '—'),
                ...q.options.map(o => el('option', { value: o.value }, o.label))))
          : el('input', { name: 'answer_value', type: 'hidden' }),
        el('label', {}, 'Notes (optional)', el('textarea', { name: 'answer_md' })),
        el('button', { type: 'submit' }, 'Answer'));
      card.appendChild(form);
      content.appendChild(card);
    });
  }

  content.appendChild(el('h2', {}, 'History'));
  const hist = el('div', { class: 'history' });
  (data.history || []).forEach(e => {
    hist.appendChild(el('div', {},
      el('span', { class: 'ts' }, fmtTime(e.ts)),
      ' ', el('span', { class: 'kind' }, e.kind),
      ' ', el('span', { class: 'muted' }, JSON.stringify(e.detail || {}).slice(0, 200))));
  });
  content.appendChild(hist);
});

function copyText(text, label = 'Copied') {
  navigator.clipboard.writeText(text).then(
    () => toast(label, 'success'),
    () => toast('Copy failed — select the text and copy manually', 'error'));
}

function claudeMdFor(name, slug) {
  return [
    '## Task tracking (OrchestrAi)',
    '',
    'Track your work for this project in OrchestrAi via the `orchestrai` MCP tools.',
    '',
    `- At the start of a session, call \`use_project\` (name: "${name}", slug: "${slug}").`,
    '- Break the work into tasks with `create_task`.',
    '- Before starting a task call `update_task(task_id, status="in_progress")`; when',
    '  finished, `update_task(task_id, status="done")`. Use `note` to record anything useful.',
    '- Between steps, call `list_tasks` to pick up tasks the human added or reprioritized',
    '  in the OrchestrAi UI.',
  ].join('\n');
}

const _CODE_STYLE = 'background:#0d1117;border:1px solid #30363d;padding:12px;border-radius:6px;' +
  'white-space:pre-wrap;word-break:break-all;font-family:monospace;font-size:12px;margin:6px 0;';

function mcpJsonForAgent(origin, token) {
  const server = { type: 'http', url: `${origin}/mcp` };
  if (token) server.headers = { Authorization: `Bearer ${token}` };
  return JSON.stringify({ mcpServers: { orchestrai: server } }, null, 2);
}

function downloadText(filename, text, mime = 'application/json') {
  const a = el('a', { href: URL.createObjectURL(new Blob([text], { type: mime })), download: filename });
  document.body.appendChild(a); a.click(); a.remove();
}

route('/connect', async () => {
  setActiveNav('connect');
  setBreadcrumb([{ label: 'Connect an agent' }]);
  const origin = location.origin;
  const content = $('#content');
  content.innerHTML = `
    <h1>Connect an agent</h1>
    <p class="muted">Register an agent (e.g. a Claude Code instance), drop its config into your
    project, and it connects to OrchestrAi identified — so you can see it here and control what
    it works on.</p>

    <h2>1. Register the agent</h2>
    <div style="display:flex;gap:8px;align-items:center;">
      <input id="agent-name" placeholder="e.g. Claude on my-laptop" style="flex:1;max-width:360px;" />
      <button id="register-agent">Register &amp; get config</button>
    </div>
    <div id="agent-config" style="display:none;margin-top:10px;">
      <p class="muted">⚠️ The token is shown <strong>once</strong> — save this config. Run it where your agent lives:</p>
      <pre id="add-cmd" style="${_CODE_STYLE}"></pre>
      <button id="copy-cmd">Copy command</button>
      <button id="dl-json">Download .mcp.json</button>
    </div>
    <p class="muted" style="margin-top:8px;">Just trying it out? Connect anonymously (no identity):
      <code>claude mcp add --transport http orchestrai ${origin}/mcp</code></p>

    <h2>Your agents</h2>
    <div id="agents-list"><p class="muted">…</p></div>

    <h2>2. Tell the agent to track tasks</h2>
    <p class="muted">Pick the project, then paste this into its <code>CLAUDE.md</code> — this is
    what makes the agent actually use the tools.</p>
    <select id="connect-project"></select>
    <pre id="claude-md" style="${_CODE_STYLE}"></pre>
    <button id="copy-md">Copy snippet</button>
  `;

  const renderAgents = async () => {
    let agents = [];
    try { agents = ((await api('/agents')).items || []).filter((a) => a.kind === 'external'); }
    catch (e) { /* ignore */ }
    const host = $('#agents-list');
    host.innerHTML = '';
    if (!agents.length) {
      host.appendChild(el('p', { class: 'muted' }, 'No agents registered yet.'));
      return;
    }
    const tb = el('tbody', {});
    for (const a of agents) {
      tb.appendChild(el('tr', {},
        el('td', {}, a.name), el('td', {}, pill(a.status)),
        el('td', {}, fmtTime(a.last_heartbeat_at))));
    }
    host.appendChild(el('table', { class: 'table' },
      el('thead', {}, el('tr', {}, el('th', {}, 'Name'), el('th', {}, 'Status'), el('th', {}, 'Last seen'))),
      tb));
  };
  await renderAgents();

  $('#register-agent').onclick = async () => {
    const name = $('#agent-name').value.trim();
    if (!name) { toast('Enter a name for the agent', 'error'); return; }
    let reg;
    try { reg = await api('/agents/register', { method: 'POST', body: { name, kind: 'external' } }); }
    catch (e) { toast('Register failed: ' + e.message, 'error'); return; }
    const token = reg.lease_token;
    const cmd = `claude mcp add --transport http --header "Authorization: Bearer ${token}" orchestrai ${origin}/mcp`;
    $('#add-cmd').textContent = cmd;
    $('#agent-config').style.display = '';
    $('#copy-cmd').onclick = () => copyText(cmd, 'Command copied');
    $('#dl-json').onclick = () => downloadText('.mcp.json', mcpJsonForAgent(origin, token));
    toast(`Registered "${name}" — config below (token shown once)`, 'success');
    await renderAgents();
  };

  const sel = $('#connect-project');
  let projects = [];
  try { projects = (await api('/projects')).items || []; } catch (e) { /* ignore */ }
  if (!projects.length) {
    sel.appendChild(el('option', { value: '' }, '(no projects yet — your agent can create one)'));
  }
  for (const p of projects) {
    const tag = p.execution_mode === 'auto' ? ' — autopilot' : '';
    sel.appendChild(el('option', { value: p.slug }, `${p.name} (${p.slug})${tag}`));
  }
  const renderSnippet = () => {
    const slug = sel.value || 'my-project';
    const proj = projects.find((p) => p.slug === slug);
    $('#claude-md').textContent = claudeMdFor(proj ? proj.name : 'My Project', slug);
  };
  renderSnippet();
  sel.onchange = renderSnippet;
  $('#copy-md').onclick = () => copyText($('#claude-md').textContent, 'Snippet copied');
});

route('/vault', async () => {
  setActiveNav('vault');
  setBreadcrumb([{ label: 'Vault' }]);
  const content = $('#content');
  content.innerHTML = `
    <h1>Key Vault <button id="add-secret">+ Add Secret</button></h1>
    <p class="muted">Names + metadata are stored here. Values are write-only — never shown after creation. Agents fetch values per-task via authenticated endpoint with full audit.</p>
    <div id="secrets-list"></div>
  `;
  $('#add-secret').onclick = () => openSecretModal();
  await refreshSecrets();
});

async function refreshSecrets() {
  const list = $('#secrets-list');
  if (!list) return;
  const data = await api('/secrets');
  list.innerHTML = '';
  if (!data.items.length) {
    list.appendChild(el('p', { class: 'muted' }, 'No secrets yet.'));
    return;
  }
  for (const s of data.items) {
    list.appendChild(el('div', { class: 'card' },
      el('div', { class: 'card-row' },
        el('div', { class: 'grow' },
          el('div', {}, el('strong', {}, s.name),
            ' · ', el('span', { class: 'muted' }, `scope: ${s.scope}`)),
          el('div', { class: 'muted' },
            `${s.description || '(no description)'} · accessed ${s.access_count} times · ` +
            `last ${s.last_accessed_at ? fmtTime(s.last_accessed_at) : 'never'}`)),
        el('button', { class: 'secondary',
          onClick: async () => {
            const newVal = prompt(`Rotate ${s.name} — enter new value (write-only):`);
            if (newVal !== null) {
              await api(`/secrets/${s.name}`, { method: 'PATCH', body: { value: newVal } });
              refreshSecrets();
            }
          }}, 'Rotate'),
        el('button', { class: 'secondary',
          onClick: async () => {
            if (!confirm(`Delete ${s.name}?`)) return;
            await api(`/secrets/${s.name}`, { method: 'DELETE' });
            refreshSecrets();
          }}, 'Delete'))));
  }
}

function openSecretModal() {
  // Inline form above the list — no content replacement.
  let host = document.getElementById('secret-form-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'secret-form-host';
    const list = $('#secrets-list');
    if (list && list.parentNode) list.parentNode.insertBefore(host, list);
    else $('#content').appendChild(host);
  }
  host.innerHTML = '';
  const card = el('div', { class: 'card', style: 'max-width:680px;' },
    el('h3', {}, 'New Secret'),
    el('form', { class: 'form-grid', onSubmit: async (e) => {
      e.preventDefault();
      const f = e.target;
      const btn = f.querySelector('button[type=submit]');
      btn.disabled = true; btn.textContent = 'Saving…';
      try {
        const r = await api('/secrets', { method: 'POST', body: {
          name: f.name.value.trim().toUpperCase(),
          value: f.value.value,
          description: f.description.value,
          scope: f.scope.value || 'global',
        }});
        toast(`Secret "${r.name}" saved`, 'success');
        host.innerHTML = '';
        await refreshSecrets();
      } catch (err) {
        toast(`Save failed: ${err.message}`, 'error', 6000);
        btn.disabled = false; btn.textContent = 'Save';
      }
    }},
      el('label', {}, 'Name (UPPER_SNAKE_CASE)',
        el('input', { name: 'name', type: 'text', required: true,
          pattern: '[A-Z][A-Z0-9_]*', placeholder: 'GITHUB_TOKEN' })),
      el('label', {}, 'Value (write-only — never readable again)',
        el('input', { name: 'value', type: 'password', required: true })),
      el('label', {}, 'Description',
        el('input', { name: 'description', type: 'text' })),
      el('label', {},
        el('span', {}, 'Scope (global, or project:<id>, or repo:<id>)'),
        el('input', { name: 'scope', type: 'text', value: 'global' })),
      el('div', { style: 'display:flex;gap:8px;' },
        el('button', { type: 'submit' }, 'Save'),
        el('button', { type: 'button', class: 'secondary',
          onClick: () => { host.innerHTML = ''; } }, 'Cancel'))));
  host.appendChild(card);
  card.querySelector('input[name=name]').focus();
}

// -------- Boot ----------------------------------------------------------
function isFormActive() {
  // True if any inline form host has form fields with focus or with
  // dirty (non-empty) inputs — re-rendering would lose the user's input.
  const ae = document.activeElement;
  if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.tagName === 'SELECT')) {
    return true;
  }
  // Any inline form host with content => form is open even if not focused
  const hosts = $$('#goal-form-host, #repo-form-host, #task-form-host, #inline-form-host');
  return hosts.some(h => h.children.length > 0);
}

let _renderDebounce = null;
function debouncedRender() {
  if (_renderDebounce) clearTimeout(_renderDebounce);
  _renderDebounce = setTimeout(() => {
    _renderDebounce = null;
    if (isFormActive()) return;          // don't clobber user input
    render();
  }, 400);
}

async function boot() {
  connectWS();

  // Live-refresh current screen on relevant events (debounced + form-aware)
  onEvent((ev) => {
    if (ev.kind.startsWith('agent.') || ev.kind.startsWith('question.')) {
      refreshHealth();
      refreshNotifications();
    }
    debouncedRender();
  });

  // Notification bell → toggle a dropdown of pending questions
  const notifBtn = $('#notif-btn');
  if (notifBtn) notifBtn.onclick = (e) => { e.stopPropagation(); toggleNotifDropdown(); };
  document.addEventListener('click', () => closeNotifDropdown());

  await refreshHealth();
  await refreshNotifications();
  setInterval(refreshHealth, 15000);
  setInterval(refreshNotifications, 30000);

  if (!location.hash) location.hash = '#/agents';
  await render();
}

boot();
