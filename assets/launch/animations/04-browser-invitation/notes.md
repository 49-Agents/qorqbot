# 04 — The invitation

Launch animation candidate 04 from `../PLAN.md`: 30 s, 1920×1080, one
self-contained `index.html` (inline JS/CSS/SVG, fonts as data URLs, GSAP
inlined; no network access). It plays once and holds the endcard.

**Authored product concept.** The chat shell, browser card, landing preview,
terminal and provider links are illustrations rebuilt from the approved launch
artwork. They are not a recording of a live Qorqbot session and are not
evidence of a working product flow.

## Scenes

| Time | Beat |
|---|---|
| 0–6 | Close up on Adam's dark chat, with no nav. The camera eases in slightly. "Can you help me prepare the launch?" is typed into the composer and sent (1.9). Adam's magenta face and the reply "I’ve reviewed the page. Let’s take a look together." follow (2.85–3.45), and Adam blinks (4.4). |
| 6–15 | A browser frame grows inside the conversation (5.9–6.9). The approved landing page builds in: mascot on the left, "get stuff done / stay independent" on the right. The mascot glances at the headline, blinks, then looks down as "Adam invited you to browse together." and the left-aligned "Take control" pill appear below the frame (8.45–9.2). A neutral white pointer presses the pill (10.45). The browser comes toward the viewer at 1.52× (10.75–11.75) while the dimmed chat stays visible on all four sides. The mascot's eyes follow the pointer across the page, and the CTA lifts on hover (13.0). The browser then settles back into the chat (13.65–14.6). |
| 15–21 | The camera pulls back to the full desktop window on the ivory grid (15.0–16.7). The nav fills with Mike, Turgut, Adam (selected) and Borte, plus the head icons. "any cli, any provider" appears top left (16.7). Thin lines then draw from the agents to Codex, Claude and DeepSeek, which sit outside the window's left edge (17.05–18.5): two links go to Codex, one to Claude and one to DeepSeek. The lines and copy leave by 20.8. The invitation stays in the conversation throughout. |
| 21–25 | The window recentres beneath "use your subscription, not API pricing", which appears once, outside the app (20.95). The pointer clicks the terminal icon (22.02). The same pane becomes a slide-03-style teal Claude Code terminal showing the same exchange. Clicking the chat icon (24.25) switches it back. |
| 25–30 | The landing page lifts out of the browser card at its exact position and grows to full frame (25.05–26.4) while the app recedes and fades out. "provider-agnostic " fades, leaving "qorqbot". The endcard has the approved black mascot, "get stuff done / stay independent", qorqbot and a quiet "See on Github". The mascot glances at the headline and blinks (26.6–27.95). The card is readable from 26.4 and fully static from 27.95 to 30.0 (2.05 s). |

The reduced-motion start frame is 9.2 s: the landing in the chat, with the invitation line and pill.

## Reused assets (exact paths)

In this repository:

- `assets/launch/product-hunt/01-cover.png`, the source for most of the chat and browser frame:
  - dark chat composition: bubble, avatar and composer spacing;
  - the browser frame embedded in the conversation;
  - the invitation line with the white "Take control" pill below the frame;
  - sampled palette: chat #0e0e0e, bubble #252626, composer #1f1f1e, send #c9caca, chrome #1a1a19, nav #151414, selected row #242323.

  The cover's own wording, "Research invited you to browse together." and "Message Research…", is reused with the agent name changed to Adam.
