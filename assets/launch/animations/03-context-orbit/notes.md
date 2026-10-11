# 03 · Context orbit

Authored product concept for the launch review gallery. It is not a capture of a live product flow. The workspace,
document cards, chat row and "Launch checklist" file card are illustrations. Nothing on screen shows a real import,
sync, model run or measured result.

A top-down diagram film with the approved black face as its fixed centre of gravity. The approved slide 04 opens the
film, its paths deliver three context cards, four agents orbit the shared workspace, and their work resolves into one
file card before everything contracts into the endcard.

Files: `index.html` (self-contained, 30 s, 1920×1080), `metadata.json`, `LICENSE-gsap.txt` (GSAP/CustomEase),
`OFL-ZenKakuGothicNew.txt` (embedded font).

## Scene timing

| Time | Beat |
| --- | --- |
| 0–5 | Slide 04 at full frame: centred "your context stays local", black face at right, Notion / Google Drive / Obsidian at left, no labels. The three paths draw one after another (0.6–1.8, 1.4–2.6, 2.2–3.4) with the slide's arrowhead clearance. The face glances along each path as it arrives (1.45–4.0), returns to centre and blinks (4.5). |
| 5–12 | One small ivory document card emerges from behind each icon (5.0–6.1) and follows its path into a neat stack beside the face (5.75–8.75). The face watches the stack. Each path fades as its card lands (7.7–9.2), then the icons fade (9.0–9.9). The camera pulls out slightly (9.0–11.4) and the headline settles smaller. A thin rounded workspace boundary draws around the face and stack (10.0–11.6) and fills with a lighter paper tone (11.0–11.9). |
| 12–19 | The copy changes to "your favourite models working together" (12.2–13.25). Mike, Adam, Turgut and Borte appear as satellites in an asymmetrical layout, with names (12.5–13.8). Four thin lines fan out from the stack (13.5–14.9). One modest pulse travels each line in turn: Mike 14.8–15.7, Turgut 15.3–16.2, Adam 15.8–16.9, Borte 16.35–17.45. Each arrival brings in that agent's provider mark (Codex, Codex, Claude, DeepSeek), a single soft breath and a glance back at the stack. The black face follows each pulse. |
| 19–24 | Labels, marks, copy and lines withdraw (19.0–19.7). The camera follows the satellites down (19.1–20.7) as they move into a compact dark chat row (19.35–20.9). A checklist document lifts from the stack and travels into the row (20.25–21.4). It opens into the file card "Launch checklist" / "Ready for your review." (21.3–22.3). The black face and the four agents look at it; it holds until 24.0. |
| 24–30 | The row, stack and workspace contract into the black face and fade (24.0–25.4). The camera pushes in to the endcard framing with the face at left (24.6–26.4). The endcard follows: "get stuff done" / "stay independent", "qorqbot" and a quiet "See on Github" pill (25.8–27.5). The face looks toward the type and blinks once (28.9). The complete endcard holds for 2.5 s (27.5–30.0). |

## Reused assets (exact paths)

- `assets/launch/product-hunt/04-local-context-v2.svg`: canvas, radial paper gradient, 52-unit grid pattern, the
  three arrow paths verbatim, the arrow-marker geometry (rebuilt as head paths so they can fade), icon placements,
  mascot placement and the headline's outlined Zen Kaku Gothic New glyphs verbatim. The whole film runs in slide 04
  world coordinates; the camera is one transform over that geometry, with no PNG pan.
- `assets/launch/product-hunt/sources/context-icons/notion.svg`, `google-drive.png`, `obsidian.svg`: embedded as data
  URLs. I checked that they are byte-identical to the copies embedded in the slide 04 SVG.
- `assets/launch/product-hunt/sources/context-icons/qorqbot-mark-dark.svg`: black face body, eyes and mustache paths,
  which are the same paths slide 04 uses.
- `assets/brand/qorqbot-logo.svg`: almond eyes and mustache geometry used on all four agent faces.
- `assets/launch/product-hunt/02-provider-choice.png`: the four face colours (sampled) and body silhouettes (traced into
  editable SVG paths). Also the source of the DeepSeek whale mark (cropped and keyed to a small transparent PNG data URL).
- `/home/alp/businesses/49IDE/code/landing/anim/runtime/gsap.min.js`, `CustomEase.min.js`: GSAP 3.13.0 and CustomEase,
  inlined unchanged with their license banners. `LICENSE-gsap.txt` is copied verbatim.
- `/home/alp/businesses/49IDE/code/landing/anim/index.html`: OpenAI (Codex) and Claude mark vector paths, filled in ink
  like slide 02.
- `/home/alp/businesses/49IDE/code/landing/anim/assets/japanese-minimalism/motion-curves.md` (via
  `styles/tokens.css`): curves `hero` 0.22,1,0.36,1, `release` 0.45,0,0.15,1 and `inhale` 0.55,0,0.35,1. The film also
  borrows the 49IDE runtime's seek discipline (chained `fromTo` channels and an end/start prime pass).
