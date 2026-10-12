# 05 — Independent by design

The typography-led editorial film: 28.0 s at 1920×1080. It is an authored product concept for launch review, not a capture of a live product flow. The terminal and chat images are crops of the approved slide 03 artwork. The message hand-off and the context arrows illustrate the idea; they do not record real behaviour.

## Playback

- `index.html` is the whole film. Open it directly; it makes no network requests. Layout:
  - fonts as data URLs in `<style>` (lines 16–18)
  - GSAP core (lines 65–76) and CustomEase (77–88), inlined verbatim with their banners
  - one JSON block of image data and traced paths (89–95)
  - the readable authored film script (96–703)
- One paused GSAP master timeline, fixed at 28.0 s. Every visual state is a seekable tween on it, and there is no other animation loop.
- `window.__anim`: `{ready, duration, play(), pause(), restart(), seek(s), errors}`. `seek` clamps to 0–28 and pauses. After a seek the frame is the same whatever the previous time was.
- By default the film plays once and holds the endcard; Play at the end restarts.
  - `?paused=1` opens paused at 0; `?t=N` opens paused at N; `?controls=0` hides the 44 px control bar (play/pause, restart, seek bar, time).
  - The bar sits below the 16:9 artwork, never on it. The stage scales to fit the space that remains.
  - Space toggles playback.
- Parent messages `{type:"qorqbot-animation", action:"play"|"pause"|"restart"|"seek", time?}` are accepted only from `window.parent`. When ready, the film posts `{type:"qorqbot-animation-ready", id:"05-kinetic-type", duration:28}` to the parent.
- `prefers-reduced-motion: reduce` opens paused at 4.9 s: the stepped-apart words with the four faces on their type lines. Play works normally.
- Fonts load before the timeline is built, with a 4 s cap. If Zen Kaku Gothic New is not ready by then, the film still starts and records the fallback in `__anim.errors`.

## Scene timing

| Time (s) | Beat |
|---|---|
| 0.3–2.1 | "get stuff done" (Zen Light, 280 px) typesets in two calm motions: "get stuff" rises from its mask, then "done". |
| 2.05–3.25 | The approved black mascot rises behind "done", cut at the word's x-height. It glances sideways along the word and blinks once. |
| 3.3–4.3 | The three word blocks step apart and down to 75 %. The gaps open on Mike, Turgut, Adam and Borte, each sitting on a different type line: ascender, x-height, baseline, descender. |
| 3.85–4.9 | The four type lines draw in as thin guides; this frame holds. |
| 4.95–6.05 | The words sink out and the mascot ducks away. The guides spread into four horizontal tracks, and the faces gather at the head of each track. |
| 5.55–7.6 | "your favourite / models", then "working / together", set on the tracks, one line per face. |
| 7.6–9.4 | Each statement folds flat into its track. A dark list slides in from the right, and the faces travel along the tracks into it as a zoomed agent list: Mike, Turgut, Adam, Borte. |
| 8.95–10.2 | The tracks bend into thin arrows (two to Codex, one to Claude, one to DeepSeek) as the provider marks and labels arrive. |
| 9.65–10.45 | An ink bar wipes across and reveals "any cli, any provider". |
| 10.95–11.6 | The whole cast is wiped away right to left. |
| 11.25–12.3 | Huge "your subscription" (200 px) rises. |
| 12.05–14.15 | A cropped slice of the approved teal terminal opens below it and shifts left. An expanding circular mask reveals the matching dark chat slice. |
| 13.65–14.85 | "your subscription" scales down into "use your subscription, not API pricing". |
| 14.75–16.6 | A thin connector draws between the views. The reply block lifts off the terminal while its source line steps back. It travels along the connector, wipes into its chat rendering mid-flight, and lands as the chat reply. |
| 16.95–17.55 | The scene wipes away top to bottom. |
| 17.3–19.65 | "your context stays" rises. "local" drops in and settles onto the shared baseline, with a short ink rule under it that fades. |
| 18.35–19.3 | The mascot rises from a horizon line into the centre. |
| 18.95–20.65 | Notion, Google Drive and Obsidian arrive from the left, bottom and right. Each rides its own thin curve and leaves an arrow behind it. The arrowheads stop 70 px short of the mascot. No labels. |
| 20.95 | The mascot blinks; the composition holds. |
| 22.0–23.1 | The icons continue along their arrows into the mascot, and the statement lifts away. |
| 22.85–23.95 | The mascot grows and settles at the landing page's lower-left position, gaining the landing drop shadow. |
| 23.3–25.45 | Right-aligned "get stuff done" / "stay independent" (124 px), then "qorqbot", then a quiet "See on Github" pill (artwork, not a link). |
| 25.3–25.75 | The mascot glances toward the copy and blinks. This is the last motion. |
| 25.75–28.0 | The endcard holds for 2.25 s. |

