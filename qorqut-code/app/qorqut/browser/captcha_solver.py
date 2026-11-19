"""The paid captcha solver: one captcha's details in, its answer token out.

Anti-Captcha's protocol is two JSON calls over HTTPS: ``createTask`` returns a task number, ``getTaskResult`` is asked
until the task is ready. Each ready task costs a fraction of a cent, charged to the owner's account there.

The owner's key authenticates both calls, so it travels in their bodies and nowhere else. Nothing here logs, and every
failure is raised as a ``DomainError`` whose text is written here: no reply text, address or underlying exception is
passed on, because any of them could carry the key. Only the service's own error code is, and only in capital letters,
which a key cannot be made of.

The task names and fields are Anti-Captcha's as its public documentation gave them when this was written; they have not
been run against the service (see ``docs/BROWSER-CONTROL.md``).
"""

from __future__ import annotations

import json
import re
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from qorqut_operations.contracts import DomainError

KEY, PROVIDER = "CAPTCHA_SOLVER_API_KEY", "CAPTCHA_SOLVER_PROVIDER"
# One service today. The Settings variable exists, so a name that is not served is refused rather than ignored.
SERVICES = {"anticaptcha": "https://api.anti-captcha.com"}
SOLVE_LIMIT = 120        # Seconds one captcha may take at the service. Its own median is well under a minute.
POLL = 3                 # Seconds between questions about a task, as the service asks of its clients.
REQUEST_LIMIT = 15       # Seconds for one HTTPS call.
REPLY_LIMIT = 65536      # Bytes of one reply; a token is a few thousand characters.
V3_SCORE = 0.7           # The reCAPTCHA v3 score asked for. The service offers 0.3, 0.7 and 0.9; most sites pass at 0.7.

_CODE = re.compile(r"[A-Z][A-Z_]{2,59}")
_COST = re.compile(r"\d{1,6}(\.\d{1,8})?")
# What the service's error codes mean for the owner. Anything else is reported with its code.
REASONS = {
    "captcha_solver_key_refused": "The captcha solver refused the key saved in Settings (CAPTCHA_SOLVER_API_KEY).",
    "captcha_solver_no_balance": "The captcha solver account has no money left.",
    "captcha_solver_unsolved": "The captcha solver could not solve this captcha.",
    "captcha_solver_unsupported": "The captcha solver does not solve this kind of captcha.",
    "captcha_solver_timeout": "The captcha solver did not answer in time.",
    "captcha_solver_unreachable": "The captcha solver could not be reached.",
    "captcha_solver_no_key": "No captcha solver key is saved in Settings (CAPTCHA_SOLVER_API_KEY).",
    "captcha_solver_unknown": "The captcha solver named in Settings (CAPTCHA_SOLVER_PROVIDER) is not one Qorqbot can use; "
                              "only anticaptcha is.",
}
_ERRORS = {
    "ERROR_KEY_DOES_NOT_EXIST": "captcha_solver_key_refused", "ERROR_ACCOUNT_SUSPENDED": "captcha_solver_key_refused",
    "ERROR_IP_NOT_ALLOWED": "captcha_solver_key_refused", "ERROR_IP_BLOCKED": "captcha_solver_key_refused",
    "ERROR_ZERO_BALANCE": "captcha_solver_no_balance",
    "ERROR_CAPTCHA_UNSOLVABLE": "captcha_solver_unsolved", "ERROR_RECAPTCHA_TIMEOUT": "captcha_solver_unsolved",
    "ERROR_NO_SLOT_AVAILABLE": "captcha_solver_unsolved", "ERROR_FAILED_LOADING_WIDGET": "captcha_solver_unsolved",
    "ERROR_RECAPTCHA_INVALID_SITEKEY": "captcha_solver_unsolved", "ERROR_RECAPTCHA_INVALID_DOMAIN": "captcha_solver_unsolved",
    "ERROR_TOKEN_EXPIRED": "captcha_solver_unsolved",
    "ERROR_TASK_NOT_SUPPORTED": "captcha_solver_unsupported",
}


