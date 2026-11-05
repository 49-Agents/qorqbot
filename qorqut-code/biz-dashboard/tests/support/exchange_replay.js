// Replays recorded host exchanges for the browser checks. An answer exists only when the request equals a recorded one inside the
// currently selected stage (a named host state); an unrecorded request is a visible failure, never a made-up success. The
// idempotency_key is the one field not compared: the dashboard mints its own keys, the host's recorded key is fixed.
const fs = require('node:fs'), path = require('node:path');

const canon = value => Array.isArray(value) ? value.map(canon)
  : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canon(value[key])])) : value;
const withoutKey = body => { if (!body || typeof body !== 'object') return body; const {idempotency_key, ...rest} = body; return rest; };
const keyOf = (method, pathname, query, body) => JSON.stringify([method, pathname, canon(Object.fromEntries(Object.entries(query || {}))), canon(withoutKey(body) ?? null)]);

class Replay {
  constructor(file) {
    this.document = JSON.parse(fs.readFileSync(file, 'utf8'));
    this.stage = null;
    this.byStage = new Map();
    this.served = new Map();
    for (const exchange of this.document.exchanges) {
      const request = exchange.request, key = keyOf(request.method, request.path, request.query, request.body);
      if (!this.byStage.has(exchange.stage)) this.byStage.set(exchange.stage, new Map());
      const stage = this.byStage.get(exchange.stage);
      if (!stage.has(key)) stage.set(key, []);
      stage.get(key).push(exchange);
    }
  }
  get stages() { return [...this.byStage.keys()]; }
  use(stage) { assertKnown(this.byStage, stage); this.stage = stage; this.served.clear(); }
  // The recorded exchange, or null. Several recordings of one request in a stage (a replay of the same command) are served in order.
  find(method, pathname, query, body, stage = this.stage) {
    const matches = this.byStage.get(stage)?.get(keyOf(method, pathname, query, body));
    if (!matches) return null;
    const tag = stage + keyOf(method, pathname, query, body), used = this.served.get(tag) || 0;
    this.served.set(tag, used + 1);
    return matches[Math.min(used, matches.length - 1)];
  }
  // Recorded exchanges of a stage whose request path ends with a suffix (to build the page URL or arguments a test needs).
  all(stage, predicate) { return this.document.exchanges.filter(exchange => exchange.stage === stage && predicate(exchange.request, exchange.response)); }
  first(stage, predicate) { const found = this.all(stage, predicate)[0]; if (!found) throw new Error('No recorded exchange in stage ' + stage); return found; }
  document_(stage, label, kind = 'context_upload') {
    const listed = this.all(stage, (request, response) => request.path.endsWith('/context_list') && request.body.kind === kind).flatMap(e => e.response.body.data.items);
    const found = listed.find(item => item.title === label);
    if (!found) throw new Error(`No ${label} in the ${stage} list`);
    return found;
  }
}
const assertKnown = (map, stage) => { if (!map.has(stage)) throw new Error('Unknown stage ' + stage + '; recorded: ' + [...map.keys()].join(', ')); };

// One HTTP handler for both browser checks. `control` carries the per-check hooks: hold (a gate on one operation), mutation (an
// injected transport failure or malformed answer - never host evidence), tamper (a deliberately malformed copy of a recorded
// response), served (called after a recorded answer, e.g. to move the host to its next recorded state).
async function handle(req, res, url, {replay, control, calls}) {
  const match = /^\/api(\/root|\/biz\/[^/]+)(\/operations\/([a-z_]+)|\/context|\/notes|\/context-library\/[^/]+\/(retract))$/.exec(url.pathname);
  if (!match) return false;
  const json = (data, status = 200) => { res.writeHead(status, {'Content-Type': 'application/json'}); res.end(JSON.stringify(data)); };
  let raw = ''; for await (const chunk of req) raw += chunk;
  const body = raw ? JSON.parse(raw) : null, op = match[3] || (match[2] === '/context' ? 'processes' : match[2] === '/notes' ? 'notes' : 'library_' + match[4]);
  const query = Object.fromEntries(url.searchParams);
  const call = {path: url.pathname + url.search, pathname: url.pathname, op, method: req.method, body: body || {}, query};
  calls.push(call);
  if (control.hold?.[op]) { const gate = control.hold[op]; if (gate.when(call.body)) { delete control.hold[op]; gate.arrived(); await gate.promise; } }
  if (control.mutation?.[op]) {
    const answer = await control.mutation[op](call.body, call);
    if (answer === 'drop') { res.writeHead(502, {'Content-Type': 'text/html'}); return res.end('<html>Bad gateway</html>') || true; }
    if (answer) { call.injected = true; return json(answer, answer.status === 'ok' ? 200 : 409), true; }
  }
  const exchange = replay.find(req.method, url.pathname, query, body);
  if (!exchange) {
    call.unrecorded = true;
    return json({status: 'error', error: {code: 'unrecorded_request', message: `No recorded exchange in stage ${replay.stage} for ${req.method} ${url.pathname}${url.search} ${JSON.stringify(body)}`}}, 400), true;
  }
  let answer = exchange.response.body;
  if (control.tamper) { const changed = control.tamper(op, call.body, structuredClone(answer), call); if (changed) { call.injected = true; answer = changed; } }
  call.stage = replay.stage;
  json(answer, exchange.response.status);
  control.served?.(call, exchange);
  return true;
}

const fixtureFile = name => path.join(__dirname, '..', 'fixtures', 'context-source', name);
module.exports = {handle, Replay, canon, keyOf, fixtureFile};
