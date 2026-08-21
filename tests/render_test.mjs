// Renders every view against a fixture and asserts the things that would actually
// be wrong if they broke. Run with:
//
//     python3 scripts/reindex.py --data tests/fixture --out /tmp/fx/data
//     node tests/render_test.mjs /tmp/fx
//
// or just `bash tests/run.sh`, which does both.

import { readFileSync } from 'node:fs';
import { installDOM, Element } from './dom_stub.mjs';

const siteRoot = process.argv[2];
if (!siteRoot) {
  console.error('usage: node tests/render_test.mjs <built-site-dir>');
  process.exit(2);
}

let failures = 0;
let checks = 0;

function check(name, condition, detail) {
  checks++;
  if (condition) {
    console.log('  ok   ' + name);
  } else {
    failures++;
    console.log('  FAIL ' + name + (detail ? '  — ' + detail : ''));
  }
}

// Serve the built site from disk, exactly as Pages would over HTTP.
const fetchImpl = async (url) => {
  const path = siteRoot + '/' + String(url).replace(/^\.\//, '');
  try {
    const body = readFileSync(decodeURI(path), 'utf8');
    return {
      ok: true, status: 200, statusText: 'OK',
      json: async () => JSON.parse(body),
      text: async () => body,
    };
  } catch (err) {
    return { ok: false, status: 404, statusText: 'Not Found', json: async () => ({}), text: async () => '' };
  }
};

const flush = () => new Promise(resolve => setImmediate(resolve));

// Install a DOM before the module is first imported: its top-level code renders
// immediately, and it captures nothing, so each later call just needs fresh globals.
installDOM({ fetchImpl, hash: '' });
const app_module = await import('../site/assets/app.js');

async function renderAt(hash) {
  const dom = installDOM({ fetchImpl, hash });
  await app_module.route();
  for (let i = 0; i < 40; i++) await flush();
  return dom;
}

console.log('\nstylesheet invariants');
{
  // Strip comments first: prose about the rule must not be mistaken for the rule.
  const css = readFileSync(new URL('../site/assets/app.css', import.meta.url), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '');
  const thead = (css.match(/table\.tbl thead th \{[^}]*\}/) || [''])[0];
  const scroll = (css.match(/\.table-scroll \{[^}]*\}/) || [''])[0];
  // A sticky header inside an overflow-x container resolves against that container,
  // not the page. Without a height constraint it never scrolls, so a non-zero `top`
  // pushes the header down over its own rows — which is exactly what shipped once.
  const sticky = /position:\s*sticky/.test(thead);
  // `\s*` backtracks to zero width, so `top:\s*(?!0)` matches "top: 0". Capture and
  // compare instead of asserting with a lookahead.
  const topValue = (thead.match(/top:\s*([^;]+);/) || [])[1];
  const offset = !!topValue && topValue.trim() !== '0';
  const bounded = /max-height/.test(scroll);
  check('table headers are not sticky-with-offset inside an unbounded scroll box',
    !(sticky && offset && !bounded),
    'give .table-scroll a max-height and use top:0, or drop position:sticky');
  check('long metric headers may wrap', /white-space:\s*normal/.test(thead));

  // A <details> marker is itself a grid item, so a grid summary needs every child
  // placed explicitly or auto-placement drops the last one into the marker column —
  // which rendered a variant description one character per line, down the page.
  const summaryRule = (css.match(/details\.panel\.variant > summary \{[^}]*\}/) || [''])[0];
  const isGrid = /display:\s*grid/.test(summaryRule);
  const placesChildren = /grid-column/.test(css);
  check('the variant summary does not rely on grid auto-placement',
    !isGrid || placesChildren,
    'a grid summary must place its children explicitly; flex with one wrapper is safer');
}

console.log('\nrelative time (a system of record must not misdate its own runs)');
{
  const now = Date.now();
  const ago = (secs) => app_module.fmtAgo(new Date(now - secs * 1000).toISOString());
  const cases = [
    [45, '45s ago'], [100, '1m ago'], [60 * 59, '59m ago'], [3700, '1h ago'],
    [90000, '1d ago'], [86400 * 29, '4w ago'], [86400 * 400, '1y ago'],
  ];
  cases.forEach(([secs, want]) => check(`${secs}s reads as "${want}"`, ago(secs) === want, `got "${ago(secs)}"`));
}

