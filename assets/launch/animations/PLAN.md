# Qorqbot launch motion — Manager production brief

## Owner request and scope
Create exactly FIVE different playable animation candidates, one per Opus ACLA worker, for one shared review gallery. Each worker executes only its assigned candidate. Owner explicitly selected Opus, MAX effort and self-review disabled; that overrides older Sonnet/Astra instructions. No internal reviewers, no nested agents, no delegation. This is a marketing animation assignment, not application implementation or deployment.

Owner's latest amendment, verbatim:
"And by the way, we already made a bunch of assets for the launch. So I would like if we just reuse a bunch of assets we already have. It doesn't have to be just full reusing. We can make new ones, but let's try to reuse as much as we can."

Role: implementation. Manager has decided the concept, story sequence, style, deliverable interface and constraints below. Implement them; ordinary keyframes, SVG coordinates, line breaks and timings inside the specified scene windows are implementation detail. Do not invent a different plot or change scope. Return a planning question through ACLA ask-question if the design requires a material change.

## Product and editorial direction
Qorqbot: provider-agnostic agents, the familiar CLI/subscription workflow, a local chat interface, browser invitations, and the user's context. The owner has approved the four launch-slide compositions listed below. Audience: people already using coding-agent CLIs who want them in one approachable interface. The next action is to inspect the GitHub repository https://github.com/49-Agents/qorqbot . End with the exact two-line headline "get stuff done" / "stay independent", the product name qorqbot and a quiet "See on Github" CTA. No narrated voiceover, no sound required, no mobile/phone scenes.

Use the owner's short copy where the assigned scene calls for it:
- "your favourite models working together"
- "any cli, any provider"
- "use your subscription, not API pricing"
- "we stream your cli processes directly into the chat interface"
- "your context stays local"
- "Take control"
Do not add claims about provider readiness, security, zero data egress, benchmarks, exact prices, automatic CLI launch or unimplemented application features. This is an authored product concept, not evidence of a live product flow. Record that in notes, not as clutter on the film.

Clarity and minimalism: every visible item must explain the current beat. One short statement at a time, meaningful holds, restrained motion, no piles of tiny explanatory labels. Avoid a slideshow of whole static launch PNGs: reuse the actual artwork, but move individual faces, paths, UI crops, panes, chat bubbles or glyphs to tell the assigned story. New supporting SVG/CSS shapes are allowed when they make the reuse work.

## Existing assets — reuse first
Read this worktree's assets/brand/README.md and assets/launch/product-hunt/README.md. Inspect the actual approved images as visual references, not just their prompts.
- assets/brand/qorqbot-logo.svg: approved white round mascot, tilted almond eyes, small upward mustache, no horns.
- assets/launch/product-hunt/sources/context-icons/qorqbot-mark-dark.svg: approved black face with white eyes/mustache.
- assets/launch/product-hunt/01-cover.png: chat/browser invitation composition.
- assets/launch/product-hunt/02-provider-choice.png: approved names Mike, Turgut, Adam, Borte; four differently shaped colored faces; two Codex connections, one Claude, one DeepSeek.
- assets/launch/product-hunt/03-tmux-streaming-v4.png: approved narrow teal terminal and matching dark chat exchange.
- assets/launch/product-hunt/04-local-context-v2.svg and .png: original context icons, thin arrows, generous arrowhead clearance, large mascot.
- assets/launch/product-hunt/sources/context-icons/{notion.svg,google-drive.png,obsidian.svg}: original source marks. No labels beneath context icons.
- assets/launch/product-hunt/sources/claude-code-terminal.png: owner-supplied terminal source; use the simplified approved v4 composition for what to retain.

Read-only additional reusable sources:
- /home/alp/qorqbot/worktrees/landing-concepts/site/dist/mascot.js and mascot.css: existing eyes/blink/float/interaction implementation.
- /home/alp/qorqbot/worktrees/landing-concepts/site/dist/assets/qorqbot-mark-dark.svg
- /home/alp/qorqbot/worktrees/landing-concepts/site/dist/index.html and styles.css: approved landing composition with large mascot left and right-aligned headline/CTA.
- /home/alp/businesses/49IDE/code/landing/anim/: original 49IDE HTML/SVG/GSAP motion source, source storyboard, runtime and motion vocabulary. Reuse its local GSAP library and preserve its accompanying license notice. Do not copy the 49IDE product claims, phone finale or brand endcard.
- /home/alp/businesses/49IDE/code/landing/anim/assets/fonts/: actual ZenKakuGothicNew Light/Regular/Medium and GeistMono WOFF2 with OFL notices.
- /home/alp/businesses/49IDE/code/landing/anim/assets/japanese-minimalism/: useful existing thin-arrow/grid/pane primitives.

