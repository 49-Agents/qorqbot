// Environment defaults belong to Global; a business saves only its explicit overrides.
let envView = null;
const ENV_KEY_JS = /^[A-Za-z_][A-Za-z0-9_]*$/;
const envScope = () => state.scope === "global" ? "global" : state.biz;
const envCurrent = p => envView === p && p.pane.isConnected && envScope() === p.scope;

async function renderSettings(c) {
  const scope = envScope(), global = state.scope === "global";
  const pane = document.createElement("div");
  pane.className = "pane settingspane";
  const p = envView = { pane, scope, global, url: global ? "/root/env" : "/biz/" + encodeURIComponent(state.biz) + "/env",
    rows: [], inherited: [], loaded: false, saving: false, dirty: false };
  pane.innerHTML = `
    ${global ? `<section class="onboarding-settings"><div><h2>find your way around</h2><p>A quick, interactive introduction to your workspace. Replay all of it, or one chapter.</p><div class="onboarding-settings-chapters" aria-label="replay one chapter">${onboarding.chapters().map(c => `<button type="button" class="quiet" data-onboarding-chapter="${esc(c.id)}">${esc(c.title)}</button>`).join('')}</div></div><button type="button" class="quiet" data-onboarding-replay>restart walkthrough</button></section>` : ''}
    <div class="set-head"><div>
      <h1 class="set-title">${global ? "Global environment" : "Environment"}</h1>
      <div class="muted small">${global ? "Shared defaults for all projects and Global agents. Projects can override individual values." : "Global values are inherited automatically. Override a value for this project, or use Global again."}</div>
    </div><button class="btn" id="envsave" disabled>Save</button></div>
    ${global ? '<p class="muted small">Agent Browser views come from the installation’s browser viewer bindings, not from these settings.</p>' : ''}
    <div class="envrows" id="envrows"></div>
    <button class="envadd" id="envadd" disabled>+ Add variable</button>
    <div class="set-note muted small" id="envnote" role="status"></div>
    <p class="muted small">Changes apply to new agent sessions and subsequent jobs. Already-running agent sessions keep their current environment.</p>`;
  c.appendChild(pane);
  pane.querySelector('[data-onboarding-replay]')?.addEventListener('click', () => {
    if (p.dirty || p.saving) { toast('Save your environment changes before starting the walkthrough.'); return; }
    onboarding.start();
  });
  pane.querySelectorAll('[data-onboarding-chapter]').forEach(button => button.addEventListener('click', () => {
    if (p.dirty || p.saving) { toast('Save your environment changes before starting the walkthrough.'); return; }
    onboarding.start(button.dataset.onboardingChapter);
  }));
  pane.querySelector("#envsave").onclick = () => saveEnv(p);
  pane.querySelector("#envadd").onclick = () => {
    p.rows.push({ key: "", value: "" }); envDirty(p); paintEnv(p);
    pane.querySelector(".envrow:last-child .ek")?.focus();
  };
  await loadEnv(p);
}

