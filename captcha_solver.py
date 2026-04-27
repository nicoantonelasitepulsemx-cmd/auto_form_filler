"""captcha_solver.py — free, offline best-effort CAPTCHA auto-solver.

Two strategies, no API costs:

1. **Math/text challenge** (e.g. ``5 + 3 = ?``) — parsed and evaluated
   locally via regex + ``eval`` on a clean expression. ~99% on simple
   shapes, 0 ms, 0 cost.

2. **reCAPTCHA v2 audio bypass** (the "Buster" approach) — switch the
   challenge to its audio variant, download the mp3, transcribe it with
   ``faster-whisper`` (tiny English model, ~75 MB, runs on CPU), and type
   the answer. Realistic success rate ~50-70% — Google sometimes blocks
   the audio button when bot patterns are obvious, in which case the
   caller falls back to ``pause_for_human``.

We deliberately do NOT try to solve hCaptcha, Cloudflare Turnstile, or
FunCaptcha — they explicitly fingerprint the browser and the open-source
audio/visual solvers are unreliable.

The Whisper model is loaded lazily on the first audio solve, so adding
this module to the project doesn't slow down imports for users who don't
opt in (``--captcha-solver=off`` is the default).
"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from playwright.async_api import FrameLocator, Locator, Page


_SOLVABLE_KINDS = {"recaptcha-v2", "image-captcha", "text-challenge"}


# --------------------------------------------------------------------------- #
#  Math / text challenges
# --------------------------------------------------------------------------- #

_DIGIT_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "không": 0, "một": 1, "hai": 2, "ba": 3, "bốn": 4, "năm": 5,
    "sáu": 6, "bảy": 7, "tám": 8, "chín": 9, "mười": 10,
}
_OP_WORDS = {
    "plus": "+", "minus": "-", "times": "*", "multiplied": "*",
    "divided": "/", "cộng": "+", "trừ": "-", "nhân": "*", "chia": "/",
}


def _try_solve_math(text: str) -> Optional[str]:
    """Solve a small arithmetic challenge expressed in plain text.

    Returns the answer string, or ``None`` if the input doesn't look like
    one of the supported shapes.
    """
    s = text.lower().strip().rstrip("?=.")
    m = re.search(r"(-?\d+)\s*([+\-*/x×])\s*(-?\d+)", s)
    if m:
        a, op, b = m.group(1), m.group(2), m.group(3)
        op = {"x": "*", "×": "*"}.get(op, op)
        try:
            return str(int(eval(f"{a}{op}{b}")))  # noqa: S307 — known operands
        except Exception:
            return None
    tokens = re.split(r"\s+", s)
    nums: list[int] = []
    op: Optional[str] = None
    for tok in tokens:
        if tok in _DIGIT_WORDS:
            nums.append(_DIGIT_WORDS[tok])
        elif tok in _OP_WORDS:
            op = _OP_WORDS[tok]
    if len(nums) == 2 and op:
        try:
            return str(int(eval(f"{nums[0]}{op}{nums[1]}")))  # noqa: S307
        except Exception:
            return None
    return None


async def _grab_text_around_inputs(page: Page) -> str:
    """Snapshot the visible text near every CAPTCHA-named input."""
    sel = (
        "input[name*='captcha' i], input[id*='captcha' i], "
        "input[placeholder*='captcha' i]"
    )
    js = """
        sel => {
            const out = [];
            for (const el of document.querySelectorAll(sel)) {
                const block = el.closest('label, fieldset, div, form') || el.parentElement;
                if (block) out.push((block.innerText || '').trim());
            }
            return out.join('\\n---\\n');
        }
    """
    try:
        return (await page.evaluate(js, sel)) or ""
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
#  reCAPTCHA v2 — audio bypass
# --------------------------------------------------------------------------- #

# Model handle is shared across calls so we don't reload tiny.en for every
# challenge (loading takes ~2-3s).
_WHISPER_MODEL = None


def _get_whisper_model():
    """Lazy-load and cache the faster-whisper tiny English model."""
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel
        # tiny.en is ~75 MB, English only, fastest on CPU. int8 quantization
        # cuts both download size and inference latency further.
        _WHISPER_MODEL = WhisperModel(
            "tiny.en", device="cpu", compute_type="int8",
        )
    return _WHISPER_MODEL


async def _whisper_transcribe(mp3_bytes: bytes) -> str:
    """Run faster-whisper on raw mp3 bytes. Returns the transcribed text."""
    def _run() -> str:
        model = _get_whisper_model()
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fh:
            fh.write(mp3_bytes)
            tmp_path = fh.name
        try:
            segments, _info = model.transcribe(
                tmp_path, language="en", beam_size=1, best_of=1,
            )
            return " ".join(s.text.strip() for s in segments).strip()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return await asyncio.get_running_loop().run_in_executor(None, _run)


_DIGIT_TO_WORD = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}


def _clean_transcription(text: str) -> str:
    """Normalize Whisper output for reCAPTCHA's matcher.

    reCAPTCHA's audio answer field accepts lowercase letters/digits with
    spaces between words. We:
        * lowercase
        * replace ``-`` and other punctuation with whitespace
        * convert standalone digits back to words (Whisper-tiny often
          outputs ``2`` when the audio said "two"; reCAPTCHA expects the
          spoken form)
        * collapse whitespace
    """
    s = text.lower().strip()
    s = s.replace("-", " ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    parts = []
    for tok in s.split():
        if tok.isdigit() and len(tok) == 1:
            parts.append(_DIGIT_TO_WORD[tok])
        elif tok.isdigit():
            parts.extend(_DIGIT_TO_WORD[c] for c in tok)
        else:
            parts.append(tok)
    return " ".join(parts).strip()


def _recaptcha_anchor_iframe(page: Page) -> Optional[FrameLocator]:
    """Return the FrameLocator for the small "I'm not a robot" checkbox iframe."""
    try:
        anchor = page.frame_locator(
            "iframe[src*='recaptcha/api2/anchor'], "
            "iframe[src*='recaptcha/enterprise/anchor']"
        ).first
        return anchor
    except Exception:
        return None


def _recaptcha_challenge_iframe(page: Page) -> Optional[FrameLocator]:
    """Return the FrameLocator for the popup challenge iframe (image grid / audio)."""
    try:
        return page.frame_locator(
            "iframe[src*='recaptcha/api2/bframe'], "
            "iframe[src*='recaptcha/enterprise/bframe']"
        ).first
    except Exception:
        return None


async def _click_anchor_checkbox(page: Page, logger) -> bool:
    """Tick the 'I'm not a robot' checkbox to surface the challenge popup."""
    anchor = _recaptcha_anchor_iframe(page)
    if anchor is None:
        return False
    try:
        cb = anchor.locator("#recaptcha-anchor")
        await cb.wait_for(state="visible", timeout=5000)
        await cb.click()
        return True
    except Exception as exc:
        logger.warning(f"[CAPTCHA-SOLVER] couldn't click reCAPTCHA anchor: {exc}")
        return False