## Reused assets (exact paths)

**From this repository (read-only):**
- `assets/launch/product-hunt/02-provider-choice.png`
  - The four agent faces (green round Mike, blue bean Turgut, magenta teardrop Adam, ochre rounded-triangle Borte, with almond eyes and mustache) and the Codex, Claude and DeepSeek marks are vector-traced from this image into editable SVG paths.
  - The names, the 2/1/1 provider assignment and the left-hand provider column follow its composition.
- `assets/launch/product-hunt/03-tmux-streaming-v4.png` (1671×941). Three raster crops at 1:1, boxes given as x0, y0, x1, y1:
  - terminal: 199, 285, 729, 580
  - chat header and prompt: 937, 284, 1473, 425
  - chat reply: 937, 425, 1473, 532

  The travelling message block is drawn from those same crops: the terminal reply lines and the chat reply bubble.
- `assets/launch/product-hunt/04-local-context-v2.svg`
  - Used for styling only, not pixels: the arrow colour #707477 at 1.7 px, the arrowhead shape and the generous clearance.
  - Icon proportions are scaled to 95 % (122/154/162 → 116/146/154 px).
- `assets/launch/product-hunt/sources/context-icons/notion.svg`, `google-drive.png` and `obsidian.svg`: embedded byte-for-byte as data URLs.
- `assets/launch/product-hunt/sources/context-icons/qorqbot-mark-dark.svg`: the mascot geometry, used verbatim. The eyes are wrapped in groups so they can glance and blink.

**From the approved landing page (`/home/alp/qorqbot/worktrees/landing-concepts/site/dist/`, read-only):**
- `index.html` and `styles.css`: the endcard composition at 1920×1080 — headline copy and size, right alignment, line positions, mascot size and lower-left placement.
- `mascot.css`: the drop shadow and the blink depth (`scaleY(.08)`). The blink length follows `mascot.js` (≈140 ms closed).
- `assets/preview/agency-20261004/portfolio/booking.css`: the CTA pill's gradient, border and shadows.

**From the 49IDE motion source (`/home/alp/businesses/49IDE/code/landing/anim/`, read-only):**
- `runtime/gsap.min.js` (GSAP 3.13.0) and `runtime/CustomEase.min.js`: inlined verbatim.
- `runtime/LICENSE-gsap.txt`: copied unchanged to `licenses/`.
- `assets/fonts/ZenKakuGothicNew-Light.woff2`, `-Regular.woff2` and `-Medium.woff2`: embedded as data URLs.
- `assets/fonts/OFL-ZenKakuGothicNew.txt`: copied unchanged to `licenses/`.
- `assets/japanese-minimalism/motion-curves.md`: the two custom eases.
  - `hero` (0.22, 1, 0.36, 1), the `--ease-hero` token
  - `release` (0.45, 0, 0.15, 1), `maRelease`

Geist Mono is not embedded. The only terminal text is inside the slide 03 crop.

## New additions

- **Live typography:** every headline is real Zen Kaku Gothic New text set inside its own clipping mask.
  - Word positions come from one measured run, which keeps natural spacing and kerning.
  - The rise, sink and lift moves are proportional to type size.
