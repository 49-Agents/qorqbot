# Visible agent browser on Mac: plan

Status: steps 1 to 3 and the step 5 flags are built (see "Built so far"); steps 4, 6 and 7 are not. Nothing has run on a Mac. Branch `feature/visible-agent-browser-mac`.

**Built so far** (on this branch, not yet run on a Mac): steps 1 to 3 and the three flags of step 5, with visible as the
only mode on a Mac and Chrome for Testing as the fallback on a Mac without Google Chrome. See "A visible browser on a Mac"
in [BROWSER-MANAGEMENT.md](BROWSER-MANAGEMENT.md). Not built: step 2's Settings wording, step 4, step 5's focus emulation
for every tab, steps 6 and 7.

## Goal

On a Mac, the browser the agents use is a normal, visible Google Chrome window on Qorqbot's own profile. The owner sees
the agents work in it and can click and type in it directly. The Browser tab in the app keeps showing the same browser.

Out of scope for now: servers and other computers without a screen (they stay headless), Windows/WSL, and the owner's own
everyday Chrome profile, which Qorqbot never touches.

## Why

A headless Chrome differs from a normal one in ways websites test for (name, screen size, graphics, no mouse). We have
been removing those one at a time. A visible Chrome on a Mac has a real screen, window, graphics card and home address, so
it reads like any person's browser without overrides.

## What exists today (the starting point)

- One Shared browser per installation, on its own profile under the runtime folder, started by
  `qorqut/browser/agent_supervisor.py` (`chrome_command`) with `--headless=new`, `--disable-3d-apis` and a
  `--user-agent=` override. On a Mac the supervisor is started detached by `native_launcher.py`.
- New browsers run on Qorqbot's own Chrome for Testing when it is installed (`managed_chrome.preferred`), else on the
  installed Google Chrome (`native_paths.installed_chrome`).
- The owner watches and types through the live view (`live.py`), which already uses focus emulation so a tab keeps
  drawing when it is not in front.
- Website sign-in opens a tab in this browser (pull request 209).

## Steps

Each step is one pull request and leaves the product working. Steps 1 to 3 give a usable visible browser; 4 to 7 make it
pleasant to live with.

### 1. A browser mode, visible on Mac

- Record a `mode` (`headless` or `visible`) in the browser's ownership record (`meta.json`), chosen when the browser is
  created or repaired: `visible` on a Mac, `headless` everywhere else. An existing Mac browser moves to `visible` at its
  next start; its profile and sign-ins are kept.
- `chrome_command` in visible mode: no `--headless=new`, no `--disable-3d-apis`, no `--user-agent=`; adds
  `--window-size=1440,900`. Headless mode is unchanged.
- The readiness check, stop and restart paths are checked for the visible case (same DevTools port, same supervisor).

### 2. Run on the installed Google Chrome

- In visible mode the browser uses the installed Google Chrome when there is one. Chrome for Testing shows a "for
  automated testing" bar and calls itself Chromium, so it is only the fallback on a Mac without Chrome.
- A profile last opened by a newer Chrome can lose sign-ins when an older one opens it. Existing browsers recorded on
  Chrome for Testing 158 move to the installed Chrome (newer) only in that direction, never back.
- Settings says which Chrome is in use and, on a Mac without Chrome, that installing Google Chrome removes the bar.

### 3. Make the window recognisably Qorqbot's

- Name the profile "Qorqbot agents" and give it its own theme colour, written into the profile's preferences before the
  first start, so the window and its Dock icon are not mistaken for the owner's Chrome.
- First tab is a short local page saying what this window is and that agents work here.

### 4. Never take the owner's focus

- Agents' new tabs open in the background, and an agent's tab switch does not bring the Chrome window to the front of
  other apps. Find every place that activates a tab (`agent-browser` tab commands, `Page.bringToFront` in `live.py` and
  `captcha.py`) and decide per place: keep inside Chrome, but no app activation.
- The window starts without activating (the Mac launch goes through `open -g` or the equivalent for a direct start).
- Test: the owner types in another app for five minutes while agents open, switch and close tabs; no keystroke lands in
  Chrome and Chrome never comes forward.

### 5. Keep working when hidden, minimised or covered

- Add `--disable-backgrounding-occluded-windows`, `--disable-renderer-backgrounding` and
  `--disable-background-timer-throttling` in visible mode, and turn on focus emulation for every agent tab, not only the
  watched one. Measured on Linux (Chrome 153): without these a tab that is not in front stops drawing, its timers slow
  to a tenth and screenshots never answer; with them it runs normally.
- Mac additions to check: App Nap for the Chrome process, a minimised window, a window on another Space, a locked
  screen, and display sleep.

### 6. Closing and quitting

- Closing the window (red button): Chrome on a Mac keeps running without a window. Agents keep working; the next agent
  tab reopens a window in the background. The app's Browser tab still shows the pages.
- Quitting Chrome (Cmd+Q): treated as the owner's pause. The browser is not restarted until an agent next needs it or the
  owner presses a "Show the agents' browser" button in the app. No restart loop.
- Quitting Qorqbot closes the agents' Chrome.

### 7. Chrome's own pop-ups

A visible Chrome asks on the desktop where headless answers by itself. Decide and build each:

- Permission questions (notifications, location, camera, microphone): denied automatically through DevTools permission
  settings.
- File picker: uploads go through the agents' upload command, which sets the file without a picker; a picker a page opens
  by itself is cancelled.
- Print dialog, "restore pages", default-browser and sign-in-to-Chrome prompts, password-save bubbles: switched off by
  flags or profile preferences.
- macOS prompts on first start (Keychain is avoided with `--use-mock-keychain`, already set; check "Chrome wants to
  access…" prompts and the download-folder prompt).

## What the owner decides

1. Visible is the only mode on a Mac, or a setting ("Show the agents' browser window": on by default) lets a Mac user go
   back to headless.
2. Step 6: what closing the window and quitting Chrome should do, if not the above.
3. A Mac without Google Chrome: fall back to Chrome for Testing with its bar, or ask the user to install Chrome.

## Testing

- Unit tests for the mode choice, the argv per mode, the Chrome choice and the preference file (run anywhere).
- Mac behaviour (steps 3 to 7) cannot be tested on this server. Two ways, both used before: GitHub's Mac runners (free,
  no real desktop interaction, good for start-up and the 30-mark comparison below) and a rented Mac by the day (needed
  for focus, Dock, Spaces and pop-ups).
- The 30-mark comparison: one page that reads how the browser presents itself (name, version details, screen, window,
  graphics, pointer). Target: the agents' Chrome reads the same as the Mac's Chrome opened by hand.
- Real sites: Google sign-in first step, a Cloudflare-protected sign-up, and one Slate school sign-up page.

## Risks and unknowns

- Focus stealing (step 4) is the make-or-break item and is untested on a Mac.
- The browser tool forces the page area to the window size; in a visible window that may resize or fight the real
  window. To check in step 1.
- A visible Chrome uses the graphics card and more memory than headless; measure on an 8 GB Mac.
- Two Chromes (the owner's and the agents') share one Dock icon unless the app bundle differs; the profile colour and name
  are what tell them apart. To check in step 3.
- The live view against a visible Chrome was checked on Linux only.

## Order and size

Steps 1 to 3: about one day of building, plus a first Mac run. Steps 4 to 7: sized after that run, because they depend on
what the Mac shows.
