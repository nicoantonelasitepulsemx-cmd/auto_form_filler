"""Smoke test for recorder.py: drive the page programmatically via Playwright,
then confirm the recorder produces the expected JSON config.

Also includes a no-network smoke for `proxy_utils` (parsing + rotation), so a
broken proxy parser is caught on every CI run, not just when someone manually
plugs in a real proxy.
"""
import asyncio, json, os, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import proxy_utils
import recorder
from playwright.async_api import async_playwright


def smoke_proxy_utils() -> None:
    """In-process smoke for proxy_utils — no network, no browser."""
    # Plain host:port → defaults to http
    p = proxy_utils.parse_proxy_string("1.2.3.4:8080")
    assert p == {"server": "http://1.2.3.4:8080"}, p

    # Full URL with credentials
    p = proxy_utils.parse_proxy_string("http://user:pa%21ss@host.example:3128")
    assert p == {
        "server": "http://host.example:3128",
        "username": "user",
        "password": "pa!ss",
    }, p

    # SOCKS5
    p = proxy_utils.parse_proxy_string("socks5://1.2.3.4:1080")
    assert p == {"server": "socks5://1.2.3.4:1080"}, p

    # Invalid scheme
    try:
        proxy_utils.parse_proxy_string("ftp://1.2.3.4:21")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for ftp scheme")

    # Missing port
    try:
        proxy_utils.parse_proxy_string("http://1.2.3.4")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for missing port")

    # Dict input with separate creds
    p = proxy_utils.normalize_proxy(
        {"server": "http://1.2.3.4:8080", "username": "u", "password": "p"}
    )
    assert p == {
        "server": "http://1.2.3.4:8080",
        "username": "u",
        "password": "p",
    }, p

    # build_playwright_proxy
    p = proxy_utils.build_playwright_proxy(
        server="http://1.2.3.4:8080", username="u", password="p", bypass="*.local"
    )
    assert p == {
        "server": "http://1.2.3.4:8080",
        "username": "u",
        "password": "p",
        "bypass": "*.local",
    }, p

    # Rotator: round-robin
    rot = proxy_utils.ProxyRotator(
        ["http://a:1", "http://b:2", "http://c:3"], mode="round_robin"
    )
    seen = [rot.next()["server"] for _ in range(6)]
    assert seen == [
        "http://a:1", "http://b:2", "http://c:3",
        "http://a:1", "http://b:2", "http://c:3",
    ], seen

    # Rotator: random — every result is in the set
    rot_r = proxy_utils.ProxyRotator(["http://a:1", "http://b:2"], mode="random")
    for _ in range(20):
        assert rot_r.next()["server"] in {"http://a:1", "http://b:2"}

    # Empty rotator
    rot_e = proxy_utils.ProxyRotator([], mode="round_robin")
    assert not rot_e
    assert rot_e.next() is None

    # load_proxy_list (skips comments / blanks)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("# comment\n\nhttp://a:1\nhttp://b:2\n   \n# another\n")
        path = fh.name
    try:
        urls = proxy_utils.load_proxy_list(path)
        assert urls == ["http://a:1", "http://b:2"], urls
    finally:
        os.unlink(path)

    # resolve_proxy priority: --no-proxy beats everything
    assert proxy_utils.resolve_proxy(cli_no_proxy=True, cli_proxy="http://a:1") is None

    # CLI > config
    p = proxy_utils.resolve_proxy(
        cli_proxy="http://from-cli:1", config={"proxy": "http://from-cfg:2"}
    )
    assert p == {"server": "http://from-cli:1"}, p

    # Config falls back when no CLI
    p = proxy_utils.resolve_proxy(config={"proxy": "http://from-cfg:2"}, env=False)
    assert p == {"server": "http://from-cfg:2"}, p

    # mask_proxy redacts password
    masked = proxy_utils.mask_proxy(
        {"server": "http://h:1", "username": "u", "password": "secret"}
    )
    assert "secret" not in masked, masked
    assert "u:***" in masked, masked

    print("✓ proxy_utils smoke OK")


