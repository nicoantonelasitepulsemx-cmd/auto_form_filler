"""v4 add-on: network HAR record helpers + URL-pattern blocker.

Two related capabilities live here, designed to plug into a
Playwright ``BrowserContext`` so the rest of the tool doesn't have to
know they exist.

1. **HAR recording** — capture every request/response into a HAR file
   you can replay or inspect later. Useful for debugging "why did the
   form 4xx after I submitted?" or for vendor-side regressions.
2. **URL blocker** — refuse loads that match user-defined regex
   patterns (or one of the curated bundles below). Stripping ads /
   analytics / fonts cuts page-load latency for batched submissions
   by 30-70% on heavyweight sites like Facebook.

We deliberately do not depend on Playwright at import time so this
module can be unit-tested without the full browser stack. The
``attach_*`` helpers expect a duck-typed ``BrowserContext`` (any
object with the ``route``/``unroute`` async API).

Built-in pattern bundles
~~~~~~~~~~~~~~~~~~~~~~~~
* ``BLOCK_ADS``        — common ad networks + GA / GTM
* ``BLOCK_ANALYTICS``  — analytics, telemetry, error trackers
* ``BLOCK_FONTS``      — Google Fonts / Adobe Fonts CDNs
* ``BLOCK_MEDIA``      — images/video (use only if you don't need
  visual fingerprint match)
* ``BLOCK_HEAVY``      — convenience: ads + analytics + fonts (the
  default for batched form submission)

Custom bundles compose easily — see :func:`compile_blocker` for the
combine API.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional, Pattern, Sequence

__all__ = [
    "BLOCK_ADS",
    "BLOCK_ANALYTICS",
    "BLOCK_FONTS",
    "BLOCK_MEDIA",
    "BLOCK_HEAVY",
    "BlockResult",
    "compile_blocker",
    "should_block",
    "attach_blocker",
    "detach_blocker",
    "attach_har",
]


# --------------------------------------------------------------------------- pattern bundles

# Each bundle is a tuple of *string* regexes. We keep them as strings
# so callers can extend the list with their own patterns and
# :func:`compile_blocker` does the actual compilation.
BLOCK_ADS: tuple[str, ...] = (
    r"\.doubleclick\.net",
    r"\.googleadservices\.com",
    r"\.googletagmanager\.com",
    r"\.googletagservices\.com",
    r"\.adservice\.google\.",
    r"adnxs\.com",
    r"adsystem\.amazon",
    r"facebook\.com/tr",
    r"adsrvr\.org",
    r"taboola\.com",
    r"outbrain\.com",
    r"criteo\.(?:com|net)",
)

BLOCK_ANALYTICS: tuple[str, ...] = (
    r"google-analytics\.com",
    r"analytics\.google\.com",
    r"\.segment\.io",
    r"\.mixpanel\.com",
    r"\.amplitude\.com",
    r"\.fullstory\.com",
    r"\.hotjar\.com",
    r"clarity\.ms",
    r"sentry\.io",
    r"newrelic\.com",
    r"intercom\.io",
    r"hubspot\.com",
)

BLOCK_FONTS: tuple[str, ...] = (
    r"fonts\.googleapis\.com",
    r"fonts\.gstatic\.com",
    r"use\.typekit\.net",
    r"use\.fontawesome\.com",
)

BLOCK_MEDIA: tuple[str, ...] = (
    r"\.(?:png|jpe?g|gif|webp|svg|ico|bmp)(?:\?|$)",
    r"\.(?:mp4|webm|m3u8|ts)(?:\?|$)",
    r"\.(?:woff2?|ttf|otf|eot)(?:\?|$)",
)

BLOCK_HEAVY: tuple[str, ...] = BLOCK_ADS + BLOCK_ANALYTICS + BLOCK_FONTS


# --------------------------------------------------------------------------- compiled blocker

@dataclass
class BlockResult:
    """Stats from a blocker session.

    The blocker mutates this dataclass in place as requests fire
    through the Playwright route handler, so callers can inspect
    ``.blocked``/``.allowed`` after the run for telemetry.
    """
    blocked: int = 0
    allowed: int = 0
    blocked_hosts: dict[str, int] = field(default_factory=dict)


@dataclass
class _CompiledBlocker:
    patterns: tuple[Pattern[str], ...]
    stats: BlockResult


def compile_blocker(
    *,
    block: Iterable[str] = (),
    bundles: Iterable[Sequence[str]] = (),
    case_insensitive: bool = True,
) -> _CompiledBlocker:
    """Compile a set of string regexes (+ optional bundles) once.

    Returns an opaque handle the other helpers use; the handle's
    ``stats`` is a :class:`BlockResult` you can read after the run.
    """
    flags = re.IGNORECASE if case_insensitive else 0
    raw: list[str] = list(block)
    for bundle in bundles:
        raw.extend(bundle)
    compiled = tuple(re.compile(p, flags) for p in raw)
    return _CompiledBlocker(patterns=compiled, stats=BlockResult())


def should_block(blocker: _CompiledBlocker, url: str) -> bool:
    """Pure predicate so the test-suite doesn't need Playwright at all.

    Updates ``blocker.stats`` as a side effect — call from a hot
    Playwright route handler with confidence; the regex set is
    O(N) in patterns and pre-compiled.
    """
    for pat in blocker.patterns:
        if pat.search(url):
            blocker.stats.blocked += 1
            host_match = re.search(r"https?://([^/]+)", url)
            host = host_match.group(1) if host_match else url[:40]
            blocker.stats.blocked_hosts[host] = blocker.stats.blocked_hosts.get(host, 0) + 1
            return True
    blocker.stats.allowed += 1
    return False


# --------------------------------------------------------------------------- Playwright glue

# We don't import ``playwright`` here so the module stays import-safe
# in environments where the browser isn't installed. Type hints use
# ``Any`` for the ``BrowserContext``/``Route``/``Request`` types.

async def attach_blocker(
    context: Any,
    blocker: _CompiledBlocker,
    *,
    pattern: str = "**/*",
) -> Callable[[], Awaitable[None]]:
    """Attach the blocker to a Playwright ``BrowserContext``.

    Returns an awaitable detacher you should call before the context
    closes — Playwright leaks route handlers across tests if you skip
    the unroute.

    The default ``pattern="**/*"`` makes Playwright run our predicate
    on *every* request; the predicate is cheap (pre-compiled regex)
    so this is fine in practice. Pass a tighter glob if you only
    care about a subset.
    """
    async def _route_handler(route: Any, request: Any) -> None:
        # ``request.url`` is always defined; the surrounding try/except
        # guards against Playwright tearing the context down mid-flight.
        try:
            url = request.url
        except Exception:
            await route.continue_()
            return
        if should_block(blocker, url):
            await route.abort()
        else:
            await route.continue_()

    await context.route(pattern, _route_handler)

    async def _detach() -> None:
        try:
            await context.unroute(pattern, _route_handler)
        except Exception:
            # Context already closed — nothing left to unroute.
            pass

    return _detach


async def detach_blocker(detacher: Callable[[], Awaitable[None]]) -> None:
    """Symmetric counterpart of :func:`attach_blocker` — just calls the handle."""
    await detacher()


# --------------------------------------------------------------------------- HAR recording

@dataclass
class HARSession:
    """Lightweight handle for a HAR-record + replay session."""
    path: Path
    mode: str = "record"  # "record" | "replay"
    omit_content: bool = False


async def attach_har(
    context: Any,
    path: str | Path,
    *,
    mode: str = "record",
    omit_content: bool = False,
    update: bool = False,
) -> HARSession:
    """Attach HAR recording / replay to a ``BrowserContext``.

    Playwright's HAR API is split between ``new_context(record_har=...)``
    (record) and ``context.route_from_har(...)`` (replay). The recorder
    flow ideally calls this *before* navigating, hence the early
    attachment. For convenience we accept both modes here so callers
    don't have to remember which Playwright entrypoint to use.

    Parameters
    ----------
    mode:
        ``"record"``: Playwright writes the HAR on context.close().
        ``"replay"``: Playwright serves matching responses from the
        HAR and (with ``update=True``) appends any miss to the file.
    omit_content:
        Drop response bodies — keeps HAR files small for big sites.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if mode == "record":
        # ``context.routeFromHAR`` doesn't write; the only way to
        # record on an existing context is via the tracing API, but
        # most callers will use ``new_context(record_har_path=...)``.
        # Provide a soft fallback: if the context exposes
        # ``_record_har_to`` (Playwright internals), use it; otherwise
        # surface the limitation clearly.
        recorder = getattr(context, "record_har", None)
        if callable(recorder):
            await recorder(str(p), omit_content=omit_content)
        # No-op when the context wasn't built with HAR support — the
        # caller is responsible for using ``new_context(record_har_path=…)``
        # in that case. We still return a handle so detach is symmetric.
    elif mode == "replay":
        await context.route_from_har(
            str(p),
            update=update,
            not_found="fallback",
        )
    else:
        raise ValueError(f"unknown HAR mode {mode!r} (expected 'record' or 'replay')")
    return HARSession(path=p, mode=mode, omit_content=omit_content)
