"""Load reviewed companions from an optional isolated test overlay."""
import os
from pathlib import Path


def host_overlay():
    overlay = os.environ.get('ATTENTION_TEST_OVERLAY')
    if not overlay:
        return
    import bazarlyk.agents
    import bazarlyk.agentic
    import bazarlyk.composition
    import bazarlyk.inbox
    import bazarlyk.operations_history
    root = Path(overlay)
    for package, directory in ((bazarlyk.agents, 'data/app/bazarlyk/agents'),
                               (bazarlyk.agents, 'clock/app/bazarlyk/agents'),
                               (bazarlyk.composition, 'data/app/bazarlyk/composition'),
                               (bazarlyk.inbox, 'data/app/bazarlyk/inbox'),
                               (bazarlyk.operations_history, 'data/app/bazarlyk/operations_history'),
                               (bazarlyk.agentic, 'workflow/app/bazarlyk/agentic')):
        package.__path__.insert(0, str(root / directory))


def companions():
    overlay = os.environ.get('ATTENTION_TEST_OVERLAY')
    if overlay:
        import bazarlyk.agents
        import bazarlyk.composition
        root = Path(overlay)
        bazarlyk.agents.__path__.extend([str(root / 'data/app/bazarlyk/agents'),
                                        str(root / 'clock/app/bazarlyk/agents')])
        bazarlyk.composition.__path__.append(str(root / 'data/app/bazarlyk/composition'))
    from bazarlyk.agents.attention import Attention
    from bazarlyk.composition.attention import SCHEMA
    return Attention, SCHEMA
