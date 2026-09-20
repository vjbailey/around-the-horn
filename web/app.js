import { Engine } from './engine.js';
import {
  puzzleDate, loadState, saveState, dayResult, recordResult,
  streak, submitResult, percentileFor, lengthKey, MAX_BUCKET, LEN_BUCKETS,
} from './results.js';

// The finish is staged rather than instant. The closing link takes ~840ms to
// settle, so anything overlapping it reads as a glitch: the relay waits for
// it, and the way through to the results waits for the relay.
//
// The results sheet is NOT on a timer. Any fixed delay is either too quick for
// someone admiring the chain or too slow for someone who wants their score, so
// the last step is a deliberate press instead of a guess at the right pause.
const RELAY_AT = 900;     // after the closing link settles
const PER_HOP = 420;      // one throw, name to name

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const game = {
  engine: null,
  puzzle: null,
  chain: [],          // player indices, start first
  origin: [],         // 'start' | 'self' | 'assisted' | 'revealed'
  assists: 0,
  solved: false,
  gaveUp: false,
  locked: false,
  testMode: false,     // dev: playing an out-of-schedule puzzle, never persisted
  justAdded: -1,       // chain index to animate on the next render
  scope: null,         // active assist filter over the search bar, or null
  timers: [],         // pending finish-sequence timeouts, cleared on reload
  state: loadState(),
  cursor: -1,         // highlighted autocomplete row
  matches: [],        // [playerIndex, inScope] pairs
};

// ---- board ---------------------------------------------------------------

function nodeEl(i, kind, role) {
  const n = el('div', `node ${kind}`);
  if (role) n.appendChild(el('span', 'node-role', role));
  const line = el('div', 'node-line');
  line.appendChild(el('span', 'node-name', game.engine.names[i]));
  line.appendChild(el('span', 'node-years',
    `${game.engine.y0[i]}–${game.engine.lastYear(i)}`));
  if (kind === 'assisted') line.appendChild(el('span', 'tag assist', 'clue'));
  n.appendChild(line);
  return n;
}

function linkEl(a, b, fresh) {
  const wrap = el('div', 'link' + (fresh ? ' enter' : ''));
  const parts = game.engine.linkParts(a, b) || [];
  const label = el('div', 'link-label');
  parts.forEach((p, k) => {
    if (k) label.appendChild(el('span', 'link-sep', '·'));
    const chip = el('span', 'chip');
    chip.appendChild(el('span', 'chip-team', p.team));
    chip.appendChild(el('span', 'chip-years', p.span));
    label.appendChild(chip);
  });
  wrap.appendChild(label);
  return wrap;
}

function gapEl() {
  const wrap = el('div', 'link gap');
  wrap.appendChild(el('div', 'gap-marker', '?'));
  // First move of the puzzle: say what is being asked for, once.
  if (game.chain.length === 1 && !game.solved) {
    wrap.appendChild(el('div', 'gap-hint',
      `Name anyone who played with ${game.engine.names[game.chain[0]]}.`));
  }
  return wrap;
}

function render() {
  const board = $('board');
  board.innerHTML = '';
  const { chain, origin, engine, puzzle } = game;
  // Only the link and node just added should animate; a full rebuild would
  // otherwise replay the whole chain on every keystroke.
  const fresh = game.justAdded;
  game.justAdded = -1;

  for (let i = 0; i < chain.length; i++) {
    const node = nodeEl(chain[i], i === 0 ? 'start' : origin[i],
                        i === 0 ? 'Start from' : null);
    node.style.setProperty('--i', String(i));
    if (i === fresh) node.classList.add('enter');
    if (i > 0) board.appendChild(linkEl(chain[i - 1], chain[i], i === fresh));
    board.appendChild(node);
  }
  const tip = chain[chain.length - 1];
  // The closing link is colour-coded by how the puzzle ended: green if you
  // closed it, red if the game had to.
  const closing = game.solved ? linkEl(tip, puzzle.t, fresh >= 0) : gapEl();
  if (game.solved) closing.classList.add(game.gaveUp ? 'revealed' : 'closed');
  board.appendChild(closing);
  const targetNode = nodeEl(puzzle.t, 'target',
                            game.solved ? (game.gaveUp ? 'Revealed' : 'Reached')
                                        : 'Get to');
  targetNode.style.setProperty('--i', String(chain.length));
  if (!game.solved) targetNode.classList.add('unreached');
  if (game.solved && fresh >= 0) targetNode.classList.add('landed');
  board.appendChild(targetNode);

  renderRoutes();
  renderScope();
  $('controls').hidden = game.locked;
}