def _failure(code):
    return DomainError(code, REASONS[code])


class _NoRedirect(HTTPRedirectHandler):
    """The key goes to the address written above and to no address a reply names."""

    def redirect_request(self, *_arguments, **_options):
        return None


def _post(url, body, timeout):
    """One JSON call. No proxy from the environment: the key must not pass through whatever a shell happened to export."""
    request = Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout) as response:
        return json.loads(response.read(REPLY_LIMIT + 1)[:REPLY_LIMIT])


def task(kind, url, sitekey, *, invisible=False, enterprise=False, action=None, cdata=None):
    """The service's description of one captcha. ``kind`` is recaptcha, recaptcha_v3, hcaptcha or turnstile."""
    described = {"websiteURL": url, "websiteKey": sitekey}
    if kind == "recaptcha_v3":
        return {**described, "type": "RecaptchaV3TaskProxyless", "minScore": V3_SCORE, "isEnterprise": bool(enterprise),
                **({"pageAction": action} if action else {})}
    if kind == "turnstile":
        extra = {name: value for name, value in (("action", action), ("turnstileCData", cdata)) if value}
        return {**described, "type": "TurnstileTaskProxyless", **extra}
    if kind == "hcaptcha":
        return {**described, "type": "HCaptchaTaskProxyless", **({"isInvisible": True} if invisible else {})}
    if kind == "recaptcha":
        name = "RecaptchaV2EnterpriseTaskProxyless" if enterprise else "RecaptchaV2TaskProxyless"
        return {**described, "type": name, **({"isInvisible": True} if invisible and not enterprise else {})}
    raise _failure("captcha_solver_unsupported")


def _ask(post, url, body, timeout):
    """One call's reply, or the failure it stands for. Every exception ends here, so none carries the key further."""
    try:
        reply = post(url, body, timeout)
    except Exception:
        raise _failure("captcha_solver_unreachable") from None
    if not isinstance(reply, dict):
        raise _failure("captcha_solver_unreachable")
    if reply.get("errorId"):
        code = reply.get("errorCode")
        code = code if isinstance(code, str) and _CODE.fullmatch(code) else "ERROR_UNKNOWN"
        if code in _ERRORS:
            raise _failure(_ERRORS[code])
        raise DomainError("captcha_solver_error", f"The captcha solver answered with an error ({code}).")
    return reply


def solve(described, *, key, provider=None, wait=SOLVE_LIMIT, post=None, clock=time.monotonic, sleep=time.sleep):
    """The answer token for one captcha and what the service charged for it, as ``(token, cost or None)``.

    Bounded twice: ``wait`` seconds in all, and one question every ``POLL`` seconds within them. A task that is not ready
    in time is left at the service; it may still be charged."""
    if not key:
        raise _failure("captcha_solver_no_key")
    base = SERVICES.get((provider or "anticaptcha").strip().lower())
    if base is None:
        raise _failure("captcha_solver_unknown")
    post, deadline = post or _post, clock() + wait

    def remaining():
        left = deadline - clock()
        if left <= 0:
            raise _failure("captcha_solver_timeout")
        return min(REQUEST_LIMIT, left)

    number = _ask(post, base + "/createTask", {"clientKey": key, "task": described}, remaining()).get("taskId")
    if isinstance(number, bool) or not isinstance(number, (int, str)) or not number:
        raise _failure("captcha_solver_unreachable")
    while True:
        remaining()
        sleep(POLL)
        reply = _ask(post, base + "/getTaskResult", {"clientKey": key, "taskId": number}, remaining())
        if reply.get("status") != "ready":
            continue
        solution = reply.get("solution") if isinstance(reply.get("solution"), dict) else {}
        token = solution.get("gRecaptchaResponse") or solution.get("token")
        if not isinstance(token, str) or not token:
            raise _failure("captcha_solver_unsolved")
        cost = str(reply.get("cost", ""))
        return token, float(cost) if _COST.fullmatch(cost) else None