Visual consistency: warm ivory #FAF8F4 stage, near-black #1A1A1A shapes, quiet grid only where useful, Zen Kaku Gothic New for headlines/UI titles, Geist Mono only terminal code, dark product panels and saturated agent faces. The four approved faces are green round Mike, blue bean Turgut, magenta teardrop Adam, ochre rounded triangle Borte; preserve the almond eyes and mustache on every face. Reconstruct the individual editable face shapes from the approved source geometry as needed; don't swap to generic emoji. No needless red pointer dot for Qorqbot. Smooth ease-outs, precise motion, no elastic/bouncy template look. Same style does NOT mean same story across candidates.

## Deliverables and shared gallery interface
Own ONLY assets/launch/animations/<your-variant>/ in your isolated worktree. Do not edit shared files, existing images, application modules, gallery code or another actor's directory.
1. index.html — a complete playable 1920x1080 animation, 28–32 seconds as specified below. Self-contained: inline author JS/CSS/SVG, inline reused GSAP code with license attribution, data URLs for local font/raster assets. No CDN, live iframe website, server API, remote fonts or network dependency. This lets the gallery snapshot one exact file. Keep readable authored JS despite inlined vendor code. Clip the stage cleanly and scale it to its container preserving 16:9. Desktop is the target.
2. notes.md — scene timing, reused source assets with exact paths, short record of new additions, how it differs from the other concept descriptions, validation actually performed and limitations. Label product imagery as authored concepts.
3. metadata.json — {id,title,duration,width:1920,height:1080,entry:"index.html",reused_assets:[...],scenes:[{start,end,label}]}.
4. Include font/library license text locally in the candidate folder when embedding those assets.

Controls/API: expose window.__anim with ready:boolean, duration:number, play(), pause(), restart(), seek(seconds), and errors:array. seek must be deterministic, clamped and pause playback; all scene animation should live on one seekable GSAP timeline. Default play once through and hold the endcard; no infinite background animation. Query ?paused=1 starts paused at 0; ?t=N starts paused at N. Query ?controls=0 hides your slim bottom play/pause, restart and seek bar; by default show those controls outside the 1920x1080 artwork. The artwork is never covered by controls.
Listen for same-page parent postMessage commands {type:"qorqbot-animation",action:"play"|"pause"|"restart"|"seek",time?:number}; use the same API methods. Send parent {type:"qorqbot-animation-ready",id,duration} on readiness. This is just a local preview interface, not product functionality. Respect prefers-reduced-motion by starting paused on a meaningful early frame; Play still works.

## Implementation sequence and checks
1. Inspect git status and relevant AGENTS.md; read the listed brand/launch references and source animation. Do not redo product research or browse for replacement assets.
2. Build only the assigned story with asset reuse and deterministic timeline.
3. Check authored JS syntax, metadata, local asset completeness, duration and required API/control wiring. If your native browser tool is available, check playback, pause/restart/seek and a few scene times. Do not launch custom browser/CDP/Playwright tooling. If browser control is unavailable, state that and do the bounded static checks; Manager will verify the gallery. Do not write a large test suite for this visual draft.
4. Commit only your candidate directory on the supplied branch and return the exact SHA. Do not push, merge, deploy, start a persistent server or modify the running Qorqut services. Observe root commit-date hooks: no author/committer timestamp in weekdays 09:30–20:30 inclusive America/Puerto_Rico; current date is Sunday. If the actual clock crosses that constraint, leave the completed changes staged and report instead of inventing a commit time.
5. One ACLA completion report only, after the entire candidate is ready: SHA/path, duration, reused assets, checks, unresolved limitations. No progress reports, acknowledgments or interim milestones. Questions use ask-question. No self-review or reviewer agents.

Native content-session tools are unavailable to Manager in this session. Manager will assemble one isolated local gallery using the existing shared template, five variants × one option by explicit owner request. Do not make a separate gallery or server.


