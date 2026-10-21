"""Тесты эндпоинтов без сети: импорт приложения, отдача кэша, проброс скриншотов в ТКП.

Запуск:  python -m engine.test_endpoints
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import fitz
from fastapi.testclient import TestClient

from engine.app import app
from engine.config import CACHE_DIR
from engine.schemas import BuildRequest
from engine.models import PositionResult

client = TestClient(app)


def test_search_request_preserves_unit():
    """Ручной поиск должен передавать единицу позиции в ценовой движок."""
    captured = {}

    def fake_research(_browser, pos, _year, _quarter, existing_offers=None):
        captured["unit"] = pos.unit
        return PositionResult(position=pos)

    class FakeBrowser:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    with patch("engine.search.base_db.lookup", return_value=None), \
            patch("engine.search.base_db.upsert"), \
            patch("engine.search.agent.research_position", side_effect=fake_research), \
            patch("engine.app._browser_factory", return_value=FakeBrowser()):
        r = client.post("/search_position", json={
            "name": "Кабель", "type_mark": "ППГнг(А)-HF 1x150", "unit": "м", "all_suppliers": False,
        })
    assert r.status_code == 200
    assert captured["unit"] == "м"


def test_search_response_reports_card_rejection() -> None:
    """A manual retry must show why a supplier card was not accepted."""
    from engine.search.agent import _record_phase_timing, _reject_card

    def fake_research(_browser, pos, _year, _quarter, existing_offers=None):
        _reject_card("https://supplier.example/item", "article_mismatch", "другой артикул")
        _record_phase_timing("primary", 1.2)
        return PositionResult(position=pos)

    class FakeBrowser:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    with patch("engine.search.base_db.lookup", return_value=None), \
            patch("engine.search.base_db.upsert"), \
            patch("engine.search.agent.research_position", side_effect=fake_research), \
            patch("engine.app._browser_factory", return_value=FakeBrowser()):
        response = client.post("/search_position", json={"name": "Лоток", "unit": "м", "all_suppliers": False})

    assert response.status_code == 200
    assert response.json()["diagnostics"] == [{
        "url": "https://supplier.example/item",
        "code": "article_mismatch",
        "reason": "другой артикул",
    }]


def _make_png(path: Path, label: str = "test") -> Path:
    """Генерирует небольшой PNG-образец (без внешних файлов)."""
    doc = fitz.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((20, 100), label, fontsize=20)
    pix = page.get_pixmap()
    pix.save(path)
    doc.close()
    return path


def test_health_and_cache():
    assert client.get("/health").json()["ok"] is True

    # положим тестовый PNG в кэш и проверим отдачу + защиту от обхода каталогов
    _make_png(CACHE_DIR / "prod_test.png", "card")
    r = client.get("/cache/prod_test.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert client.get("/cache/nope.png").status_code == 404
    # обход каталога обезвреживается (берётся только basename)
    assert client.get("/cache/..%2f..%2fconfig.py").status_code in (404, 400)
    print("✓ /health и /cache (отдача + защита) работают")


def test_screenshot_threading_into_tkp():
    """Скриншоты, переданные в офферах, должны попасть в Том ТКП."""
    _make_png(CACHE_DIR / "prod_thread.png", "card")
    _make_png(CACHE_DIR / "req_thread.png", "requisites")

    build = BuildRequest(
        object_name="ТЕСТ скриншоты", skip_search=True, use_tsn=False,
        positions=[{
            "number": 1, "name": "Кабель", "unit": "м",
            "offers": [{
                "price_with_vat": 2361, "org_name": "ООО Тест", "inn": "7700000000",
                "screenshot_product": "prod_thread.png",
                "screenshot_requisites": "req_thread.png",
            }],
        }],
    )
    res = build.positions[0].to_result()
    off = res.offers[0]
    assert off.screenshot_product and Path(off.screenshot_product).exists()
    assert off.screenshot_requisites and Path(off.screenshot_requisites).exists()

    from engine.builders.tkp_pdf import build_tkp
    out, _ = build_tkp([res], "ТЕСТ скриншоты", CACHE_DIR / "tkp_thread.pdf")
    import fitz
    d = fitz.open(out)
    # На странице позиции встроен скриншот карточки товара; реквизиты рисуются ТЕКСТОМ
    # (наименование/ИНН/КПП), поэтому ожидаем ≥1 изображение на странице.
    imgs = d[1].get_images()
    assert len(imgs) >= 1, f"ожидался встроенный скриншот карточки, найдено {len(imgs)}"
    text = d[1].get_text()
    assert "Реквизиты продавца" in text, "ожидалась текстовая панель реквизитов"
    print(f"✓ карточка-скриншот + текстовые реквизиты в Томе ТКП (изображений: {len(imgs)})")


if __name__ == "__main__":
    test_search_request_preserves_unit()
    print("✓ /search_position сохраняет единицу измерения")
    test_search_response_reports_card_rejection()
    test_health_and_cache()
    test_screenshot_threading_into_tkp()
    print("\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ ✓")
