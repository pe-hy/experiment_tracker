// Drives the site against the REAL tracked data (data/projects/…), the way a
// reader would: renders the story, folds and unfolds subtrees, checks that every
// description renders whole and every interactive control actually does something.
//
// The fixture suite proves the views work; this suite proves they work on the data
// we actually serve — which is where the last two UX bugs lived (an unwired
// Expand-all button, and a fold badge whose CSS class was never defined). Run with:
//
//     python3 scripts/reindex.py --data data --out /tmp/real/data
//     node tests/clickthrough_real.mjs /tmp/real
//
// or `bash tests/run.sh`, which does both and skips this stage gracefully when the
// repo has no tracked projects yet.

import { readFileSync, readdirSync } from 'node:fs';
import { installDOM } from './dom_stub.mjs';

const siteRoot = process.argv[2];
if (!siteRoot) {
  console.error('usage: node tests/clickthrough_real.mjs <built-site-dir>');
  process.exit(2);
}

let failures = 0;
let checks = 0;
function check(name, condition, detail) {
  checks++;
  if (condition) console.log('  ok   ' + name);
  else { failures++; console.log('  FAIL ' + name + (detail ? '  — ' + detail : '')); }
}

const fetchImpl = async (url) => {
  const path = siteRoot + '/' + String(url).replace(/^\.\//, '');
  try {
    const body = readFileSync(decodeURI(path), 'utf8');
    return { ok: true, status: 200, statusText: 'OK',
             json: async () => JSON.parse(body), text: async () => body };
  } catch (err) {
    return { ok: false, status: 404, statusText: 'Not Found',
             json: async () => ({}), text: async () => '' };
  }
};
const flush = () => new Promise(resolve => setImmediate(resolve));

installDOM({ fetchImpl, hash: '' });
const app_module = await import('../site/assets/app.js');

async function renderAt(hash, keepStorage) {
  const store = keepStorage && globalThis.localStorage;
  const dom = installDOM({ fetchImpl, hash });
  if (store) globalThis.localStorage = store;   // simulate a returning reader
  await app_module.route();
  for (let i = 0; i < 60; i++) await flush();
  return dom;
}

const index = JSON.parse(readFileSync(siteRoot + '/data/index.json', 'utf8'));
const project = (index.projects || [])[0];
if (!project) {
  console.log('no tracked projects — nothing to click through');
  process.exit(0);
}
const slug = project.slug;
const projectDoc = JSON.parse(
  readFileSync(`${siteRoot}/data/projects/${slug}/project.json`, 'utf8'));
const rows = ((projectDoc.lineage || {}).rows) || [];
const hasClass = (e, c) => String(e.className || '').split(/\s+/).includes(c);

console.log(`\nreal-data story view (#/p/${slug}) — ${rows.length} lineage rows`);
{
  const { app } = await renderAt(`#/p/${slug}`);
  const liRows = app.findAll(e => hasClass(e, 'lineage-row'));
  check('every lineage row renders', liRows.length === rows.length,
    `${liRows.length} of ${rows.length}`);

  // Full prose, no clamps: every description and conclusion string from the data
  // must appear verbatim in the rendered page.
  const text = app.textContent;
  const missingDesc = rows.filter(r => r.description && !text.includes(r.description));
  check('every description renders in full, unclamped', missingDesc.length === 0,
    missingDesc.slice(0, 2).map(r => r.variant).join(', '));
  const missingConcl = rows.filter(r => r.conclusion && !text.includes(r.conclusion));
  check('every conclusion renders in full, unclamped', missingConcl.length === 0,
    missingConcl.slice(0, 2).map(r => r.variant).join(', '));
  check('no more/less clamp controls', app.findAll(e => hasClass(e, 'morelink')).length === 0);

  // Primary-edge "why" notes render as visible prose, not hover-only titles.
  const notes = rows.flatMap(r => (r.parents || [])
    .filter((p, i) => i === 0 && p.relation === 'derived-from' && p.variant === r.primary_parent && p.note)
    .map(p => p.note));
  const missingNotes = notes.filter(n => !text.includes(n));
  check(`every why-note renders (${notes.length} notes)`, missingNotes.length === 0,
    missingNotes.slice(0, 2).join(' | '));

  // No surface shows a slug where a display name exists.
  const provChips = app.findAll(e => hasClass(e, 'chip-prov'));
  const nameOf = {}; rows.forEach(r => { nameOf[r.variant] = r.variant_name || r.variant; });
  check('provenance chips use display names, not slugs', provChips.every(ch =>
    !rows.some(r => r.variant_name && r.variant !== r.variant_name
      && ch.textContent.includes(r.variant))),
    'a chip still carries a join key');

  // Legend: one entry per status actually present.
  const present = new Set(rows.map(r => r.status).filter(Boolean));
  const legendItems = app.findAll(e => hasClass(e, 'legend-item'));
  check('a status legend exists and covers the statuses present',
    legendItems.length >= present.size, `${legendItems.length} items for ${present.size} statuses`);

  // Fold interaction. Parents = rows followed by a deeper row.
  const parents = rows.filter((r, i) => rows[i + 1] && rows[i + 1].indent > r.indent);
  const chevs = app.findAll(e => hasClass(e, 'lg-chev'));
  check('exactly the parent rows get a fold control', chevs.length === parents.length,
    `${chevs.length} controls for ${parents.length} parents`);
  check('fold badges are hidden while expanded',
    app.findAll(e => hasClass(e, 'lg-foldbadge')).every(b => hasClass(b, 'hidden')),
    'a permanent "N more ideas" label is noise');

  // Fold the largest subtree and count what disappears.
  let biggest = null, biggestKids = 0;
  rows.forEach((r, i) => {
    let end = i + 1;
    while (end < rows.length && rows[end].indent > r.indent) end++;
    if (end - i - 1 > biggestKids) { biggestKids = end - i - 1; biggest = { row: r, index: i }; }
  });
  if (biggest) {
    const li = liRows[biggest.index];
    const chev = li.find(e => hasClass(e, 'lg-chev'));
    chev.dispatch('click');
    const hidden = app.findAll(e => hasClass(e, 'lineage-row') && hasClass(e, 'lg-hidden'));
    check(`folding the biggest subtree hides its ${biggestKids} descendants`,
      hidden.length === biggestKids, `${hidden.length} hidden`);
    const badge = li.find(e => hasClass(e, 'lg-foldbadge'));
    check('the folded row shows a labelled count', !!badge && !hasClass(badge, 'hidden')
      && badge.textContent.includes(String(biggestKids)), badge && badge.textContent);
    badge.dispatch('click');
    check('clicking the count unfolds again',
      app.findAll(e => hasClass(e, 'lineage-row') && hasClass(e, 'lg-hidden')).length === 0);

    // Persistence: fold once more, then re-render as the same reader.
    chev.dispatch('click');
    const again = await renderAt(`#/p/${slug}`, true);
    const stillHidden = again.app.findAll(e => hasClass(e, 'lineage-row') && hasClass(e, 'lg-hidden'));
    check('fold state survives leaving and returning', stillHidden.length === biggestKids,
      `${stillHidden.length} hidden after re-render`);
    globalThis.localStorage.setItem(`fold:${slug}`, '[]');   // clean up for later stages
  }

  // Expand-all / collapse-all are wired (the bug class the harness exists for).
  const fresh = await renderAt(`#/p/${slug}`);
  const btn = (label) => fresh.app.find(e => e.tagName === 'BUTTON' && e.textContent === label);
  const collapseAll = btn('Collapse all'), expandAll = btn('Expand all');
  check('offers expand/collapse all', !!collapseAll && !!expandAll);
  if (collapseAll && expandAll) {
    collapseAll.dispatch('click');
    const hiddenAfter = fresh.app.findAll(e => hasClass(e, 'lineage-row') && hasClass(e, 'lg-hidden')).length;
    check('collapse-all actually hides rows', hiddenAfter > 0, 'button rendered but not wired?');
    expandAll.dispatch('click');
    check('expand-all restores every row',
      fresh.app.findAll(e => hasClass(e, 'lineage-row') && hasClass(e, 'lg-hidden')).length === 0);
    globalThis.localStorage.setItem(`fold:${slug}`, '[]');
  }
}

console.log(`\nreal-data runs tab (#/p/${slug}/runs)`);
{
  const { app } = await renderAt(`#/p/${slug}/runs`);
  const text = app.textContent;
  const variants = projectDoc.variants || [];
  const missingDesc = variants.filter(v => v.description && !text.includes(v.description));
  check('every variant description renders in full while collapsed',
    missingDesc.length === 0, missingDesc.slice(0, 2).map(v => v.variant).join(', '));
  const missingConcl = variants.filter(v => v.conclusion && !text.includes(v.conclusion));
  check('every variant conclusion is visible without expanding',
    missingConcl.length === 0, missingConcl.slice(0, 2).map(v => v.variant).join(', '));
  check('statuses with strong verdicts still get a badge', (() => {
    // Regression: the runs tab once had its own partial status map, so refuted /
    // superseded / inconclusive variants — the strongest verdicts — showed nothing.
    const strong = variants.filter(v => ['refuted', 'superseded', 'inconclusive'].includes(v.status));
    if (!strong.length) return true;
    const sums = app.findAll(e => e.tagName === 'SUMMARY');
    return strong.every(v => {
      const sum = sums.find(sm => sm.textContent.includes(v.variant_name || v.variant));
      return !!sum && !!sum.find(e => String(e.className || '').includes('badge-'));
    });
  })());
}

console.log('\n' + (failures ? `${failures} of ${checks} checks FAILED` : `all ${checks} checks passed`));
process.exit(failures ? 1 : 0);
