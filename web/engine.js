// Teammate graph engine. Runs entirely client-side: the browser holds the whole
// roster index, so it can validate guesses, prove reachability and derive the
// worked solution without a server, and without ever shipping an answer.
//
// The graph is stored as a bipartite incidence list (player <-> team-season)
// rather than a materialized edge list. The edge list would be ~1.5M pairs;
// the incidence list is 118k entries, and every query we need is a short walk
// over it.

const DATA = 'data/';

export class Engine {
  async load() {
    const [players, teams, puzzles, buf] = await Promise.all([
      fetch(DATA + 'players.json').then(r => r.json()),
      fetch(DATA + 'teams.json').then(r => r.json()),
      fetch(DATA + 'puzzles.json').then(r => r.json()),
      fetch(DATA + 'roster.bin').then(r => r.arrayBuffer()),
    ]);

    this.names = players.names.split('\n');
    this.y0 = players.y0;
    this.dy = players.dy;
    this.fame = players.fame;
    this.primary = players.team;
    this.pos = players.pos;
    this.posPool = players.posPool;
    this.teamPool = teams.pool;
    this.tsName = teams.name;
    this.tsYear = teams.year;
    this.puzzles = puzzles;

    // roster.bin v2: "SDBB", u32 version, u32 nPlayers, u32 nStints,
    //   (nPlayers+1) u32 offsets, nStints u16 team-season ids,
    //   nStints u8 window start, nStints u8 window end
    const magic = new TextDecoder().decode(new Uint8Array(buf, 0, 4));
    if (magic !== 'SDBB') throw new Error('bad roster.bin');
    const head = new Uint32Array(buf, 4, 3);
    const [version, nPlayers, nStints] = head;
    if (version !== 2) throw new Error('roster.bin version ' + version);
    this.n = nPlayers;
    let off = 16;
    this.offsets = new Uint32Array(buf, off, nPlayers + 1);
    off += (nPlayers + 1) * 4;
    this.stints = new Uint16Array(buf, off, nStints);
    off += nStints * 2;
    this.spanLo = new Uint8Array(buf, off, nStints);
    off += nStints;
    this.spanHi = new Uint8Array(buf, off, nStints);

    // Sharing a club and a season is not enough: a player traded away in May
    // was never a teammate of the man who replaced him in July. These are
    // *appearance* windows though, so they under-state roster tenure -- hence
    // the pad at each end and the exemption for windows too short to mean
    // anything. 0/255 marks a stint with no window recorded (pre-1980 and the
    // current season) and overlaps everything.
    this.spanPad2 = (players.spanPad ?? 10) * 2;
    this.spanMin = players.spanMin ?? 21;

    this.buildReverseIndex();
    this.buildSearchIndex();
    return this;
  }

  // team-season -> players, by counting sort over the incidence list
  buildReverseIndex() {
    const nTs = this.tsYear.length;
    const counts = new Uint32Array(nTs + 1);
    for (let k = 0; k < this.stints.length; k++) counts[this.stints[k] + 1]++;
    for (let t = 0; t < nTs; t++) counts[t + 1] += counts[t];
    this.tsOffsets = counts;
    const fill = counts.slice();
    const players = new Uint32Array(this.stints.length);
    const lo = new Uint8Array(this.stints.length);
    const hi = new Uint8Array(this.stints.length);
    for (let p = 0; p < this.n; p++) {
      for (let k = this.offsets[p]; k < this.offsets[p + 1]; k++) {
        const at = fill[this.stints[k]]++;
        players[at] = p;
        lo[at] = this.spanLo[k];
        hi[at] = this.spanHi[k];
      }
    }
    this.tsPlayers = players;
    this.tsLo = lo;
    this.tsHi = hi;
    // scratch buffers reused across queries so play stays allocation-free
    this._dist = new Int32Array(this.n);
    this._queue = new Int32Array(this.n);
    this._stamp = new Int32Array(this.n);
    this._epoch = 0;
  }

  /** Did two stints on the same club overlap in time? */
  overlaps(loA, hiA, loB, hiB) {
    if (hiA - loA < this.spanMin || hiB - loB < this.spanMin) return true;
    return loA - hiB <= this.spanPad2 && loB - hiA <= this.spanPad2;
  }

  // ---- naming ----------------------------------------------------------
  lastYear(i) { return this.y0[i] + this.dy[i]; }

  // "Ken Griffey (1989-2010, Seattle Mariners)" when a name is ambiguous
  describe(i) {
    return `${this.names[i]} (${this.y0[i]}-${this.lastYear(i)}, ` +
           `${this.teamPool[this.primary[i]]})`;
  }

