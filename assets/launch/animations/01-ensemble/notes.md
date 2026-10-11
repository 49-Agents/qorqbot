# 01 — The ensemble

A 30.0 s, 1920×1080 character-led launch film. The approved black mascot wakes,
four colored faces slide out from behind it, and those same four faces carry the
story: provider list, chat navigation, browser invitation, then back behind the
mascot for the endcard.

Everything shown is an **authored product concept** for review, not a capture of
a live product flow. The chat, the browser invitation card, the pointer and the
landing page inside the card are illustrations built from the approved launch
slides. Providers are shown only as choices. The film makes no claims about
readiness, pricing, security or automation.

## Files

| file | purpose |
| --- | --- |
| `index.html` | the film; self-contained (≈203 KiB): inline CSS/SVG/JS, embedded fonts, inlined GSAP, DeepSeek mark as a data URL |
| `metadata.json` | gallery metadata |
| `build.mjs` | refreshes the three generated blocks in `index.html` (`build:fonts`, `build:vendor`, `build:deepseek`); idempotent; `QB_49IDE_ANIM` overrides the 49IDE folder |
| `sources/deepseek-mark-from-02.png` | DeepSeek mark cut from approved slide 02 (see below) |
| `LICENSE-gsap.txt` | GSAP licence notice, verbatim from the 49IDE runtime; it covers the inlined GSAP 3.13.0 and CustomEase 3.13.0 |
| `OFL-ZenKakuGothicNew.txt` | SIL OFL 1.1 notice for the embedded Zen Kaku Gothic New fonts |

Authored markup, CSS and the film script are edited directly in `index.html`. Run
`node build.mjs` only when a generated source changes.

## Scene timing

| time (s) | beat |
| --- | --- |
| 0–4 | **Wake.** The mascot is centred on ivory. It breathes once (0.1–1.1), blinks once (0.62) and glances left, then right. "qorqbot" rises quietly beneath it (1.1). From 1.75 the four faces slide out from behind it along one wide arc. Each face sits on a pivot far below the stage, so the arc is real geometry, with no bounce or pop. The faces glance at the mascot (3.15). |
| 4–10 | **The ensemble.** The mascot closes its eyes. Its body becomes the slide 02 agent panel (4.25–5.45) while the same four faces glide into the list rows. Names and messages fade in (5.15), Mike's row is highlighted (5.4) and the headline rises (5.3). Thin curves draw from the rows: Mike and Turgut to Codex, Adam to Claude, Borte to DeepSeek (6.2–7.8). Each face looks toward its provider. Marks and labels appear (7.15–7.9), then "any cli, any provider" (7.95). Hold until 9.95. |
| 10–17 | **Into the chat.** The diagram clears. The panel grows into the app window (10.25–11.45) while the rows and faces slide left and scale into the navigation as one rigid group. Window chrome appears (10.9) and Adam is selected (11.4). Bubbles enter one at a time: the owner's "Help me prepare the launch." (12.0), Adam's "I’ll review the page and draft the checklist." (13.1) and Mike's "I’ve gathered the context." (14.4). Tiny faces sit beside the two replies, and each nav preview updates with its agent's latest line. |
| 17–24 | **The invitation.** Adam: "Want to take a look together?" (17.0). The conversation scrolls and the browser card grows beneath it (17.55–18.85). Inside the card, the approved landing page builds: mascot, two-line headline, "provider-agnostic qorqbot" and "See on Github" (18.55–19.4). The left-aligned "Take control" pill sits below the frame (19.15). A neutral white pointer arrives (19.75), hovers (20.9) and presses (21.3), then drifts into the page (21.85). The landing mascot looks at it and blinks (22.9). Hold until 24. |
| 24–30 | **Endcard.** The UI fades (24.0) and the window contracts into the black mascot at the landing-page position (24.25–25.45). The faces leave the navigation, pause on a wider ring, then settle behind the mascot (25.6–26.6). The mascot opens its eyes (25.62) and looks at the headline. "get stuff done" (25.85), "stay independent" (25.99), "qorqbot" (26.55) and "See on Github" (26.75) appear. The endcard is complete at 27.25. One blink at 28.4, then it holds to 30.0, a 2.75 s fully readable hold. |

