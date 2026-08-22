"""The browser half of the suite.

Everything here runs against an app started by this file, on a spare port, on
the same scratch database the rest of the suite uses. It never touches a
running install: the root conftest refuses to start unless every path is
scratch, and the server below is one this process created and kills.

Playwright is optional. Without it these tests skip with a reason rather than
failing, so `pytest tests` still means something on a machine with no browser.
"""
import os
import socket
import threading
import time

import pytest

sync_api = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright is not installed; run scripts/test.sh for the UI suite")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def base_url(plex_url):
    """A real server, in this process, on a port nothing else is using."""
    import uvicorn
    from app.web import app

    # The periodic sync would fire underneath a test and rewrite the guide.
    os.environ["COUCHELEPHANT_NO_SYNC_LOOP"] = "1"

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("the test server never came up")
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="session")
def browser():
    with sync_api.sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox"])
        yield b
        b.close()


def _page(browser, base_url, **context):
    ctx = browser.new_context(base_url=base_url, **context)
    page = ctx.new_page()
    # A console error is a failure. Half of what a UI suite is for is catching
    # the exception that leaves a panel half drawn and no server-side trace.
    page.on("pageerror", lambda e: pytest.fail(f"uncaught page error: {e}"))
    return ctx, page


@pytest.fixture
def page(browser, base_url, synced):
    # Pin the system preference. Headless Chromium reports light, and the boot
    # script honours the system on a first visit, so an unpinned context would
    # start in the theme the toggle test is trying to switch to.
    ctx, p = _page(browser, base_url, viewport={"width": 1440, "height": 900},
                   color_scheme="dark")
    yield p
    ctx.close()


@pytest.fixture
def phone(browser, base_url, synced):
    """A narrow viewport, because the mobile layout is a separate design."""
    ctx, p = _page(browser, base_url,
                   viewport={"width": 390, "height": 844}, is_mobile=True,
                   has_touch=True, device_scale_factor=3, color_scheme="dark")
    yield p
    ctx.close()


@pytest.fixture
def guide(page):
    page.goto("/")
    page.wait_for_selector(".gprog", timeout=15000)
    return page


@pytest.fixture
def light_system(browser, base_url, synced):
    """A browser whose owner has asked their OS for a light theme."""
    ctx, p = _page(browser, base_url, viewport={"width": 1280, "height": 800},
                   color_scheme="light")
    yield p
    ctx.close()
