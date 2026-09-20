// Local persistence, streaks, and the results-API adapter.
//
// Gameplay needs no backend. Only the cross-player assist distribution does, so
// that lives behind one interface. Until a real endpoint exists the results
// card simply omits those sections -- it never invents numbers. Point API_BASE
// at a backend and they appear; nothing else changes.
//
//   POST {API_BASE}/result  {date, assists, links, gaveUp}
//        -> {date, histogram: {"0": n, ...},      // assists used
//            lengths: {"2": n, "3": n, "5+": n},  // chain length in links
//            total, percentile}
//   GET  {API_BASE}/stats?date=YYYY-MM-DD
//        -> {date, histogram, lengths, total}

const API_BASE = null;          // set to e.g. '/api' to use a real backend
const STORE = 'sdob.v2';   // v1 stored `hints`; v2 stores `assists`
const MAX_BUCKET = 6;           // "6+" is the last assist bucket
const LEN_BUCKETS = 5;          // chain-length buckets, from the shortest up

// Daily rollover: a single fixed global reset so "today's puzzle", the shared
// board and the distribution are unambiguous everywhere (spec 12).
const RESET_UTC_HOUR = 8;       // 08:00 UTC == 04:00 ET / 01:00 PT

export function puzzleDate(now = new Date()) {
  const shifted = new Date(now.getTime() - RESET_UTC_HOUR * 3600 * 1000);
  return shifted.toISOString().slice(0, 10);
}

export function loadState() {
  try {
    return JSON.parse(localStorage.getItem(STORE)) || { days: {}, seenHowTo: false };
  } catch {
    return { days: {}, seenHowTo: false };
  }
}

export function saveState(state) {
  try { localStorage.setItem(STORE, JSON.stringify(state)); } catch { /* private mode */ }
}

export function dayResult(state, date) { return state.days[date] || null; }

export function recordResult(state, date, result) {
  state.days[date] = result;
  saveState(state);
  return state;
}

/** Consecutive solved days ending today (or yesterday, if today is unplayed). */
export function streak(state, today) {
  // A malformed date used to throw out of here and take the whole results
  // sheet with it; there is no streak worth that.
  if (!today || Number.isNaN(Date.parse(today + 'T12:00:00Z'))) return 0;
  const day = (iso, delta) => {
    const d = new Date(iso + 'T12:00:00Z');
    d.setUTCDate(d.getUTCDate() + delta);
    return d.toISOString().slice(0, 10);
  };
  let cursor = today;
  if (!state.days[cursor]?.solved) {
    cursor = day(today, -1);
    if (!state.days[cursor]?.solved) return 0;
  }
  let n = 0;
  while (state.days[cursor]?.solved) { n++; cursor = day(cursor, -1); }
  return n;
}

// ---- distribution -------------------------------------------------------

/** Bucket key for a chain of `links`, given the shortest possible is `min`. */
export function lengthKey(links, min) {
  const cap = min + LEN_BUCKETS - 1;
  return links >= cap ? `${cap}+` : `${links}`;
}

/** Percentile of players who needed strictly more assists than `assists`. */
export function percentileFor(histogram, assists) {
  let worse = 0, total = 0;
  for (const [k, n] of Object.entries(histogram)) {
    total += n;
    if (Number(k) > assists) worse += n;
  }
  return total ? Math.round(100 * worse / total) : 0;
}

/**
 * Submit a result and get the day's distributions back.
 *
 * Returns null when there is no backend, or when the call fails. There is
 * deliberately no synthetic fallback: inventing a distribution would put a
 * number on screen that looks like other people and isn't, which is worse than
 * showing nothing. The results card simply omits those sections until a real
 * endpoint exists.
 */
export async function submitResult(date, assists, links, gaveUp) {
  if (!API_BASE) return null;
  try {
    const res = await fetch(`${API_BASE}/result`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ date, assists, links, gaveUp }),
    });
    if (!res.ok) throw new Error('http ' + res.status);
    return await res.json();
  } catch {
    return null;          // never block the results screen on a backend problem
  }
}

export { MAX_BUCKET, LEN_BUCKETS };