async def _solve_recaptcha_v2_audio(
    page: Page,
    logger,
    *,
    max_attempts: int = 3,
) -> bool:
    """Solve reCAPTCHA v2 by switching to audio + Whisper.

    Returns True if the challenge appears to have been passed (we type the
    transcript and click Verify; reCAPTCHA hides the iframe on success).
    """
    if not await _click_anchor_checkbox(page, logger):
        return False
    # Some sites pass on a simple click without a popup.
    await asyncio.sleep(1.2)
    anchor = _recaptcha_anchor_iframe(page)
    if anchor is not None:
        try:
            checked = await anchor.locator(
                "#recaptcha-anchor[aria-checked='true']"
            ).count()
            if checked > 0:
                logger.info("[CAPTCHA-SOLVER] reCAPTCHA passed on anchor click — no challenge")
                return True
        except Exception:
            pass

    challenge = _recaptcha_challenge_iframe(page)
    if challenge is None:
        logger.warning("[CAPTCHA-SOLVER] no challenge iframe appeared")
        return False

    # Switch to audio. The button id is stable across regions/locales.
    try:
        audio_btn = challenge.locator("#recaptcha-audio-button")
        await audio_btn.wait_for(state="visible", timeout=8000)
        await audio_btn.click()
    except Exception as exc:
        logger.warning(f"[CAPTCHA-SOLVER] couldn't click audio button: {exc}")
        return False

    # Give the audio panel a moment to load OR for Google to swap in the
    # "Try again later" lockout panel (it kicks in on automated UAs and
    # datacenter IPs almost immediately).
    await asyncio.sleep(1.5)
    try:
        for phrase in (
            "Try again later",
            "automated queries",
            "Your computer or network may be sending",
        ):
            if await challenge.get_by_text(phrase, exact=False).count() > 0:
                logger.warning(
                    f"[CAPTCHA-SOLVER] Google blocked audio bypass on this "
                    f"IP/UA ({phrase!r}) — fallback to pause-for-human"
                )
                return False
    except Exception:
        pass

    for attempt in range(1, max_attempts + 1):
        try:
            audio_src_loc = challenge.locator("audio#audio-source")
            await audio_src_loc.wait_for(state="attached", timeout=10000)
            mp3_url = await audio_src_loc.get_attribute("src")
        except Exception as exc:
            logger.warning(
                f"[CAPTCHA-SOLVER] couldn't read audio src (attempt {attempt}): {exc}"
            )
            return False
        if not mp3_url:
            logger.warning("[CAPTCHA-SOLVER] empty audio src")
            return False

        # Use Playwright's request context so cookies/headers travel correctly.
        try:
            resp = await page.context.request.get(mp3_url, timeout=20000)
            if not resp.ok:
                logger.warning(
                    f"[CAPTCHA-SOLVER] audio download failed: HTTP {resp.status}"
                )
                return False
            mp3_bytes = await resp.body()
        except Exception as exc:
            logger.warning(f"[CAPTCHA-SOLVER] audio download exc: {exc}")
            return False
        logger.info(
            f"[CAPTCHA-SOLVER] downloaded audio challenge ({len(mp3_bytes)} bytes), "
            f"transcribing with Whisper tiny.en"
        )

        try:
            text = await _whisper_transcribe(mp3_bytes)
        except Exception as exc:
            logger.warning(f"[CAPTCHA-SOLVER] Whisper failed: {exc!r}")
            return False
        cleaned = _clean_transcription(text)
        logger.info(f"[CAPTCHA-SOLVER] heard: {cleaned!r}")
        if not cleaned:
            # Click the 'Get a new challenge' reload button and retry.
            try:
                await challenge.locator("#recaptcha-reload-button").click()
                await asyncio.sleep(1.0)
                continue
            except Exception:
                return False

        try:
            ans_input = challenge.locator("#audio-response")
            await ans_input.fill(cleaned)
            await challenge.locator("#recaptcha-verify-button").click()
        except Exception as exc:
            logger.warning(f"[CAPTCHA-SOLVER] couldn't submit audio answer: {exc}")
            return False

        # Wait briefly for verify to complete.
        await asyncio.sleep(2.5)

        # Success heuristics: the bframe collapses or the anchor flips to
        # aria-checked=true. We probe both.
        anchor = _recaptcha_anchor_iframe(page)
        if anchor is not None:
            try:
                checked = await anchor.locator(
                    "#recaptcha-anchor[aria-checked='true']"
                ).count()
                if checked > 0:
                    logger.info(
                        f"[CAPTCHA-SOLVER] reCAPTCHA passed on attempt {attempt}"
                    )
                    return True
            except Exception:
                pass

        # Failed: try again with a fresh audio sample.
        try:
            err = challenge.get_by_text(
                "Multiple correct solutions required", exact=False
            )
            if await err.count() > 0:
                logger.info(
                    "[CAPTCHA-SOLVER] reCAPTCHA wants another sample "
                    f"(attempt {attempt})"
                )
                continue
        except Exception:
            pass
        # Generic failure → reload and retry once.
        try:
            await challenge.locator("#recaptcha-reload-button").click()
            await asyncio.sleep(1.0)
        except Exception:
            return False

    logger.warning(
        f"[CAPTCHA-SOLVER] gave up after {max_attempts} audio attempt(s)"
    )
    return False


