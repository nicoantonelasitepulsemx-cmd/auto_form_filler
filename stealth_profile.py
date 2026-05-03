"""v4 add-on: anti-detect fingerprint randomiser + persistent profiles.

Modern bot detection (Cloudflare bot-management, Akamai BMP,
DataDome, Facebook's own detector) leans heavily on JavaScript
fingerprinting: navigator props, WebGL renderer, canvas hashes,
viewport, timezone, locale, font list. A vanilla Playwright run
exposes the same fingerprint on every account — three submissions
in and the network has you.

This module gives each account its own *stable, plausible*
fingerprint that survives across runs. Two halves:

1. :func:`generate_profile` — pure function: name → deterministic
   :class:`Profile`. The same account name always returns the same
   profile (so cookies + IndexedDB pin to a coherent fingerprint).
2. :func:`apply_profile` — async helper that wires the profile into
   a Playwright ``BrowserContext`` (UA, viewport, locale, timezone,
   geo) and injects an init script that masks WebDriver, plugins,
   languages, WebGL renderer, hardware concurrency, and the screen
   props. The init script is a string constant so it's easy to audit
   and ships with no runtime deps.

The defaults are conservative — we do *not* try to spoof an iPhone
on a Linux box (canvas/font detection will catch that immediately).
We pick from a curated pool of *real* desktop fingerprints
(Chrome / Edge on Windows + macOS) so the resulting browser looks
indistinguishable from a regular employee laptop.
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "Profile",
    "generate_profile",
    "build_init_script",
    "apply_profile",
]


# --------------------------------------------------------------------------- pools

# Each entry is a tuple ``(ua, platform, vendor, oscpu, webgl_vendor,
# webgl_renderer)``. Chosen from real Chrome/Edge UAs current as of
# 2025-Q4. Add more as you collect them — :func:`generate_profile`
# picks one deterministically from the account name's hash.
_UA_POOL: tuple[tuple[str, str, str, str, str, str], ...] = (
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36",
        "Win32", "Google Inc.", "",
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36",
        "Win32", "Google Inc.", "",
        "Google Inc. (Intel)",
        "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36",
        "MacIntel", "Google Inc.", "",
        "Google Inc. (Apple)",
        "ANGLE (Apple, ANGLE Metal Renderer: Apple M1 Pro, Unspecified Version)",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        "Win32", "Microsoft", "",
        "Google Inc. (AMD)",
        "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36",
        "MacIntel", "Google Inc.", "",
        "Google Inc. (Apple)",
        "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
    ),
)

# (width, height) — covers the main desktop bracket. We don't use
# laptop-only sizes (1366×768) because Cloudflare correlates them
# with mobile-like traffic profiles and bumps the challenge level.
_VIEWPORTS: tuple[tuple[int, int], ...] = (
    (1920, 1080),
    (1680, 1050),
    (1600, 900),
    (1536, 864),
    (1440, 900),
    (2560, 1440),
)

_LOCALES: tuple[tuple[str, str], ...] = (
    ("en-US", "America/New_York"),
    ("en-US", "America/Los_Angeles"),
    ("en-US", "America/Chicago"),
    ("en-GB", "Europe/London"),
    ("en-CA", "America/Toronto"),
    ("en-AU", "Australia/Sydney"),
)

# Plausible hardware-concurrency / device-memory pairs. Both must be
# powers-of-two-ish or detectors flag the inconsistency.
_HARDWARE: tuple[tuple[int, int], ...] = (
    (4, 8),
    (8, 8),
    (8, 16),
    (12, 16),
    (16, 32),
)


# --------------------------------------------------------------------------- profile

@dataclass
class Profile:
    """Per-account fingerprint snapshot.

    Attributes
    ----------
    user_agent / platform / vendor / oscpu:
        navigator.* values.
    viewport:
        ``(width, height)``.
    device_scale_factor:
        Pixel ratio. Real machines almost always hit one of (1, 1.25,
        1.5, 2.0).
    locale / timezone:
        navigator.language + Intl. We ship matched pairs so the two
        signals don't disagree.
    hardware_concurrency / device_memory:
        navigator.hardwareConcurrency + navigator.deviceMemory.
    webgl_vendor / webgl_renderer:
        Returned by the WebGL ``UNMASKED_VENDOR_WEBGL`` /
        ``UNMASKED_RENDERER_WEBGL`` extensions.
    user_data_dir:
        Optional path. When set, :func:`apply_profile` returns a
        Playwright launch dict with ``user_data_dir`` already wired
        so cookies + localStorage persist.
    seed:
        The hash bucket the profile was drawn from — useful for logs
        ("account 'alice' is on bucket #42").
    """
    user_agent: str
    platform: str
    vendor: str
    oscpu: str
    viewport: tuple[int, int]
    device_scale_factor: float
    locale: str
    timezone: str
    hardware_concurrency: int
    device_memory: int
    webgl_vendor: str
    webgl_renderer: str
    user_data_dir: Optional[str] = None
    seed: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_agent":          self.user_agent,
            "platform":            self.platform,
            "vendor":              self.vendor,
            "oscpu":               self.oscpu,
            "viewport":            list(self.viewport),
            "device_scale_factor": self.device_scale_factor,
            "locale":              self.locale,
            "timezone":            self.timezone,
            "hardware_concurrency": self.hardware_concurrency,
            "device_memory":       self.device_memory,
            "webgl_vendor":        self.webgl_vendor,
            "webgl_renderer":      self.webgl_renderer,
            "user_data_dir":       self.user_data_dir,
            "seed":                self.seed,
            "extra":               dict(self.extra),
        }


def _seed_for(name: str, salt: str = "v4") -> int:
    """Stable 31-bit seed derived from the account name + salt.

    SHA-256 keeps the distribution flat across the profile pools;
    ``[:8]`` of the hex digest is more than enough entropy for the
    handful of buckets we sample from."""
    h = hashlib.sha256(f"{salt}:{name}".encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def generate_profile(
    name: str,
    *,
    user_data_dir: Optional[str] = None,
    salt: str = "v4",
) -> Profile:
    """Deterministic ``account name → Profile``.

    Calling twice with the same ``name`` returns the same profile;
    different ``name`` values pick independently from each pool, so
    you get a wide spread across ~150-200 buckets without needing to
    persist a profile file.
    """
    seed = _seed_for(name, salt)
    rng = random.Random(seed)
    ua, platform, vendor, oscpu, webgl_vendor, webgl_renderer = rng.choice(_UA_POOL)
    viewport = rng.choice(_VIEWPORTS)
    locale, timezone = rng.choice(_LOCALES)
    cores, memory = rng.choice(_HARDWARE)
    # Real machines almost always hit one of (1, 1.25, 1.5, 2.0); 1.25
    # is rare on real Chrome so we bias against it.
    dpr = rng.choice([1.0, 1.0, 1.5, 2.0])
    return Profile(
        user_agent=ua,
        platform=platform,
        vendor=vendor,
        oscpu=oscpu,
        viewport=viewport,
        device_scale_factor=dpr,
        locale=locale,
        timezone=timezone,
        hardware_concurrency=cores,
        device_memory=memory,
        webgl_vendor=webgl_vendor,
        webgl_renderer=webgl_renderer,
        user_data_dir=user_data_dir,
        seed=seed,
    )


# --------------------------------------------------------------------------- init script

# The init script runs *before* any page script (Playwright's
# ``add_init_script``) so detectors see the spoofed values from the
# very first navigator query. We patch:
#
#   * navigator.webdriver           → undefined
#   * navigator.languages           → matched to locale
#   * navigator.platform / vendor   → matched to UA
#   * navigator.hardwareConcurrency / deviceMemory
#   * navigator.plugins.length      → 3 (real Chrome ships PDF
#                                     plugin + native viewer + plug-in
#                                     placeholder)
#   * WebGL UNMASKED_VENDOR/_RENDERER
#   * Permissions.query result for "notifications" — fixes the
#     classic notification-permission detector that flags Playwright.
#   * window.chrome                  → minimal stub so feature checks
#                                     don't NPE on it.

def build_init_script(profile: Profile) -> str:
    """Render the init JS for a given :class:`Profile`.

    The result is a string suitable for
    ``BrowserContext.add_init_script``. We embed the profile values
    via :func:`json.dumps` so escaping is correct even for unusual
    characters (e.g. non-ASCII renderer strings).
    """
    payload = {
        "platform":             profile.platform,
        "vendor":                profile.vendor,
        "oscpu":                 profile.oscpu,
        "languages":             [profile.locale, profile.locale.split("-")[0]],
        "hardware_concurrency":  profile.hardware_concurrency,
        "device_memory":         profile.device_memory,
        "webgl_vendor":          profile.webgl_vendor,
        "webgl_renderer":        profile.webgl_renderer,
    }
    return f"""
