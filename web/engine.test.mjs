// Node harness for engine.js. Shims fetch onto the filesystem so the exact
// browser code path is exercised, including the binary roster parse.
import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
globalThis.fetch = async (p) => {
  const buf = await readFile(join(here, p));
  return {
    json: async () => JSON.parse(buf.toString('utf8')),
    arrayBuffer: async () => buf.buffer.slice(
      buf.byteOffset, buf.byteOffset + buf.byteLength),
  };
};

const { Engine } = await import('./engine.js');

let failures = 0;
const check = (label, cond, detail = '') => {
  if (!cond) { failures++; console.log(`  FAIL ${label} ${detail}`); }
  else console.log(`  ok   ${label} ${detail}`);
};

const t0 = Date.now();
const e = await new Engine().load();
console.log(`loaded ${e.n} players, ${e.stints.length} stints in ${Date.now() - t0}ms\n`);

const find = (name, year) => {
  const hits = [];
  for (let i = 0; i < e.n; i++) if (e.names[i] === name) hits.push(i);
  if (!hits.length) throw new Error('missing player ' + name);
  if (year !== undefined) {
    const yr = hits.filter(i => e.y0[i] <= year && e.lastYear(i) >= year);
    if (yr.length) return yr.sort((a, b) => e.fame[b] - e.fame[a])[0];
  }
  return hits.sort((a, b) => e.fame[b] - e.fame[a])[0];
};

console.log('-- teammate facts --');
check('Ruth & Gehrig', e.areTeammates(find('Babe Ruth'), find('Lou Gehrig')),
      e.linkLabel(find('Babe Ruth'), find('Lou Gehrig')));
check('Jeter & Rivera', e.areTeammates(find('Derek Jeter'), find('Mariano Rivera')),
      e.linkLabel(find('Derek Jeter'), find('Mariano Rivera')));
check('Judge & Ohtani are NOT teammates',
      !e.areTeammates(find('Aaron Judge'), find('Shohei Ohtani')));
check('Ruth & Ohtani are NOT teammates',
      !e.areTeammates(find('Babe Ruth'), find('Shohei Ohtani')));

console.log('\n-- ambiguous names --');
check('Ken Griffey is flagged ambiguous', e.ambiguous.has('Ken Griffey'));
const griffeys = [];
for (let i = 0; i < e.n; i++) if (e.names[i] === 'Ken Griffey') griffeys.push(i);
griffeys.forEach(i => console.log('       ' + e.describe(i)));

console.log('\n-- autocomplete --');
for (const q of ['jose alt', 'griffey', 'jd mart', 'ohtani', 'ripken']) {
  const hits = e.search(q, 3);
  console.log(`  "${q}" -> ${hits.map(i => e.names[i]).join(', ')}`);
  check(`search "${q}" returns hits`, hits.length > 0);
}

console.log('\n-- validation rules --');
const start = find('Derek Jeter'), target = find('Shohei Ohtani');
check('rejects a non-teammate',
      e.validate(find('Babe Ruth'), [start], target).reason === 'not-teammate');
check('rejects a duplicate',
      e.validate(start, [start], target).reason === 'duplicate');
check('accepts a real teammate',
      e.validate(find('Mariano Rivera'), [start], target).ok);

console.log('\n-- auto-solve every puzzle in the bank, one shortest step at a time --');
const dates = Object.keys(e.puzzles).sort();
let worst = 0, totalLinks = 0, checked = 0;
const t1 = Date.now();
for (const date of dates) {
  const p = e.puzzles[date];
  const chain = [p.s];
  let guard = 0;
  while (!e.areTeammates(chain[chain.length - 1], p.t)) {
    const h = e.nextStep(chain, p.t);
    if (h === null) { console.log(`  FAIL ${date}: nextStep returned null`); failures++; break; }
    const v = e.validate(h, chain, p.t);
    if (!v.ok) { console.log(`  FAIL ${date}: step ${e.names[h]} invalid (${v.reason})`); failures++; break; }
    chain.push(h);
    if (++guard > 8) { console.log(`  FAIL ${date}: no convergence`); failures++; break; }
  }
  // every consecutive pair must be a real teammate link
  for (let i = 0; i + 1 < chain.length; i++) {
    if (!e.areTeammates(chain[i], chain[i + 1])) {
      console.log(`  FAIL ${date}: broken link`); failures++;
    }
  }
  const links = chain.length; // start + intermediates
  worst = Math.max(worst, links);
  totalLinks += links;
  checked++;
  if (checked <= 3) {
    console.log(`  ${date}: ${chain.map(i => e.names[i]).join(' -> ')} -> ${e.names[p.t]}`);
  }
}
console.log(`  solved ${checked} puzzles in ${Date.now() - t1}ms`);
console.log(`  mean chain ${(totalLinks / checked).toFixed(2)} links, deepest ${worst}`);
check('every puzzle auto-solved', failures === 0);

console.log('\n-- worked-solution quality (should be a name you know) --');
const sample = dates.slice(0, 5);
for (const date of sample) {
  const p = e.puzzles[date];
  const h = e.nextStep([p.s], p.t);
  console.log(`  ${e.names[p.s]} -> [${e.names[h]}] -> ${e.names[p.t]}`);
}

console.log('\n-- perf --');
const t2 = Date.now();
for (let i = 0; i < 200; i++) e.distancesFrom(dates.length ? e.puzzles[dates[i % dates.length]].t : 0);
console.log(`  200 full BFS: ${Date.now() - t2}ms`);
const t3 = Date.now();
for (let i = 0; i < 200; i++) e.search('mart', 8);
console.log(`  200 autocompletes: ${Date.now() - t3}ms`);

console.log(failures ? `\n${failures} FAILURES` : '\nall checks passed');
process.exit(failures ? 1 : 0);
