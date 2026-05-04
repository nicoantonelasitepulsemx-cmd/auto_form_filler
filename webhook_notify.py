"""v4 add-on: send job-result notifications to Discord / Telegram / Slack.

Plugs into the worker pool / replay engine so a long-running batch
("mint 200 accounts overnight") posts a single summary message at the
end — and optionally a per-account update on failure. All three
providers use plain HTTP webhook URLs (no SDK), so this module ships
with zero new pip dependencies.

Routing model
~~~~~~~~~~~~~
* You construct one :class:`Notifier` per channel:

    >>> n = Notifier.discord("https://discord.com/api/webhooks/…")
    >>> n.send("All 200 accounts ✓ in 12m38s")

* For larger summaries, use :meth:`Notifier.send_summary` which
  formats counts, attaches an optional log link, and (for Discord
  only) renders a pretty embed with colour-coded status.
* :func:`broadcast` accepts a list of notifiers and dispatches the
  same payload to each — handy when you want the same message in two
  places (private DM + public team channel).

Failure handling
~~~~~~~~~~~~~~~~
None of the calls raise on HTTP failure: a notification is *never*
allowed to crash a long-running scrape. We collect failures into the
:class:`SendResult` so the caller can log them; the main task
continues regardless.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

__all__ = [
    "Notifier",
    "SendResult",
    "broadcast",
]


# --------------------------------------------------------------------------- result type

@dataclass
class SendResult:
    """Outcome of a single ``Notifier.send`` call.

    ``ok``: True when the provider returned a 2xx status. ``error``
    contains the human-readable reason on failure (URLError message,
    HTTP body) so the caller can include it in the run log.
    """
    ok: bool = False
    provider: str = ""
    status: Optional[int] = None
    error: str = ""
    body: str = ""


# --------------------------------------------------------------------------- low-level HTTP

def _post(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float = 15.0,
    extra_headers: Optional[dict[str, str]] = None,
) -> SendResult:
    """POST JSON. Catches network exceptions, never raises."""
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            status = resp.getcode()
        return SendResult(ok=200 <= (status or 0) < 300, status=status, body=text)
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return SendResult(ok=False, status=exc.code, error=str(exc), body=err_body)
    except urllib.error.URLError as exc:
        return SendResult(ok=False, error=f"network: {exc}")


def _post_form(
    url: str,
    fields: dict[str, Any],
    *,
    timeout: float = 15.0,
) -> SendResult:
    """POST application/x-www-form-urlencoded — Telegram likes this."""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            status = resp.getcode()
        return SendResult(ok=200 <= (status or 0) < 300, status=status, body=text)
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return SendResult(ok=False, status=exc.code, error=str(exc), body=err_body)
    except urllib.error.URLError as exc:
        return SendResult(ok=False, error=f"network: {exc}")


# --------------------------------------------------------------------------- formatting helpers

def _truncate(s: str, limit: int) -> str:
    """Hard cut a string with an ellipsis marker. Never raises."""
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)] + "…"


# Discord embed colours (decimal ints).
_DISCORD_COLOURS = {
    "ok":      0x22C55E,  # green-500
    "warning": 0xF59E0B,  # amber-500
    "error":   0xEF4444,  # red-500
    "info":    0x3B82F6,  # blue-500
}


# --------------------------------------------------------------------------- Notifier

@dataclass
class Notifier:
    """One destination channel.

    Construct via the ``discord`` / ``telegram`` / ``slack`` class
    methods so the right adapter is selected without the caller
    having to remember which payload shape goes where.

    Attributes
    ----------
    provider:
        ``"discord"``, ``"telegram"`` or ``"slack"``.
    url:
        Webhook URL (Discord/Slack) or Bot API base
        (``https://api.telegram.org/bot<TOKEN>``).
    chat_id:
        Telegram only — the channel/user numeric id.
    mention:
        Optional ``@user``/``@here`` snippet prepended to the
        message body.
    """
    provider: str
    url: str
    chat_id: str = ""
    mention: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ factories

    @classmethod
    def discord(cls, webhook_url: str, *, mention: str = "") -> "Notifier":
        return cls(provider="discord", url=webhook_url, mention=mention)

    @classmethod
    def telegram(cls, bot_token: str, chat_id: str | int, *, mention: str = "") -> "Notifier":
        return cls(
            provider="telegram",
            url=f"https://api.telegram.org/bot{bot_token}",
            chat_id=str(chat_id),
            mention=mention,
        )

    @classmethod
    def slack(cls, webhook_url: str, *, mention: str = "") -> "Notifier":
        return cls(provider="slack", url=webhook_url, mention=mention)

    # ------------------------------------------------------------------ public

    def send(self, text: str) -> SendResult:
        """Send a plain text message. Returns a :class:`SendResult`."""
        body = (self.mention + " " + text).strip() if self.mention else text
        if self.provider == "discord":
            # Discord rejects > 2000 chars on the ``content`` field.
            return _post(self.url, {"content": _truncate(body, 1990)}, extra_headers=self.extra_headers)
        if self.provider == "telegram":
            return _post_form(
                f"{self.url}/sendMessage",
                {
                    "chat_id": self.chat_id,
                    "text": _truncate(body, 4090),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
            )
        if self.provider == "slack":
            return _post(self.url, {"text": _truncate(body, 39000)}, extra_headers=self.extra_headers)
        return SendResult(ok=False, provider=self.provider, error=f"unknown provider {self.provider!r}")

    def send_summary(
        self,
        title: str,
        *,
        ok_count: int = 0,
        fail_count: int = 0,
        warn_count: int = 0,
        elapsed_s: float = 0.0,
        link: str = "",
        details: str = "",
    ) -> SendResult:
        """Send a formatted batch-job summary.

        For Discord this emits a coloured embed (green/amber/red on
        ok/warn/error). For Telegram and Slack it sends a plain-text
        block — they don't have a universal embed format so we keep
        it readable instead of provider-specific.
        """
        status = "ok" if fail_count == 0 else ("warning" if ok_count else "error")
        if self.provider == "discord":
            return self._discord_embed(
                title=title, ok=ok_count, fail=fail_count, warn=warn_count,
                elapsed_s=elapsed_s, link=link, details=details, status=status,
            )
        # Plain-text rendering for Telegram + Slack.
        lines = [f"**{title}**"]
        lines.append(
            f"✓ {ok_count}    ⚠ {warn_count}    ✗ {fail_count}    ⏱ {elapsed_s:.1f}s"
        )
        if link:
            lines.append(f"Logs: {link}")
        if details:
            lines.append("")
            lines.append(_truncate(details, 1500))
        return self.send("\n".join(lines))

    # ------------------------------------------------------------------ private

    def _discord_embed(
        self,
        *,
        title: str,
        ok: int,
        fail: int,
        warn: int,
        elapsed_s: float,
        link: str,
        details: str,
        status: str,
    ) -> SendResult:
        embed: dict[str, Any] = {
            "title": _truncate(title, 250),
            "color": _DISCORD_COLOURS.get(status, _DISCORD_COLOURS["info"]),
            "fields": [
                {"name": "✓ ok",      "value": str(ok),   "inline": True},
                {"name": "⚠ warning", "value": str(warn), "inline": True},
                {"name": "✗ fail",    "value": str(fail), "inline": True},
                {"name": "⏱ elapsed", "value": f"{elapsed_s:.1f}s", "inline": True},
            ],
        }
        if details:
            # Discord caps embed description at 4096 chars.
            embed["description"] = _truncate(details, 4000)
        if link:
            embed["url"] = link
        payload: dict[str, Any] = {"embeds": [embed]}
        if self.mention:
            payload["content"] = self.mention
        return _post(self.url, payload, extra_headers=self.extra_headers)


# --------------------------------------------------------------------------- broadcast

def broadcast(
    notifiers: Iterable[Notifier], text: str
) -> list[SendResult]:
    """Send ``text`` to every notifier; return per-channel results.

    Errors in one channel never affect another — each call is
    independent. Useful when you want the same alert mirrored to a
    private DM and a team channel.
    """
    results: list[SendResult] = []
    for n in notifiers:
        try:
            res = n.send(text)
        except Exception as exc:  # noqa: BLE001  (defensive: never crash batch)
            res = SendResult(ok=False, provider=n.provider, error=f"{type(exc).__name__}: {exc}")
        res.provider = n.provider
        results.append(res)
    return results
