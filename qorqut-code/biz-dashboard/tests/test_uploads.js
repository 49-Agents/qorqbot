#!/usr/bin/env node
// Headless test for js/uploads.js pure helpers (the Uploads tab of the right panel): mode from the form state,
// topic slugs out of the context tree, topic slugify, media-vs-text exclusivity, batch ids, and the call/meeting
// payload, send label and participant typeahead rules.
//   node tests/test_uploads.js
const fs = require("fs"), path = require("path"), vm = require("vm");
const HERE = path.join(__dirname, "..");
let fails = 0, n = 0;
const ok = (cond, msg) => { n++; if (!cond) { fails++; console.log(`FAIL ${msg}`); } else console.log(`ok   ${msg}`); };
const eq = (a, b, msg) => ok(JSON.stringify(a) === JSON.stringify(b), `${msg} (${JSON.stringify(a)} == ${JSON.stringify(b)})`);

const mkEl = (tag = "div") => ({ tagName: tag.toUpperCase(), children: [], style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  hidden: false, textContent: "", innerHTML: "", disabled: false, value: "", querySelector() { return null; }, querySelectorAll() { return []; },
  appendChild(c) { this.children.push(c); return c; }, remove() {}, addEventListener() {}, setAttribute() {}, getAttribute() { return null; }, focus() {}, closest() { return null; } });
const g = globalThis, store = {};
g.window = g;
g.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
g.location = { search: "", hash: "" };
const byId = {};
g.document = { body: mkEl("body"), activeElement: null, querySelector: s => (s.startsWith("#") ? (byId[s.slice(1)] = byId[s.slice(1)] || mkEl()) : null), querySelectorAll: () => [], createElement: t => mkEl(t), addEventListener() {} };
g.addEventListener = () => {}; g.setInterval = () => 0; g.fetch = async () => ({ json: async () => ({}) });
for (const f of ["core", "uploads"]) vm.runInThisContext(fs.readFileSync(path.join(HERE, "js", f + ".js"), "utf8"), { filename: f + ".js" });

// mode: what the form holds decides which path a submit takes
eq(upMode({ text: "", files: [] }), "empty", "nothing pasted -> empty");
eq(upMode({ text: "  hi ", files: [] }), "text", "text only -> text");
eq(upMode({ text: "", files: [{ name: "a.png" }] }), "media", "files only -> media");
eq(upMode({ text: "   ", files: [] }), "empty", "whitespace is empty");

// exclusivity: the reason a paste is refused, or "" when it may go in
eq(upRefuse({ text: "notes", files: [] }, "media"), "Clear the text before attaching files.", "files refused while text present");
eq(upRefuse({ text: "", files: [{ name: "a.png" }] }, "text"), "Remove the attached files before adding text.", "text refused while files present");
eq(upRefuse({ text: "", files: [] }, "media"), "", "files fine on empty form");
eq(upRefuse({ text: "", files: [{ name: "a.png" }] }, "media"), "", "more files fine in media mode");
eq(upRefuse({ text: "x", files: [] }, "text"), "", "more text fine in text mode");

// a typed topic becomes the slug the server accepts (one segment; nested paths keep the last segment)
eq(upTopicSlug("Product Hunt launch"), "product-hunt-launch", "free text slugified");
eq(upTopicSlug("launch/producthunt"), "producthunt", "nested path -> leaf");
eq(upTopicSlug("  "), "", "blank stays blank");
eq(upTopicSlug("42 things"), "x42-things", "must start with a letter");

// media kind for the preview strip
eq(upKind({ type: "image/png", name: "a.png" }), "image", "image by mime");
eq(upKind({ type: "video/mp4", name: "a.mp4" }), "video", "video by mime");
eq(upKind({ type: "", name: "clip.MOV" }), "video", "video by extension when mime missing");
eq(upKind({ type: "application/pdf", name: "a.pdf" }), "file", "anything else is a file");

// hint + send label, the two strings patched in place while typing (never a rebuild: focus must survive)
eq(upSendLabel({ text: "x", files: [] }, false), "Upload", "send label text");
eq(upSendLabel({ text: "", files: [{}] }, false), "Upload", "send label media");
eq(upSendLabel({ text: "x", files: [] }, true), "Uploading…", "send label busy");

// call or meeting: the payload the text route takes. An ordinary upload sends exactly what it always did.
const plain = { text: "  Michael: hi  ", notes: "n", topic: "t", tasks: [{ id: "s1" }], origin: "", occurred_on: "2026-09-17", participants: [{ name: "Priya" }] };
eq(upPayload(plain), { text: "Michael: hi", notes: "n", topic: "t", task_ids: ["s1"] }, "ordinary upload sends no encounter fields, even with stale ones in the form");
eq(Object.keys(upPayload({ text: "x", notes: "", topic: "", origin: "", participants: [] })), ["text", "notes", "topic", "task_ids"], "ordinary payload keys are unchanged");
const mike = { person_id: "p1", name: "Mike" }, priya = { name: "Priya" };
eq(upPayload({ ...plain, origin: "call", participants: [mike, priya] }),
  { text: "Michael: hi", notes: "n", topic: "t", task_ids: ["s1"], origin: "call", participants: [mike, priya], occurred_on: "2026-09-17" }, "call sends origin, participants and date");
