"""Скриншоты всех экранов на демо-данных (testing/demo_home.py) в headless Chrome.

    uv run --all-packages python testing/demo_screens.py docs/screens/2026-11-15

Нужен установленный Google Chrome (playwright берёт его через channel="chrome").
Сеть и ключ DAWG не нужны: каталог данных временный, keyring пустой.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_home  # noqa: E402

DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 390, "height": 844}


def shots(ids: dict[str, str]) -> list[tuple[str, str, dict, bool, str | None]]:
    """(файл, путь, размер окна, тёмная тема, что раскрыть или к чему прокрутить)."""
    out = []
    for dark in (False, True):
        sfx = "dark" if dark else "light"
        out += [
            (f"01-home-{sfx}", "/", DESKTOP, dark, None),
            (f"02-library-{sfx}", "/scripts", DESKTOP, dark, None),
            (f"03-script-schedules-minutes-{sfx}", "/scripts/ozon-search-cards", DESKTOP, dark, None),
            (f"04-run-failed-repair-{sfx}", f"/runs/{ids['failed']}#repair", DESKTOP, dark, None),
            (f"05-run-silent-breakage-{sfx}", f"/runs/{ids['silent']}#repair", DESKTOP, dark, None),
            (f"06-history-{sfx}", "/scripts/wb-search-cards", DESKTOP, dark, "#runs-title"),
            (f"07-settings-{sfx}", "/settings", DESKTOP, dark, None),
        ]
    out += [
        ("08-home-phone-light", "/", PHONE, False, None),
        ("09-run-failed-phone-light", f"/runs/{ids['failed']}#repair", PHONE, False, None),
        ("10-home-phone-dark", "/", PHONE, True, None),
        ("11-run-silent-phone-dark", f"/runs/{ids['silent']}#repair", PHONE, True, None),
    ]
    return out


def main() -> int:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/screens/demo").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="dawg-replay-demo-")) / "home"
    ids = demo_home.seed(home)
    proc, url = demo_home.start_ui(home)
    base = url.split("/?", 1)[0]
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            for name, path, size, dark, focus in shots(ids):
                ctx = browser.new_context(viewport=size, color_scheme="dark" if dark else "light",
                                          device_scale_factor=2 if size is PHONE else 1, locale="ru-RU")
                theme = "dark" if dark else "light"
                ctx.add_init_script(f"try {{ localStorage.setItem('dawg-replay-theme', '{theme}') }} catch (e) {{}}")
                page = ctx.new_page()
                page.goto(url)  # вход по токену: cookie
                page.goto(base + path)
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(400)
                if focus:
                    page.locator(focus).scroll_into_view_if_needed()
                    page.screenshot(path=str(out_dir / f"{name}.png"))
                else:
                    page.evaluate("window.scrollTo(0, 0)")
                    page.screenshot(path=str(out_dir / f"{name}.png"), full_page=True)
                wide = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                print(f"{name}.png{'  ГОРИЗОНТАЛЬНАЯ ПРОКРУТКА' if wide else ''}")
                ctx.close()
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=20)
    return 0


if __name__ == "__main__":
    sys.exit(main())
