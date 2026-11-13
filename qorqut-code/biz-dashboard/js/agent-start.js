// The owner's one-time trust consent for Qorqbot's agent folders. Agents are started by the owner in their own terminal.
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
        body: `<p>Claude Code and Codex ask before they work in a new folder. Allow this once, and they can open straight into
          the session in the folders Qorqbot prepares for its agents.</p>
          <p>Qorqbot will:</p>
          <ul><li>mark each agent’s own folder as trusted in Claude Code and Codex</li>
          <li>run the hooks Qorqbot installs in an agent’s own folder without asking Codex to review them each time</li></ul>
          <p class="muted">Nothing else is trusted. Claude Code or Codex can still ask you about a larger folder around an agent,
          such as the git repository its folder sits in: Qorqbot does not answer that for you, and you decide in the terminal.
          Your other projects are not changed, and claude and codex run as usual anywhere outside an agent’s own folder.
          Nothing starts by itself. You can turn this off in Settings.</p>
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