eq(Object.keys(upPayload({ ...plain, origin: "meeting", occurred_on: "", participants: [priya] })), ["text", "notes", "topic", "task_ids", "origin", "participants"], "no date, no occurred_on key");

// the send button waits for a participant, and its label says what for
eq(upSendLabel({ text: "x", files: [], origin: "call", participants: [] }, false), "Add who the call was with", "call without participants");
eq(upSendLabel({ text: "x", files: [], origin: "meeting" }, false), "Add who the meeting was with", "meeting without participants");
eq(upSendLabel({ text: "x", files: [], origin: "call", participants: [priya] }, false), "Upload", "call with a participant");
eq(upSendLabel({ text: "x", files: [], origin: "call", participants: [] }, true), "Uploading…", "busy wins");
eq(upNeedsWho({ text: "x", files: [], origin: "", participants: [] }), "", "context needs no participants");
eq(upNeedsWho({ text: "", files: [{}], origin: "call", participants: [] }), "", "media never waits for participants");

// calls and meetings are text: files are refused while one is chosen
eq(upRefuse({ text: "", files: [], origin: "call" }, "media"), "Calls and meetings are pasted as text. Choose Context to attach files.", "files refused for a call");
eq(upRefuse({ text: "x", files: [], origin: "meeting" }, "text"), "", "text fine for a meeting");

// typeahead rows: role and org after the name, chosen Persons excluded, Add "<typed>" unless it already is a name or alias
const found = [{ id: "p1", metadata: { name: "Michael Linford", role: "Founder", org: "Acme", aka: ["Mike"] } },
  { id: "p2", metadata: { name: "Michelle Ortiz" } }, { id: "p3", metadata: { name: "Mich Ortega", org: "Beta" } }];
eq(upWhoLabel(found[0]), "Michael Linford · Founder · Acme", "name, role and org");
eq(upWhoLabel(found[1]), "Michelle Ortiz", "name only");
eq(upWhoLabel(found[2]), "Mich Ortega · Beta", "name and org");
eq(upWhoItems("Mich", found, []).map(r => r.label), ["Michael Linford · Founder · Acme", "Michelle Ortiz", "Mich Ortega · Beta", 'Add "Mich"'], "Add row last when the text is not a name");
eq(upWhoItems("Mich", found, []).filter(r => r.person).map(r => r.value), ["p1", "p2", "p3"], "person rows carry the Person id");
eq(upWhoItems("Mich", found, []).slice(-1)[0], { value: "", label: 'Add "Mich"', add: "Mich" }, "Add row carries the typed text");
eq(upWhoItems("  Mich  ", found, [])[3].add, "Mich", "typed text is trimmed");
eq(upWhoItems("mike", found, []).map(r => r.label).slice(-1)[0], "Mich Ortega · Beta", "an exact alias, any case, needs no Add row");
eq(upWhoItems("MICHELLE ORTIZ", found, []).some(r => r.add), false, "an exact name, any case, needs no Add row");
eq(upWhoItems("Michael", found, []).some(r => r.add), true, "a prefix of a name is not an exact match");
eq(upWhoItems("Mich", found, [{ person_id: "p1", name: "Mike" }]).map(r => r.value), ["p2", "p3", ""], "chosen Persons are not offered again");
eq(upWhoItems("Priya", [], [{ name: "Priya" }]), [], "typing a chosen name again offers nothing, not even Add");
eq(upWhoItems("Priya", [], []), [{ value: "", label: 'Add "Priya"', add: "Priya" }], "no match: only the Add row");
eq(upWhoItems("   ", [], []), [], "blank text: no rows");

// adding a participant: Persons by id, typed names as they are; the same Person or name twice changes nothing
const none = [];
eq(upWhoAdd(none, { person_id: "p1", name: " Mike " }), [{ person_id: "p1", name: "Mike" }], "picked Person keeps id and chosen name");
eq(upWhoAdd(none, { name: "Priya" }), [{ name: "Priya" }], "typed name has no person_id");
eq(none, [], "the previous list is not mutated");
eq(upWhoAdd([mike], { person_id: "p1", name: "Michael" }), [mike], "same Person is not added twice");
eq(upWhoAdd([priya], { name: " PRIYA " }), [priya], "same name, any case, is not added twice");
eq(upWhoAdd([mike], { name: "Priya" }), [mike, priya], "typed name joins a picked Person");
eq(upWhoAdd([mike], { name: "   " }), [mike], "blank names are ignored");

// batch id: one per submit, sortable, short
const b = upBatchId();
ok(/^\d{4}-\d{2}-\d{2}-\d{4}-[0-9a-z]{4}$/.test(b), `batch id shape ${b}`);

console.log(`\n${n - fails}/${n} passed`);
process.exit(fails ? 1 : 0);