const reducedMotion = () =>
  window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

/**
 * A chain of teammates is a relay throw, so on a solve the ball travels it --
 * start to cutoff man to cutoff man to target, popping each name as it is
 * caught. Duration scales with the chain, so a long wander earns a longer
 * relay than a direct two-link solve.
 */
function relayThrow() {
  const board = $('board');
  const nodes = [...board.querySelectorAll('.node')];
  if (nodes.length < 2) return 0;

  const top = board.getBoundingClientRect().top;
  const centres = nodes.map((n) => {
    const r = n.getBoundingClientRect();
    return r.top - top + r.height / 2;
  });

  const ball = el('div', 'ball', '⚾');
  board.appendChild(ball);

  // Two keyframes per hop: the catch, and an arced midpoint between catches.
  const frames = [];
  centres.forEach((y, i) => {
    frames.push({
      transform: `translate(-50%, ${y}px) rotate(${i * 220}deg)`,
      easing: 'cubic-bezier(.35,0,.5,1)',
    });
    if (i + 1 < centres.length) {
      frames.push({
        transform: `translate(calc(-50% + ${i % 2 ? -13 : 13}px), ` +
                   `${(y + centres[i + 1]) / 2}px) rotate(${i * 220 + 110}deg)`,
        easing: 'cubic-bezier(.5,0,.65,1)',
      });
    }
  });

  const hops = centres.length - 1;
  const total = hops * PER_HOP;
  const anim = ball.animate(frames, { duration: total, fill: 'forwards' });
  centres.forEach((_, i) => {
    game.timers.push(setTimeout(() => {
      nodes[i].classList.add('caught');
      setTimeout(() => nodes[i].classList.remove('caught'), 400);
      tap(i === centres.length - 1 ? 26 : 9);
    }, PER_HOP * i));
  });
  anim.finished.then(() => ball.remove()).catch(() => ball.remove());
  return total;
}

/** A short haptic tap where the platform offers one. */
function tap(ms = 12) {
  try { navigator.vibrate?.(ms); } catch { /* unsupported */ }
}

/**
 * Live count of the shortest chains still open from wherever the chain ends.
 * It is a gauge, not a spoiler: it says how much room is left, never who is in
 * it. Wander somewhere poorly connected and it tightens; find a hub and it
 * opens back up.
 */
function renderRoutes() {
  const box = $('routes');
  if (game.solved) { box.hidden = true; return; }
  const { routes, dist } = game.engine.routesFrom(game.chain, game.puzzle.t);
  if (!routes || dist < 1) { box.hidden = true; return; }
  const n = Math.round(routes);
  const text = `${n.toLocaleString()} route${n === 1 ? '' : 's'}`;
  box.hidden = false;
  if (box.dataset.n !== String(n)) {
    box.dataset.n = String(n);
    const narrowed = game.lastRoutes !== undefined && n < game.lastRoutes;
    const widened = game.lastRoutes !== undefined && n > game.lastRoutes;
    box.className = 'routes' + (narrowed ? ' narrowed' : widened ? ' widened' : '');
    box.textContent = text;
    // restart the flash
    void box.offsetWidth;
    box.classList.add('changed');
    setTimeout(() => box.classList.remove('changed'), 700);
  }
  game.lastRoutes = n;
}

function say(text, kind = '') {
  const m = $('message');
  m.className = `message ${kind}`;
  m.textContent = text;
}

// ---- autocomplete --------------------------------------------------------