  buildSearchIndex() {
    // Fold diacritics and punctuation so "Jose" finds "José" and "JD" finds
    // "J. D. Martinez".
    this.norm = new Array(this.n);
    this.compact = new Array(this.n);
    const dup = new Map();
    for (let i = 0; i < this.n; i++) {
      const folded = this.names[i]
        .normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
      this.norm[i] = folded.replace(/[^a-z0-9 ]/g, '');
      this.compact[i] = folded.replace(/[^a-z0-9]/g, '');
      dup.set(this.names[i], (dup.get(this.names[i]) || 0) + 1);
    }
    this.ambiguous = new Set([...dup].filter(([, k]) => k > 1).map(([n]) => n));
  }

  search(query, limit = 8, exclude = new Set()) {
    const q = query.normalize('NFD').replace(/[̀-ͯ]/g, '')
                   .toLowerCase().replace(/[^a-z0-9 ]/g, '').trim();
    if (!q) return [];
    const qc = q.replace(/ /g, '');
    const out = [];
    for (let i = 0; i < this.n; i++) {
      if (exclude.has(i)) continue;
      const nm = this.norm[i];
      let score;
      if (nm === q) score = 0;
      else if (nm.startsWith(q)) score = 1;
      else if (this.compact[i].startsWith(qc)) score = 2;
      else {
        // surname match, then anywhere
        const sp = nm.lastIndexOf(' ');
        if (sp >= 0 && nm.startsWith(q, sp + 1)) score = 3;
        else if (nm.includes(q)) score = 4;
        else continue;
      }
      out.push([score, -this.fame[i], i]);
    }
    out.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    return out.slice(0, limit).map(r => r[2]);
  }

  // ---- teammate queries -------------------------------------------------
  // Both stint lists are sorted, so this is a merge, not a scan.
  sharedSeasons(a, b) {
    const out = [];
    let i = this.offsets[a], j = this.offsets[b];
    const ea = this.offsets[a + 1], eb = this.offsets[b + 1];
    while (i < ea && j < eb) {
      const x = this.stints[i], y = this.stints[j];
      if (x === y) {
        if (this.overlaps(this.spanLo[i], this.spanHi[i],
                          this.spanLo[j], this.spanHi[j])) out.push(x);
        i++; j++;
      } else if (x < y) i++;
      else j++;
    }
    return out;
  }

  areTeammates(a, b) { return this.sharedSeasons(a, b).length > 0; }

  /**
   * Where two players overlapped, as [{team, span, seasons}].
   * Structured rather than pre-joined so the UI can style the club and the
   * years differently -- this label is the payoff for a correct guess.
   */
  linkParts(a, b) {
    const shared = this.sharedSeasons(a, b);
    if (!shared.length) return null;
    const byTeam = new Map();
    for (const ts of shared) {
      const name = this.teamPool[this.tsName[ts]];
      if (!byTeam.has(name)) byTeam.set(name, []);
      byTeam.get(name).push(this.tsYear[ts]);
    }
    const parts = [];
    for (const [team, years] of byTeam) {
      years.sort((x, y) => x - y);
      // Years for one club can be non-contiguous (a player leaves and returns),
      // so render runs rather than a single span: "1998–2001, 2004".
      const runs = [];
      let lo = years[0], prev = years[0];
      for (let i = 1; i <= years.length; i++) {
        const y = years[i];
        if (y === prev || y === prev + 1) { prev = y; continue; }
        runs.push(lo === prev ? `${lo}` : `${lo}–${prev}`);
        lo = prev = y;
      }
      parts.push({ team, span: runs.join(', '), seasons: years.length });
    }
    return parts;
  }

  // "New York Yankees, 1923–1934 / Milwaukee Braves, 1954–1965"
  linkLabel(a, b) {
    const parts = this.linkParts(a, b);
    if (!parts) return null;
    return parts.map(p => `${p.team}, ${p.span}`).join(' / ');
  }

  // ---- reachability -----------------------------------------------------
  /**
   * BFS from `source` over players, skipping any player in `blocked`.
   * Returns hop distance to every player (-1 = unreachable).
   * Run from the target, so a single pass answers both "is this a dead end?"
   * and "which neighbours sit one hop closer?".
   */
  distancesFrom(source, blocked = new Set()) {
    const dist = this._dist, queue = this._queue, stamp = this._stamp;
    const epoch = ++this._epoch;
    stamp[source] = epoch;
    dist[source] = 0;
    let head = 0, tail = 0;
    queue[tail++] = source;
    while (head < tail) {
      const p = queue[head++];
      const d = dist[p] + 1;
      for (let k = this.offsets[p]; k < this.offsets[p + 1]; k++) {
        const ts = this.stints[k];
        const lo = this.spanLo[k], hi = this.spanHi[k];
        for (let m = this.tsOffsets[ts]; m < this.tsOffsets[ts + 1]; m++) {
          const q = this.tsPlayers[m];
          if (stamp[q] === epoch || blocked.has(q)) continue;
          if (!this.overlaps(lo, hi, this.tsLo[m], this.tsHi[m])) continue;
          stamp[q] = epoch;
          dist[q] = d;
          queue[tail++] = q;
        }
      }
    }
    return { dist, stamp, epoch };
  }