The film plays once and holds the endcard. `prefers-reduced-motion` starts it paused
at 3.6 s, showing the mascot, the wordmark and the four faces on their arc.

## Reused assets (exact paths)

Qorqbot repository (this worktree):

- `assets/brand/qorqbot-logo.svg` — mascot body, almond eyes and mustache paths,
  used verbatim (`BODY`, `EYE_NEAR`, `EYE_FAR`, `WHISKERS` in the script). The
  same paths appear in `assets/launch/product-hunt/sources/context-icons/qorqbot-mark-dark.svg`,
  whose black/white treatment the hero uses.
- `assets/launch/product-hunt/02-provider-choice.png` — the whole 4–10 s
  composition: headline placement, the right-hand list (names, preview lines,
  Mike's highlight), curve routing, mark and label positions. It also supplies
  the four face colors and shapes and the DeepSeek mark crop.
- `assets/launch/product-hunt/01-cover.png` — the dark chat layout (light
  titlebar with traffic lights, dark navigation, selected row, bubbles with tiny
  faces, pill composer), the browser invitation card with `localhost:57365`, the
  landing page inside it, and the "Take control" pill below the frame.
- `assets/launch/product-hunt/04-local-context-v2.svg` — the quiet grid (52 px at
  1672 → 60 px here, `#d6d8db` at .55), the paper gradient, and the thin-line
  stroke color and arrowhead marker geometry for the provider curves.
- `ui/src/view-icons.tsx` — the composer's send-arrow path.

Read-only sources outside this repository:

- `/home/alp/qorqbot/worktrees/landing-concepts/site/dist/index.html`, `styles.css`,
  `mascot.js`, `mascot.css` — the mascot's group structure (`.q-gaze`, `.q-eye`,
  `.q-whiskers`) and its blink and gaze behavior; the landing composition the
  endcard follows (mascot left, right-aligned two-line headline, CTA beneath); the
  dark gradient "See on Github" button.
- `/home/alp/businesses/49IDE/code/landing/anim/runtime/gsap.min.js`,
  `runtime/CustomEase.min.js` and `runtime/LICENSE-gsap.txt` — GSAP 3.13.0 and
  CustomEase, inlined with their banners and notice (source commit
  `6a46094815e6bc05660db4924a1cd378dba83427`; these files are clean there).
- `/home/alp/businesses/49IDE/code/landing/anim/assets/fonts/ZenKakuGothicNew-{Light,Regular,Medium}.woff2`
  and `OFL-ZenKakuGothicNew.txt` — embedded as data URLs. Geist Mono is not used
  because the film has no terminal code.
- `/home/alp/businesses/49IDE/code/landing/anim/index.html` — the Codex mark
  (`#spot-s15`) and the Claude mark (`#spot-t1`), copied as exact vector paths.
- `/home/alp/businesses/49IDE/code/landing/anim/STYLE.md` and `frames/01-ma.js` —
  the motion vocabulary: hero settle `0.22, 1, 0.36, 1`, breath inhale
  `0.55, 0, 0.35, 1` and release `0.45, 0, 0.15, 1`.

No 49IDE product claims, phone finale or brand endcard are used.

## New additions

- **Agent face bodies**: editable SVG reconstructions of the four approved faces
  from slide 02: green round Mike, blue bean Turgut, magenta teardrop Adam and
  ochre rounded-triangle Borte. Each wears the logo's exact eyes and mustache,
  re-centred and scaled ×0.84.
- **DeepSeek mark**: `sources/deepseek-mark-from-02.png` is a 143×110 px crop at
  (745, 571) of slide 02. The paper is chroma-keyed to transparency and edge
  colors are un-mixed against the sampled paper. No vector source exists in the
  approved assets, so no logo was redrawn.
