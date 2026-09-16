"""
Email via Resend. Best effort: a delivery failure is logged, never raised into a
request, and never leaves the caller believing something was sent.
"""

import logging

import httpx

from ..core.config import settings

log = logging.getLogger("creai.mailer")

CODE_BODY = """Your CreAI sign-in code is {code}

It expires in 15 minutes and can only be used once.

If you didn't ask for this, you can ignore it — nobody can sign in without
the code.
"""


def _send(to: str, subject: str, text: str) -> bool:
    if not settings.resend_key:
        log.error("email not configured; %s to %s was not delivered", subject, to)
        return False
    try:
        r = httpx.post("https://api.resend.com/emails",
                       headers={"Authorization": f"Bearer {settings.resend_key}"},
                       json={"from": settings.mail_from, "to": [to],
                             "subject": subject, "text": text},
                       timeout=15)
        if r.status_code >= 300:
            log.error("resend rejected mail to %s: %s %s", to, r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        log.error("mail to %s failed: %s", to, e)
        return False


def send_code(to: str, code: str) -> bool:
    return _send(to, "Your CreAI sign-in code", CODE_BODY.format(code=code))


def send_domain_live(to: str, domain: str) -> bool:
    return _send(to, f"{domain} is live",
                 f"{domain} now resolves and its certificate is issued.\n\n"
                 f"https://{domain}\n")