  /**
   * Validate a guess against the four acceptance rules (spec 3).
   * `chain` is the current chain of player indices, start first.
   */
  validate(guess, chain, target) {
    if (chain.includes(guess)) {
      return { ok: false, reason: 'duplicate' };
    }
    if (guess === target) {
      return { ok: false, reason: 'is-target' };
    }
    const tip = chain[chain.length - 1];
    if (!this.areTeammates(tip, guess)) {
      return { ok: false, reason: 'not-teammate', tip };
    }
    if (this.areTeammates(guess, target)) {
      return { ok: true, solves: true };
    }
    // Rule 4: adding this must still leave a route to the target. Block the
    // players already used, then ask whether the guess can still see the
    // target -- one BFS answers it directly.
    const { dist, stamp, epoch } = this.distancesFrom(target, new Set(chain));
    if (stamp[guess] !== epoch || dist[guess] < 0) {
      return { ok: false, reason: 'dead-end', tip };
    }
    return { ok: true, solves: false };
  }

  /**
   * Next link on a shortest remaining path from the chain tip to the target.
   * Among equally-short options, prefer the most recognizable player -- the
   * reveal should read as a name you know, not a technicality.
   *
   * Used by "give up" and by the worked solution on the results sheet. The
   * assist never calls this: it must not know which player is the intended one.
   */
  nextStep(chain, target) {
    const tip = chain[chain.length - 1];
    const blocked = new Set(chain.slice(0, -1));
    const { dist, stamp, epoch } = this.distancesFrom(target, blocked);
    if (stamp[tip] !== epoch) return null;      // should not happen
    const want = dist[tip] - 1;
    let best = -1, bestFame = -Infinity;
    for (let k = this.offsets[tip]; k < this.offsets[tip + 1]; k++) {
      const ts = this.stints[k];
      const lo = this.spanLo[k], hi = this.spanHi[k];
      for (let m = this.tsOffsets[ts]; m < this.tsOffsets[ts + 1]; m++) {
        const q = this.tsPlayers[m];
        if (q === target || chain.includes(q)) continue;
        if (!this.overlaps(lo, hi, this.tsLo[m], this.tsHi[m])) continue;
        if (stamp[q] !== epoch || dist[q] !== want) continue;
        if (this.fame[q] > bestFame) { bestFame = this.fame[q]; best = q; }
      }
    }
    return best >= 0 ? best : null;
  }

  normalizeQuery(query) {
    return query.normalize('NFD').replace(/[̀-ͯ]/g, '')
                .toLowerCase().replace(/[^a-z0-9 ]/g, '').trim();
  }

  /** Filter an existing list of players by a typed query. */
  filterNames(list, query) {
    const q = this.normalizeQuery(query);
    if (!q) return list;
    const qc = q.replace(/ /g, '');
    return list.filter(i => this.norm[i].includes(q) || this.compact[i].includes(qc));
  }

  /**
   * The assist: everyone who played with the current tip and is not already in
   * the chain, best-known first.
   *
   * It deliberately does not say which of them reaches the target -- that is
   * the puzzle. It only removes the part that is not fun, being unable to name
   * a single teammate.
   */
  assistScope(chain, target) {
    const tip = chain[chain.length - 1];
    const { dist, stamp, epoch } =
      this.distancesFrom(target, new Set(chain.slice(0, -1)));
    const seen = new Set();
    const players = [];
    for (let k = this.offsets[tip]; k < this.offsets[tip + 1]; k++) {
      const ts = this.stints[k];
      const lo = this.spanLo[k], hi = this.spanHi[k];
      for (let m = this.tsOffsets[ts]; m < this.tsOffsets[ts + 1]; m++) {
        const q = this.tsPlayers[m];
        if (q === tip || q === target || seen.has(q) || chain.includes(q)) continue;
        if (!this.overlaps(lo, hi, this.tsLo[m], this.tsHi[m])) continue;
        seen.add(q);
        // A legal move must still leave a route to the target. In this graph
        // that has never once excluded anybody, but the guarantee costs
        // nothing and survives a change of data.
        if (stamp[q] !== epoch || dist[q] < 0) continue;
        players.push(q);
      }
    }
    players.sort((a, b) => this.fame[b] - this.fame[a]);
    return { label: `Played with ${this.names[tip]}`, players };
  }