- `/home/alp/businesses/49IDE/code/landing/anim/assets/fonts/ZenKakuGothicNew-{Light,Regular,Medium}.woff2`: embedded
  as data URLs. `OFL-ZenKakuGothicNew.txt` is copied verbatim.
- `/home/alp/qorqbot/worktrees/landing-concepts/site/dist/mascot.js`: gaze model (unit direction × 4.5 / 3.5 units on
  a gaze group) and eye-closing blink. Here they are timeline-driven and deterministic rather than random timers.
- `/home/alp/qorqbot/worktrees/landing-concepts/site/dist/index.html`, `styles.css`: endcard composition, with the
  mascot at left, the right-aligned two-line headline, "qorqbot" and the dark "See on Github" pill.

## New additions

- Three ivory document cards with a folded corner, each tinted by its source (ink, Drive blue, Obsidian violet), and
  the stack they form.
- A thin rounded workspace boundary (stroke drawn, then filled) that later tightens into a ring and fades.
- Four fan-out lines (thin, arrowheads using the slide 04 marker geometry), one traveling pulse per line, and small
  provider marks beside the names.
- Editable agent faces (traced bodies plus brand features) with timeline-driven gaze and breath.
- The compact dark chat row, the checklist document and the "Launch checklist" file card.
- Endcard set in live Zen Kaku Gothic New. The slide 04 headline remains outlined glyphs; "your favourite models
  working together" is live text matched to the same size and tracking.
- Slim preview controls below the artwork (play/pause, restart, seek), the `window.__anim` API, query parameters and
  postMessage handling, as specified for the gallery.

## How it differs from the other concepts

- 01 The ensemble is character-led: faces spread from behind the mascot, become slide 02's agent list and lead into the
  full chat and browser invitation. Concept 03 never shows a conversation, browser or "Take control". The agents only
  orbit a workspace and form a compact row.
- 02 Terminal to chat transforms one conversation between terminal and chat. Concept 03 has no terminal, streaming or
  subscription copy.
- 04 The invitation is a product close-up of the chat and browser invitation. Concept 03 is a top-down diagram with a
  mostly stationary camera.
- 05 Independent by design is typography-led, with very large type and masks, and uses slide 04 only briefly. Concept 03
  is built on the slide 04 geometry from start to finish. Its type stays at slide scale, and the story is carried by
  paths, pulses and the stack.

## Validation actually performed

- No native browser tool was available to this worker, and no browser, CDP or Playwright session was launched.
  Real-browser playback, frame pacing, font loading through `document.fonts`, CSS scaling of the stage and controls, the
  `prefers-reduced-motion` media query and messages from the actual gallery iframe are unverified. Manager's gallery
  check covers them.
- `node --check` on the authored script extracted from the built `index.html`; `metadata.json` parsed with Node. Scene
  windows run 0–30 and match `duration` and the timeline length.
- Self-containment scan: no `<link>`, `@import`, `fetch`, XHR or `http(s)` `src`/`href`. The only URLs are XML namespaces
  and the GSAP license banners. Fonts, icons and the DeepSeek mark are data URLs.
- Logic checks: the built file's three scripts ran in Node `vm` against a throwaway minimal DOM stub. The stub is not
  part of the deliverable. Results:
  - `ready`, `duration` 30, empty `errors` and the four API methods are present.
  - The ready postMessage is `{type:"qorqbot-animation-ready",id:"03-context-orbit",duration:30}`.
  - `?paused=1` starts at 0. `?t=18.5&controls=0` starts paused at 18.5 with controls hidden. Reduced motion starts
    paused at 4.8. Default autoplays.
  - `seek` clamps above, below and NaN.
  - 136 seek times rendered identical SVG attribute snapshots in forward, shuffled and reverse order.
  - `seek` pauses. postMessage play, pause, restart and seek work. Play at the end restarts from 0.
  - Key-scene state assertions pass at 0, 4.8, 9, 12, 18.5, 23 and 27.6. Between 27.6 and 30 only the scheduled
    blink changes.
- Visual check without a browser: the stub's SVG state was rasterized at 24 timestamps between 0 and 29.9 s with resvg-js, a static SVG
  rasterizer from the local artwork tools. The embedded Zen fonts were used, decoded to TTF in /tmp. Those frames led
  to three fixes:
  - each path now fades as its card lands, so no arrowhead peeks from behind the stack;
  - the camera follows the row down, so the 19–24 climax sits centred;
  - contracting layers fade while they shrink, so they no longer linger behind the face.

## Limitations

- Authored concept only. The film implies no provider readiness, security property, data-egress property, sync,
  benchmark, price or automatic CLI launch. Notion and Google Drive appear as approved slide 04 context sources. The
  film does not claim how their content is obtained.
- The agent bodies are traced from a raster slide, and the DeepSeek mark is a small raster crop from that slide. Both
  are close to, but not pixel-identical with, the approved artwork. The Codex and Claude marks are vector paths from the
  49IDE film, shown in ink as on slide 02.
- "See on Github" is drawn artwork, not a link, and the controls bar is a preview aid only.
- The right-hand name labels position their marks from a runtime text measurement. If measurement throws, an entry is
  added to `window.__anim.errors`. If it throws or returns 0, a width estimate is used.
