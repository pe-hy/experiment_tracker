// Experiment Tracker — the whole client.
// No framework, no CDN, no build step. Plain ES modules.
//
// Data comes from agents, so it is untrusted: every value reaches the DOM through
// textContent (via the `h` helper) and never through innerHTML.

const DATA = './data';
const REPO = 'https://github.com/pe-hy/experiment_tracker';

/* ------------------------------------------------------------------ helpers */

/** Build an element. Children may be nodes, strings, or falsy (skipped). */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'html') el.innerHTML = v;           // only ever for our own SVG
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v);
    }
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false || c === '') continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const $ = (sel, root = document) => root.querySelector(sel);

function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

async function getJSON(url) {
  const res = await fetch(url, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${url}`);
  return res.json();
}

async function getText(url) {
  const res = await fetch(url, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${url}`);
  return res.text();
}

/* ------------------------------------------------------------- formatting */

function fmtNumber(v) {
  if (typeof v !== 'number' || !isFinite(v)) return String(v);
  if (Number.isInteger(v) && Math.abs(v) < 1e6) return String(v);
  const abs = Math.abs(v);
  if (abs !== 0 && (abs < 1e-3 || abs >= 1e7)) return v.toExponential(3);
  return String(Number(v.toPrecision(6)));
}

function fmtValue(v) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number') return fmtNumber(v);
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (Array.isArray(v)) return v.map(fmtValue).join(', ');
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