- **Line work:** type-line guides that become tracks and then arrows; the ink-bar reveal; the terminal-to-chat connector; the context-icon curves. All are thin SVG lines in the slide 04 arrow style.
- **Masks and wipes:** the gap reveals between word blocks; the terminal's bottom-up opening; the circular mask for the chat slice; the cast and scene wipes; the mid-flight skin swap of the message block; the horizon clip for the mascot's entrance.
- **Supporting shapes:** the dark agent-list panel, styled like slide 02's list, and a small teal patch that dims the terminal's source line while its copy is in flight.
- **Face and mark traces:** new vector paths that reproduce the approved raster shapes.

## How it differs from the other concepts

- **01 The ensemble** is character-led: the mascot is the protagonist and the UI unfolds around the faces.
- **02 Terminal to chat** moves full matching conversations between two views.
- **03 Context orbit** is a stationary diagram with document cards and pulses.
- **04 The invitation** is a product demo built around the browser invitation.

05 is built from type, scale and masks:
- The words' own type lines become the faces' baselines.
- The statements flatten into the tracks the faces ride.
- A 200 px word shrinks into a sentence.
- Only a few objects are on screen at a time, with wipes and masks rather than crossfades.

It has no full UI scene, no browser invitation, no document cards and no multi-message conversation. A single message block makes the terminal-to-chat point.

## Validation actually performed

- **No real browser playback.** No native browser tool was available in this session, so playback, rendering and timing in a real browser have not been checked. Manager's gallery check will be the first real playback.
- **Offline smoke test, 43/43 checks passed.** jsdom 30.1.0 ran the committed `index.html` with its real inlined GSAP. jsdom has no layout engine, so the harness supplied text widths from the embedded fonts' advance and kerning tables and sampled SVG path geometry. The checks covered:
  - boot with an empty `errors` array, the `__anim` shape and duration 28
  - `?paused=1`, `?t=N`, clamping of out-of-range seeks, and that seek pauses
  - deterministic seeking: forward, backward and shuffled seek orders produce identical frames at 56 sample times, and fresh `?t=` loads at six of those times (4.9, 12.5, 15.6, 19.9, 22.4, 27.9) match the seeked frames
  - play-through to the held endcard, Play-at-end restart, the play/pause and restart buttons, and the seek bar
  - reduced motion opening paused at 4.9 s, and `?controls=0`
  - in an iframe: the ready message, the parent's play/pause/seek/restart commands, and ignoring messages from non-parent windows or with other types
- **Preview frames.** The 56 harness states were serialised to SVG and rasterised with cairosvg for a visual review of every scene, plus full-resolution spot checks. These previews are approximate: no box shadows or CSS filters, and a flattened CTA gradient.
- **Static checks:**
  - `node --check` on the extracted authored script
  - `metadata.json` parses and matches the timeline (duration 28; five scenes cover 0–28)
  - every data URL decodes
  - no remote `src`, `href`, `url()`, `fetch` or `import` in `index.html`; the only URLs are SVG namespaces and the vendor licence banners
  - licence files are present

The build script and test harness are scratch tools kept outside the repository. The committed `index.html` is the deliverable, and its authored script is readable as-is.

## Limitations

- **Authored concept.** It does not demonstrate live provider readiness, real streaming or real file access, and it makes no security, data-egress or pricing claims.
- **Not checked in a real browser** (see above). Clip-path and filter transitions, font loading and frame pacing at 1920×1080 are unverified.
- **Raster imagery:**
  - The terminal and chat crops come from a 1671×941 slide and look slightly soft at 1:1.
  - The Google Drive source is a 128 px PNG shown at 146 px, so it is a little soft.
- **Third-party marks.** The provider marks and context icons are trademarks of their owners, reproduced from the approved launch assets. The provider marks are traced from the slide 02 raster rather than taken from official vector files.
- **Endcard differences from the landing page:**
  - The "See on Github" pill is set in Zen Kaku Gothic New Regular. The landing uses its own monospace (BookingMono), and the brief limits mono to terminal code.
  - Beneath the headline, the brief's "qorqbot" replaces the landing's "provider-agnostic qorqbot".
- **File size.** About 679 KB, mostly the slide 03 crops (about 374 KB base64), the three font subsets (about 65 KB base64) and GSAP with CustomEase (about 79 KB).