console.log('\nproject list (#/)');
{
  const { app, builtAt } = await renderAt('');
  const text = app.textContent;
  check('renders the project name', text.includes('Fixture Project'));
  check('renders the project description', text.includes('long-running fixture'));
  check('shows a variant blurb on the card', text.includes('Increase encoder depth'));
  check('shows the cross-project recent-activity feed',
    text.includes('Recent activity across all projects'));
  check('recent feed lists a run from the second project', text.includes('Second Project'));
  check('surfaces unreadable files as an error banner', text.includes('could not be read'),
    'the malformed fixture run must not vanish silently');
  check('reports accelerator-hours', /accelerator-hours/.test(text));
  check('stamps when the index was built', builtAt.textContent.startsWith('data as of'));
  const cards = app.findAll(e => e.className.split(/\s+/).includes('card'));
  check('project cards exist and every one links to its project',
    cards.length === 2 && cards.every(e => (e.getAttribute('href') || '').startsWith('#/p/')),
    `found ${cards.length} cards`);
}

console.log('\nproject story (#/p/fixture-project)');
{
  const { app } = await renderAt('#/p/fixture-project');
  const t = app.textContent;
  check('leads with the story, not a metric table',
    app.findAll(e => e.tagName === 'TABLE').length === 0);
  const hasClass = (e, c) => String(e.className || '').split(/\s+/).includes(c);
  check('shows every variant as a lineage row',
    app.findAll(e => hasClass(e, 'lineage-row')).length === 2);
  check('draws a rail per row', app.findAll(e => hasClass(e, 'rail')).length === 2);
  check('every row carries a status dot',
    app.findAll(e => hasClass(e, 'rail-dot')).length === 2);
  check('shows the idea in the row', t.includes('Increase encoder depth'));
  check('shows the conclusion in the row', t.includes('Did not help'));
  check('says plainly when no lineage is recorded', t.includes('No idea-lineage recorded yet'));
  check('offers a verdict tally', t.includes('have a recorded conclusion'));
  check('offers both tabs', !!app.find(e => e.className.includes('tab') && e.textContent === 'Story')
    && !!app.find(e => e.tagName === 'A' && e.textContent.startsWith('Runs (')));
}

console.log('\nproject runs tab (#/p/fixture-project/runs)');
{
  const { app } = await renderAt('#/p/fixture-project/runs');
  let text = app.textContent;
  check('renders the variant description', text.includes('Increase encoder depth'));
  check('the idea is readable while collapsed', (() => {
    const gist = app.find(e => e.className.includes('variant-gist'));
    return !!gist && gist.textContent.includes('Increase encoder depth');
  })(), 'explanations must not be hidden behind a click');
  check('nothing is expanded by default', (() => {
    const panels = app.findAll(e => e.tagName === 'DETAILS' && e.className.includes('variant'));
    return panels.length > 0 && panels.every(p => !p.hasAttribute('open'));
  })(), 'a page of expanded variants is a wall, not a view');
  check('run tables are not built until opened',
    app.findAll(e => e.tagName === 'TABLE').length === 0,
    'lazy tables keep the first paint cheap');
  check('offers expand/collapse all',
    !!app.find(e => e.tagName === 'BUTTON' && e.textContent === 'Expand all'));
  check('the summary holds exactly one wrapper, not three loose children', (() => {
    const summary = app.find(e => e.tagName === 'SUMMARY');
    if (!summary) return false;
    const kids = summary.childNodes.filter(n => n.tagName);
    return kids.length === 1 && String(kids[0].className).includes('variant-summary');
  })(), 'extra children get auto-placed into the narrow marker column');
  check('the description sits inside that wrapper', (() => {
    const wrap = app.find(e => String(e.className || '').includes('variant-summary'));
    return !!wrap && !!wrap.find(e => String(e.className || '').includes('variant-gist'));
  })());

  // Everything below concerns the run table, so open the variants first.
  app.findAll(e => e.tagName === 'BUTTON' && e.textContent === 'Expand all')[0].dispatch('click');
  for (let i = 0; i < 10; i++) await flush();
  text = app.textContent;
  check('expanding builds the run tables', app.findAll(e => e.tagName === 'TABLE').length > 0);
  check('renders the variant conclusion', text.includes('Did not help'));
  check('marks a concluded variant', text.includes('concluded'));
  check('shows the run table with metric columns', (() => {
    // The header renders the evaluation suite as a badge, so the full key lives in
    // the title rather than in the visible text.
    const th = app.find(e => e.tagName === 'TH' && e.getAttribute('title') === 'val_accuracy');
    return !!th && th.textContent.includes('accuracy');
  })());
  check('splits the evaluation suite out of the metric name',
    !!app.find(e => e.className === 'suite' && e.textContent === 'val'),
    'frontier_solve_rate and graded_solve_rate must not read as unrelated strings');
  check('rates render as percentages, consistently within a column', (() => {
    const cells = app.findAll(e => e.tagName === 'TD' && e.className.includes('num'))
      .map(e => e.textContent.trim());
    // fixture val_accuracy values are 0.8241 and 0.4 -> one column, both at 4dp.
    // The best cell also carries a star, so compare on the numeric prefix.
    const nums = cells.map(c => c.replace(/\s*★$/, ''));
    return nums.includes('0.8241') && nums.includes('0.4000');
  })(), 'a column must not mix "0.4" with "0.8241"');
  check('marks the best value with a glyph, not colour alone', text.includes('★'),
    'WCAG: colour must not be the only signal');
  check('respects metric_goals direction (lower val_loss wins)', (() => {
    const best = app.findAll(e => e.className.includes('best'));
    return best.some(e => e.textContent.includes('0.31'));
  })(), 'metric_goals says val_loss is min');
  check('sorting keeps the same header element (focus survives)', (() => {
    const th = app.findAll(e => e.tagName === 'TH' && e.className.includes('sortable'))[1];
    const before = th;
    th.dispatch('click');
    const after = app.findAll(e => e.tagName === 'TH' && e.className.includes('sortable'))[1];
    return before === after && after.hasAttribute('aria-sort');
  })(), 'rebuilding thead drops keyboard focus to the top of the page');
  check('offers CSV export', text.includes('Copy CSV'));
  check('offers LaTeX export', text.includes('Copy LaTeX'));
}

