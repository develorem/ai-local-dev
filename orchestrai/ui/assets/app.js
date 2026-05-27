// OrchestrAi UI — vanilla JS, hash routes, WebSocket live updates.

const API = '/api';
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

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
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
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
  ws = new WebSocket(`${proto}://${location.host}/api/events`);
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

  const kvs = el('div', { class: 'kvs' });
  for (const [k, v] of [
    ['Status', pill(a.status)],
    ['Host', a.host || '—'],
    ['Version', a.version],
    ['Capabilities', (a.capabilities || []).join(', ') || '—'],
    ['Registered', fmtTime(a.registered_at)],
    ['Last heartbeat', fmtTime(a.last_heartbeat_at)],
  ]) {
    kvs.appendChild(el('div', { class: 'k' }, k));
    kvs.appendChild(typeof v === 'string' ? el('div', {}, v) : el('div', {}, v));
  }
  content.appendChild(el('div', { class: 'card' }, kvs));

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
