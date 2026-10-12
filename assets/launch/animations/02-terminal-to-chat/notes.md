# 02 — Terminal to chat

Launch animation candidate, 30 s, 1920 × 1080, `index.html` (self-contained, no network).

**This is an authored product concept.** The terminal, chat, view switch, agent rail and
streaming behaviour are illustrations built for this film. They are not captures of the
running product and are not evidence of a live product flow.

## Scene timing

Everything runs on a single paused GSAP timeline that lasts exactly 30.0 s.

| Time | Beat |
| --- | --- |
| 0.0–5.0 | The camera is pushed in 1.12× on a lone narrow teal Claude Code terminal, showing the glyph, "Claude Code", "Sonnet 5.5 · Claude Max" and a clean prompt area. The cursor blinks from 0–0.8. "Can you help me plan the launch?" is typed from 0.80–2.44, Enter lands at 2.85, and the reply bullet appears at 3.02. "Yes. I’ll draft the launch checklist." streams word by word from 3.10–3.75. An empty prompt with a blinking cursor follows at 4.05. |
| 5.0–12.0 | The terminal slides left while the camera eases out (5.0–6.3), and the dark chat window enters on the right (5.7–6.5). A thin curved connector draws between them (6.2–7.0). The prompt line lifts out of the terminal as a capsule, rides the curve and becomes chat bubble 1 (7.0–8.3). The reply line does the same and lands as bubble 2 with Adam’s face (7.85–9.15). "tmux streaming" appears once on the connector at 9.0. Both windows grow together to make room (9.25–9.8). "Start with the Product Hunt page." is typed from 9.40–10.50 and Enter lands at 10.62. One pulse runs along the connector and the owner bubble appears at 10.95. "I’ll prepare the headline, gallery / and first comment." then streams word by word in the terminal and the chat at the same moments (11.10–11.84), with a dashed flow on the connector. A new prompt appears at 12.0. |
| 12.0–18.0 | "use your subscription, not API pricing" enters at 12.1 and the approved smaller sentence "we stream your cli processes directly into the chat interface" at 12.55. A small view switch sits between the title bars: one Adam face plus terminal and chat icons (13.5). It focuses the terminal at 13.95 (the chat is veiled), then the chat at 15.35, then both at 16.75. The two windows always show the same conversation. |
| 18.0–24.0 | The copy, the view switch and the connector label leave at 18.0. The camera pulls back to 0.8× (18.1–19.5) to reveal a compact Mike / Turgut / Adam / Borte rail under the pair (18.75), with Adam selected. "any cli, any provider" enters at 19.2. Thin wires draw from the faces (19.5–20.6) to Codex, Codex, Claude and DeepSeek (19.7–20.5). The pair stays the dominant object, and the frame holds until 23.8. |
| 24.0–30.0 | The rail, marks and connector fade (23.8) as the camera returns to 1.0× (24.0–25.0). The windows slide together until their edges meet at the centre seam, and the inner corners square off (24.3–25.1). The terminal folds onto the chat (25.1–25.7). The chat glides to the centre and becomes one clean ink-outline conversation (25.5–26.3). The outline gathers into a circle (26.75–27.45) that fills black and hands over to the approved black mascot (27.45–27.57). Its eyes open at 27.6, the whiskers appear at 27.62, and it settles by 27.97. "get stuff done" enters at 27.0, "stay independent" at 27.15, "qorqbot" at 27.35 and the quiet "See on Github" at 27.45. Nothing moves from 28.0 to 30.0, a 2.0 s hold on the endcard. |

Reduced motion: the film opens paused at 9.24 s, which shows both views with the first
exchange and the "tmux streaming" label. Play still works.

## Reused source assets (exact paths)

All artwork is rebuilt as editable DOM/SVG. No launch PNG is embedded or panned over.

- `assets/launch/product-hunt/03-tmux-streaming-v4.png`: the whole pair composition. This covers window proportions and corner radii, the title bars "Adam — Terminal" / "Adam — Chat", traffic lights, Claude Code header lines, chevron and bullet rhythm, the exact second-exchange copy, bubble layout and colours, Adam’s face beside replies, the composer ("+", "Message Adam…", send), the headline and navy sub sentence, the "tmux streaming" label and the quiet grid.
- `assets/launch/product-hunt/sources/claude-code-terminal.png` (owner capture): terminal background, text and dim colours, the orange reply bullet, and the Claude Code glyph, rebuilt cell for cell as SVG rects (110 × 75 grid). The auto-update and permission chrome is dropped, following the approved v4 simplification.
- `assets/launch/product-hunt/02-provider-choice.png`: the four faces (green round Mike, blue bean Turgut, magenta teardrop Adam, ochre rounded triangle Borte), traced to vector with almond eyes and mustache kept as separate paths. Also reused: the names, rail and panel colours, the two Codex / one Claude / one DeepSeek mapping, and the DeepSeek whale (traced; no vector source exists in the repository).
- `assets/launch/product-hunt/sources/context-icons/qorqbot-mark-dark.svg`: endcard mascot body, eyes and mustache geometry and colours, verbatim. The geometry is identical to `assets/brand/qorqbot-logo.svg`.
- `/home/alp/qorqbot/worktrees/landing-concepts/site/dist/index.html`: endcard composition (large mascot left, right-aligned two-line headline, CTA), the eye/whisker grouping, the mascot viewBox `20 25 125 125` and the repository URL. `…/dist/mascot.css`: the mascot drop shadows. `…/dist/assets/preview/agency-20261004/portfolio/booking.css`: the dark gradient pill used for "See on Github". Headline set as on the landing: Zen Light, −0.045em tracking, 1.06 line height.
- `/home/alp/businesses/49IDE/code/landing/anim/runtime/gsap.min.js` and `CustomEase.min.js` (GSAP 3.13.0): inlined verbatim with their @license banners. `…/runtime/LICENSE-gsap.txt` is copied to `licenses/`.
- `/home/alp/businesses/49IDE/code/landing/anim/assets/fonts/ZenKakuGothicNew-{Light,Regular,Medium}.woff2` and `GeistMono.woff2` (variable, wght 100–900): inlined as data URLs. Their OFL texts are copied to `licenses/`.
- `/home/alp/businesses/49IDE/code/landing/anim/index.html`: the Claude mark (`#spot-t1`) and OpenAI mark used for Codex (`#spot-s15`), SVG paths verbatim.
- `/home/alp/businesses/49IDE/code/landing/anim/STYLE.md`: the motion vocabulary curves hero `0.22,1,0.36,1`, inhale `0.55,0,0.35,1` and release `0.45,0,0.15,1`. No 49IDE claims, phone finale or endcard are used.