## Assigned concept 01 — The ensemble (30 seconds)
This is the character-led film. The mascot is the visual protagonist, not a cursor. Camera mostly holds a spacious wide field; continuity comes from the faces moving into functional positions.
0–4: One large approved black mascot on ivory wakes with one blink and a subtle eye movement. "qorqbot" appears quietly. Four small differently colored/shaped faces emerge behind it and spread along a graceful horizontal arc, not a pop/bounce gag.
4–10: Those exact four faces become Mike/Turgut/Adam/Borte in a tidy right-hand agent list. Reuse slide 02 composition. Thin curves draw from the four faces to Codex, Codex, Claude, DeepSeek at left. Headline upper-left: "your favourite models working together"; smaller "any cli, any provider". Use actual provider marks if present in original assets, otherwise crop their existing approved artwork cleanly; no invented logos.
10–17: The agent rows slide left into the navigation of the approved dark chat layout. Adam is selected. Chat bubbles enter one at a time: owner "Help me prepare the launch."; Adam "I’ll review the page and draft the checklist."; Mike "I’ve gathered the context." Tiny faces attach the latter two responses; no status dashboards. The transformation should visibly preserve the four faces from the prior scene.
17–24: The conversation grows the approved browser invitation card showing our approved landing page. Adam's line: "Want to take a look together?" A left-aligned "Take control" pill appears OUTSIDE/below the browser frame. The pointer hover/click is a subtle local illustration, not a live browser. Hold the recognizable invitation.
24–30: UI recedes; the four colorful faces return behind the black hero face, with warm spacing, and settle into the endcard. Exact two-line get stuff done / stay independent, qorqbot and See on Github. Hold the fully readable ending for at least 2.0s. Assets from slides 01/02 are central.


## Assigned concept 02 — Terminal to chat (30 seconds)
This is the transformation film: same conversation in two views, subscription CLI at the center. No mascot multiplying or context orbit. The movement of identical text makes the thesis visible.
0–5: An elegantly cropped narrow teal Claude terminal, reusing the owner terminal capture/approved slide 03 treatment. Retain only Claude Code, Sonnet 5.5 · Claude Max, one prompt and clean code area, none of the auto-update errors or permission chrome. Owner prompt types "Can you help me plan the launch?" and reply "Yes. I’ll draft the launch checklist."
5–12: Terminal moves to the left; exact prompt/reply lines travel along a thin curve into dark rounded chat bubbles on the right. "tmux streaming" appears once on the connector. A second exchange appears in BOTH views: "Start with the Product Hunt page." / "I’ll prepare the headline, gallery and first comment." Animate matching messages in sync. Maintain proper text-size readability, no empty code panes.
12–18: Hold the terminal/chat pair under centered "use your subscription, not API pricing" with the approved smaller sentence "we stream your cli processes directly into the chat interface". Briefly switch focus between terminal and chat using small icon toggles; both views preserve the same conversation. Make clear it is a view transformation, not two independent bot processes.
18–24: Zoom back just enough to reveal Mike/Turgut/Adam/Borte as a compact agent rail and Codex/Claude/DeepSeek marks connected under it. Short "any cli, any provider". Do not recreate concept 01's full provider diagram: this is a brief supporting beat with the terminal/chat pair still dominant.
24–30: Terminal and chat edges align then fold into a single clean conversation outline; the approved mascot settles beside the exact endcard. Hold 2.0s. Reuse slide 03, owner terminal source and matching approved messages heavily.


## Assigned concept 03 — Context orbit (30 seconds)
This is a top-down diagram/choreography film with the black face as a stable center of gravity. Use a mostly stationary camera, elegant thin paths and sequential pulse travel. Never show bouncing blobs or a 3D space scene.
0–5: Open with the approved slide 04 composition: centered "your context stays local", black face right, the ACTUAL Notion/Google Drive/Obsidian source icons left, no labels or subtitle. Draw the three curves toward the face with the approved larger clear gap between arrowheads and face. The face glances at each incoming path.
5–12: Three small ivory document cards emerge from the icons, follow the paths and become a neat document stack beside the face. They are illustrations of context, not live connector/import progress. Camera pulls out slightly; a thin rounded desktop/workspace boundary draws around the face and documents. Do not draw clouds, locks, security guarantees, sync claims or percentages.
12–19: Four existing colored faces appear as satellites around the central workspace. The same thin lines fan out from the document stack, and modest traveling pulses connect the shared context to Mike/Turgut/Adam/Borte. Small Codex/Claude/DeepSeek marks clarify two/one/one assignments. Copy transitions to "your favourite models working together". The diagram remains asymmetrical and spacious, not a perfect radial network.
19–24: The satellites move into a compact dark chat row. One simple output artifact, "Launch checklist", resolves from the document stack and appears as a file card with one short line "Ready for your review." The context-to-work result is the climax, not a simulated real product execution.
24–30: The document/path diagram contracts into the approved black face; the exact endcard sits beside it. Hold 2.0s. Reuse slide 04 SVG source geometry and source icons verbatim as much as possible; this is not a static pan over its PNG.


