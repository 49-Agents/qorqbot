// ---------------------------------------------------------------- context
async function renderContext(c) {
  const files = await api(`/biz/${state.biz}/context`);
  const names = Object.keys(files || {});
  c.innerHTML = `<div class="split"><div class="listcol" id="ctxlist"></div><div class="pane" id="ctxview"></div></div>`;
  if (!names.length) { $("#ctxview").innerHTML = `<div class="empty">No context files.</div>`; return; }
  if (!names.includes(state.ctx)) state.ctx = names[0];
  const paint = () => {
    const l = $("#ctxlist");
    l.innerHTML = names.map(n => `<div class="item ${n === state.ctx ? "on" : ""}" data-n="${esc(n)}"><div class="nm"><span>${esc(n)}</span></div></div>`).join("");
    l.querySelectorAll(".item").forEach(d => d.onclick = () => { state.ctx = d.dataset.n; paint(); });
    $("#ctxview").innerHTML = `<h2>${esc(state.ctx)}</h2><pre>${esc(files[state.ctx] || "")}</pre>`;
  };
  paint();
}
