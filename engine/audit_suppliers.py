"""Живой аудит способности движка открыть и разобрать карточку каждого поставщика.

Запуск из корня репозитория:
    python -m engine.audit_suppliers

Отчёты сохраняются в ``engine/data/output/supplier_audit.{json,md}``.
"""
from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .browser import Browser
from .config import OUTPUT_DIR, settings
from .search.agent import (
    _extract_from_page,
    _http_response_is_blocked,
    _looks_like_bot_check,
    _page_text_after_browser_check,
    _price_per_requested_unit,
)
from .search.search_engine import xmlriver_search
from .search.whitelist import source_type, whitelist_hosts, fallback_hosts  # Inert compatibility alias.


_BAD_PATH_PARTS = (
    "/search", "/contacts", "/about", "/delivery", "/payment", "/news", "/blog",
    "/brands", "/manufacturers", "/tag-page/",
)
_PRODUCT_PATH_PARTS = (
    "/product", "/item", "/goods", "/catalog/", "/shop/", "/cat/", "/p/", ".html",
)
_NON_HTML_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".zip")
# Контрольные карточки, уже подтверждённые реальным поиском, но нестабильно попадающие в
# общую выдачу без наименования товара.
_KNOWN_PRODUCT_URLS = {
    "chipdip.ru": (
        "https://www.chipdip.ru/product/4046-kabel-silovoy-ppgng-a-hf-"
        "1x150mk-trts-konkord-8024524946"
    ),
    "cable.ru": "https://cable.ru/cable/marka-ppgng_hf_1x150_1.php",
    "lanitnord.ru": "https://www.lanitnord.ru/product/din-reyka-perforirovannaya-omega-3f-35kh7-5mm/",
    "vseinstrumenti.ru": (
        "https://www.vseinstrumenti.ru/product/"
        "kabel-nvo-vbshvng-a-ls-3x4-gost-40-metrov-nvo-kpp-vbshvng-ls-340-40-10315286/"
    ),
    "rs24.ru": "https://rs24.ru/product/3449485",
    "etm.ru": "https://www.etm.ru/cat/nn/8580188",
    "minimaks.ru": (
        "https://www.minimaks.ru/catalog/nizkovoltnoe-oborudovanie-iek/"
        "vyklyuchatel-avtomaticheskiy-modulnyy/"
    ),
    "artem-tools.ru": (
        "https://www.artem-tools.ru/catalog/ruchnoy-instrument/otvertki/nabory-otvertok/"
        "dielektricheskaya-otvertka-so-smennymi-bitami-dlya-rabot-pod-napryazheniem-"
        "do-1000-v-kvt-nio-1113-61_320139/"
    ),
    "electro-mpo.ru": (
        "https://www.electro-mpo.ru/catalog/avtomaty_uzo_difavtomaty/"
        "a13_avtomaticheskie_vyklyuchateli_va57_va51_ap50b_/"
        "a1348-avtomaticheskiy-vyklyuchatel-va-57-35-340010/"
    ),
    "dinastyasnab.ru": (
        "https://dinastyasnab.ru/shop/goods/"
        "akkumulyator_18650_37v_Li_Ion_3000mach_bez_platyi_zaschityi_faZa_5004757-113025"
    ),
    "xcom-shop.ru": (
        "https://www.xcom-shop.ru/xiaomi_redmi_a7_pro_ru_4128gb_1305103.html"
    ),
    "olmi-connect.ru": (
        "https://olmi-connect.ru/category/avtomaticheskie-vykljuchateli/"
        "avtomaticheskij-vyklyuchatel-16a/"
    ),
    "kdds.ru": (
        "https://www.kdds.ru/antivandalnye-shkafy/klimaticheskie-shkafy/"
        "shkaf-klimaticheskiy-telekommunikatsionnyy-navesno2547"
    ),
    "satro-paladin.com": "https://satro-paladin.com/catalog/product/17054/",
    "e-kc.ru": "https://e-kc.ru/cena/cable-avvg-4-95",
    "edkm.ru": "https://www.edkm.ru/dekorativnyy-kirpich-baltfasad-kolotyy-kirpich-011/",
}


@dataclass
class AuditRow:
    tier: str
    host: str
    status: str
    url: str = ""
    http_status: int | None = None
    text_chars: int = 0
    product_title: str = ""
    price: float | None = None
    detail: str = ""
    attempted_urls: list[str] | None = None
    checked_at: str = ""