  /**
   * One shortest chain from `s` to `t`, as player indices including both ends.
   *
   * Among equally short options it prefers recognizable players, and it
   * deprioritizes anyone in `avoid` -- so showing a player "a shortest
   * solution" after they already found one offers a route they did not take.
   */
  shortestChain(s, t, avoid = new Set()) {
    const { dist, stamp, epoch } = this.distancesFrom(t);
    if (stamp[s] !== epoch) return null;
    const path = [s];
    let cur = s;
    while (dist[cur] > 1) {
      const want = dist[cur] - 1;
      let best = -1, bestScore = -Infinity;
      for (let k = this.offsets[cur]; k < this.offsets[cur + 1]; k++) {
        const ts = this.stints[k];
        const lo = this.spanLo[k], hi = this.spanHi[k];
        for (let m = this.tsOffsets[ts]; m < this.tsOffsets[ts + 1]; m++) {
          const q = this.tsPlayers[m];
          if (q === t || path.includes(q)) continue;
          if (!this.overlaps(lo, hi, this.tsLo[m], this.tsHi[m])) continue;
          if (stamp[q] !== epoch || dist[q] !== want) continue;
          const score = this.fame[q] - (avoid.has(q) ? 1e6 : 0);
          if (score > bestScore) { bestScore = score; best = q; }
        }
      }
      if (best < 0) return null;
      path.push(best);
      cur = best;
    }
    path.push(t);
    return path;
  }

  /**
   * How many shortest chains still run from the chain's tip to the target,
   * and how many links that takes. Recomputed after every move, so it reads as
   * a live "how much room do I have left" gauge.
   *
   * Counted during one BFS out from the target: a node discovered at level d
   * accumulates the counts of every neighbour already sitting at level d-1.
   * Two players can share several clubs, so a neighbour is only counted once
   * per expansion -- otherwise a pair who were teammates for six seasons would
   * contribute six routes instead of one.
   */
  routesFrom(chain, target) {
    const tip = chain[chain.length - 1];
    if (tip === target) return { dist: 0, routes: 0 };
    const blocked = new Set(chain.slice(0, -1));
    const dist = this._dist, queue = this._queue, stamp = this._stamp;
    if (!this._count || this._count.length !== this.n) {
      this._count = new Float64Array(this.n);
      this._seenBy = new Int32Array(this.n);
      this._seenEpoch = new Int32Array(this.n);
    }
    const count = this._count, seenBy = this._seenBy, seenEpoch = this._seenEpoch;
    const epoch = ++this._epoch;

    stamp[target] = epoch;
    dist[target] = 0;
    count[target] = 1;
    let head = 0, tail = 0;
    queue[tail++] = target;
    let tipLevel = -1;

    while (head < tail) {
      const u = queue[head++];
      const d = dist[u];
      // once the tip's level is complete there is nothing further to count
      if (tipLevel >= 0 && d >= tipLevel) break;
      for (let k = this.offsets[u]; k < this.offsets[u + 1]; k++) {
        const ts = this.stints[k];
        const ulo = this.spanLo[k], uhi = this.spanHi[k];
        for (let m = this.tsOffsets[ts]; m < this.tsOffsets[ts + 1]; m++) {
          const v = this.tsPlayers[m];
          if (v === u || blocked.has(v)) continue;
          if (!this.overlaps(ulo, uhi, this.tsLo[m], this.tsHi[m])) continue;
          // The mark has to be epoch-scoped as well as per-expansion: a bare
          // seenBy would carry over from the previous call and silently drop
          // contributions on the next one.
          if (seenEpoch[v] === epoch && seenBy[v] === u) continue;
          seenEpoch[v] = epoch;
          seenBy[v] = u;
          if (stamp[v] !== epoch) {
            stamp[v] = epoch;
            dist[v] = d + 1;
            count[v] = count[u];
            queue[tail++] = v;
            if (v === tip) tipLevel = d + 1;
          } else if (dist[v] === d + 1) {
            count[v] += count[u];
          }
        }
      }
    }
    if (stamp[tip] !== epoch) return { dist: -1, routes: 0 };
    return { dist: dist[tip], routes: count[tip] };
  }

  /** Shortest distance between the puzzle endpoints, for display/stats. */
  distance(a, b) {
    const { dist, stamp, epoch } = this.distancesFrom(a);
    return stamp[b] === epoch ? dist[b] : -1;
  }

  // ---- daily puzzle -----------------------------------------------------
  puzzleFor(dateKey) {
    const p = this.puzzles[dateKey];
    return p ? { ...p, date: dateKey } : null;
  }
}