function renderSuggestions() {
  const box = $('suggestions');
  const input = $('guess');
  box.innerHTML = '';
  if (!game.matches.length) {
    box.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    return;
  }
  box.hidden = false;
  input.setAttribute('aria-expanded', 'true');
  const eng = game.engine;
  game.matches.forEach(([idx, inScope], k) => {
    const li = el('li', (k === game.cursor ? 'active ' : '') + (inScope ? 'scoped' : ''));
    li.id = `sug-${k}`;
    li.setAttribute('role', 'option');
    li.setAttribute('aria-selected', k === game.cursor ? 'true' : 'false');
    if (k === game.cursor) input.setAttribute('aria-activedescendant', li.id);
    const name = el('span', 'sug-name', eng.names[idx]);
    if (inScope) {
      const pos = eng.posPool[eng.pos[idx]];
      if (pos) name.appendChild(el('span', 'sug-pos', pos));
    }
    li.appendChild(name);
    // Disambiguate shared names by career span and primary club (spec 7)
    const detail = eng.ambiguous.has(eng.names[idx])
      ? `${eng.y0[idx]}–${eng.lastYear(idx)} · ${eng.teamPool[eng.primary[idx]]}`
      : `${eng.y0[idx]}–${eng.lastYear(idx)}`;
    li.appendChild(el('span', 'sug-meta', detail));
    li.addEventListener('mousedown', (ev) => { ev.preventDefault(); submit(idx); });
    box.appendChild(li);
  });
}

function updateMatches() {
  const q = $('guess').value.trim();
  const used = new Set(game.chain);
  const { engine, scope } = game;

  if (scope) {
    // Scoped names come first, but the bar is never a cage: a player who knows
    // a valid link outside the scope can still type it.
    const inScope = scope.players.filter(p => !used.has(p));
    const hits = engine.filterNames(inScope, q);
    const others = q
      ? engine.search(q, 5, new Set([...used, ...scope.players]))
      : [];
    game.matches = [...hits.map(p => [p, true]), ...others.map(p => [p, false])];
  } else {
    game.matches = q ? engine.search(q, 8, used).map(p => [p, false]) : [];
  }
  game.cursor = game.matches.length ? 0 : -1;
  renderSuggestions();
}

function clearScope() {
  game.scope = null;
  renderScope();
  updateMatches();
}

function renderScope() {
  const bar = $('scope');
  const { scope } = game;
  bar.hidden = !scope;
  if (scope) {
    $('scope-label').textContent =
      `${scope.label} · ${scope.players.length} players`;
  }
  $('guess').placeholder = scope
    ? `Search these ${scope.players.length} players…`
    : `Name a teammate of ${game.engine.names[game.chain[game.chain.length - 1]]}…`;
  $('assist-btn').disabled = !!scope || game.locked;
  $('assist-count').textContent = game.assists ? String(game.assists) : '';
}

function clearInput() {
  $('guess').value = '';
  game.matches = [];
  game.cursor = -1;
  renderSuggestions();
}

// ---- play ----------------------------------------------------------------

function submit(idx) {
  if (game.locked || idx === undefined || idx < 0) return;
  const { engine, chain, puzzle } = game;
  const verdict = engine.validate(idx, chain, puzzle.t);
  const tipName = engine.names[chain[chain.length - 1]];

  if (!verdict.ok) {
    const name = engine.names[idx];
    if (verdict.reason === 'duplicate') {
      say(`${name} is already in your chain.`, 'bad');
    } else if (verdict.reason === 'is-target') {
      say(`${name} is the target — get to someone who played with them.`, 'bad');
    } else if (verdict.reason === 'dead-end') {
      say(`${name} was a teammate of ${tipName}, but there's no route from ` +
          `there to ${engine.names[puzzle.t]}.`, 'bad');
    } else {
      say(`${name} wasn't a teammate of ${tipName}.`, 'bad');
    }
    clearInput();
    return;
  }

  chain.push(idx);
  game.origin.push(game.scope ? 'assisted' : 'self');
  game.justAdded = chain.length - 1;
  game.scope = null;
  clearInput();
  tap();
  if (verdict.solves) {
    say('');
    finish();
  } else {
    // Name the connection you just made -- the reason the guess was right.
    say(`✓ ${engine.names[idx]} — ${engine.linkLabel(chain[chain.length - 2], idx)}`,
        'good');
    render();
  }
}