console.log('\nrun detail');
{
  const { app } = await renderAt('#/p/fixture-project/r/20260101T000000Z-aaaaaa');
  const text = app.textContent;
  check('shows the run name', text.includes('fixture-run-a'));
  check('shows metrics', text.includes('0.8241') || text.includes('0.824'));
  check('shows the hypothesis', text.includes('capacity is the limit'));
  check('shows the conclusion', text.includes('DEMO'));
  check('shows the commit', text.includes('abc123'));
  check('warns that the commit was never pushed', text.includes('never pushed'),
    'a SHA nobody else can resolve must be flagged');
  check('renders training curves', text.includes('Training curves'));
  check('offers an edit link', app.findAll(e => (e.getAttribute('href') || '').includes('/edit/main/')).length > 0);
  check('offers a delete link', app.findAll(e => (e.getAttribute('href') || '').includes('/delete/main/')).length > 0);
  check('shows the config', text.includes('batch_size'));
  check('shows the snapshotted training script', text.includes('LEARNING_RATE = 3e-4'),
    'snapshots are how no-remote projects stay reproducible');
  check('flags a truncated patch loudly', text.includes('TRUNCATED'));
}

console.log('\ncomparing runs');
{
  const { app } = await renderAt('#/p/fixture-project/runs');
  app.findAll(e => e.tagName === 'BUTTON' && e.textContent === 'Expand all')[0].dispatch('click');
  for (let i = 0; i < 10; i++) await flush();
  const boxes = app.findAll(e => e.tagName === 'INPUT' && e.getAttribute('type') === 'checkbox');
  check('every run row has a selection checkbox', boxes.length >= 2, `found ${boxes.length}`);
  boxes.slice(0, 2).forEach(b => { b.checked = true; b.dispatch('change'); });
  const btn = app.find(e => e.tagName === 'BUTTON' && e.textContent.startsWith('Compare 2'));
  check('the compare button activates at two selections', !!btn);
  if (btn) {
    btn.dispatch('click');
    for (let i = 0; i < 40; i++) await flush();
    const text = app.textContent;
    check('renders a comparison', text.includes('Comparing 2 runs'));
    check('defaults to showing only what differs', text.includes('Differences only'));
    check('compares config values', text.includes('batch_size') || text.includes('lr'));
    check('compares metrics', text.includes('val_accuracy'));
  }
}

console.log('\nrun-to-run references');
{
  const { app } = await renderAt('#/p/second-project/r/20260201T000000Z-dddddd');
  check('shows what this run was derived from', app.textContent.includes('evaluates'));
  check('links to the referenced run',
    app.findAll(e => (e.getAttribute('href') || '')
      .includes('/p/fixture-project/r/20260101T000000Z-aaaaaa')).length > 0);
}

console.log('\nescaping (agent data is untrusted)');
{
  const { app } = await renderAt('#/p/fixture-project/r/20260101T000000Z-bbbbbb');
  const text = app.textContent;
  check('script tags in agent text are not executed as markup',
    text.includes('<script>alert(1)</script>'),
    'must appear as literal text');
  check('no element received raw agent HTML', (() => {
    const withHtml = app.findAll(e => e.innerHTML && e.innerHTML.includes('alert(1)'));
    return withHtml.length === 0;
  })(), 'innerHTML must never carry agent-supplied content');
}

console.log('\nmissing run (404 path)');
{
  const { app } = await renderAt('#/p/fixture-project/r/does-not-exist');
  check('shows an error rather than a blank page', app.textContent.includes('404'));
  check('offers a way back', app.findAll(e => e.getAttribute('href') === '#/').length > 0);
}

console.log('\n' + (failures ? `${failures} of ${checks} checks FAILED` : `all ${checks} checks passed`));
process.exit(failures ? 1 : 0);
