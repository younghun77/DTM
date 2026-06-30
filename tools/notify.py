"""
notify.py - Microsoft Teams notification helper (Incoming Webhook)
==================================================================

Sends a failure notification to a Microsoft Teams channel using an
*Incoming Webhook* URL. No external dependencies: the POST is done with
the standard-library ``urllib`` so it works in the factory kit as-is.

Configuration (no secrets in code)
----------------------------------
The webhook URL is read, in priority order, from:

  1) Environment variable ``DTM_TEAMS_WEBHOOK``
  2) A ``notify.ini`` file located next to this script (or in CWD):

        [teams]
        webhook = https://outlook.office.com/webhook/....

If no URL is configured, :func:`notify_failure` silently does nothing and
returns ``False`` - notifications are an optional add-on and must never
break the test loop.

Teams webhook note
------------------
Both the classic Office365 "Incoming Webhook" connector URL and a Power
Automate "Workflows" HTTP-trigger URL accept a JSON body. This helper
sends a MessageCard payload, which the classic connector renders as a
card. For a Workflows URL the same JSON is delivered as the trigger body.
"""
from __future__ import annotations

import configparser
import json
import os
import urllib.error
import urllib.request

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def get_webhook_url() -> str:
    """Return the configured Teams webhook URL, or ``""`` if unset."""
    url = os.environ.get("DTM_TEAMS_WEBHOOK", "").strip()
    if url:
        return url
    for base in (os.getcwd(), _SCRIPT_DIR):
        path = os.path.join(base, "notify.ini")
        if os.path.isfile(path):
            cfg = configparser.ConfigParser()
            try:
                cfg.read(path, encoding="utf-8")
                val = cfg.get("teams", "webhook", fallback="").strip()
            except (configparser.Error, OSError):
                val = ""
            if val and not val.startswith("<"):
                return val
    return ""


def _build_card(title: str, summary: str,
                facts: "list[tuple[str, str]]",
                text: str = "",
                theme: str = "D7263D") -> dict:
    """Build a legacy MessageCard payload (classic Incoming Webhook).

    ``theme`` is the card's accent colour (hex, no '#'); defaults to red for
    failures - pass a green value (e.g. ``2DA44E``) for success cards.
    """
    return {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": theme,
        "summary": summary,
        "sections": [{
            "activityTitle": title,
            "facts": [{"name": n, "value": v} for n, v in facts],
            "markdown": True,
            "text": text,
        }],
    }


def _build_adaptive(title: str,
                    facts: "list[tuple[str, str]]",
                    text: str = "",
                    color: str = "Attention") -> dict:
    """Build a Power Automate "Workflows" Adaptive Card message payload.

    ``color`` is an Adaptive Card text colour (``Attention`` for failure,
    ``Good`` for success).
    """
    body: list[dict] = [{
        "type": "TextBlock",
        "size": "Large",
        "weight": "Bolder",
        "color": color,
        "wrap": True,
        "text": title,
    }, {
        "type": "FactSet",
        "facts": [{"title": n, "value": v} for n, v in facts],
    }]
    if text:
        body.append({"type": "TextBlock", "wrap": True,
                     "isSubtle": True, "text": text})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "type": "AdaptiveCard",
                "version": "1.4",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "body": body,
            },
        }],
    }


def _is_workflows_url(url: str) -> bool:
    """True for Power Automate "Workflows" HTTP-trigger URLs (Adaptive Card),
    False for classic Office365 Incoming Webhook connector URLs (MessageCard).
    """
    u = url.lower()
    return ("powerplatform.com" in u
            or "powerautomate" in u
            or "logic.azure.com" in u
            or "azure-apihub.net" in u)


