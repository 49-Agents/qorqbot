"""One scripted model call through the Claude Code CLI: no tools, no settings, no project instructions, nothing saved.

The CLI is used instead of the API client because the agents themselves run through it, on the same sign-in.
"""
import json
import subprocess
import tempfile
import time

AGENT_MODEL = "claude-sonnet-5-5"       # what the agents run on: writes handoffs and answers as the woken agent
REVIEW_MODEL = "claude-opus-5-5"        # drafts the questions and judges the answers
CLI_TRIES, CLI_WAIT = 10, 30


class ModelError(RuntimeError):
    pass


def ask(system, prompt, model=AGENT_MODEL, timeout=1200):
    """Reply text and cost in dollars. Raises ModelError when the call fails or another model served it."""
    command = ["claude", "-p", "--model", model, "--output-format", "json", "--tools", "", "--setting-sources", "",
               "--strict-mcp-config", "--no-session-persistence", "--system-prompt", system]
    for attempt in range(CLI_TRIES):
        try:
            done = subprocess.run(command, input=prompt, capture_output=True, text=True, timeout=timeout,
                                  cwd=tempfile.gettempdir())
            reply = json.loads(done.stdout)
            break
        except OSError as error:            # the CLI is missing for a minute or two while it updates itself
            if attempt == CLI_TRIES - 1:
                raise ModelError(f"the claude command cannot be started: {error}") from error
            time.sleep(CLI_WAIT)
        except (subprocess.TimeoutExpired, ValueError) as error:
            raise ModelError(f"{model}: {error}") from error
    if reply.get("is_error") or not isinstance(reply.get("result"), str):
        raise ModelError(f"{model}: {str(reply.get('result'))[:300]}")
    served = [name for name in reply.get("modelUsage") or {} if name.startswith(model)]
    if not served:
        raise ModelError(f"{model} was asked, {list(reply.get('modelUsage') or {})} answered")
    return reply["result"], float(reply.get("total_cost_usd") or 0)


def ask_json(system, prompt, model=AGENT_MODEL):
    """Like ask(), for a reply that is one JSON value, with or without a code fence around it."""
    text, cost = ask(system, prompt + "\n\nReply with the JSON only.", model)
    starts = [at for at in (text.find("{"), text.find("[")) if at >= 0]
    end = max(text.rfind("}"), text.rfind("]"))
    try:
        return json.loads(text[min(starts):end + 1]), cost
    except ValueError as error:
        raise ModelError(f"{model} did not return JSON: {text[:300]}") from error