- `assets/launch/product-hunt/02-provider-choice.png` provides:
  - the names Mike, Turgut, Adam and Borte;
  - their four face shapes and colours (#00cb8b, #157cfe, #fc1c8c, #c79264), with almond eyes and mustaches, vector-traced from this image;
  - the DeepSeek mark, traced the same way;
  - the agent-to-provider assignment: two Codex, one Claude, one DeepSeek.
- `assets/launch/product-hunt/03-tmux-streaming-v4.png` provides:
  - the teal terminal (#00252f);
  - the narrow Claude Code header, prompt chevrons and orange reply dot;
  - the chat and terminal icon toggle.
- `assets/launch/product-hunt/sources/claude-code-terminal.png` provides the Claude Code pixel mascot, redrawn as an exact rectilinear path (#fb7648). It also provides the header lines "Claude Code" and "Sonnet 5.5 · Claude Max".
- `assets/launch/product-hunt/04-local-context-v2.svg` provides the thin connection-line colour #707477, the arrowhead shape and the gap before each mark.
- `assets/launch/product-hunt/sources/context-icons/qorqbot-mark-dark.svg` is the approved black mascot. Its geometry is identical to the landing's inline mascot and to `assets/brand/qorqbot-logo.svg`.

Read-only sources outside the repository:

- `/home/alp/qorqbot/worktrees/landing-concepts/site/dist/index.html` provides the landing markup:
  - the inline mascot SVG, copied verbatim, with its `.q-gaze`, `.q-eye`, `.q-eye-far` and `.q-whiskers` groups;
  - the headline, sub line and "See on Github" CTA.

  It is used twice: at one third scale inside the browser card, and at full size as the endcard.
- Landing stylesheets, which provide the right-aligned layout, the pill CTA, the 300-weight headline with −0.045em tracking, the sub colour #1a1a1acc, and the CTA gradient with its rest and hover shadows:
  - `/home/alp/qorqbot/worktrees/landing-concepts/site/dist/styles.css`
  - `/home/alp/qorqbot/worktrees/landing-concepts/site/dist/assets/preview/agency-20261004/minimal/style.css`
  - `/home/alp/qorqbot/worktrees/landing-concepts/site/dist/assets/preview/agency-20261004/newsletter/selected-polish.css`
  - `/home/alp/qorqbot/worktrees/landing-concepts/site/dist/assets/preview/agency-20261004/portfolio/booking.css`
- `/home/alp/qorqbot/worktrees/landing-concepts/site/dist/mascot.js` and `mascot.css` provide the mascot's motion:
  - the `look()` gaze: anchor at 64%/34% of the mascot box, eye offset dx/d·4.5 and dy/d·3.5, lean dx/d·5°, and a 160 px minimum distance at the 568.8 px mascot size (scaled with the rendered size here);
  - blink `scaleY(.08)`, lean origin 50% 65%, and the drop shadow.

  These are replayed as timeline tweens. The landing's random blinks and float loop are not used.
- `/home/alp/businesses/49IDE/code/landing/anim/runtime/gsap.min.js` and `CustomEase.min.js` (GSAP 3.13.0) are inlined verbatim with their `@license` banners. `runtime/LICENSE-gsap.txt` is copied here as `LICENSE-gsap.txt`; CustomEase carries the same GreenSock banner inline.
- Fonts in `/home/alp/businesses/49IDE/code/landing/anim/assets/fonts/` (`ZenKakuGothicNew-Light.woff2`, `ZenKakuGothicNew-Regular.woff2`, `ZenKakuGothicNew-Medium.woff2`, `GeistMono.woff2`) are embedded as base64. `OFL-ZenKakuGothicNew.txt` and `OFL-GeistMono.txt` are copied here.
- `/home/alp/businesses/49IDE/code/landing/anim/index.html` provides the Claude and OpenAI mark paths, used verbatim. The OpenAI mark stands for Codex.
- `/home/alp/businesses/49IDE/code/landing/anim/assets/japanese-minimalism/motion-curves.md` provides the `0.22, 1, 0.36, 1` hero settle and the `0.45, 0, 0.15, 1` release curve.

## New additions

- One paused, seekable GSAP timeline. A three-layer camera (position, scale, focus) moves over a 1440×840 app window, and every position is computed from fixed constants.
- A DOM/SVG rebuild of the cover's dark desktop shell:
  - "localhost - qorqbot" title bar;
  - nav rows with short authored preview lines;
  - "New agent" footer;
  - composer with typed text;
  - browser chrome with "localhost:57365";
  - chat, terminal and document head icons.
- Vector traces of the four faces and the DeepSeek mark replace raster crops, so they stay sharp at every camera scale.
- A neutral white pointer (no red dot), with press feedback on the pill and icons and a CTA hover.
- A shared-element finale: the page inside the invited browser becomes the endcard, with no cut.
- Preview controls below the artwork, `window.__anim` and the postMessage interface from `PLAN.md`.

## How it differs from the other concepts

- **01 The ensemble** is character-led. It has a wide, mostly still camera, faces multiply out of the mascot, and the invitation is a late beat. Here the film starts inside one conversation, the invitation is the centrepiece (6–15), and the team appears only on the pull-back.
- **02 Terminal to chat** moves identical text from a terminal into chat bubbles, side by side. Here the terminal is one in-place view switch through the head icons, with no text transfer and no split view.
- **03 Context orbit** is a stationary top-down diagram of context documents and pulses around the black face. Here it is a product close-up with camera moves, and context icons are not used.
- **05 Independent by design** is typography-led, with very large type and masks. Here type stays at interface scale until the endcard, and the interface carries the story.

## Validation performed

- **No visual check.** No native browser tool was available in this session, so playback was not watched and no frame was inspected visually. No custom browser, CDP or Playwright tooling was used.
- **Syntax.** Every inline script in the built file parses (`node:vm`), both authored and vendor.
- **Logic harness** in jsdom: no layout or rendering, with stubs for fonts, `matchMedia` and SVG geometry.
  - Boot: `ready` true, `duration` 30, `errors` [], all API methods present.
  - Seek clamps to 0 and 30 and pauses. Play from the end restarts.
  - postMessage play, pause, restart and seek work, including clamping. The ready message reaches a stubbed parent as `{type:"qorqbot-animation-ready", id:"04-browser-invitation", duration:30}`.
  - Start modes: `?t=21.4`, `?paused=1`, `?controls=0&t=5` and default playback. Reduced motion starts paused at 9.2 s and Play still runs.
  - Opacity spot checks at 0, 3.6, 9.2, 12.5, 18.8, 22.8 and 29 s.
  - Determinism: 26 sample times were compared across four seek orders. They are identical except two rotation values that differ only in string precision. The largest numeric deviation across 82 seek paths is 3.3e-5.
- **Font metrics.** Advance widths from the embedded fonts confirm every line fits its box. Glyph coverage was checked: ❯ ▌ █ are not in the fonts and are drawn as SVG. Vertical metrics set the line boxes that centre Zen text optically in bubbles, the pill, the composer and the URL bar.
- **Static checks.**
  - The file makes no external requests: the only `http(s)` strings are XML namespace URIs and the GSAP banners.
  - It has no `src`, `href`, `<link>`, `<iframe>`, `@import`, `fetch` or non-data `url()`.
  - `metadata.json` parses, and its duration matches the timeline and the API.
  - The license texts are present.

## Limitations

- **Authored concept.** The app shell, nav previews, URL, terminal and provider links illustrate the idea. The film makes no claim about provider readiness, pricing, security or automatic CLI launch.
- **Trademarks.** The Codex (OpenAI), Claude and DeepSeek marks belong to their owners. They appear only to identify CLI providers, as on approved slide 02.
- **Not checked visually by this worker.** Layout was derived from geometry and font metrics. Timing feel, edge anti-aliasing of the scaled landing preview, and shadow rendering should be confirmed in the gallery.
- **CTA font.** The landing sets its CTA in JetBrains Mono ("BookingMono"). Because the brief limits monospace type to terminal code, "See on Github" uses Zen Kaku Gothic New Medium, and JetBrains Mono is not embedded.
- **Size.** About 255 KB, mostly the embedded fonts and GSAP.