/**
 * The assist lists everyone who played with the current tip. It never says
 * which of them reaches the target -- that is the puzzle. One press, no
 * levels: the only rule in this game is "were they teammates", so the only
 * help worth giving is the answer to that question.
 */
function takeAssist() {
  if (game.locked || game.solved || game.scope) return;
  const { engine, chain, puzzle } = game;
  if (engine.areTeammates(chain[chain.length - 1], puzzle.t)) { finish(); return; }

  const scope = engine.assistScope(chain, puzzle.t);
  if (!scope || !scope.players.length) { say('No assist available.', 'bad'); return; }

  game.assists++;
  game.scope = scope;
  say(`${scope.players.length} players played with ` +
      `${engine.names[chain[chain.length - 1]]}. One of them reaches ` +
      `${engine.names[puzzle.t]}.`);
  clearInput();
  render();
  $('guess').focus();
}

function reveal() {
  if (game.locked || game.solved) return;
  game.gaveUp = true;
  let guard = 0;
  while (guard++ < 12) {
    const tip = game.chain[game.chain.length - 1];
    if (game.engine.areTeammates(tip, game.puzzle.t)) break;
    const h = game.engine.nextStep(game.chain, game.puzzle.t);
    if (h === null) break;
    game.chain.push(h);
    game.origin.push('revealed');
  }
  game.scope = null;
  game.justAdded = game.chain.length - 1;
  finish();
}

function finish() {
  game.solved = true;
  game.locked = true;
  render();

  if (game.gaveUp || reducedMotion()) {
    game.timers.push(setTimeout(showFanfare, 500));
  } else {
    const hops = game.chain.length;          // names + the target
    game.timers.push(setTimeout(relayThrow, RELAY_AT));
    game.timers.push(setTimeout(showFanfare, RELAY_AT + hops * PER_HOP + 320));
  }
  if (!game.testMode) {
    recordResult(game.state, game.puzzle.date, {
      solved: true,
      gaveUp: game.gaveUp,
      assists: game.assists,
      chain: game.chain,
      origin: game.origin,
    });
  }
}

/** The final score, rendered as a ballpark scoreboard. */
function scoreboardEl() {
  const { assists, chain, puzzle } = game;
  const wrap = el('div', 'score-wrap' + (game.gaveUp ? ' revealed' : ''));
  const board = el('div', 'scoreboard');
  const cell = (label, value) => {
    const c = el('div', 'sb-cell');
    c.appendChild(el('span', 'sb-label', label));
    c.appendChild(el('span', 'sb-val', String(value)));
    return c;
  };
  board.appendChild(cell('LINKS', chain.length));
  board.appendChild(cell('ASSISTS', assists));
  board.appendChild(cell('STREAK', streak(game.state, puzzle.date)));
  wrap.appendChild(board);
  wrap.appendChild(el('div', 'sb-verdict', game.gaveUp ? 'Revealed'
    : assists === 0 ? 'Unassisted!'
    : `Solved with ${assists} assist${assists === 1 ? '' : 's'}`));
  return wrap;
}

/**
 * What lands on the board once the relay finishes. Just the way through to the
 * results -- the numbers belong on the results card, not over the chain you
 * just built.
 */
function showFanfare() {
  const box = $('fanfare');
  box.className = 'fanfare';
  box.innerHTML = '';
  const go = el('button', 'primary see-how', 'See how you did');
  go.addEventListener('click', showResults);
  box.appendChild(go);
  box.hidden = false;
  go.focus({ preventScroll: true });
}

// ---- results -------------------------------------------------------------

function shareText() {
  const { puzzle, engine, origin, assists } = game;
  const emoji = { self: '🟩', assisted: '🟨', revealed: '🟥' };
  const strip = origin.slice(1).map(o => emoji[o] || '🟩').join('');
  const label = game.gaveUp ? 'revealed'
    : assists === 0 ? 'Unassisted!'
    : `${assists} assist${assists === 1 ? '' : 's'}`;
  const links = game.chain.length;
  return [
    `⚾ Around the Horn #${puzzle.n}`,
    `${engine.names[puzzle.s]} → ${engine.names[puzzle.t]}`,
    `${strip}  ${links} links · ${label}`,
  ].join('\n');
}

