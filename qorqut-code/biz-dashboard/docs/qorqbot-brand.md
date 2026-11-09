# Qorqbot product mark

- `css/brand/qorqbot-mark.svg` — local product mark master: black round body, white tilted eyes and upward-curled mustache, transparent background.
- `css/brand/favicon.svg` — same geometry, cropped square viewBox with modest padding, plus a thin white outer halo so the black body stays visible on dark browser tabs.
- `css/brand/favicon-32.png` — 32x32 transparent PNG fallback rendered from `favicon.svg` with Playwright (Chromium, `omitBackground`).

## Provenance

The geometry (body silhouette, eye and mustache paths and transforms) is copied unchanged from the original external product logo asset, which is white body with black face. That file is not modified or copied here. The local mark is the owner-chosen inversion (black ball, white eyes). Existing agent glyphs, the Focus icon and the "Qorqut" title are unchanged.

## Serving

Assets live under `css/brand/` because the host mounts `/css` as static files (`qorqut-code/app/qorqut/service/web.py`); `index.html` links `/css/brand/favicon.svg` and `/css/brand/favicon-32.png`.
