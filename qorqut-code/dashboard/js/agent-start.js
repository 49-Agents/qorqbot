// The owner's one-time trust consent for Qorqbot's agent folders, used while agents run in terminals (the legacy mode).
const agentStart = (() => {
  const url = '/root/agent-trust';
  const read = async () => coreData(await api(url));
  const grant = async () => coreData(await post(url, {granted: true}));
  // The owner's consent, in plain language. Resolves to the new trust view, or null when the owner declines.
  function consent() {
    return new Promise(resolve => {
      let settled = false;
      const finish = value => { if (!settled) { settled = true; resolve(value); } };
      modal({
        title: 'Trust Qorqbot’s agent folders',
        width: 520,
        body: `<p>Claude Code asks before it works in a new folder. Allow this once, and it can open straight into the
          session in the folders Qorqbot prepares for its agents.</p>
          <p>Qorqbot will mark each agent’s own folder as trusted in Claude Code.</p>
          <p class="muted">Nothing else is trusted. Claude Code can still ask you about a larger folder around an agent,
          such as the git repository its folder sits in: Qorqbot does not answer that for you, and you decide in the terminal.
          Your other projects are not changed, and claude runs as usual anywhere outside an agent’s own folder.
          You can turn this off in Settings.</p>
          <div class="ferr" role="alert" hidden></div>`,
        onClose: () => finish(null),
        actions: [
          {label: 'Not now', quiet: true, onClick: close => close()},
          {label: 'Allow', primary: true, onClick: async (close, el) => {
            const error = el.querySelector('.ferr');
            try { const view = await grant(); finish(view); close(); }
            catch (failure) { error.textContent = failure.message; error.hidden = false; }
          }},
        ],
      });
    });
  }
  return {read, consent};
})();