function histogramEl(entries, mineKey) {
  const wrap = el('div', 'hist');
  const max = Math.max(1, ...entries.map(([, n]) => n));
  entries.forEach(([key, n]) => {
    const row = el('div', 'hist-row' + (key === mineKey ? ' mine' : ''));
    row.appendChild(el('span', 'hist-k', key));
    const track = el('div', 'hist-track');
    const bar = el('div', 'hist-bar');
    bar.style.width = `${Math.max(1.5, 100 * n / max)}%`;
    track.appendChild(bar);
    track.appendChild(el('span', 'hist-n', n.toLocaleString()));
    row.appendChild(track);
    wrap.appendChild(row);
  });
  return wrap;
}

/** A shortest route, preferring one the player did not already walk. */
function shortestSolutionEl() {
  const { engine, puzzle, chain } = game;
  const best = engine.shortestChain(puzzle.s, puzzle.t, new Set(chain.slice(1)));
  if (!best) return null;
  const box = el('div', 'ss-box');
  const ss = el('div', 'ss-chain');
  best.forEach((q, i) => {
    ss.appendChild(el('div', 'ss-name', engine.names[q]));
    if (i + 1 < best.length) {
      ss.appendChild(el('div', 'ss-link', engine.linkLabel(q, best[i + 1]) || ''));
    }
  });
  box.appendChild(ss);
  const routes = puzzle.chains;
  box.appendChild(el('p', 'pct',
    `${best.length - 1} links · ` +
    (routes ? `${routes.toLocaleString()} different shortest routes existed`
            : 'the shortest route')));
  return box;
}

async function showResults() {
  const { engine, puzzle, chain, origin, assists } = game;
  const body = $('results-body');
  body.innerHTML = '';

  body.appendChild(scoreboardEl());

  const chainBox = el('div', 'result-chain');
  for (let i = 0; i < chain.length; i++) {
    const row = el('div', `rc-node ${i === 0 ? 'start' : origin[i]}`);
    row.appendChild(el('span', 'rc-name', engine.names[chain[i]]));
    if (origin[i] === 'revealed') row.appendChild(el('span', 'tag bad', 'revealed'));
    if (origin[i] === 'assisted') row.appendChild(el('span', 'tag assist', 'assist'));
    chainBox.appendChild(row);
    const next = i + 1 < chain.length ? chain[i + 1] : puzzle.t;
    const rcLink = el('div', 'rc-link', engine.linkLabel(chain[i], next) || '');
    if (i === chain.length - 1) {
      rcLink.classList.add(game.gaveUp ? 'revealed' : 'closed');
    }
    chainBox.appendChild(rcLink);
  }
  const last = el('div', 'rc-node target');
  last.appendChild(el('span', 'rc-name', engine.names[puzzle.t]));
  chainBox.appendChild(last);
  body.appendChild(chainBox);

  const pending = el('div', 'dist-pending', '');
  body.appendChild(pending);
  $('results').hidden = false;

  const links = chain.length;          // start->...->target, counted in links
  const min = puzzle.d;
  const data = await submitResult(puzzle.date, assists, links, game.gaveUp);
  pending.remove();

  const dist = el('div', 'dist');
  dist.appendChild(el('h3', '', 'Your chain'));
  dist.appendChild(el('p', 'pct',
    links === min
      ? `Connected in ${links} links — the fewest possible.`
      : `${links} links; it could be done in ${min}.`));

  // Distributions appear only when a backend has actually returned them.
  if (data && data.histogram) {
    dist.appendChild(el('h3', '', 'Assists used today'));
    const entries = [];
    for (let k = 0; k <= MAX_BUCKET; k++) {
      entries.push([k === MAX_BUCKET ? `${k}+` : `${k}`, data.histogram[k] || 0]);
    }
    dist.appendChild(histogramEl(entries,
      assists >= MAX_BUCKET ? `${MAX_BUCKET}+` : `${assists}`));
    const pct = data.percentile ?? percentileFor(data.histogram, assists);
    dist.appendChild(el('p', 'pct', `Fewer assists than ${pct}% of players today`));
  }
  if (data && data.lengths) {
    dist.appendChild(el('h3', '', 'Links used today'));
    const lenEntries = [];
    for (let k = 0; k < LEN_BUCKETS; k++) {
      const key = lengthKey(min + k, min);
      lenEntries.push([key, data.lengths[key] || 0]);
    }
    dist.appendChild(histogramEl(lenEntries, lengthKey(links, min)));
  }
  body.appendChild(dist);

  // Seeing a model answer and replaying are both opt-in: neither should be
  // shoved in front of someone who just solved it cleanly.
  const actions = el('div', 'result-actions');
  const showBtn = el('button', 'ghost-btn', 'Show a shortest solution');
  const againBtn = el('button', 'ghost-btn',
    game.puzzle.practice ? 'Another practice' : 'Play again');
  actions.appendChild(showBtn);
  actions.appendChild(againBtn);
  body.appendChild(actions);

  const holder = el('div', 'ss-holder');
  body.appendChild(holder);
  showBtn.addEventListener('click', () => {
    if (holder.firstChild) {
      holder.innerHTML = '';
      showBtn.textContent = 'Show a shortest solution';
      return;
    }
    const sol = shortestSolutionEl(min);
    if (sol) holder.appendChild(sol);
    showBtn.textContent = 'Hide the solution';
  });
  againBtn.addEventListener('click', () => {
    if (game.puzzle.practice) { openPractice(); return; }
    $('results').hidden = true;
    startPuzzle(game.puzzle.date, 'replay');
  });
  if (!game.puzzle.practice) {
    const practiceBtn = el('button', 'ghost-btn', 'Practice puzzle');
    practiceBtn.addEventListener('click', openPractice);
    actions.appendChild(practiceBtn);
  }

  const share = el('button', 'primary', 'Share');
  share.addEventListener('click', async () => {
    const text = shareText();
    try {
      if (navigator.share) await navigator.share({ text });
      else { await navigator.clipboard.writeText(text); share.textContent = 'Copied!'; }
    } catch { /* user dismissed */ }
  });
  body.appendChild(share);
}