## Assigned concept 04 — The invitation (30 seconds)
This is the product-led close-up film. Start inside the chat, let the viewer understand one useful conversation, then reveal the broader team. The browser invitation is the main memorable moment, not a late decorative card.
0–6: Tight view of Adam's dark chat, no left nav yet. Messages appear with natural short stagger: owner "Can you help me prepare the launch?"; Adam "I’ve reviewed the page. Let’s take a look together." Reuse approved slide 01 chat spacing, face and composer. No tiny provenance, delivery or debug labels.
6–15: A browser frame grows within the chat and reveals the approved Qorqbot landing artwork, mascot left and headline right. Gently animate that mascot's eyes/blink using the existing landing mascot source. Show the invitation line and the left-aligned "Take control" pill OUTSIDE the frame. A neutral pointer presses the pill; the browser expands toward the viewer for a brief immersive moment, keeping a visible chat edge so the relationship is clear. Do not include the removed embedded 49agents/qorqbot corner branding or 'Open qorqbot website'. No real external iframe.
15–21: Camera pulls back into the full desktop chat shell; Mike/Turgut/Adam/Borte appear in the existing left nav with the approved colored faces. Provider connections briefly draw to Codex/Claude/DeepSeek around the outer frame. Copy "any cli, any provider". The browser invitation remains visible in the conversation.
21–25: Toggle from the chat icon to the terminal icon, using a slide 03-style terminal with the same task, then back. "use your subscription, not API pricing" appears once outside the app. The switch is a concise demonstration, not the matching-text transfer choreography of concept 02.
25–30: App scales back and leaves the exact endcard with approved black mascot. Hold 2.0s. Reuse the actual cover/browser/landing artwork heavily, with new DOM/SVG overlays only to give it motion.


## Assigned concept 05 — Independent by design (28 seconds)
This is the typography-led editorial film, distinct from the other UI stories: very large Zen type, few objects, decisive wipes/masks, generous blank space, bold changes in scale. No rapid unreadable flashes; do not just crossfade five static cards. Match the established ivory/ink palette rather than adopting neon or another font.
0–5: Very large "get stuff done" typesets in two calm motions. An approved black mascot peeks from behind the final word, looking toward it. Individual word blocks slide apart to reveal four colorful approved agent faces aligned with different baselines, preserving the type as the structural frame.
5–11: The words "your favourite models" and then "working together" become horizontal tracks. Mike/Turgut/Adam/Borte move along these tracks into a zoomed agent list. Thin arrows visibly connect the faces to Codex/Claude/DeepSeek. A typographic mask reveals "any cli, any provider"; provider marks and faces remain the only supporting images. Reuse slide 02 shapes/marks.
11–17: Huge "your subscription" resolves above an actual cropped slice of the approved terminal, then an expanding mask reveals the matching approved chat slice. The complete short sentence becomes "use your subscription, not API pricing". One connecting line visibly moves the same small message block from terminal to chat; do not use a large full UI scene here.
17–22: "your context stays local" holds centered. Existing Notion/Drive/Obsidian icons move in from three directions along thin curves toward the mascot, with generous arrowhead clearance; no labels. Reuse slide 04 components. Animate the final word's baseline settling, no heavy letter-scramble effects.
22–28: All shapes ease into the approved black mascot at left; the exact right-aligned two-line get stuff done / stay independent endcard completes, qorqbot and See on Github beneath. The landing page composition is the visual conclusion. Hold 2.2s. This version is about scale/masks/type; the others are character, transformation, diagram and product demo.
