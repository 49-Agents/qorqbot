# Local access acceptance and test deployment

2026-10-09. Manager accepted Core `bbbcc7acb5ab7254e107ca07ead75dca292c7e53`
and UI `ad96341fd6abc0c6ebb6a47e6c767d8b46547526` against D55. Both are merged
and pushed. ACLA approvals 364 and 362 record the separate source reviews.

Independent checks passed: one direct-opening/reconnect/draft UI group (13.0s)
and two Core HTTP/MCP/setup/restart groups (10.406s). The normal generated-file
migration replaces old token bindings with public agent attribution.

The existing owner test instance at `https://qorqut.tail740c74.ts.net:8875/`
now serves this pair without application authentication. A fresh real Chrome
context opened directly with no credentials or cookies. Actual Terminal input,
pinned CLI version output, Chat save/reload and automatic dated transcript
publication passed; screenshots were inspected and there were no page errors.
The original chat entry, agent identity and tmux terminal UUID survived the
update. The instance's generated MCP/hook actor headers and ready state were
verified. Unused console credential files were removed.

Private evidence: `/home/alp/.local/share/qorqutbot-test/checks/noauth/`, plus
`checks/before-noauth.json` and `checks/after-noauth.json`. Native provider login
is independent; no native model session was started or provider call made.
VM/browser integration and complete child/media capture remain unfinished.