// ---- boot ----------------------------------------------------------------

function restore(saved) {
  game.chain = saved.chain;
  game.origin = saved.origin;
  game.assists = saved.assists;
  game.gaveUp = saved.gaveUp;
  game.solved = true;
  game.locked = true;
}

/**
 * Load a puzzle by date. `mode` of 'test' (a random puzzle) or 'replay' (this
 * one again) plays without touching saved progress -- the day's real result
 * is already locked in and must not be overwritten by a practice run.
 */
function startPuzzle(date, mode = null, generated = null) {
  const test = mode !== null;
  const puzzle = generated || game.engine.puzzleFor(date);
  if (!puzzle) {
    $('board').replaceWith(el('p', 'empty',
      'No puzzle scheduled for today. The bank needs extending.'));
    return false;
  }
  game.timers.forEach(clearTimeout);
  $('fanfare').hidden = true;
  Object.assign(game, {
    puzzle, chain: [puzzle.s], origin: ['start'], assists: 0, timers: [],
    solved: false, gaveUp: false, locked: false, testMode: test, justAdded: -1,
    scope: null, lastRoutes: undefined,
  });
  $('routes').dataset.n = '';
  const LEVEL_NAME = { mon: 'Very easy', tue: 'Easy', wed: 'Easy', thu: 'Medium',
                       fri: 'Medium', sat: 'Tricky', sun: 'Hard' };
  $('puzzle-no').textContent = puzzle.practice ? 'Practice' : `Puzzle #${puzzle.n}`;
  $('puzzle-date').textContent = puzzle.practice
    ? `${LEVEL_NAME[puzzle.band] || 'Medium'} · ${puzzle.era}`
    : new Date(date + 'T12:00:00Z')
        .toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' });
  document.body.classList.toggle('testing', test);
  $('back-daily').hidden = !test;
  if (test) {
    $('test-flag').textContent =
      mode === 'replay' ? 'replay — not saved' : 'practice — not saved';
  }

  const saved = test ? null : dayResult(game.state, date);
  if (saved) restore(saved);
  say('');
  clearInput();
  render();
  if (saved) showResults();          // already finished: no celebration
  if (!game.locked) $('guess').focus();
  return true;
}