def review_reason(row: AuditRow) -> str:
    """Short, actionable explanation for an estimator reviewing a failed check."""
    if row.host == "vseinstrumenti.ru" and row.status == "blocked":
        return ("Сайт открывается в обычном Chrome, но автоматический браузер и HTTP-запросы "
                "через тот же прокси получают 403. Надёжный автоматический сбор цены не "
                "подтверждён; нужен согласованный прайс/API или ручная проверка.")
    if row.status == "blocked":
        if row.http_status == 403:
            return "Сайт отклонил автоматический запрос (HTTP 403). Проверьте карточку вручную."
        if row.http_status == 503:
            return "Сайт временно не отдал карточку (HTTP 503). Повторите проверку вручную."
        return "Сайт показал защиту от автоматизации или капчу. Проверьте карточку вручную."
    if row.status == "navigation_error":
        return "Не удалось открыть сайт: соединение прервано или истекло время ожидания."
    if row.status == "no_candidate":
        return "Сайт доступен, но поиск не нашёл подходящую карточку товара."
    if row.status == "no_price":
        return "Карточка открылась, но сервис не смог извлечь числовую цену."
    if row.status == "parser_error":
        return "Карточка открылась, но автоматический разбор завершился ошибкой."
    if row.status == "empty":
        return "Сайт открылся без видимого текста товара."
    return "Проверка не подтвердила доступность цены."


def _candidate_score(url: str, host: str) -> int:
    parsed = urlparse(url)
    if parsed.netloc.lower().removeprefix("www.") != host.removeprefix("www."):
        return -1000
    path = parsed.path.lower()
    if path.endswith(_NON_HTML_EXTENSIONS):
        return -1000
    score = path.count("/")
    score += 8 * sum(part in path for part in _PRODUCT_PATH_PARTS)
    score -= 12 * sum(part in path for part in _BAD_PATH_PARTS)
    if not path.strip("/"):
        score -= 20
    return score


def _discover(host: str) -> list[str]:
    known = _KNOWN_PRODUCT_URLS.get(host)
    discovered: list[str] = []
    for query in (f'site:{host} "руб."', f"site:{host} inurl:product купить"):
        discovered.extend(xmlriver_search(query, 10))
    discovered = list(dict.fromkeys(discovered))
    candidates = sorted(
        (url for url in discovered if url != known),
        key=lambda url: _candidate_score(url, host),
        reverse=True,
    )
    candidates = [url for url in candidates if _candidate_score(url, host) >= 0]
    return ([known] if known else []) + candidates[:4 if known else 5]


def discover_all(hosts: list[str]) -> dict[str, list[str]]:
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(zip(hosts, pool.map(_discover, hosts)))


def audit_page(browser: Browser, tier: str, host: str, url: str) -> AuditRow:
    if not url:
        return AuditRow(tier=tier, host=host, status="no_candidate",
                        detail="XMLRiver не нашёл подходящую карточку")
    try:
        with browser.page(url) as page:
            page.wait_for_timeout(1500)
            status = getattr(page, "_kac_response_status", None)
            text = _page_text_after_browser_check(page, status)
            title = page.title()
    except Exception as exc:
        return AuditRow(tier=tier, host=host, status="navigation_error", url=url,
                        detail=f"{type(exc).__name__}: {str(exc)[:160]}")

    row = AuditRow(tier=tier, host=host, status="opened", url=url,
                   http_status=status, text_chars=len(text))
    if _http_response_is_blocked(status, text):
        row.status = "blocked"
        row.detail = f"HTTP {status}"
        return row
    if not text.strip():
        row.status = "empty"
        row.detail = "Нет видимого текста"
        return row
    if _looks_like_bot_check(text, title):
        row.status = "blocked"
        row.detail = "Антибот/капча"
        return row

    data = _extract_from_page(text, expected_unit="шт")
    if not data:
        row.status = "parser_error"
        row.detail = "LLM не вернула JSON"
        return row
    row.product_title = str(data.get("product_title") or "")[:160]
    row.price = _price_per_requested_unit(data)
    if row.price is None:
        row.status = "no_price"
        row.detail = "Карточка открылась, цена не извлечена"
        return row
    row.status = "parsed"
    row.detail = "Цена извлечена"
    return row


def audit_host(browser: Browser, tier: str, host: str,
               urls: list[str]) -> AuditRow:
    if not urls:
        homepage = audit_page(browser, tier, host, f"https://{host}/")
        if homepage.status in {"opened", "no_price", "parser_error"}:
            homepage.status = "no_candidate"
            homepage.detail = "Сайт доступен, но XMLRiver не нашёл карточку товара"
        homepage.checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
        return homepage
    attempted: list[str] = []
    best: AuditRow | None = None
    rank = {
        "parsed": 6,
        "no_price": 5,
        "parser_error": 4,
        "empty": 3,
        "navigation_error": 2,
        "blocked": 1,
    }
    for url in urls:
        attempted.append(url)
        row = audit_page(browser, tier, host, url)
        if best is None or rank.get(row.status, 0) > rank.get(best.status, 0):
            best = row
        if row.status == "parsed":
            break
        # Кодовая блокировка обычно действует на весь домен; следующие карточки не помогут.
        if row.status == "blocked" and row.http_status and row.http_status >= 400:
            break
    assert best is not None
    best.attempted_urls = attempted
    if len(attempted) > 1:
        best.detail += f"; проверено карточек: {len(attempted)}"
    best.checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return best


