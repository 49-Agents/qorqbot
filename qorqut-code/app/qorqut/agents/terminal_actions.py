"""Ordubashy's terminal tools in Terminals mode: the agent's tmux terminal, as the product did before managed sessions.

``TerminalControl`` (``terminal_control.py``) asks ``mode.is_terminal`` at every call and sends the call here only in Terminals
mode; the managed meaning stays there. Everything below goes through ``control.backend`` (the terminal backend the service sets
at start), which itself refuses with ``terminal_mode_off`` on a managed installation, so none of it can touch tmux then.
"""
from qorqut.managed_runtime import mode
from qorqut_operations.contracts import DomainError

SWITCHING = 'Agents are being switched. Try again in a minute.'


def backend_of(control):
    backend = getattr(control, 'backend', None)
    if backend is None:
        raise DomainError('operation_unavailable', 'Terminal controller is not attached')
    return backend


def switching(control) -> bool:
    return mode.switching(getattr(control.runtime, 'paths', None))


def refuse_while_switching(control, action):
    """Start and restart open a session, which nothing may do while the installation is being switched (either mode)."""
    if action in ('start', 'restart') and switching(control):
        raise DomainError('runtime_switching', SWITCHING, retryable=True)


def snapshot(control, row):
    """The registered tmux terminal's observation: no ``managed`` key, so a plan prepared in the other mode never matches."""
    return backend_of(control).snapshot(row)


def validate(control, action, observed):
    """Start needs an absent terminal or an idle shell; every other action needs an existing one; ping needs its client."""
    backend_of(control).validate(action, observed)


def execute(control, target, action, message, binding, input_key):
    """Run one authorized action against the terminal. ``binding`` is the stored observation the plan was prepared against; the
    backend compares it with the terminal as it is now and refuses with ``revision_conflict`` when it changed."""
    return backend_of(control).execute(target, action, message, binding, input_key=input_key)