# --------------------------------------------------------------------------- #
#  Plain image-text CAPTCHA (best-effort with Whisper-driven OCR is not great;
#  we keep this as a no-op so callers that ask for kind=image-captcha don't
#  silently break).
# --------------------------------------------------------------------------- #


async def _solve_image_captcha(page: Page, logger) -> bool:
    """We don't ship an OCR model in the free build — math/text only."""
    page_text = await _grab_text_around_inputs(page)
    if not page_text:
        return False
    ans = _try_solve_math(page_text)
    if ans is None:
        return False
    in_loc = page.locator(
        "input[name*='captcha' i], input[id*='captcha' i], "
        "input[placeholder*='captcha' i]"
    ).first
    try:
        await in_loc.fill(ans)
        logger.info(f"[CAPTCHA-SOLVER] solved math/text locally: {ans!r}")
        return True
    except Exception as exc:
        logger.warning(f"[CAPTCHA-SOLVER] couldn't fill math answer: {exc}")
        return False


# --------------------------------------------------------------------------- #
#  Public API
# --------------------------------------------------------------------------- #


async def solve_captcha(
    page: Page,
    kind: str,
    logger,
    *,
    max_attempts: int = 3,
    **_unused,
) -> bool:
    """Try to solve ``kind`` autonomously.

    ``**_unused`` accepts (and ignores) legacy kwargs like ``api_key=`` so
    older call sites keep working without crashing.
    """
    if kind not in _SOLVABLE_KINDS:
        logger.info(f"[CAPTCHA-SOLVER] kind={kind!r} not supported, skipping")
        return False

    if kind == "recaptcha-v2":
        return await _solve_recaptcha_v2_audio(
            page, logger, max_attempts=max_attempts,
        )

    # Math / image-captcha kinds — local arithmetic only.
    return await _solve_image_captcha(page, logger)


__all__ = ["solve_captcha"]