async def drive_and_record() -> None:
    """Launch the recorder against test_form.html and simulate user interactions."""
    here = Path(__file__).parent
    url = (here / "test_form.html").resolve().as_uri()
    out_path = here / "samples" / "smoke_recorded.json"

    # We can't easily use record_to_config directly because it owns its own browser.
    # Instead, we override the subprocess-style flow: connect a *separate* automation
    # task that drives the visible browser via CDP. The recorder starts headed but
    # that's OK for the smoke test — we just need to verify the pipeline works.
    # For CI we run with a small async task that auto-clicks Done after 6 seconds.

    async def auto_done() -> None:
        await asyncio.sleep(4)
        # Find any open browser and inject some inputs + click Done. Done via a side
        # channel: we monkey-patch the panel by sending the finish signal directly.
        # Simpler: rely on the recorder's own future via `__afFinish` exposed function.
        # The easiest way is to patch session state: call _Session via global.
        pass

    # Easier strategy: spin up our own playwright, inject the overlay JS manually,
    # simulate the user, then validate session.add() output.
    sess = recorder._Session()

    async def on_record(payload: str) -> None:
        sess.add(json.loads(payload))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        await ctx.expose_function("__afRecord", on_record)
        await ctx.expose_function("__afFinish", lambda: None)
        await ctx.expose_function("__afCancel", lambda: None)
        await ctx.expose_function("__afUndo", lambda: None)
        await ctx.add_init_script(recorder.OVERLAY_JS)
        page = await ctx.new_page()
        await page.goto(url)

        # Drive the form like a user would.
        await page.fill("#email", "phu@example.com")
        await page.locator("#email").blur()

        await page.fill("#full_name", "Nguyen Phu")
        await page.locator("#full_name").blur()

        await page.fill("#message", "Hello from recorder smoke test")
        await page.locator("#message").blur()

        await page.select_option("#country", "vn")

        await page.check("#agree")
        await page.check("input[name=plan][value=pro]")

        await page.click("#submit")

        # Give events a moment to drain.
        await asyncio.sleep(0.3)
        await ctx.close()
        await browser.close()

    fields = sess.fields()
    submits = sess.submit_selectors

    config = recorder.build_config(sess, target_url=url)
    out_path.parent.mkdir(exist_ok=True, parents=True)
    out_path.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    (out_path.parent / "smoke_recorded.events.json").write_text(
        json.dumps(sess.events, indent=2, ensure_ascii=False)
    )

    print(f"\n✓ {len(fields)} field(s), {len(submits)} submit selector(s), {len(sess.events)} event(s)")
    print(f"  → {out_path}")
    for f in fields:
        v = f.get("value", "<no value>")
        print(f"    [{f['field_type']:8}] {f['field_id']:15} = {v!r}  (targets={len(f['targets'])})")

    # ---- Assertions ----
    assert len(fields) >= 6, f"expected >=6 fields, got {len(fields)}"
    field_ids = {f["field_id"] for f in fields}
    for expected in {"email", "full_name", "message", "country", "agree"}:
        assert any(expected in fid for fid in field_ids), f"missing field for {expected}"
    assert any(f.get("value") == "phu@example.com" for f in fields), "email value missing"
    assert any(f.get("field_type") == "checkbox" and f.get("value") is True for f in fields), \
        "agree checkbox not captured as True"
    assert any(f.get("field_type") == "select" and f.get("value") == "vn" for f in fields), \
        "country select not captured"
    assert submits, "submit not captured"
    # Each field should have multiple target strategies.
    for f in fields:
        assert len(f["targets"]) >= 2, f"field {f['field_id']} only has {len(f['targets'])} target(s)"
    print("\n✓ All assertions passed")


if __name__ == "__main__":
    smoke_proxy_utils()
    asyncio.run(drive_and_record())