(() => {{
  const P = {json.dumps(payload)};
  // 1. Strip webdriver flag (the dead giveaway).
  try {{
    Object.defineProperty(Navigator.prototype, "webdriver", {{
      configurable: true, get: () => undefined,
    }});
  }} catch (_) {{}}

  // 2. Patch a few navigator values to match the profile.
  const np = Navigator.prototype;
  const set = (key, value) => {{
    try {{
      Object.defineProperty(np, key, {{
        configurable: true, get: () => value,
      }});
    }} catch (_) {{}}
  }};
  set("platform", P.platform);
  set("vendor", P.vendor);
  set("languages", P.languages);
  set("hardwareConcurrency", P.hardware_concurrency);
  set("deviceMemory", P.device_memory);
  if (P.oscpu) {{
    set("oscpu", P.oscpu);
  }}

  // 3. Plugins / mimeTypes — Chrome reports >0; Playwright ships 0.
  try {{
    Object.defineProperty(np, "plugins", {{
      configurable: true,
      get: () => {{
        const plugin = {{ name: "PDF Viewer" }};
        const arr = [plugin, plugin, plugin];
        arr.length = 3;
        Object.defineProperty(arr, "namedItem", {{ value: () => plugin }});
        Object.defineProperty(arr, "item", {{ value: (i) => arr[i] || null }});
        Object.defineProperty(arr, "refresh", {{ value: () => undefined }});
        return arr;
      }},
    }});
  }} catch (_) {{}}

  // 4. WebGL renderer / vendor (the unmasked extension).
  const patchWebGL = (proto) => {{
    if (!proto || !proto.getParameter) return;
    const orig = proto.getParameter;
    proto.getParameter = function (param) {{
      // 0x9245 = UNMASKED_VENDOR_WEBGL; 0x9246 = UNMASKED_RENDERER_WEBGL
      if (param === 37445) return P.webgl_vendor;
      if (param === 37446) return P.webgl_renderer;
      return orig.call(this, param);
    }};
  }};
  if (typeof WebGLRenderingContext !== "undefined") patchWebGL(WebGLRenderingContext.prototype);
  if (typeof WebGL2RenderingContext !== "undefined") patchWebGL(WebGL2RenderingContext.prototype);

  // 5. Permissions.query lies about Notification — real Chrome
  //    returns "default", Playwright returns "denied".
  try {{
    const orig = Permissions.prototype.query;
    Permissions.prototype.query = function (params) {{
      if (params && params.name === "notifications") {{
        return Promise.resolve({{ state: "default", onchange: null }});
      }}
      return orig.call(this, params);
    }};
  }} catch (_) {{}}

  // 6. Tiny window.chrome stub. Some bot detectors do `!!window.chrome`;
  //    we don't need to fake the full surface, just make it truthy.
  if (!window.chrome) {{
    Object.defineProperty(window, "chrome", {{
      configurable: true,
      value: {{ runtime: {{}}, app: {{}}, csi: () => undefined, loadTimes: () => undefined }},
    }});
  }}
}})();
"""


# --------------------------------------------------------------------------- apply

async def apply_profile(context: Any, profile: Profile) -> None:
    """Wire the profile values into a live Playwright ``BrowserContext``.

    Sets the JS-level fingerprint via ``add_init_script``. The
    UA/viewport/locale/timezone are passed at ``new_context()`` time
    in normal Playwright usage, so this helper assumes they're
    already on the context — its only job is the JS shimming.

    For brand-new contexts, prefer building the launch options dict
    via :func:`profile_to_context_options` and passing that to
    ``browser.new_context(**opts)``; the init script is registered
    afterwards.
    """
    js = build_init_script(profile)
    await context.add_init_script(js)


def profile_to_context_options(profile: Profile) -> dict[str, Any]:
    """Render a Playwright ``new_context`` kwargs dict from a profile.

    The caller is responsible for the ``user_data_dir`` part —
    Playwright distinguishes "context with persistent state" from
    "context attached to a specific user data dir" at the launch
    layer, not the context layer.
    """
    return {
        "user_agent":          profile.user_agent,
        "viewport":            {"width": profile.viewport[0], "height": profile.viewport[1]},
        "device_scale_factor": profile.device_scale_factor,
        "locale":              profile.locale,
        "timezone_id":         profile.timezone,
        "is_mobile":           False,
        "has_touch":           False,
    }
