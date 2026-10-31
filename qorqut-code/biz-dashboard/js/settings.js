// ---------------------------------------------------------------- settings tab
// Per-business environment: the gitignored `.env` in the biz repo root, edited as key-value rows.
// Every agent session and CLI the dashboard spawns for this business gets these merged over the
// machine's environment (biz values win). Saved as a whole file: comments in a hand-edited .env
// are dropped on the first save from here.

let _envRows = [];   // [{key, value}] as edited, before save

async function renderSettings(c) {
  const pane = document.createElement("div");
  pane.className = "pane settingspane";
  pane.innerHTML = `
    <div class="set-head">
      <div>
        <div class="set-title">Environment</div>
        <div class="muted small">Key-value pairs in this business's <span class="mono">.env</span> (gitignored).
        Agents and CLIs run for this business see them; they override the machine's own variables.</div>
      </div>
      <button class="btn" id="envsave" disabled>Save</button>
    </div>
    <div class="envrows" id="envrows"></div>
    <button class="envadd" id="envadd">+ Add variable</button>
    <div class="set-note muted small" id="envnote"></div>`;
  c.appendChild(pane);
  const r = await api(`/biz/${state.biz}/env`);
  _envRows = (r && Array.isArray(r.pairs)) ? r.pairs.map(p => ({ ...p })) : [];
  if (!_envRows.length) _envRows.push({ key: "", value: "" });
  paintEnv();
  $("#envadd").onclick = () => { _envRows.push({ key: "", value: "" }); paintEnv(); envDirty(); focusEnvRow(_envRows.length - 1); };
  $("#envsave").onclick = saveEnv;
}

const ENV_KEY_JS = /^[A-Za-z_][A-Za-z0-9_]*$/;

function paintEnv() {
  const box = $("#envrows");
  box.innerHTML = _envRows.map((p, i) => `
    <div class="envrow" data-i="${i}">
      <input class="ek mono" type="text" placeholder="KEY" value="${esc(p.key)}" spellcheck="false">
      <input class="ev mono" type="text" placeholder="value" value="${esc(p.value)}" spellcheck="false">
      <button class="envdel" title="Remove">&times;</button>
    </div>`).join("");
  box.querySelectorAll(".envrow").forEach(row => {
    const i = +row.dataset.i;
    row.querySelector(".ek").oninput = e => { _envRows[i].key = e.target.value; envDirty(); markKey(e.target); };
    row.querySelector(".ev").oninput = e => { _envRows[i].value = e.target.value; envDirty(); };
    row.querySelector(".envdel").onclick = () => { _envRows.splice(i, 1); paintEnv(); envDirty(); };
    markKey(row.querySelector(".ek"));
  });
}

function markKey(inp) { inp.classList.toggle("bad", !!inp.value.trim() && !ENV_KEY_JS.test(inp.value.trim())); }
function focusEnvRow(i) { const row = $(`#envrows .envrow[data-i="${i}"] .ek`); if (row) row.focus(); }
function envDirty() { const b = $("#envsave"); if (b) b.disabled = false; const n = $("#envnote"); if (n) n.textContent = ""; }

async function saveEnv() {
  const pairs = _envRows.map(p => ({ key: p.key.trim(), value: p.value })).filter(p => p.key);
  const bad = pairs.find(p => !ENV_KEY_JS.test(p.key));
  if (bad) { $("#envnote").textContent = `Bad key: ${bad.key} — letters, digits and _ only, not starting with a digit.`; return; }
  const b = $("#envsave");
  b.disabled = true;
  const r = await post(`/biz/${state.biz}/env`, { pairs });
  if (r && r.error) { $("#envnote").textContent = r.error; b.disabled = false; return; }
  _envRows = (r && Array.isArray(r.pairs) && r.pairs.length) ? r.pairs.map(p => ({ ...p })) : [{ key: "", value: "" }];
  paintEnv();
  toast("Environment saved");
}