def _write_reports(rows: list[AuditRow], started_at: str, *, headed: bool) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / "supplier_audit.json"
    md_path = OUTPUT_DIR / "supplier_audit.md"
    payload = {
        "started_at": started_at,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "browser_mode": "visible" if headed else "headless",
        "proxy_enabled": bool(settings.browser_proxy_server),
        "summary": {
            status: sum(row.status == status for row in rows)
            for status in sorted({row.status for row in rows})
        },
        "direct_supplier_summary": {
            "total": sum(source_type(row.host) == "direct_supplier" for row in rows),
            "parsed": sum(source_type(row.host) == "direct_supplier" and row.status == "parsed"
                          for row in rows),
        },
        "source_types": {row.host: source_type(row.host) for row in rows},
        "rows": [asdict(row) for row in rows],
    }
    json_tmp = json_path.with_suffix(".json.tmp")
    json_tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    json_tmp.replace(json_path)

    lines = [
        "# Аудит сайтов поставщиков",
        "",
        f"Начало: {payload['started_at']}",
        f"Окончание: {payload['finished_at']}",
        "",
        f"Подтверждено извлечение цены: **{payload['summary'].get('parsed', 0)} из {len(rows)}**.",
        f"Прямые поставщики: **{payload['direct_supplier_summary']['parsed']} из "
        f"{payload['direct_supplier_summary']['total']}**. Агрегаторы и корпоративные "
        "сайты учитываются отдельно.",
        "",
        "Остальные статусы показывают внешнюю блокировку, отсутствие опубликованной цены, "
        "пустой ответ сайта или ошибку навигации. Это не считается подтверждённой поддержкой.",
        "",
        "| Уровень | Домен | Результат | HTTP | Цена | Карточка | Пояснение |",
        "|---|---|---|---:|---:|---|---|",
    ]
    for row in rows:
        title = re.sub(r"\s+", " ", row.product_title).replace("|", "\\|")
        detail = re.sub(r"\s+", " ", row.detail).replace("|", "\\|")
        link = f"[{title or 'ссылка'}]({row.url})" if row.url else "—"
        lines.append(
            f"| {row.tier} | `{row.host}` | **{row.status}** | "
            f"{row.http_status or '—'} | {row.price if row.price is not None else '—'} | "
            f"{link} | {detail} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def run_audit(*, retry_unconfirmed: bool = False, headed: bool | None = None,
              progress=None) -> dict:
    """Run the site audit and return a summary; suitable for CLI or a background job."""
    if headed is None:
        headed = settings.browser_headed
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    primary = whitelist_hosts()
    all_hosts = primary
    previous_by_host: dict[str, AuditRow] = {}
    report_path = OUTPUT_DIR / "supplier_audit.json"
    if retry_unconfirmed and report_path.exists():
        previous = json.loads(report_path.read_text(encoding="utf-8"))
        previous_by_host = {
            item["host"]: AuditRow(**item) for item in previous.get("rows", [])
        }
        target_hosts = [
            host for host in all_hosts
            if previous_by_host.get(host) is None
            or previous_by_host[host].status != "parsed"
        ]
    else:
        target_hosts = all_hosts
    print(f"[аудит] сайтов в проходе: {len(target_hosts)} из {len(all_hosts)} "
          f"(единый перечень: {len(primary)})")
    candidates = discover_all(target_hosts)
    updated: dict[str, AuditRow] = {}
    with Browser(headed=headed) as browser:
        for index, host in enumerate(target_hosts, start=1):
            tier = "единый"
            print(f"[аудит] {index}/{len(target_hosts)} {host}", flush=True)
            if progress:
                progress(f"Проверка {index}/{len(target_hosts)}: {host}",
                         (index - 1) / max(len(target_hosts), 1))
            row = audit_host(browser, tier, host, candidates.get(host, []))
            updated[host] = row
            print(f"[аудит]   {row.status}: {row.detail}", flush=True)
    rows = [updated.get(host) or previous_by_host[host] for host in all_hosts]
    json_path, md_path = _write_reports(rows, started_at, headed=headed)
    print(f"[аудит] JSON: {json_path}")
    print(f"[аудит] Markdown: {md_path}")
    if progress:
        progress("Проверка сайтов завершена.", 1.0)
    return {"summary": {status: sum(row.status == status for row in rows)
                        for status in sorted({row.status for row in rows})},
            "report_path": str(json_path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--retry-unconfirmed", action="store_true",
        help="Повторить только неподтверждённые сайты и объединить отчёт",
    )
    parser.add_argument("--headed", action="store_true",
                        help="Проверять сайты в видимом Chrome")
    args = parser.parse_args()
    run_audit(retry_unconfirmed=args.retry_unconfirmed,
              headed=args.headed or settings.browser_headed)


if __name__ == "__main__":
    main()
