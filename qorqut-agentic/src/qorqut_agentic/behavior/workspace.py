"""Prepared workspace instruction composition; host supplies authorized facts."""


def documents(instructions, *, display_name, agent_id, software_boundary, extension_orientations,
              ordubashy, code_roots, delegate_operations, inbox_available):
    inbox_guidance = instructions.render('workspace-inbox') if inbox_available else ""
    orientation = (instructions.render('workspace-identity', DISPLAY_NAME=display_name, AGENT_ID=agent_id))
    orientation += "\n" + software_boundary + "\n"
    for guidance in extension_orientations:
        orientation += guidance
    if ordubashy:
        orientation += (instructions.render('workspace-global-coordination'))
    if code_roots:
        orientation += (instructions.render('workspace-code-roots') + "".join(f"- `{path}`\n" for path in code_roots))
    codex_delegation = ""
    if delegate_operations:
        codex_delegation = "\n## Operational delegation\n\n"
        if ordubashy:
            # The permanent team is the default; the durable procedure is scoped to an explicitly selected legacy assignment.
            lead = (instructions.render('workspace-team-delegation'))
            unavailable = (instructions.render('workspace-team-unavailable'))
            queued = instructions.render('workspace-team-queue')
            browser = (instructions.render('workspace-team-browser'))
        else:
            lead = (instructions.render('workspace-worker-delegation'))
            unavailable = instructions.render('workspace-worker-unavailable')
            queued = instructions.render('workspace-worker-queue')
            browser = (instructions.render('workspace-worker-browser'))
        codex_delegation += (lead +
            instructions.render('workspace-native-subagents')
            + unavailable +
            instructions.render('workspace-no-substitutes')
            + queued +
            instructions.render('workspace-completion') + browser)
        codex_delegation += (
            instructions.render('workspace-delegation-summary'))
    return {
        'CLAUDE.md': (orientation + instructions.render('workspace-claude') + inbox_guidance).encode(),
        'AGENTS.md': (orientation + instructions.render('workspace-codex') + codex_delegation).encode(),
    }
