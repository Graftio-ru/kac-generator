"""Конфигурация движка из переменных окружения (.env)."""
from __future__ import annotations

from pathlib import Path
from typing import Literal
from pydantic import SecretStr

from pydantic_settings import BaseSettings, SettingsConfigDict

ENGINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = ENGINE_DIR.parent
DATA_DIR = ENGINE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "output"
CACHE_DIR = DATA_DIR / "cache"
BASE_DIR = DATA_DIR / "base"               # постоянная база связок позиция→цены/скриншоты/реквизиты
BASE_FILE = BASE_DIR / "index.json"

# Локальные справочники ТСН-2001 (лежат в корне репо). Используются вместо mos.ru,
# который недоступен с зарубежного IP/VPN.
TSN_REFERENCE_FILE = REPO_ROOT / "ТСН-2001_электро_автоматика.md"
TSN_INDICES_FILE = REPO_ROOT / "индексы.md"

# Основной список доверенных поставщиков (первый уровень поиска + готовые реквизиты).
SUPPLIERS_FILE = REPO_ROOT / "Поставщики.md"
# Неактивный исторический путь для совместимости; поиск читает только SUPPLIERS_FILE.
FALLBACK_SUPPLIERS_FILE = REPO_ROOT / "Поставщики_резерв_Москва.md"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENGINE_DIR / ".env", extra="ignore")

    # Optional shared server. Credentials are provisioned by operator, never UI.
    deployment_mode: Literal['local','server'] = 'local'
    # Remote control never opens the native runner catalogue.
    execution_mode: Literal['local','remote'] = 'local'
    runner_role: Literal['control','executor'] = 'control'
    runner_username: str = ''
    runner_password_hash: SecretStr = SecretStr('')
    runner_password: SecretStr = SecretStr('')
    runner_control_origin: str = ''
    server_username: str = ''
    server_password_hash: SecretStr = SecretStr('')
    server_public_origin: str = ''

    # Existing S3 bucket for dated native card screenshots (not the LLM API key).
    s3_endpoint_url: str = "https://s3.cloud.ru"
    s3_region: str = "ru-central-1"
    s3_bucket: str = ""
    s3_access_key_id: SecretStr = SecretStr("")
    s3_secret_access_key: SecretStr = SecretStr("")
    s3_screenshot_prefix: str = "catalogue/screenshots"

    # Провайдер LLM: anthropic, deepseek или cloudru.
    # Cloud.ru распознаёт сканы отдельной OCR-моделью перед текстовой моделью.
    llm_provider: str = "anthropic"

    anthropic_api_key: str = ""
    model_smart: str = "claude-opus-4-8"
    model_fast: str = "claude-sonnet-4-6"

    # Text model settings remain independent of original-image recognition.
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model_smart: str = "deepseek-chat"
    deepseek_model_fast: str = "deepseek-chat"
    deepseek_model_vision: str = "deepseek-flash"

    # Cloud.ru owns billing; external text models and internal OCR are separate.
    cloudru_api_key: str = ""
    cloudru_base_url: str = "https://foundation-models.api.cloud.ru/v1"
    cloudru_proxy_server: str = ""  # Opt-in Cloud transport only; empty is deliberate DIRECT without ambient proxies.
    cloudru_model_fast: str = "deepseek-ai/DeepSeek-V4-Flash"
    cloudru_model_smart: str = "deepseek-ai/DeepSeek-V4-Flash"
    cloudru_model_ocr: str = "deepseek-ai/DeepSeek-OCR-2"
    cloudru_spec_reader: str = "vision"  # vision | ocr (legacy OCR2 path)
    cloudru_model_vision: str = "qwen/qwen3-vl-30b-a3b-instruct"
    # Pixel price corroboration is independent of specification OCR/vision.
    cloudru_text_fallback_models: str = "deepseek-ai/DeepSeek-V4-Pro"
    cloudru_vision_fallback_models: str = "Qwen/Qwen3.6-35B-A3B"
    cloudru_model_pixel: str = "google/gemini-3-flash-preview"
    capture_evidence_mode: str = "auto"
    # Opt-in billed second image reading; enable only after live acceptance.
    cloudru_vision_verify: bool = False

    dadata_token: str = ""
    yandex_search_api_key: str = ""
    yandex_search_folder_id: str = ""

    # SERP-API: XMLRiver/XMLStock — выдача Яндекса в XML без капчи. Если заданы user+key,
    # поиск идёт через API (без браузера); иначе фолбэк на браузерную выдачу.
    # URL: XMLRiver — https://xmlriver.com/search_yandex/xml ; XMLStock — https://xmlstock.com/yandex/xml/
    xmlriver_user: str = ""
    xmlriver_key: str = ""
    xmlriver_url: str = "https://xmlriver.com/search_yandex/xml"
    xmlriver_lr: int = 213  # код региона Яндекса (213 = Москва)
    # Operator choice applies only when a new job resolves its immutable route.
    xmlriver_backend_preference: Literal['auto','xmlriver-google'] = 'auto'

    min_sources: int = 12
    top_prices: int = 3
    # Soft per-position limits: leave time for the Moscow-region reserve list.
    primary_search_seconds: int = 90
    position_search_seconds: int = 150
    research_job_seconds: int = 900  # first-pass budget; ongoing I/O may finish later
    search_api_workers: int = 2
    vat_rate: int = 22
    region: str = "Москва и Московская область"
    # производители уникального оборудования (цена из писем, онлайн-поиск не нужен);
    # через «;», напр. CUSTOM_MAKERS=ЗаводА;ЗаводБ
    custom_makers: str = ""

    browser_headed: bool = False
    # Optional proxy used only for supplier page navigation/screenshots. A local mixed
    # HTTP/SOCKS inbound (for example Throne) lets the browser use a Russian egress while
    # LLM and search API calls continue over the regular connection.
    browser_proxy_server: str = ""
    # Optional installed Chromium channel. ``chrome`` uses the current Google Chrome build,
    # which passes some supplier checks that reject Playwright's bundled headless shell.
    browser_channel: str = ""

    # ресурсы для скриншота карточки реквизитов по ИНН (пробуются по порядку, до первого
    # отрисовавшего карточку). checko.ru исключён — блокирует зарубежный IP/VPN. {inn} —
    # подстановка ИНН. Переопределяется через .env: REQUISITES_SOURCES="url1;url2"
    requisites_sources: str = (
        "https://www.audit-it.ru/contragent/{inn};"
        "https://sbis.ru/contragents/{inn};"
        "https://e-ecolog.ru/org/{inn};"
        "https://zachestnyibiznes.ru/search?query={inn}"
    )

    @property
    def requisites_sources_list(self) -> list[str]:
        return [u.strip() for u in self.requisites_sources.split(";") if u.strip()]

    @property
    def custom_makers_list(self) -> list[str]:
        return [m.strip().lower() for m in self.custom_makers.split(";") if m.strip()]

    @property
    def vat_divisor(self) -> float:
        """Коэффициент для перевода цены С НДС в цену БЕЗ НДС (графа H = G / divisor)."""
        return 1 + self.vat_rate / 100.0


settings = Settings()

for _d in (UPLOAD_DIR, OUTPUT_DIR, CACHE_DIR, BASE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
