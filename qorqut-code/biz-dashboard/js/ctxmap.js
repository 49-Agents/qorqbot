// ---------------------------------------------------------------- shared map primitives
// Icons, text wrapping, SVG element creation and Mermaid text escaping shared by the Context graphs (ctxgraph.js).

const MAP_WRAP = 32, MAP_MAXLINES = 3;
const MAP_ICONA = `width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"`;
const SVG_MAPFIT = `<svg ${MAP_ICONA}><path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/></svg>`;
const SVG_MAPCOPY = `<svg ${MAP_ICONA}><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>`;

// word-wrap to `cols` characters, at most `max` lines, the last one ellipsised
function mapWrap(text, cols = MAP_WRAP, max = MAP_MAXLINES) {
  const words = String(text || "").split(/\s+/).filter(Boolean), lines = [];
  let cur = "";
  for (const w of words) {
    if (cur && (cur + " " + w).length > cols) { lines.push(cur); cur = ""; }
    cur = cur ? cur + " " + w : w;
  }
  if (cur) lines.push(cur);
  if (lines.length > max) { lines.length = max; lines[max - 1] = lines[max - 1].slice(0, cols - 1).replace(/\s+\S*$/, "") + "…"; }
  return lines.length ? lines : ["-"];
}

const svgEl = (tag, attrs = {}) => { const e = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };

// Mermaid text escaping: brackets, braces and quotes cannot appear in node text
const mmText = s => { s = String(s || "").replace(/\s+/g, " ").replace(/[\[\]{}()"`]/g, "").replace(/::/g, ":").trim(); return s.length > 60 ? s.slice(0, 59).trimEnd() + "…" : (s || "-"); };