- **The morphing slab**: one dark rounded rectangle carries the mascot's body
  through panel → app window → mascot. It makes the transformations continuous
  without swapping whole scenes.
- **Simple line icons**: browser back/forward/reload, lock, open-external, the
  composer plus, and a neutral white pointer arrow (no red dot).
- **Film plumbing**: the playback API, slim controls below the artwork, query and
  `postMessage` handling, and `build.mjs`.

## How it differs from the other concepts

- **02 Terminal to chat** follows a terminal conversation transforming into chat.
  01 has no terminal and no matching-text transfer.
- **03 Context orbit** is a stationary diagram of context flowing to a central
  face. 01 shows no context icons or documents, and its camera follows characters
  rather than paths.
- **04 The invitation** starts close inside the chat and makes the browser
  invitation its main event. In 01 the invitation is one beat in a character
  journey.
- **05 Independent by design** is led by huge typography and masks. 01 keeps type
  quiet and lets the five characters carry the story.

01's continuity device is that the same four face elements are never replaced.
They move from the arc to the list, the navigation, the chat and back behind the
mascot, while the mascot's own body becomes the panel and the window.

## Validation actually performed

No native browser tool was available in this session, and the brief forbids
custom browser, CDP or Playwright tooling, so **the film has not been watched in a
real browser**. These bounded checks were run instead:

- **JavaScript syntax**: the authored film script was extracted and checked with
  `node --check`.
- **API and controls in jsdom 30.1.0** (a DOM emulator, not a browser):
  - `ready` is true, `duration` and the timeline duration are 30, and `errors` is
    empty with no console warnings.
  - `seek` pauses and clamps: −5 → 0, 99 → 30, and a non-number → 0.
  - `play` at the end restarts from 0; `restart` plays.
  - `?paused=1` holds at 0; `?t=17.5` holds at 17.5.
  - `?controls=0` hides the bar; by default the bar sits below the scaled 16:9
    artwork, never over it.
  - `postMessage` commands from the parent drive the same API, and the
    ready message is posted to the parent.
  - Reduced motion starts paused at 3.6.
- **Approximate frame previews**: the jsdom DOM was walked after `seek(t)` and
  rebuilt as SVG, then rasterized with resvg using TTF conversions of the same
  fonts. Roughly 40 frames across all five scenes were inspected.
  - These checked layout, overlaps, text placement and handoffs.
  - Fixes from this pass: explicit `px` radii for the slab, an inline resting
    shape for the slab, a cleaner mascot-to-slab handoff, and the pointer resting
    off the landing headline.
  - Blur, filters, shadows, masks and font rendering were approximated or left
    out.
- **Self-containment**: the only `http(s)` strings are XML namespace
  identifiers and GSAP's licence banner. There are no `src` or `href` values
  except data URLs, and the only `url()` is the internal `url(#ah)` marker. No
  fetch, XHR, dynamic `import`, iframe or link elements. All three generated
  blocks are filled, and every visible glyph, including ’ and …, is present in
  each embedded font weight (checked with fontTools).
- **Metadata**: `metadata.json` parses, has the required keys, and its scenes
  tile 0–30 contiguously.

## Limitations

- Playback smoothness, real font rendering, CSS filters and shadows, and the
  timing feel are unverified in a real browser.
- The DeepSeek mark is a raster crop from the 1672 px slide, shown at 1.15×, so
  it is slightly softer than the vector Codex and Claude marks.
- The faces are faithful reconstructions, not traced vector masters, because no
  separate face SVGs exist among the approved assets.
- The mascot body path is hand-drawn and very slightly non-circular. At the two
  instants where the circular slab takes over from the mascot, or hands back to
  it, the edge may shift by a few pixels for under 0.1 s.
- The app window simplifies slide 01. The titlebar text, view-toggle icons and
  "New agent" row are omitted to keep each beat to one statement.
