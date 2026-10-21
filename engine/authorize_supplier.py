"""Refresh human verification for suppliers that require a persistent browser session.

Run from the repository root:
    python -m engine.authorize_supplier
"""
from __future__ import annotations

from .audit_suppliers import _KNOWN_PRODUCT_URLS
from .browser import Browser
from .search.agent import _looks_like_bot_check


def main() -> None:
    url = _KNOWN_PRODUCT_URLS["artem-tools.ru"]
    print("Откроется Chrome с artem-tools.ru. Если появится KillBot, пройдите проверку в окне браузера.",
          flush=True)
    with Browser(headed=True) as browser:
        with browser.page(url) as page:
            for _ in range(90):
                try:
                    title = page.title()
                    text = page.inner_text("body")
                    if len(text) > 1500 and not _looks_like_bot_check(text, title):
                        print("Карточка товара открылась. Профиль браузера сохранён локально.", flush=True)
                        return
                except Exception:
                    # The challenge navigates the page while Playwright reads it.
                    pass
                page.wait_for_timeout(2000)
    raise SystemExit("Проверка не завершена за 3 минуты; повторите команду, когда сможете пройти KillBot.")


if __name__ == "__main__":
    main()