function parseDate(s) {
  if (!s) return null;
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

function fmtDate(s) {
  const d = parseDate(s);
  if (!d) return s || '—';
  return d.toISOString().slice(0, 16).replace('T', ' ') + 'Z';
}

function fmtAgo(s) {
  const d = parseDate(s);
  if (!d) return '';
  const secs = (Date.now() - d.getTime()) / 1000;
  if (secs < 0) return 'just now';
  const steps = [[60, 's'], [60, 'm'], [24, 'h'], [7, 'd'], [4.35, 'w'], [12, 'mo']];
  let v = secs, unit = 's';
  for (const [div, next] of steps) {
    if (v < div) break;
    v /= div; unit = next;
  }
  return `${Math.floor(v)}${unit} ago`;
}

function fmtDuration(seconds) {
  if (typeof seconds !== 'number' || !isFinite(seconds) || seconds < 0) return '—';
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  const h_ = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (s < 86400) return `${h_}h ${m}m`;
  return `${Math.floor(s / 86400)}d ${h_ % 24}h`;
}

const STATUS_CLASS = {
  completed: 'badge-ok', success: 'badge-ok', finished: 'badge-ok',
  failed: 'badge-bad', error: 'badge-bad', crashed: 'badge-bad',
  running: 'badge-warn', pending: 'badge-warn', queued: 'badge-warn',
  cancelled: 'badge-info', canceled: 'badge-info', stopped: 'badge-info',
};

function statusBadge(status) {
  const key = String(status || 'unknown').toLowerCase();
  return h('span', { class: `badge ${STATUS_CLASS[key] || 'badge-info'}` },
    h('span', { class: 'dot' }), key);
}

/* ------------------------------------------------------------------ layout */

function crumbs(...parts) {
  const el = h('nav', { class: 'crumbs', 'aria-label': 'Breadcrumb' });
  parts.forEach((p, i) => {
    if (i) el.append(h('span', { class: 'sep' }, '/'));
    el.append(p.href ? h('a', { href: p.href }, p.label) : h('span', {}, p.label));
  });
  return el;
}

function empty(title, ...body) {
  return h('div', { class: 'empty' }, h('h3', {}, title), ...body);
}

function errorBanner(err) {
  return h('div', { class: 'banner-error' }, String(err && err.message || err));
}

/** A description, or an honest note that the agent did not supply one. */
function description(text, what) {
  const t = (text || '').trim();
  if (!t) return h('p', { class: 'faint small' }, `No ${what} description recorded yet.`);
  // Preserve the author's line breaks without pulling in a markdown parser.
  const p = h('p', { class: 'muted' });
  t.split('\n').forEach((line, i) => {
    if (i) p.append(h('br'));
    p.append(document.createTextNode(line));
  });
  return p;
}

/* ------------------------------------------------------------------- views */

async function viewProjects(app) {
  const index = await getJSON(`${DATA}/index.json`);
  setBuiltAt(index.built_at);
  clear(app);

  const bits = [`${index.project_count} project${index.project_count === 1 ? '' : 's'}`,
                `${index.run_count} run${index.run_count === 1 ? '' : 's'}`];
  if (index.gpu_hours) bits.push(`${fmtNumber(index.gpu_hours)} accelerator-hours`);

  app.append(h('div', { class: 'page-head' },
    h('h1', {}, 'Projects'),
    h('p', { class: 'subtitle' },
      index.project_count === 0 ? 'Nothing tracked yet.' : bits.join(' · ') + '.')));

  // A file that failed to parse would otherwise just vanish from the site, which is
  // the one failure mode you must never hide in a system of record.
  if (index.invalid && index.invalid.length) {
    app.append(h('div', { class: 'banner-error' },
      `${index.invalid.length} file(s) could not be read and are missing from this view: `,
      index.invalid.map(i => i.path).join(', ')));
  }

  if (!index.projects.length) {
    app.append(empty('No experiments tracked yet',
      h('p', {}, 'Run ', h('code', {}, '/track-experiment'), ' from any project to record the first one.'),
      h('p', { class: 'small faint' }, 'A newly posted run appears here within a few minutes, once the site rebuilds.')));
    return;
  }

  const search = h('input', { class: 'input', type: 'search', placeholder: 'Filter projects…', 'aria-label': 'Filter projects' });
  const grid = h('div', { class: 'grid' });

  const render = () => {
    const q = search.value.trim().toLowerCase();
    clear(grid);
    const shown = index.projects.filter(p => !q || [
      p.name, p.slug, p.description, (p.tags || []).join(' '),
      (p.variant_preview || []).map(v => v.name + ' ' + v.description).join(' '),
    ].join(' ').toLowerCase().includes(q));

    if (!shown.length) {
      grid.append(h('p', { class: 'faint' }, 'Nothing matches that filter.'));
      return;
    }
    shown.forEach(p => grid.append(projectCard(p)));
  };

  search.addEventListener('input', render);
  app.append(h('div', { class: 'toolbar' }, h('div', { class: 'search' },
    h('span', { html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>' }),
    search)));
  app.append(grid);
  render();

  if (index.recent_runs && index.recent_runs.length) {
    app.append(recentPanel(index.recent_runs));
  }
}

/** Latest runs across every project — the "what has been happening" view. */
function recentPanel(runs) {
  const table = h('table', { class: 'tbl' },
    h('thead', {}, h('tr', {},
      h('th', {}, 'When'), h('th', {}, 'Project'), h('th', {}, 'Variant'),
      h('th', {}, 'Run'), h('th', {}, 'Status'), h('th', { class: 'num' }, 'Result'))),
    h('tbody', {},
      runs.map(r => {
        const key = r.primary_metric && (r.metrics || {})[r.primary_metric] !== undefined
          ? r.primary_metric
          : Object.keys(r.metrics || {})[0];
        const value = key ? (r.metrics || {})[key] : undefined;
        return h('tr', {},
          h('td', { class: 'faint nowrap', title: fmtDate(r.when) }, fmtAgo(r.when) || '—'),
          h('td', {}, h('a', { href: `#/p/${encodeURIComponent(r.project)}` },
            r.project_name || r.project)),
          h('td', { class: 'faint' }, r.variant || '—'),
          h('td', {}, h('a', {
            href: `#/p/${encodeURIComponent(r.project)}/r/${encodeURIComponent(r.run_id)}`
          }, r.run_name || r.run_id)),
          h('td', {}, statusBadge(r.status)),
          h('td', { class: 'num' }, typeof value === 'number'
            ? `${key} ${fmtNumber(value)}` : '—'));
      })));

  return h('details', { class: 'panel mt-4', open: '' },
    h('summary', {}, 'Recent activity across all projects',
      h('span', { class: 'badge badge-info' }, String(runs.length))),
    h('div', { class: 'panel-body flush' }, h('div', { class: 'table-scroll' }, table)));
}

function projectCard(p) {
  const card = h('a', { class: 'card', href: `#/p/${encodeURIComponent(p.slug)}` },
    h('div', { class: 'card-title' }, p.name),
    p.description
      ? h('div', { class: 'card-desc' }, p.description)
      : h('div', { class: 'card-desc faint' }, 'No project description recorded.'));

  const previews = (p.variant_preview || []).filter(v => v.description);
  if (previews.length) {
    const list = h('div', { class: 'col', style: 'gap:4px' });
    previews.forEach(v => list.append(h('div', { class: 'xsmall' },
      h('span', { class: 'chip' }, v.name || v.variant), ' ',
      h('span', { class: 'muted' }, truncate(v.description, 70)))));
    card.append(list);
  }

  card.append(h('div', { class: 'card-foot' },
    h('span', {}, `${p.variant_count} variant${p.variant_count === 1 ? '' : 's'}`),
    h('span', {}, `${p.run_count} run${p.run_count === 1 ? '' : 's'}`),
    p.last_activity && h('span', { class: 'nowrap', title: fmtDate(p.last_activity) }, fmtAgo(p.last_activity))));
  return card;
}

function truncate(text, n) {
  const t = String(text).replace(/\s+/g, ' ').trim();
  return t.length > n ? t.slice(0, n - 1) + '…' : t;
}

async function viewProject(app, slug) {
  const project = await getJSON(`${DATA}/projects/${encodeURIComponent(slug)}/project.json`);
  clear(app);

  app.append(crumbs({ label: 'Projects', href: '#/' }, { label: project.name }));
  app.append(h('div', { class: 'page-head' },
    h('h1', {}, project.name),
    description(project.description, 'project'),
    h('div', { class: 'row wrapf mt-4', style: 'gap:8px' },
      h('span', { class: 'chip' }, `${project.run_count} runs`),
      h('span', { class: 'chip' }, `${project.variant_count} variants`),
      project.repo && h('a', { class: 'chip', href: repoWebUrl(project.repo), target: '_blank', rel: 'noopener' },
        shortRepo(project.repo)),
      ...Object.entries(project.statuses || {}).map(([k, v]) =>
        h('span', { class: `badge ${STATUS_CLASS[k.toLowerCase()] || 'badge-info'}` }, `${v} ${k}`)))));

  if (!project.variants.length) {
    app.append(empty('No runs in this project yet'));
    return;
  }

  // Only the most recently active variant is expanded: ten open variants with thirty
  // runs each is a wall, not a view.
  project.variants.forEach((v, i) => app.append(variantPanel(project, v, i === 0)));
}

function variantPanel(project, variant, open) {
  const panel = h('details', { class: 'panel', open: open ? '' : null });
  panel.append(h('summary', {},
    h('span', {}, variant.variant_name || variant.variant),
    h('span', { class: 'badge badge-info' }, `${variant.run_count} run${variant.run_count === 1 ? '' : 's'}`),
    variant.conclusion ? h('span', { class: 'badge badge-ok', title: variant.conclusion }, 'concluded') : null,
    variant.status === 'abandoned' ? h('span', { class: 'badge badge-info' }, 'abandoned') : null,
    h('span', { class: 'topbar-spacer' }),
    variant.last_activity && h('span', { class: 'xsmall faint nowrap' }, fmtAgo(variant.last_activity))));

  const body = h('div', { class: 'panel-body' });
  body.append(description(variant.description, 'variant'));
  if (variant.conclusion) {
    body.append(h('div', { class: 'mt-4' },
      h('div', { class: 'xsmall muted', style: 'text-transform:uppercase;letter-spacing:.04em' }, 'Conclusion'),
      description(variant.conclusion, 'conclusion')));
  }
  panel.append(body);
  panel.append(runTable(project, variant));
  return panel;
}

/** Metric columns worth showing for this variant: the ones most runs actually have. */
function metricColumns(runs, limit = 5) {
  const counts = new Map();
  runs.forEach(r => Object.entries(r.metrics || {}).forEach(([k, v]) => {
    if (typeof v === 'number') counts.set(k, (counts.get(k) || 0) + 1);
  }));
  const primary = runs.map(r => r.primary_metric).find(Boolean);
  const keys = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(e => e[0]);
  if (primary && keys.includes(primary)) {
    keys.splice(keys.indexOf(primary), 1);
    keys.unshift(primary);
  }
  return keys.slice(0, limit);
}

function runTable(project, variant) {
  const runs = variant.runs;
  const cols = metricColumns(runs);
  // Highlight the best value per metric so a leaderboard read is instant. Direction is
  // guessed from the name — losses and errors go down, everything else goes up.
  // Direction comes from the project's declared metric_goals when available; the
  // name heuristic is only a fallback, and it is wrong for things like "regret".
  const goals = project.metric_goals || {};
  const best = {};
  cols.forEach(k => {
    const vals = runs.map(r => (r.metrics || {})[k]).filter(v => typeof v === 'number');
    if (!vals.length) return;
    const lowerIsBetter = goals[k]
      ? goals[k] === 'min'
      : /loss|err|perplexity|ppl|nll|mse|mae|wer|cer|regret/i.test(k);
    best[k] = lowerIsBetter ? Math.min(...vals) : Math.max(...vals);
  });

  const selected = new Set();
  const headers = [
    { key: 'sel', label: '', plain: true },
    { key: 'run', label: 'Run' },
    { key: 'status', label: 'Status' },
    ...cols.map(k => ({ key: `m:${k}`, label: k, num: true })),
    { key: 'duration', label: 'Duration', num: true },
    { key: 'when', label: 'When' },
  ];

  let sortKey = cols.length ? `m:${cols[0]}` : 'when';
  let sortDesc = true;

  const thead = h('thead');
  const tbody = h('tbody');
  const table = h('table', { class: 'tbl' }, thead, tbody);

  const value = (r, key) => {
    if (key === 'run') return r.run_name || r.run_id || '';
    if (key === 'status') return r.status || '';
    if (key === 'duration') return typeof r.duration_seconds === 'number' ? r.duration_seconds : -Infinity;
    if (key === 'when') return r.finished_at || r.started_at || '';
    if (key.startsWith('m:')) {
      const v = (r.metrics || {})[key.slice(2)];
      return typeof v === 'number' ? v : -Infinity;
    }
    return '';
  };

  const renderHead = () => {
    clear(thead);
    const tr = h('tr');
    headers.forEach(col => {
      if (col.plain) { tr.append(h('th', { style: 'width:28px' })); return; }
      const th = h('th', { class: 'sortable', role: 'columnheader', tabindex: '0' },
        col.label, h('span', { class: 'arrow' }, sortDesc ? '▼' : '▲'));
      if (sortKey === col.key) th.setAttribute('aria-sort', sortDesc ? 'descending' : 'ascending');
      const activate = () => {
        if (sortKey === col.key) sortDesc = !sortDesc;
        else { sortKey = col.key; sortDesc = true; }
        renderHead(); renderBody();
      };
      th.addEventListener('click', activate);
      th.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); } });
      tr.append(th);
    });
    thead.append(tr);
  };

  const renderBody = () => {
    clear(tbody);
    const sorted = [...runs].sort((a, b) => {
      const va = value(a, sortKey), vb = value(b, sortKey);
      const cmp = typeof va === 'number' && typeof vb === 'number'
        ? va - vb : String(va).localeCompare(String(vb));
      return sortDesc ? -cmp : cmp;
    });

    sorted.forEach(r => {
      const href = `#/p/${encodeURIComponent(project.slug)}/r/${encodeURIComponent(r.run_id)}`;
      const tr = h('tr');
      const box = h('input', { type: 'checkbox', 'aria-label': `select ${r.run_id}` });
      box.addEventListener('change', () => {
        if (box.checked) selected.add(r.run_id); else selected.delete(r.run_id);
        updateCompareBar();
      });
      box.checked = selected.has(r.run_id);
      tr.append(h('td', {}, box));
      tr.append(h('td', {}, h('a', { href }, r.run_name || r.run_id),
        r.code && r.code.dirty ? h('span', { class: 'badge badge-warn', style: 'margin-left:6px', title: 'Uncommitted changes were present' }, 'dirty') : null));
      tr.append(h('td', {}, statusBadge(r.status)));
      cols.forEach(k => {
        const v = (r.metrics || {})[k];
        const isBest = typeof v === 'number' && best[k] === v && runs.length > 1;
        // Colour alone must not carry the meaning, so the best value also gets a
        // marker and a title.
        tr.append(h('td', {
          class: 'num' + (isBest ? ' best' : ''),
          title: isBest ? `best ${k} in this variant` : null,
        }, typeof v === 'number' ? fmtNumber(v) : '—', isBest ? ' ★' : ''));
      });
      tr.append(h('td', { class: 'num' }, fmtDuration(r.duration_seconds)));
      const when = r.finished_at || r.started_at;
      tr.append(h('td', { class: 'faint nowrap', title: fmtDate(when) }, fmtAgo(when) || '—'));
      tbody.append(tr);
    });
  };

  const compareBar = h('div', { class: 'toolbar', style: 'margin:0 16px 12px; display:none' });
  const compareBtn = h('button', { class: 'btn', type: 'button' }, 'Compare');
  const compareCount = h('span', { class: 'xsmall faint' });
  const compareOut = h('div');
  compareBar.append(compareBtn, compareCount);

  function updateCompareBar() {
    compareBar.style.display = selected.size ? 'flex' : 'none';
    compareCount.textContent = `${selected.size} selected`;
    compareBtn.textContent = selected.size < 2 ? 'Select 2 or more' : `Compare ${selected.size} runs`;
  }
  compareBtn.addEventListener('click', async () => {
    if (selected.size < 2) return;
    clear(compareOut);
    compareOut.append(h('p', { class: 'faint small' }, 'Loading runs…'));
    try {
      const full = await Promise.all([...selected].map(id =>
        getJSON(`${DATA}/projects/${encodeURIComponent(project.slug)}/runs/${encodeURIComponent(id)}.json`)));
      clear(compareOut);
      compareOut.append(compareTable(full));
    } catch (err) {
      clear(compareOut);
      compareOut.append(errorBanner(err));
    }
  });

  renderHead(); renderBody(); updateCompareBar();

  const rowsFor = () => {
    const head = ['run', 'status', ...cols, 'duration_s', 'when'];
    const body = runs.map(r => [
      r.run_name || r.run_id, r.status || '',
      ...cols.map(k => { const v = (r.metrics || {})[k]; return typeof v === 'number' ? v : ''; }),
      typeof r.duration_seconds === 'number' ? Math.round(r.duration_seconds) : '',
      r.finished_at || r.started_at || '',
    ]);
    return [head, ...body];
  };

  const toolbar = h('div', { class: 'toolbar', style: 'margin:12px 16px 0' },
    copyButton('Copy CSV', () => rowsFor()
      .map(row => row.map(c => /[",\n]/.test(String(c)) ? `"${String(c).replace(/"/g, '""')}"` : String(c)).join(','))
      .join('\n')),
    copyButton('Copy LaTeX', () => {
      const rows = rowsFor();
      const esc = s => String(s).replace(/([&%$#_{}])/g, '\\$1');
      return [
        `\\begin{tabular}{l${'r'.repeat(rows[0].length - 1)}}`, '\\toprule',
        rows[0].map(esc).join(' & ') + ' \\\\', '\\midrule',
        ...rows.slice(1).map(r => r.map(esc).join(' & ') + ' \\\\'),
        '\\bottomrule', '\\end{tabular}',
      ].join('\n');
    }));

  return h('div', {}, toolbar,
    h('div', { class: 'panel-body flush' }, h('div', { class: 'table-scroll' }, table)),
    compareBar, compareOut);
}

/** Runs as columns, attributes as rows — the orientation that stays readable past
 *  two runs, with a filter for "only what differs", which is the point of comparing. */
function compareTable(runs) {
  const cols = runs.map(r => r.run_name || r.run_id);

  const flatten = (obj, prefix, out) => {
    for (const [k, v] of Object.entries(obj || {})) {
      const key = prefix ? `${prefix}.${k}` : k;
      if (v && typeof v === 'object' && !Array.isArray(v)) flatten(v, key, out);
      else out[key] = v;
    }
    return out;
  };

  const sections = [
    ['Metrics', runs.map(r => r.metrics || {})],
    ['Config', runs.map(r => flatten(r.config, '', {}))],
    ['Code', runs.map(r => ({
      commit: (r.code || {}).commit_short || (r.code || {}).commit,
      branch: (r.code || {}).branch,
      dirty: (r.code || {}).dirty,
    }))],
    ['Run', runs.map(r => ({
      status: r.status, variant: r.variant, seed: r.seed, group: r.group,
      duration: fmtDuration(r.duration_seconds), author: r.author,
      when: r.finished_at || r.started_at,
    }))],
  ];

  let diffOnly = true;
  const tbody = h('tbody');

  const render = () => {
    clear(tbody);
    for (const [title, maps] of sections) {
      const keys = [...new Set(maps.flatMap(m => Object.keys(m)))].sort();
      const rows = keys.filter(key => {
        if (!diffOnly) return true;
        const vals = maps.map(m => JSON.stringify(m[key] ?? null));
        return new Set(vals).size > 1;
      });
      if (!rows.length) continue;
      tbody.append(h('tr', {}, h('td', {
        colspan: String(cols.length + 1),
        class: 'xsmall muted',
        style: 'text-transform:uppercase;letter-spacing:.04em;background:var(--surface-2)',
      }, title)));
      rows.forEach(key => {
        const vals = maps.map(m => m[key]);
        const differs = new Set(vals.map(v => JSON.stringify(v ?? null))).size > 1;
        tbody.append(h('tr', {},
          h('td', { class: 'faint' }, key),
          ...vals.map(v => h('td', {
            class: 'num' + (differs ? ' best' : ''),
          }, v === undefined || v === null ? '—' : fmtValue(v)))));
      });
    }
    if (!tbody.childNodes.length) {
      tbody.append(h('tr', {}, h('td', { colspan: String(cols.length + 1), class: 'faint' },
        'These runs are identical in every field compared.')));
    }
  };

  const toggle = h('button', { class: 'btn is-active', type: 'button' }, 'Differences only');
  toggle.addEventListener('click', () => {
    diffOnly = !diffOnly;
    toggle.classList.toggle('is-active', diffOnly);
    toggle.setAttribute('aria-pressed', String(diffOnly));
    render();
  });

  render();
  return h('div', { class: 'panel' },
    h('div', { class: 'panel-head' }, h('h3', {}, `Comparing ${runs.length} runs`), toggle),
    h('div', { class: 'panel-body flush' }, h('div', { class: 'table-scroll' },
      h('table', { class: 'tbl' },
        h('thead', {}, h('tr', {}, h('th', {}, ''), ...cols.map(c => h('th', {}, c)))),
        tbody))));
}

/** Copy-to-clipboard with a real fallback: the async API needs a secure context
 *  and, in some browsers, a recent user gesture — neither is guaranteed. */
function copyButton(label, produce) {
  const btn = h('button', { class: 'btn', type: 'button' }, label);
  btn.addEventListener('click', async () => {
    const text = produce();
    let ok = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        ok = true;
      }
    } catch (e) { /* fall through to the manual path */ }
    if (ok) {
      btn.textContent = 'Copied';
      setTimeout(() => { btn.textContent = label; }, 1200);
    } else {
      // Show it in a selectable box rather than failing silently.
      const box = h('textarea', { class: 'input', rows: '10',
        style: 'height:auto;font-family:var(--font-mono);width:100%' });
      box.value = text;
      const holder = h('div', { class: 'mt-4' },
        h('p', { class: 'xsmall faint' }, 'Copy is unavailable here — select and copy manually:'), box);
      btn.parentNode.appendChild(holder);
      box.focus(); box.select();
    }
  });
  return btn;
}

async function viewRun(app, slug, runId) {
  const [project, run] = await Promise.all([
    getJSON(`${DATA}/projects/${encodeURIComponent(slug)}/project.json`),
    getJSON(`${DATA}/projects/${encodeURIComponent(slug)}/runs/${encodeURIComponent(runId)}.json`),
  ]);
  clear(app);

  const variant = (project.variants || []).find(v => v.variant === run.variant);

  app.append(crumbs(
    { label: 'Projects', href: '#/' },
    { label: project.name, href: `#/p/${encodeURIComponent(slug)}` },
    { label: run.run_name || run.run_id }));

  app.append(h('div', { class: 'page-head' },
    h('div', { class: 'row wrapf', style: 'gap:12px' },
      h('h1', {}, run.run_name || run.run_id),
      statusBadge(run.status)),
    h('div', { class: 'row wrapf mt-4', style: 'gap:8px' },
      run.variant && h('span', { class: 'chip' }, `variant: ${run.variant}`),
      run.author && h('span', { class: 'chip' }, run.author),
      ...(run.tags || []).map(t => h('span', { class: 'chip' }, t)))));

  if (variant && variant.description) {
    app.append(h('div', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h3', {}, 'What this variant is testing')),
      h('div', { class: 'panel-body' }, description(variant.description, 'variant'))));
  }

  // Metrics up front — this is what anyone opening a run came to see.
  const metrics = Object.entries(run.metrics || {}).filter(([, v]) => typeof v === 'number');
  if (metrics.length) {
    const row = h('div', { class: 'metrics-row' });
    metrics.sort(([a], [b]) => (a === run.primary_metric ? -1 : b === run.primary_metric ? 1 : a.localeCompare(b)));
    metrics.forEach(([k, v]) => row.append(h('div', { class: 'metric' },
      h('span', { class: 'k' }, k), h('span', { class: 'v' }, fmtNumber(v)))));
    app.append(h('div', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h3', {}, 'Metrics')),
      h('div', { class: 'panel-body' }, row)));
  }

  for (const [title, text] of [['Hypothesis', run.hypothesis], ['Conclusion', run.conclusion], ['Notes', run.notes]]) {
    if ((text || '').trim()) {
      app.append(h('div', { class: 'panel' },
        h('div', { class: 'panel-head' }, h('h3', {}, title)),
        h('div', { class: 'panel-body' }, description(text, title.toLowerCase()))));
    }
  }

  if (Array.isArray(run.derived_from) && run.derived_from.length) {
    app.append(detailsPanel('Based on', run.derived_from.map(ref => {
      const p = ref.project || slug;
      const label = ref.relation || 'derived from';
      return [label, ref.run_id
        ? h('a', { href: `#/p/${encodeURIComponent(p)}/r/${encodeURIComponent(ref.run_id)}` },
            `${p} / ${ref.run_id}`)
        : fmtValue(ref)];
    })));
  }

  if (run.curves && Object.keys(run.curves).length) app.append(curvesPanel(run.curves));

  app.append(detailsPanel('Run', [
    ['Run ID', run.run_id],
    ['Project', run.project],
    ['Variant', run.variant],
    ['Status', run.status],
    ['Author', run.author],
    ['Started', run.started_at && fmtDate(run.started_at)],
    ['Finished', run.finished_at && fmtDate(run.finished_at)],
    ['Duration', typeof run.duration_seconds === 'number' && fmtDuration(run.duration_seconds)],
  ]));

  const code = run.code || {};
  if (Object.keys(code).length) {
    // State plainly when the recorded commit cannot be resolved by a reader. A SHA
    // that exists on one machine looks authoritative and is not.
    if (code.no_remote) {
      app.append(h('div', { class: 'banner-error' },
        'This project had no git remote, so the commit below exists only on the machine ' +
        'that ran it and cannot be fetched by anyone else.'));
    } else if (code.commit_pushed === false) {
      app.append(h('div', { class: 'banner-error' },
        'This commit was never pushed. The SHA is recorded, but nobody else can resolve it.'));
    } else if (code.visibility === 'private' || code.visibility === 'private_or_missing') {
      app.append(h('div', { class: 'panel' }, h('div', { class: 'panel-body small muted' },
        'The source repository is private, so the links below will 404 for anyone without access. ',
        h('br'),
        'Reproduce with: ',
        h('code', {}, `git clone ${code.remote_url || '<repo>'} && git checkout ${code.commit || ''}`))));
    }

    const rows = [
      ['Commit', code.commit ? commitLink(code) : null],
      ['Branch', code.branch],
      ['Repository', code.remote_url ? h('a', { href: repoWebUrl(code.remote_url), target: '_blank', rel: 'noopener' }, code.remote_url) : (code.remote || null)],
      ['Working tree', code.dirty === true ? 'dirty — uncommitted changes present'
        : code.dirty === false ? 'clean' : null],
      ['Command', code.command],
      ['Entrypoint', code.entrypoint],
    ];
    app.append(detailsPanel('Code', rows));
    if (code.patch_file) app.append(await patchPanel(slug, code));
    (code.snapshots || []).forEach(file => app.append(snapshotPanel(file)));
  }

  if (run.env && Object.keys(run.env).length) {
    app.append(detailsPanel('Environment', Object.entries(run.env).map(([k, v]) => [k, fmtValue(v)])));
  }

  if (run.config && Object.keys(run.config).length) app.append(configPanel(run.config));

  if (Array.isArray(run.artifacts) && run.artifacts.length) {
    app.append(detailsPanel('Artifacts', run.artifacts.map(a =>
      [a.name || 'artifact', a.url ? h('a', { href: a.url, target: '_blank', rel: 'noopener' }, a.url) : (a.path || fmtValue(a))])));
  }

  app.append(h('details', { class: 'panel' },
    h('summary', {}, 'Raw JSON'),
    h('pre', { class: 'code' }, JSON.stringify(run, null, 2))));

  // Runs are immutable by design, but people still need to fix a typo or remove a
  // smoke test. GitHub's own web editor does both, and editing there retriggers the
  // rebuild — so two links turn "impossible" into a working workflow.
  const file = `data/projects/${slug}/runs/${run.run_id}.json`;
  app.append(h('div', { class: 'row wrapf mt-4', style: 'gap:8px' },
    h('a', { class: 'btn', href: `${REPO}/edit/main/${file}`, target: '_blank', rel: 'noopener' }, 'Edit on GitHub'),
    h('a', { class: 'btn', href: `${REPO}/delete/main/${file}`, target: '_blank', rel: 'noopener' }, 'Delete run'),
    h('a', { class: 'btn btn-ghost', href: `${REPO}/blob/main/${file}`, target: '_blank', rel: 'noopener' }, 'View source'),
    h('span', { class: 'xsmall faint' }, 'edits rebuild the site automatically')));
}

function snapshotPanel(file) {
  return h('details', { class: 'panel' },
    h('summary', {}, file.path,
      h('span', { class: 'badge badge-info' }, `${file.bytes} B`),
      h('span', { class: 'badge badge-accent' }, 'snapshot')),
    h('div', { class: 'panel-body flush' }, h('pre', { class: 'code' }, file.content)));
}

function detailsPanel(title, rows) {
  const dl = h('dl', { class: 'kv' });
  let any = false;
  rows.forEach(([k, v]) => {
    if (v === null || v === undefined || v === '' || v === false) return;
    any = true;
    dl.append(h('dt', {}, k));
    dl.append(h('dd', {}, v instanceof Node ? v : String(v)));
  });
  if (!any) return h('span');
  return h('div', { class: 'panel' },
    h('div', { class: 'panel-head' }, h('h3', {}, title)),
    h('div', { class: 'panel-body' }, dl));
}

function shortRepo(remote) {
  const m = String(remote).match(/[:/]([^/:]+\/[^/]+?)(?:\.git)?$/);
  return m ? m[1] : remote;
}

/** Normalise git remotes (incl. scp-style SSH) to a browsable https URL. */
function repoWebUrl(remote) {
  const s = String(remote);
  const ssh = s.match(/^git@([^:]+):(.+?)(?:\.git)?$/);
  if (ssh) return `https://${ssh[1]}/${ssh[2]}`;
  return s.replace(/\.git$/, '');
}

function commitLink(code) {
  const short = code.commit_short || String(code.commit).slice(0, 10);
  if (!code.remote_url) return h('span', {}, short);
  const base = repoWebUrl(code.remote_url);
  if (!/github\.com|gitlab\./.test(base)) return h('span', {}, short);
  return h('a', { href: `${base}/commit/${code.commit}`, target: '_blank', rel: 'noopener' }, short);
}

async function patchPanel(slug, code) {
  const tooLarge = code.patch_kind === 'too_large' || code.patch_truncated;
  const panel = h('details', { class: 'panel' });
  panel.append(h('summary', {},
    'Uncommitted changes',
    h('span', { class: 'badge badge-warn' },
      code.patch_lines ? `${code.patch_lines} lines` : 'diff'),
    typeof code.patch_files_changed === 'number'
      ? h('span', { class: 'badge badge-info' }, `${code.patch_files_changed} files`) : null,
    tooLarge ? h('span', { class: 'badge badge-bad' }, 'TRUNCATED') : null));
  const body = h('div', { class: 'panel-body flush' });
  if (tooLarge) {
    // A truncated patch will not apply. Saying so loudly is the difference between
    // "not reproducible" and "silently wrong".
    body.append(h('div', { class: 'panel-body' }, h('div', { class: 'banner-error' },
      'This diff exceeded the size cap and was cut off. It will NOT apply cleanly — ' +
      'this run cannot be reconstructed from the commit and patch alone.')));
  }
  const slot = h('div', { class: 'panel-body faint small' }, 'Loading diff…');
  body.append(slot);
  panel.append(body);

  // Fetch lazily: patches are the biggest thing in a run and most visits never open one.
  let loaded = false;
  panel.addEventListener('toggle', async () => {
    if (!panel.open || loaded) return;
    loaded = true;
    try {
      const text = await getText(`${DATA}/projects/${encodeURIComponent(slug)}/runs/${code.patch_file}`);
      slot.replaceWith(renderDiff(text));
    } catch (err) {
      slot.replaceWith(h('div', { class: 'panel-body' }, errorBanner(err)));
    }
  });
  return panel;
}

/** Colourise a unified diff. ~30 lines instead of a 335 KB library. */
function renderDiff(text) {
  const pre = h('pre', { class: 'diff' });
  const lines = text.split('\n');
  // Trailing newline produces a final empty element that would render as a blank row.
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  for (const line of lines) {
    let cls = 'ln-ctx';
    if (line.startsWith('+++') || line.startsWith('---') ||
        line.startsWith('diff ') || line.startsWith('index ') ||
        line.startsWith('new file') || line.startsWith('deleted file') ||
        line.startsWith('similarity index') || line.startsWith('rename ')) cls = 'ln-meta';
    else if (line.startsWith('@@')) cls = 'ln-hunk';
    else if (line.startsWith('+')) cls = 'ln-add';
    else if (line.startsWith('-')) cls = 'ln-del';
    // A blank string still needs to occupy a row.
    pre.append(h('span', { class: `ln ${cls}` }, line === '' ? ' ' : line));
  }
  return pre;
}

function configPanel(config) {
  const flat = [];
  (function walk(obj, prefix) {
    for (const [k, v] of Object.entries(obj)) {
      const key = prefix ? `${prefix}.${k}` : k;
      if (v && typeof v === 'object' && !Array.isArray(v)) walk(v, key);
      else flat.push([key, fmtValue(v)]);
    }
  })(config, '');
  flat.sort((a, b) => a[0].localeCompare(b[0]));

  const dl = h('dl', { class: 'kv' });
  flat.forEach(([k, v]) => { dl.append(h('dt', {}, k)); dl.append(h('dd', {}, v)); });

  return h('details', { class: 'panel', open: flat.length <= 24 ? '' : null },
    h('summary', {}, 'Configuration', h('span', { class: 'badge badge-info' }, `${flat.length} keys`)),
    h('div', { class: 'panel-body' }, dl));
}

/* ------------------------------------------------------------ metric curves */

function curvesPanel(curves) {
  const names = Object.keys(curves);
  const panel = h('div', { class: 'panel' });
  panel.append(h('div', { class: 'panel-head' }, h('h3', {}, 'Training curves')));
  const body = h('div', { class: 'panel-body' });
  const chart = h('div');
  const tabs = h('div', { class: 'toolbar' });

  let active = names[0];
  const draw = () => {
    clear(chart);
    chart.append(lineChart(curves[active], active));
    [...tabs.children].forEach(b => b.classList.toggle('is-active', b.dataset.name === active));
  };
  names.forEach(name => {
    const btn = h('button', { class: 'btn', type: 'button' }, name);
    btn.dataset.name = name;
    btn.addEventListener('click', () => { active = name; draw(); });
    tabs.append(btn);
  });
  if (names.length > 1) body.append(tabs);
  body.append(chart);
  panel.append(body);
  draw();
  return panel;
}

/** Minimal responsive SVG line chart. Accepts [[x,y],…] or [y,…]. */
function lineChart(series, label) {
  const pts = (series || []).map((p, i) => Array.isArray(p) ? [Number(p[0]), Number(p[1])] : [i, Number(p)])
    .filter(([x, y]) => isFinite(x) && isFinite(y));
  if (pts.length < 2) return h('p', { class: 'faint small' }, 'Not enough points to plot.');

  const W = 720, H = 240, PAD_L = 56, PAD_R = 12, PAD_T = 12, PAD_B = 30;
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  let x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (x1 === x0) x1 = x0 + 1;
  if (y1 === y0) { y0 -= 0.5; y1 += 0.5; }
  const pad = (y1 - y0) * 0.06;
  y0 -= pad; y1 += pad;

  const sx = x => PAD_L + ((x - x0) / (x1 - x0)) * (W - PAD_L - PAD_R);
  const sy = y => H - PAD_B - ((y - y0) / (y1 - y0)) * (H - PAD_T - PAD_B);

  const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${sx(p[0]).toFixed(2)},${sy(p[1]).toFixed(2)}`).join('');

  let grid = '';
  for (let i = 0; i <= 4; i++) {
    const y = y0 + (i / 4) * (y1 - y0);
    const py = sy(y).toFixed(2);
    grid += `<line x1="${PAD_L}" y1="${py}" x2="${W - PAD_R}" y2="${py}" stroke="var(--line)" stroke-width="1"/>`;
    grid += `<text x="${PAD_L - 8}" y="${py}" dy="0.32em" text-anchor="end" font-size="10" `
          + `fill="var(--text-faint)" font-family="var(--font-mono)">${esc(fmtNumber(Number(y.toPrecision(4))))}</text>`;
  }
  for (const xv of [x0, (x0 + x1) / 2, x1]) {
    grid += `<text x="${sx(xv).toFixed(2)}" y="${H - PAD_B + 16}" text-anchor="middle" font-size="10" `
          + `fill="var(--text-faint)" font-family="var(--font-mono)">${esc(fmtNumber(Math.round(xv)))}</text>`;
  }

  const svg = h('div', { class: 'table-scroll', style: 'border:none;background:none' });
  svg.innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="${esc(label)} over steps" `
    + `preserveAspectRatio="xMidYMid meet" style="min-width:420px">${grid}`
    + `<path d="${d}" fill="none" stroke="var(--accent)" stroke-width="1.75" `
    + `stroke-linejoin="round" stroke-linecap="round"/></svg>`;
  return svg;
}

/* ------------------------------------------------------------------ router */

function setBuiltAt(builtAt) {
  const el = $('#built-at');
  if (!el || !builtAt) return;
  el.textContent = `data as of ${fmtAgo(builtAt)}`;
  el.title = `Index built ${fmtDate(builtAt)}. A newly posted run appears within a few minutes.`;
}

// Exported so the test harness can drive each view directly instead of relying on
// module re-evaluation. Browsers ignore the export.
export async function route() {
  const app = $('#app');
  const hash = location.hash.replace(/^#\/?/, '');
  const parts = hash.split('/').filter(Boolean).map(decodeURIComponent);

  app.setAttribute('aria-busy', 'true');
  try {
    if (parts[0] === 'p' && parts[2] === 'r' && parts[3]) await viewRun(app, parts[1], parts[3]);
    else if (parts[0] === 'p' && parts[1]) await viewProject(app, parts[1]);
    else await viewProjects(app);
    window.scrollTo(0, 0);
  } catch (err) {
    clear(app);
    app.append(errorBanner(err));
    app.append(h('p', { class: 'small faint' },
      'If this is a run that was just posted, the site may not have rebuilt yet. ',
      h('a', { href: '#/' }, 'Back to projects')));
  } finally {
    app.removeAttribute('aria-busy');
  }
}

/* ------------------------------------------------------------------- theme */

function initTheme() {
  const btn = $('#theme-toggle');
  if (!btn) return;
  btn.addEventListener('click', () => {
    const explicit = document.documentElement.getAttribute('data-theme');
    const dark = explicit
      ? explicit === 'dark'
      : window.matchMedia('(prefers-color-scheme: dark)').matches;
    const next = dark ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('theme', next); } catch (e) {}
  });
}

window.addEventListener('hashchange', route);
initTheme();
route();
