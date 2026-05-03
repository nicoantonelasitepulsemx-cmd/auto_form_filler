"""v4 add-on features for ``auto_form_filler``.

This module bundles three optional escalation hooks that the recorder
and replay engine consult when their normal pipelines come up short.

* :func:`ai_heal` — invoke an LLM (OpenAI by default) to propose a
  CSS or Playwright selector when the deterministic resolver fails.
  Strictly opt-in: requires ``OPENAI_API_KEY`` in the environment, so
  recordings without the key fall back to the existing resolver
  behaviour (no-op).

* :func:`vision_match` — perceptual-hash tiebreaker for the case
  where the resolver returns more than one high-confidence candidate.
  Uses a downsampled grayscale dHash over a recorded element
  screenshot vs. each live candidate's bounding-box screenshot.
  Pure-Python, no PIL dependency (we operate directly on PNG bytes
  via a tiny built-in decoder for the small thumbnails we need).

* :func:`codegen_export` — render a captured ``actions`` array into
  a runnable Playwright Python script. Useful for hand-off /
  debugging / converting an auto_form_filler recording into a
  standalone scenario.

All three functions are pure where possible: they do not import
anything from ``recorder_v2`` / ``replay_engine`` so they can be
unit-tested without the full Playwright stack. The replay engine
opt-ins by importing the helpers and wiring them into its escalation
ladder (see ``replay_engine.py`` for the integration points).
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import textwrap
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from typing import Any, Iterable

__all__ = [
    "ai_heal",
    "AIHealResult",
    "vision_match",
    "codegen_export",
]


# ---------------------------------------------------------------------------
# 1. ai_heal — LLM-backed selector suggestion
# ---------------------------------------------------------------------------


@dataclass
class AIHealResult:
    """Selector recommendation returned by :func:`ai_heal`.

    Attributes
    ----------
    selector:
        A Playwright-compatible selector string (CSS, ``role=``,
        ``text=`` or ``xpath=``). ``None`` when the LLM did not
        produce a usable suggestion.
    rationale:
        A one-line explanation suitable for the replay log.
    raw:
        The full text the LLM returned, kept for debugging.
    """

    selector: str | None
    rationale: str
    raw: str

    @property
    def ok(self) -> bool:
        return bool(self.selector)


_DEFAULT_MODEL = os.environ.get("AUTOFORM_AI_HEAL_MODEL", "gpt-4o-mini")


def _ai_heal_prompt(
    *,
    fingerprint: dict | None,
    last_selector: str | None,
    snippet: str | None,
    failure_reason: str | None,
) -> str:
    fp_json = json.dumps(fingerprint or {}, ensure_ascii=False, indent=2)[:1500]
    snip = (snippet or "").strip()[:2500]
    return textwrap.dedent(
        f"""
        You are auto_form_filler's selector-healer. The deterministic
        resolver could not find a unique element for the action below.

        Recorded fingerprint (JSON):
        {fp_json}

        Last selector that was tried (and failed):
        {last_selector or "(none)"}

        Failure reason: {failure_reason or "no candidate matched threshold"}

        Page snippet around the suspected target (HTML):
        ---
        {snip}
        ---

        Reply with a single JSON object on one line:
            {{"selector": "...", "rationale": "..."}}

        The selector must be a Playwright-compatible string. Prefer in
        order:
          1. role=NAME (e.g. role=radio[name="I am the rights owner"])
          2. role-based + accessible name combination
          3. text=...
          4. CSS attribute selector
          5. xpath=...
        Return null for selector if you cannot pick one.
        """
    ).strip()


def ai_heal(
    *,
    fingerprint: dict | None = None,
    last_selector: str | None = None,
    snippet: str | None = None,
    failure_reason: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    timeout: float = 12.0,
) -> AIHealResult:
    """Ask an LLM for a selector when the resolver gives up.

    The function is a *no-op* when no API key is available — it
    returns an :class:`AIHealResult` with ``selector=None`` and a
    rationale explaining why, so callers can log it without
    branching.

    Parameters
    ----------
    fingerprint:
        The recorded element fingerprint (output of
        ``element_fingerprint.collect_fingerprint``).
    last_selector:
        The selector the resolver last tried (for debugging context).
    snippet:
        A small HTML snippet of the suspected target region.
    failure_reason:
        Optional human-readable reason the resolver gave up. Helps
        the LLM know whether to look for a slight rename, a moved
        element, etc.
    api_key:
        Defaults to ``OPENAI_API_KEY``. Pass explicitly for testing.
    model:
        Defaults to ``$AUTOFORM_AI_HEAL_MODEL`` or ``gpt-4o-mini``.
    timeout:
        HTTP timeout, seconds.
    """
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        return AIHealResult(
            selector=None,
            rationale="OPENAI_API_KEY not set; ai_heal is a no-op",
            raw="",
        )
    prompt = _ai_heal_prompt(
        fingerprint=fingerprint,
        last_selector=last_selector,
        snippet=snippet,
        failure_reason=failure_reason,
    )
    payload = {
        "model": model or _DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": "You are a Playwright selector expert."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 200,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        return AIHealResult(
            selector=None,
            rationale=f"ai_heal HTTP error: {exc}",
            raw="",
        )
    raw = ""
    try:
        raw = body["choices"][0]["message"]["content"].strip()
    except Exception:
        return AIHealResult(
            selector=None, rationale="ai_heal: malformed LLM response", raw=str(body)
        )

    text = raw
    # Strip markdown fences if the model wrapped JSON in ```...```.
    if text.startswith("```"):
        text = text.strip("`")
        # drop optional language tag (e.g. "json\n...")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    try:
        parsed = json.loads(text)
        sel = parsed.get("selector")
        if isinstance(sel, str) and sel.strip():
            return AIHealResult(
                selector=sel.strip(),
                rationale=str(parsed.get("rationale") or "ai_heal accepted"),
                raw=raw,
            )
    except Exception:
        pass
    return AIHealResult(
        selector=None,
        rationale="ai_heal: no usable selector in response",
        raw=raw,
    )


# ---------------------------------------------------------------------------
# 2. vision_match — perceptual hash tiebreaker
# ---------------------------------------------------------------------------


def _ihdr_size(png: bytes) -> tuple[int, int]:
    """Return (width, height) read from a PNG IHDR chunk."""
    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    # IHDR is the first chunk, length=13.
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def _decode_png_grayscale(png: bytes) -> tuple[int, int, list[list[int]]]:
    """Minimal PNG decoder returning a 2D grayscale matrix.

    Supports the subset Playwright's ``screenshot()`` produces: 8-bit
    RGB or RGBA, no interlace, single IDAT (or concatenated IDATs).
    Sufficient for the small thumbnails we hash; we deliberately
    avoid pulling Pillow as a dep.
    """
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    width = height = 0
    bit_depth = 0
    color_type = 0
    interlace = 0
    idat = bytearray()
    pos = 8
    while pos < len(png):
        length = struct.unpack(">I", png[pos : pos + 4])[0]
        ctype = png[pos + 4 : pos + 8]
        data = png[pos + 8 : pos + 8 + length]
        pos += 8 + length + 4  # skip CRC
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
                ">IIBBBBB", data
            )
        elif ctype == b"IDAT":
            idat.extend(data)
        elif ctype == b"IEND":
            break
    if interlace != 0:
        raise ValueError("interlaced PNGs not supported")
    if bit_depth != 8 or color_type not in (2, 6):
        raise ValueError(f"unsupported PNG: bit_depth={bit_depth} color={color_type}")
    raw = zlib.decompress(bytes(idat))
    bpp = 3 if color_type == 2 else 4
    stride = width * bpp + 1  # +1 for filter byte per row
    rows: list[list[int]] = []
    prev_row: list[int] | None = None
    for y in range(height):
        rstart = y * stride
        filter_type = raw[rstart]
        row = bytearray(raw[rstart + 1 : rstart + stride])
        if filter_type == 0:
            recon = list(row)
        elif filter_type == 1:  # Sub
            recon = [0] * len(row)
            for i in range(len(row)):
                left = recon[i - bpp] if i >= bpp else 0
                recon[i] = (row[i] + left) & 0xFF
        elif filter_type == 2:  # Up
            recon = [0] * len(row)
            for i in range(len(row)):
                up = prev_row[i] if prev_row is not None else 0
                recon[i] = (row[i] + up) & 0xFF
        elif filter_type == 3:  # Average
            recon = [0] * len(row)
            for i in range(len(row)):
                left = recon[i - bpp] if i >= bpp else 0
                up = prev_row[i] if prev_row is not None else 0
                recon[i] = (row[i] + (left + up) // 2) & 0xFF
        elif filter_type == 4:  # Paeth
            recon = [0] * len(row)
            for i in range(len(row)):
                left = recon[i - bpp] if i >= bpp else 0
                up = prev_row[i] if prev_row is not None else 0
                up_left = prev_row[i - bpp] if (prev_row is not None and i >= bpp) else 0
                p = left + up - up_left
                pa = abs(p - left)
                pb = abs(p - up)
                pc = abs(p - up_left)
                pred = left if pa <= pb and pa <= pc else (up if pb <= pc else up_left)
                recon[i] = (row[i] + pred) & 0xFF
        else:
            raise ValueError(f"unknown PNG filter type {filter_type}")
        prev_row = recon
        gray_row = []
        for x in range(width):
            r = recon[x * bpp]
            g = recon[x * bpp + 1]
            b = recon[x * bpp + 2]
            # Luminance approximation.
            gray_row.append((r * 299 + g * 587 + b * 114) // 1000)
        rows.append(gray_row)
    return width, height, rows


def _resize_nearest(
    grid: list[list[int]], src_w: int, src_h: int, dst_w: int, dst_h: int
) -> list[list[int]]:
    out = []
    for y in range(dst_h):
        sy = min(src_h - 1, (y * src_h) // dst_h)
        row = []
        for x in range(dst_w):
            sx = min(src_w - 1, (x * src_w) // dst_w)
            row.append(grid[sy][sx])
        out.append(row)
    return out


def _dhash(png_bytes: bytes, side: int = 9) -> str:
    """Return a 64-bit perceptual difference-hash of a PNG buffer.

    The hash is stable across small re-renders / minor pixel jitter
    because it compares neighbouring pixels rather than absolute
    intensities.
    """
    w, h, grid = _decode_png_grayscale(png_bytes)
    small = _resize_nearest(grid, w, h, side, side - 1)
    bits = []
    for row in small:
        for i in range(side - 1):
            bits.append("1" if row[i] > row[i + 1] else "0")
    binary = "".join(bits).ljust(64, "0")[:64]
    return f"{int(binary, 2):016x}"


def _hamming(a: str, b: str) -> int:
    if len(a) != len(b):
        raise ValueError("hash length mismatch")
    diff = int(a, 16) ^ int(b, 16)
    return bin(diff).count("1")


def vision_match(
    recorded_png: bytes, candidates_png: Iterable[bytes]
) -> tuple[int, int]:
    """Pick the candidate whose appearance most matches the recorded element.

    Returns ``(best_index, hamming_distance)``. ``best_index`` is the
    position in ``candidates_png`` (zero-based). Smaller hamming
    distance ⇒ closer visual match. Identical screenshots return
    distance 0.

    Raises ``ValueError`` for an empty candidates iterable.
    """
    rec_hash = _dhash(recorded_png)
    best: tuple[int, int] | None = None
    for i, cand in enumerate(candidates_png):
        try:
            d = _hamming(rec_hash, _dhash(cand))
        except Exception:
            d = 64  # treat decode errors as worst-possible match
        if best is None or d < best[1]:
            best = (i, d)
    if best is None:
        raise ValueError("vision_match: no candidates supplied")
    return best


def vision_hash(png_bytes: bytes) -> str:
    """Public alias around the internal dhash, for callers that want
    to cache an element's perceptual signature alongside its
    fingerprint."""
    return _dhash(png_bytes)


# ---------------------------------------------------------------------------
# 3. codegen_export — turn a recording into a Playwright script
# ---------------------------------------------------------------------------


def _selector_for_action(action: dict) -> str:
    """Pick the most stable Playwright selector for a recorded action."""
    fp = action.get("fingerprint") or {}
    sels = action.get("selectors") or {}
    # Prefer recorder-supplied stable selectors first.
    for key in ("data_testid", "id", "stable_id", "name", "role_name"):
        v = sels.get(key)
        if v:
            if key == "data_testid":
                return f"[data-testid='{v}']"
            if key == "id":
                return f"#{v}"
            if key == "stable_id":
                return f"[id='{v}']"
            if key == "name":
                return f"[name='{v}']"
            if key == "role_name":
                role, name = (v.split("|", 1) + [""])[:2]
                if name:
                    return f"role={role}[name='{_esc(name)}']"
                return f"role={role}"
    name = (fp.get("accessible_name") or "").strip()
    role = (fp.get("role") or "").strip()
    if role and name:
        return f"role={role}[name='{_esc(name)}']"
    if name:
        return f"text='{_esc(name)}'"
    tag = (fp.get("tag") or "").strip()
    typ = (fp.get("type") or "").strip()
    if tag and typ:
        return f"{tag}[type='{_esc(typ)}']"
    return tag or "*"


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def codegen_export(
    config: dict, *, scenario_name: str = "play_recording"
) -> str:
    """Render a recording into a stand-alone Playwright Python script.

    The generated script has no dependency on auto_form_filler — it
    just uses ``playwright.async_api``. This is purposely a *coarse*
    translation: each recorded action becomes one Playwright call,
    using the most stable selector available. Fine-grained
    self-healing logic stays in ``replay_engine.py``.
    """
    actions = config.get("actions") or []
    start_url = config.get("start_url") or ""

    lines: list[str] = []
    lines.append('"""Auto-generated by ai_features.codegen_export."""')
    lines.append("import asyncio")
    lines.append("from playwright.async_api import async_playwright")
    lines.append("")
    lines.append(f"START_URL = {start_url!r}")
    lines.append("")
    lines.append(f"async def {scenario_name}() -> None:")
    lines.append("    async with async_playwright() as pw:")
    lines.append("        browser = await pw.chromium.launch(headless=False)")
    lines.append("        ctx = await browser.new_context()")
    lines.append("        page = await ctx.new_page()")
    if start_url:
        lines.append("        await page.goto(START_URL)")
    for a in actions:
        kind = a.get("kind")
        sel = _selector_for_action(a)
        comment = (a.get("fingerprint") or {}).get("accessible_name") or a.get("field_id") or ""
        if comment:
            lines.append(f"        # {comment[:80]}")
        if kind == "fill":
            v = a.get("value") or ""
            lines.append(f"        await page.locator({sel!r}).fill({v!r})")
        elif kind == "check":
            desired = bool(a.get("checked", True))
            if desired:
                lines.append(f"        await page.locator({sel!r}).check()")
            else:
                lines.append(f"        await page.locator({sel!r}).uncheck()")
        elif kind == "click":
            lines.append(f"        await page.locator({sel!r}).click()")
        elif kind == "submit":
            lines.append(f"        await page.locator({sel!r}).click()")
        elif kind == "select":
            v = a.get("value") or ""
            lines.append(f"        await page.locator({sel!r}).select_option({v!r})")
        elif kind == "combobox":
            v = a.get("value") or ""
            lines.append(f"        await page.locator({sel!r}).click()")
            lines.append(f"        await page.get_by_role('option', name={v!r}).first.click()")
        elif kind == "set_files":
            lines.append(
                f"        await page.locator({sel!r}).set_input_files([])  # files: "
                f"{a.get('files') or []}"
            )
        else:
            lines.append(f"        # unknown action kind: {kind!r}")
        lines.append("        await page.wait_for_timeout(150)")
    lines.append("        await browser.close()")
    lines.append("")
    lines.append("if __name__ == '__main__':")
    lines.append(f"    asyncio.run({scenario_name}())")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Hash helper exposed for tests and callers wanting deterministic
# fingerprints of arbitrary bytes (NOT for visual matching).
# ---------------------------------------------------------------------------


def stable_hash(payload: Any) -> str:
    """Return a SHA-256 hex digest of any JSON-serializable payload."""
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