// Practice puzzles are generated on demand, never drawn from the schedule:
// pulling them from future dates would spoil upcoming dailies, and there are
// only 730 of those.
const LEVELS = {
  any:    { minRoutes: 4,  maxRoutes: Infinity },
  easy:   { minRoutes: 25, maxRoutes: Infinity },
  medium: { minRoutes: 8,  maxRoutes: 24 },
  hard:   { minRoutes: 4,  maxRoutes: 7 },
};

function practicePrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem('sdob.practice')) || {};
    return { minYear: saved.minYear ?? 2010, level: saved.level ?? 'medium' };
  } catch {
    return { minYear: 2010, level: 'medium' };
  }
}

function updateEraNote() {
  const n = game.engine.poolSince(Number($('era-select').value)).length;
  $('era-note').textContent = `${n.toLocaleString()} well-known players eligible`;
}

function openPractice() {
  const { minYear, level } = practicePrefs();
  $('era-select').value = String(minYear);
  $('level-select').value = level;
  $('practice-msg').textContent = '';
  updateEraNote();
  $('results').hidden = true;
  $('practice').hidden = false;
  $('deal-btn').focus({ preventScroll: true });
}

async function dealPractice() {
  const minYear = Number($('era-select').value);
  const level = $('level-select').value;
  try {
    localStorage.setItem('sdob.practice', JSON.stringify({ minYear, level }));
  } catch { /* private mode */ }

  const btn = $('deal-btn');
  btn.disabled = true;
  btn.textContent = 'Dealing…';
  $('practice-msg').textContent = '';
  // Yield a frame so the button repaints: a hard puzzle in a dense era can
  // take most of a second to find, and a frozen button reads as a broken one.
  await new Promise(r => requestAnimationFrame(() => setTimeout(r, 0)));

  const puzzle = game.engine.makePuzzle({ minYear, ...LEVELS[level] });
  btn.disabled = false;
  btn.textContent = 'Deal a puzzle';
  if (!puzzle) {
    $('practice-msg').textContent =
      'Could not find one with those settings — try a wider era or an easier level.';
    return;
  }
  $('practice').hidden = true;
  startPuzzle(null, 'practice', puzzle);
}

async function boot() {
  const engine = await new Engine().load();
  game.engine = engine;

  if (!startPuzzle(puzzleDate())) return;

  $('guess').addEventListener('input', updateMatches);
  $('guess').addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      game.cursor = Math.min(game.cursor + 1, game.matches.length - 1);
      renderSuggestions();
    } else if (ev.key === 'ArrowUp') {
      ev.preventDefault();
      game.cursor = Math.max(game.cursor - 1, 0);
      renderSuggestions();
    } else if (ev.key === 'Enter') {
      ev.preventDefault();
      if (game.cursor >= 0) submit(game.matches[game.cursor][0]);
    } else if (ev.key === 'Escape') {
      clearInput();
    }
  });
  $('assist-btn').addEventListener('click', takeAssist);
  $('reveal-btn').addEventListener('click', () => {
    if (confirm('Reveal the rest of the chain? This ends today\'s puzzle.')) reveal();
  });
  $('results-close').addEventListener('click', () => { $('results').hidden = true; });
  $('help-btn').addEventListener('click', () => { $('howto').hidden = false; });
  $('howto-close').addEventListener('click', () => {
    $('howto').hidden = true;
    game.state.seenHowTo = true;
    saveState(game.state);
  });
  $('show-results').addEventListener('click', () => { if (game.solved) showResults(); });
  $('new-btn').addEventListener('click', openPractice);
  $('practice-close').addEventListener('click', () => { $('practice').hidden = true; });
  $('deal-btn').addEventListener('click', dealPractice);
  $('era-select').addEventListener('change', updateEraNote);
  $('back-daily').addEventListener('click', () => {
    $('results').hidden = true;
    startPuzzle(puzzleDate());
  });
  $('scope-clear').addEventListener('click', () => { clearScope(); $('guess').focus(); });

  if (!game.state.seenHowTo) $('howto').hidden = false;
}

boot().catch((err) => {
  console.error(err);
  document.body.appendChild(el('p', 'empty', 'Failed to load: ' + err.message));
});