function envError(p, message, retry = false) {
  const note = p.pane.querySelector("#envnote");
  note.textContent = message;
  if (retry) {
    const button = document.createElement("button");
    button.className = "btn quiet"; button.id = "envretry"; button.textContent = "Retry";
    button.onclick = () => loadEnv(p); note.append(" ", button);
  }
}
async function loadEnv(p) {
  envError(p, "Loading environment…");
  try {
    const r = await api(p.url);
    if (!envCurrent(p)) return;
    if (r.error || !Array.isArray(r.pairs)) throw new Error(r.error || "Environment response is unavailable.");
    p.rows = r.pairs.map(row => ({ ...row }));
    p.inherited = (r.inherited || []).map(row => ({ ...row }));
    p.loaded = true; p.dirty = false;
    envError(p, ""); paintEnv(p);
  } catch (error) {
    if (envCurrent(p)) envError(p, error.message || "Could not load environment.", true);
  }
}
function envDirty(p) {
  p.dirty = true;
  p.pane.querySelector("#envsave").disabled = p.saving || !p.loaded;
  envError(p, "");
}
function paintEnv(p) {
  const inherited = new Map(p.inherited.map(row => [row.key, row.value]));
  const local = new Set(p.rows.map(row => row.key.trim()));
  const shown = [
    ...p.inherited.filter(row => !local.has(row.key)).map(row => ({ ...row, inherited: true })),
    ...p.rows.map((row, index) => ({ ...row, index })),
  ];
  p.pane.querySelector("#envrows").innerHTML = shown.map(row => {
    const override = !p.global && inherited.has(row.key.trim()) && !row.inherited;
    const source = row.inherited ? "Global" : override ? "Project override" : p.global ? "Global" : "Project";
    return `<div class="envrow" data-index="${row.index ?? ""}">
      <div class="envkey"><input class="ek mono" aria-label="Variable name" placeholder="KEY" value="${esc(row.key)}" ${row.inherited ? "disabled" : override ? "readonly" : ""} spellcheck="false"><span class="envsource">${source}</span></div>
      <input class="ev mono" aria-label="Value for ${esc(row.key || "new variable")}" placeholder="value" value="${esc(row.value)}" ${row.inherited ? "disabled" : ""} spellcheck="false">
      <button class="envdel" type="button" data-key="${esc(row.key)}" data-action="${row.inherited ? "override" : "remove"}" aria-label="${row.inherited ? "Override " : override ? "Use Global for " : "Remove "}${esc(row.key || "variable")}">${row.inherited ? "Override" : override ? "Use Global" : "×"}</button>
    </div>`;
  }).join("") || '<p class="muted small">No environment variables yet.</p>';
  p.pane.querySelectorAll(".envrow").forEach(row => {
    const button = row.querySelector("button");
    if (button.dataset.action === "override") {
      button.onclick = () => {
        p.rows.push({ key: button.dataset.key, value: inherited.get(button.dataset.key) });
        envDirty(p); paintEnv(p);
        p.pane.querySelector(".envrow:last-child .ev")?.focus();
      };
      return;
    }
    const index = Number(row.dataset.index), key = row.querySelector(".ek");
    key.oninput = () => { p.rows[index].key = key.value; key.classList.toggle("bad", !!key.value.trim() && !ENV_KEY_JS.test(key.value.trim())); envDirty(p); };
    row.querySelector(".ev").oninput = e => { p.rows[index].value = e.target.value; envDirty(p); };
    button.onclick = () => { p.rows.splice(index, 1); envDirty(p); paintEnv(p); };
  });
  p.pane.querySelector("#envsave").disabled = !p.loaded || p.saving || !p.dirty;
  p.pane.querySelector("#envadd").disabled = !p.loaded || p.saving;
  if (p.saving) p.pane.querySelectorAll("#envrows input, #envrows button").forEach(el => el.disabled = true);
}

async function saveEnv(p = envView) {
  if (!p || !envCurrent(p) || !p.loaded || p.saving) return;
  const pairs = p.rows.map(row => ({ key: row.key.trim(), value: row.value })).filter(row => row.key);
  if (pairs.some(row => !ENV_KEY_JS.test(row.key))) return envError(p, "Use letters, digits and underscores for keys; do not start with a digit.");
  if (new Set(pairs.map(row => row.key)).size !== pairs.length) return envError(p, "Each variable name must be unique.");
  p.saving = true; paintEnv(p);
  try {
    const r = await post(p.url, { pairs });
    if (!envCurrent(p)) return;
    if (r.error || !Array.isArray(r.pairs)) throw new Error(r.error || "Could not save environment.");
    p.rows = r.pairs.map(row => ({ ...row })); p.inherited = (r.inherited || []).map(row => ({ ...row }));
    p.dirty = false; envError(p, ""); toast("Environment saved");
  } catch (error) {
    if (envCurrent(p)) envError(p, error.message || "Could not save environment.");
  } finally {
    p.saving = false;
    if (envCurrent(p)) paintEnv(p);
  }
}