## New additions

- **Line flight:** each terminal line lifts out as a teal capsule over its own text. It follows the connector curve, spaced evenly by arc length, turns bubble grey, and morphs from Geist Mono into the Zen chat bubble at the exact landing position measured at runtime.
- **Connector:** a thin cubic curve with an arrowhead, one Enter pulse and a short dashed flow during the synchronized reply.
- **View switch:** a pill with one Adam face, a terminal (`>_`) icon and a chat icon. Focus changes use ivory veils over the unfocused window.
- **Compact agent rail and wires:** new layout for this film. Its faces and names come from slide 02.
- **Fold:** the terminal folds onto the chat with a fold shade, and the chat switches to an outline skin (a CSS restyle of the same chat markup). The outline gathers into the mascot circle.
- **Small UI glyphs:** prompt chevron, composer plus and send icon, and toggle icons as simple strokes.

Copy follows the brief exactly. Three intentional differences from slide 03 v4:
- The first reply is the brief’s "Yes. I’ll draft the launch checklist.", not the slide’s longer line.
- The header uses "·" as written in the brief.
- Window titles keep the slide wording but are set in Zen Kaku Gothic New, because the brief reserves Geist Mono for terminal code.

## How it differs from the other concepts

- **01 The ensemble** is character-led around the mascot, with a full provider diagram and a browser invitation. Here the mascot appears only in the last 2.5 s, providers are a brief compact rail, and there is no browser.
- **03 Context orbit** is a diagram of context icons and documents. Here there are no context sources at all.
- **04 The invitation** starts inside the chat, centres on the browser, and uses a quick terminal toggle. Here the thesis is shown by identical text physically travelling from terminal to chat, with synchronized streaming in both views.
- **05 Independent by design** is a large-type, mask-driven editorial film. Here the film is UI-led, with one short statement at a time.

## Validation actually performed

No browser control was available in this session: there is no native browser tool, and custom browser, CDP or Playwright tooling is not permitted. **Playback was therefore not visually checked.** The bounded checks were:

- `node --check` on all four inline scripts (vendor GSAP, CustomEase, shape data, authored film).
- css-tree grammar validation of the authored stylesheet: 466 declarations, 0 invalid, all 15 custom properties defined.
- Glyph coverage of every character in the film against the embedded Zen Light, Regular, Medium and Geist Mono cmaps: none missing.
- No network references: no `src=`, `<link>`, `@import`, non-data `url()`, fetch or XHR. The only URL strings are the CTA href, the GSAP banner/license URLs and SVG/XHTML namespace strings.
- License files are byte-identical to their sources, and `metadata.json` parses.
- A temporary jsdom 30.1 smoke test (not committed) covered:
  - Interface: the ready flag, duration 30, API shape, and `errors` empty apart from jsdom lacking FontFaceSet.
  - Start modes: `?paused=1`, `?t=12.5`, `?controls=0`, default autoplay, the reduced-motion start at 9.24 s (Play still works), and an API call made before ready being applied.
  - Player: seek clamping and pausing, play-at-end restarting, restart, pause, and postMessage play/pause/restart/seek, including ignoring foreign messages and sources.
  - Story beats at 15 sample times, and an unchanged endcard state from 28 to 30 s.
  - Determinism: identical state forward vs. backward at 34 times, plus a 160-jump random seek walk compared against forward-seek references.

  jsdom performs no layout. Bubble widths, rail centring and flight end points are measured in the browser at runtime and were checked only by hand calculation from the font metrics.

## Limitations

- Not viewed in a real browser. Composition, overlaps and type rendering still need the Manager’s gallery check.
- The faces and DeepSeek whale are vector traces of the approved raster, so small smoothing differences from slide 02 are possible. The Claude glyph is a cell-grid rebuild from the capture.
- Codex and Claude use the original marks from the 49IDE source rather than slide 02’s generated look. All marks are reused artwork, none invented.
- If the embedded fonts fail to load within 4 s, the film still builds with fallbacks and records the problem in `window.__anim.errors`.
- "See on Github" is a real link to https://github.com/49-Agents/qorqbot. It is hidden and not clickable until the endcard shows it.