def post_teams(payload: dict, url: str = "", timeout: float = 10.0) -> bool:
    """POST a JSON ``payload`` to the Teams webhook.

    Returns ``True`` on HTTP 2xx, ``False`` otherwise. Never raises - all
    network/HTTP errors are swallowed so the caller's test loop is safe.
    """
    url = (url or get_webhook_url()).strip()
    if not url:
        return False
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def notify_failure(*,
                   reason: str,
                   test_index: "int | str" = "",
                   iteration: "int | str" = "",
                   channel: "int | str" = "",
                   length: "int | str" = "",
                   dut_version: str = "",
                   findings: "list[str] | None" = None,
                   summary_path: str = "",
                   url: str = "") -> bool:
    """Send a DTM failure card to Teams.

    All fields are optional; only what is provided is shown. Returns
    ``True`` when the message was accepted by Teams, ``False`` when no
    webhook is configured or the POST failed.
    """
    facts: list[tuple[str, str]] = [("Reason", str(reason))]
    if test_index != "":
        facts.append(("Test index", str(test_index)))
    if iteration != "":
        facts.append(("Iteration", str(iteration)))
    if channel != "":
        facts.append(("Channel", str(channel)))
    if length != "":
        facts.append(("Length", str(length)))
    if dut_version:
        facts.append(("DUT VERSION", dut_version))

    text_lines: list[str] = []
    if findings:
        text_lines.append("**Findings:**")
        for f in findings[:10]:
            text_lines.append(f"- {f}")
        if len(findings) > 10:
            text_lines.append(f"- ...(+{len(findings) - 10} more)")
    if summary_path:
        text_lines.append(f"\n_Summary file:_ `{summary_path}`")

    target = (url or get_webhook_url()).strip()
    if not target:
        return False
    return _send_card(
        title="\u26a0\ufe0f DTM RX test FAILED",
        summary=f"DTM failure: {reason}",
        facts=facts,
        text="\n".join(text_lines),
        theme="D7263D", color="Attention",
        url=target)


def _send_card(*, title: str, summary: str,
               facts: "list[tuple[str, str]]",
               text: str = "",
               theme: str = "D7263D",
               color: str = "Attention",
               url: str = "") -> bool:
    """Pick the right payload format for the webhook and POST it."""
    target = (url or get_webhook_url()).strip()
    if not target:
        return False
    if _is_workflows_url(target):
        payload = _build_adaptive(title=title, facts=facts,
                                  text=text, color=color)
    else:
        payload = _build_card(title=title, summary=summary,
                              facts=facts, text=text, theme=theme)
    return post_teams(payload, url=target)


def notify_success(*,
                   iterations: "int | str",
                   channel: "int | str" = "",
                   length: "int | str" = "",
                   dut_version: str = "",
                   url: str = "") -> bool:
    """Send a DTM "all iterations passed" card to Teams.

    Sent when the AUTO loop completes the configured number of iterations
    with no failure. No-op (returns ``False``) when no webhook is set.
    """
    facts: list[tuple[str, str]] = [
        ("Result", "PASS"),
        ("Iterations", str(iterations)),
    ]
    if channel != "":
        facts.append(("Channel", str(channel)))
    if length != "":
        facts.append(("Length", str(length)))
    if dut_version:
        facts.append(("DUT VERSION", dut_version))
    return _send_card(
        title="\u2705 DTM RX test PASSED",
        summary=f"DTM success: {iterations} iterations",
        facts=facts,
        text=f"All {iterations} iteration(s) completed with no failure.",
        theme="2DA44E", color="Good",
        url=url)


if __name__ == "__main__":
    # Manual smoke test: `python notify.py`
    ok = notify_failure(
        reason="rx_count==0",
        test_index=1, iteration=3, channel=19, length=37,
        dut_version="Test OS 1.2.3",
        findings=["bt_test.log: '[ERROR][BT]' -> sample error line"],
        summary_path=r"D:\factory\26-06-30\logs\...\FAILURE_SUMMARY.txt")
    print("failure sent:", ok)
    ok2 = notify_success(iterations=50, channel=19, length=37,
                         dut_version="Test OS 1.2.3")
    print("success sent:", ok2, "(configured:", bool(get_webhook_url()), ")")
