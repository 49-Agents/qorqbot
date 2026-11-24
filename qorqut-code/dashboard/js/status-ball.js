// The status ball of a "-" line, shared by the Journal editor (js/notes.js) and the Focus notes (js/focus-text.js).
// Only the rules and the markup live here; each editor keeps and saves its own states. Colours: css/status-ball.css.
const NT_STATUS_ORDER = ["todo", "white", "in_progress", "done"];   // the left-click cycle
const NT_STATUS_LABEL = { todo: "Open", white: "White", in_progress: "In progress", done: "Done", blocked: "Blocked", cancelled: "Cancelled" };
const ntNextStatus = cur => { const i = NT_STATUS_ORDER.indexOf(cur); return i === -1 ? "todo" : NT_STATUS_ORDER[(i + 1) % NT_STATUS_ORDER.length]; };
const ntToggleBlockedStatus = cur => cur === "blocked" ? "todo" : "blocked";   // the right-click toggle
const ntStatusClosed = status => status === "done" || status === "cancelled";  // a closed line is struck through
const ntIsTaskLine = raw => /^[ \t]*-/.test(raw || "");
const ntBallAttr = value => String(value).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const ntBallLabel = status => `Status: ${NT_STATUS_LABEL[status] || status}. Click to change.`;
const ntBallHtml = (status, i, attrs = "") => `<button type="button" class="ntball ntb-${ntBallAttr(status)}" data-i="${i}" aria-label="${ntBallAttr(ntBallLabel(status))}"${attrs}><span class="ntdot"></span></button>`;
