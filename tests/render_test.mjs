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

console.log('\nproject list (#/)');
{
  const { app, builtAt } = await renderAt('');
  const text = app.textContent;
  check('renders the project name', text.includes('Fixture Project'));
  check('renders the project description', text.includes('long-running fixture'));
  check('shows a variant blurb on the card', text.includes('deeper encoder'));
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

console.log('\nproject detail (#/p/fixture-project)');
{
  const { app } = await renderAt('#/p/fixture-project');
  const text = app.textContent;
  check('renders the variant description', text.includes('Increase encoder depth'));
  check('renders the variant conclusion', text.includes('Did not help'));
  check('marks a concluded variant', text.includes('concluded'));
  check('shows the run table with metric columns', text.includes('val_accuracy'));
  check('marks the best value with a glyph, not colour alone', text.includes('★'),
    'WCAG: colour must not be the only signal');
  check('respects metric_goals direction (lower val_loss wins)', (() => {
    const best = app.findAll(e => e.className.includes('best'));
    return best.some(e => e.textContent.includes('0.31'));
  })(), 'metric_goals says val_loss is min');
  check('offers CSV export', text.includes('Copy CSV'));
  check('offers LaTeX export', text.includes('Copy LaTeX'));
  check('only the newest variant starts expanded', (() => {
    const panels = app.findAll(e => e.tagName === 'DETAILS' && e.className.includes('panel'));
    return panels.filter(p => p.hasAttribute('open')).length <= 1;
  })());
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
  const { app } = await renderAt('#/p/fixture-project');
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
