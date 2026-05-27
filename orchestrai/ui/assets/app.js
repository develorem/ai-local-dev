// OrchestrAi UI — vanilla JS, hash routes, WebSocket live updates.

const API = '/api';
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

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
  } catch {}
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
    const tname = a.current_task_id ? `task ${a.current_task_id.slice(-6)}` : 'no current task';
    content.appendChild(el('div', { class: 'card' },
      el('div', { class: 'card-row' },
        pill(a.status),
        el('div', { class: 'grow' },
          el('div', {},
            el('a', { href: `#/agents/${a.id}` },
              el('strong', {}, a.name)),
            ' · ', a.host || 'no host'),
          el('div', { class: 'muted' },
            `v${a.version} · last heartbeat ${fmtTime(a.last_heartbeat_at)} · ${tname}`)),
      )));
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
  const modal = el('div', { class: 'card', style: 'max-width:600px;margin:24px auto;' },
    el('h2', {}, 'New Project'),
    el('form', { class: 'form-grid', onSubmit: async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        await api('/projects', { method: 'POST', body: {
          name: f.name.value, slug: f.slug.value,
          description_md: f.description_md.value, context_md: f.context_md.value,
        }});
        location.hash = '#/projects';
        await refreshProjects();
      } catch (err) { alert(err.message); }
    }},
      el('label', {}, 'Name', el('input', { name: 'name', type: 'text', required: true })),
      el('label', {}, 'Slug', el('input', { name: 'slug', type: 'text', required: true,
                                              pattern: '[a-z0-9-]+' })),
      el('label', {}, 'Description', el('textarea', { name: 'description_md' })),
      el('label', {}, el('span', {}, 'Context (terse bullets — see PROMPTS.md token-efficient format)'),
        el('textarea', { name: 'context_md', placeholder: 'Stack: python 3.12, fastapi\nConventions: snake_case, tests/ next to source' })),
      el('button', { type: 'submit' }, 'Create')));
  $('#content').innerHTML = '';
  $('#content').appendChild(modal);
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

  content.appendChild(el('h2', {}, `Repos (${data.repos.length})`));
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

  content.appendChild(el('h2', {}, `Goals (${data.goals.length})`));
  if (data.goals.length === 0) {
    content.appendChild(el('p', { class: 'muted' }, 'No goals yet.'));
  } else {
    const tbl = el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, 'Title'), el('th', {}, 'Status'), el('th', {}, 'Priority'), el('th', {}, 'Created'))),
      el('tbody', {}, ...data.goals.map(g => el('tr', {},
        el('td', {}, g.title), el('td', {}, pill(g.status)),
        el('td', {}, g.priority),
        el('td', { class: 'muted' }, fmtTime(g.created_at))))));
    content.appendChild(tbl);
  }

  content.appendChild(el('h2', {}, `Tasks (${data.tasks.length})`));
  if (data.tasks.length === 0) {
    content.appendChild(el('p', { class: 'muted' }, 'No tasks yet.'));
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
      const form = el('form', { class: 'form-grid', onSubmit: async (e) => {
        e.preventDefault();
        const f = e.target;
        await api(`/questions/${q.id}/answer`, { method: 'POST', body: {
          answer_md: f.answer_md.value || null,
          answer_value: f.answer_value.value || null,
        }});
        render();
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
  $('#content').innerHTML = `
    <h1>Key Vault</h1>
    <p class="muted">Secrets vault — coming in Phase 5. The schema is in place; UI not yet wired.</p>
  `;
});

// -------- Boot ----------------------------------------------------------
async function boot() {
  connectWS();

  // Live-refresh current screen on relevant events
  onEvent((ev) => {
    // Always refresh notifications and health on agent/question events
    if (ev.kind.startsWith('agent.') || ev.kind.startsWith('question.')) {
      refreshHealth();
      refreshNotifications();
    }
    // Re-render current screen for any event — cheap and reliable
    render();
  });

  await refreshHealth();
  await refreshNotifications();
  setInterval(refreshHealth, 15000);
  setInterval(refreshNotifications, 30000);

  if (!location.hash) location.hash = '#/agents';
  await render();
}

boot();
