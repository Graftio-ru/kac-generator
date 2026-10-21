// Общая машинерия для обеих страниц «Генрих» (Сметчик и База-менеджер).
// Поведение настраивается через window.POS = { showSearch, showTsn, baseBadge }.
window.POS = Object.assign({ showSearch: false, showTsn: false, baseBadge: false }, window.POS || {});

const $ = (id) => document.getElementById(id);
const engineInput = $("engine");
const engineUrl = () => (engineInput ? engineInput.value.replace(/\/$/, "") : location.origin);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function requireOk(response) {
  if (response.ok) return response;
  let message = response.statusText;
  try {
    const payload = await response.json();
    message = payload.detail || message;
  } catch { /* response body is not JSON */ }
  throw new Error(message);
}

let positions = [];                 // модель таблицы позиций (общая)
let researchIssuesOpen = false;
const reviewPending = (p) => (p.needs_review || Object.keys(p.review_fields || {}).length > 0) && !(p.review_accepted && p.review_issue.trim());
const reviewUncertain = (p) => p.needs_review && p.review_accepted && !!p.review_issue.trim();
const MOSCOW_RE = /москв|московск|мо\b|подмосков/i;
const isMoscow = (city) => !city || MOSCOW_RE.test(city);

// Если открыто прямо из движка — тот же origin (без блокировки http-localhost)
if (engineInput && location.protocol === "http:" && /^(127\.0\.0\.1|localhost)/.test(location.host)) {
  engineInput.value = location.origin;
}

/* ---------- загрузка файлов ---------- */
const _stores = {};                 // listId → массив файлов
function wireDrop(dropId, inputId, store, listId, exts) {
  _stores[listId] = store;
  const drop = $(dropId), input = $(inputId);
  if (!drop || !input) return;
  drop.onclick = () => input.click();
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over");
    addFiles([...e.dataTransfer.files], store, listId, exts); };
  input.onchange = () => addFiles([...input.files], store, listId, exts);
}
function addFiles(files, store, listId, exts) {
  for (const f of files)
    if (exts.some((x) => f.name.toLowerCase().endsWith(x))) store.push(f);
  renderFiles(store, listId);
}
function renderFiles(store, listId) {
  $(listId).innerHTML = store.map((f, i) =>
    `<li>📄 ${esc(f.name)} <a href="#" data-i="${i}" data-list="${listId}" class="rm link">✕</a></li>`).join("");
}
document.addEventListener("click", (e) => {
  if (e.target.classList.contains("rm")) {
    e.preventDefault();
    const lid = e.target.dataset.list, store = _stores[lid];
    if (store) { store.splice(+e.target.dataset.i, 1); renderFiles(store, lid); }
  }
});

/* ---------- проверка движка ---------- */
async function checkEngine() {
  const el = $("engineStatus");
  try {
    const h = await (await fetch(engineUrl() + "/health")).json();
    const llmName = (h.llm_provider === "deepseek") ? "DeepSeek" : "Claude";
    const llmOk = (h.llm_key ?? h.anthropic_key);
    if (el) {
      el.innerHTML = `движок на связи · НДС ${h.vat_rate}% · ист.≥${h.min_sources} · `
        + (llmOk ? `${llmName} ✓` : `${llmName} ✗`) + " · "
        + (h.ocr_available ? "OCR ✓" : "OCR ✗") + " · "
        + (h.dadata_token ? "DaData ✓" : "DaData ✗");
      el.style.color = "var(--ok)";
    }
    const reqHint = $("reqHint");
    if (reqHint) reqHint.textContent = llmOk ? ""
      : `нужен ключ ${h.llm_provider === "deepseek" ? "DEEPSEEK_API_KEY" : "ANTHROPIC_API_KEY"}`;
    const bh = $("bulkHint");
    if (bh) bh.textContent = `3 максимальные цены для сметы; ${h.min_sources} источников — индикатор покрытия`;
    window._engineHealth = h;
  } catch {
    if (el) { el.textContent = "движок недоступен — запустите engine (start.command)"; el.style.color = "var(--err)"; }
    window._engineHealth = null;
  }
}
if (engineInput) { engineInput.onchange = checkEngine; checkEngine(); }

/* ---------- модель позиций ---------- */
function normalizePos(p) {
  const discipline = $("discipline");
  return {
    number: p.number, name: p.name || "", unit: p.unit ?? "шт", qty: p.qty || 0,
    is_unique: !!p.is_unique, discipline: p.discipline || (discipline ? discipline.value : "ЭМ"),
    needs_review: !!p.needs_review,
    review_issue: String(p.review_issue || ""),
    review_fields: { ...(p.review_fields || {}) },
    source_is_image: !!p.source_is_image,
    source_evidence: { ...(p.source_evidence || {}) },
    _sourceReviewed: !!p._sourceReviewed,
    review_accepted: !!p.review_accepted && !!String(p.review_issue || "").trim(),
    _ocr_source: !!(p._ocr_source ?? (p.source_is_image || p.needs_review)),
    source_index: Number.isInteger(p.source_index) ? p.source_index : null,
    type_mark: p.type_mark || "", full_name: p.full_name || "",
    offers: (p.offers || []).map((o) => ({
      price_with_vat: o.price_with_vat || 0, org_name: o.org_name || "", inn: o.inn || "",
      kpp: o.kpp || "", url: o.url || "", city: o.city || "",
      status: o.status || 2, from_letter: !!o.from_letter,
      product_title: o.product_title || "",
      page_title: o.page_title || "", page_article: o.page_article || "",
      identity_evidence: o.identity_evidence || {},
      price_unit: o.price_unit || "", listed_price_with_vat: o.listed_price_with_vat ?? null,
      normalized_price_unit: unitReviewPending(o) ? "" : o.normalized_price_unit || p.unit || "",
      price_unit_evidence: o.price_unit_evidence || "",
      source_quote_evidence: o.source_quote_evidence || {},
      catalogue_ref: { ...(o.catalogue_ref || {}) },
      catalogue_observed_at: Number(o.catalogue_observed_at) || 0,
      catalogue_source_sha256: o.catalogue_source_sha256 || "",
      catalogue_image: JSON.parse(JSON.stringify(o.catalogue_image || {})),
      seller_evidence: o.seller_evidence == null ? null : JSON.parse(JSON.stringify(o.seller_evidence)),
      catalogue_vat_status: o.catalogue_vat_status || "",
      catalogue_vat_notice: o.catalogue_vat_notice || "",
      price_capture_evidence: o.price_capture_evidence || {},
      price_capture_review_reason: captureReviewReason(o),
      match_review_required: !!o.match_review_required,
      model_review_accepted: !!o.model_review_accepted,
      unit_review_required: unitReviewPending(o),
      unit_review_reason: o.unit_review_reason || "",
      refresh_failed: !!o.refresh_failed,
      refresh_failure_reason: o.refresh_failure_reason || "",
      refresh_checked_at: Number(o.refresh_checked_at) || 0,
      in_moscow_region: typeof o.in_moscow_region === "boolean" ? o.in_moscow_region : null,
      region_status: o.region_status || "", region_evidence: o.region_evidence || "",
      region_approval: o.region_approval ? { ...o.region_approval } : null,
      screenshot_context: o.screenshot_context || "",
      context_capture_evidence: JSON.parse(JSON.stringify(o.context_capture_evidence || {})),
      context_capture_review_reason: o.context_capture_review_reason || "",
      screenshot_product: o.screenshot_product || "", screenshot_requisites: o.screenshot_requisites || "",
      _busy: false,
    })),
    tsn_base_price: p.tsn_base_price ?? null, tsn_coefficient: p.tsn_coefficient ?? null,
    _candidates: p._candidates || null, _in_base: p._in_base,
    _open: false, _searched: !!p._searched, _found: p._found || 0,
    _target: p._target || 0, _researchStatus: p._researchStatus || "",
    _searchStatus: p._searchStatus || p._researchStatus || "",
    _comparableSources: p._comparableSources, _coverage: p._coverage,
    _researchReason: p._researchReason || "",
    _researchDiagnostics: Array.isArray(p._researchDiagnostics) ? p._researchDiagnostics : [],
    _sourceChecks: Array.isArray(p._sourceChecks) ? p._sourceChecks : [],
    _catalogueRead: !!(p._catalogueRead ?? p.catalogue_read),
    _coverageWarning: p._coverageWarning || p.catalogue_coverage_warning || "",
    _catalogueProductId: p._catalogueProductId || p.catalogue_product_id || "",
    _catalogueRevision: p._catalogueRevision ?? p.catalogue_revision ?? 0,
    _catalogueRefreshAttempts: JSON.parse(JSON.stringify(p._catalogueRefreshAttempts || p.catalogue_refresh_attempts || [])),
    _researchApplied: typeof p._researchApplied === "string" ? p._researchApplied : "",
    _diagnosticsOpen: false,
    _matchReviewCount: p._matchReviewCount || 0,
  };
}
function applyCatalogueMetadata(position, row) {
  position._catalogueRead = !!row.catalogue_read;
  position._coverageWarning = row.catalogue_coverage_warning || "";
  position._catalogueProductId = row.catalogue_product_id || "";
  position._catalogueRevision = row.catalogue_revision ?? 0;
  position._catalogueRefreshAttempts = JSON.parse(JSON.stringify(row.catalogue_refresh_attempts || []));
}
function addPosition() {
  positions.push(normalizePos({ number: positions.length + 1, name: "", _open: true }));
  positions[positions.length - 1]._open = true;
}

/* ---------- рендер таблицы позиций ---------- */
function researchCandidates(p) {
  const candidates = (p._candidates || []).map((o) => ({ ...o }));
  for (const offer of p.offers || []) {
    const index = candidates.findIndex((c) => offer.url && c.url === offer.url);
    if (index < 0) candidates.push({ ...offer });
    else candidates[index] = { ...offer };
  }
  return candidates;
}
// Frozen Unicode 15.0.0 decimal alphabet; exactly matches supplier.py.
const SELLER_DECIMAL_ZEROS = [48, 1632, 1776, 1984, 2406, 2534, 2662, 2790, 2918, 3046, 3174, 3302, 3430, 3558, 3664, 3792, 3872, 4160, 4240, 6112, 6160, 6470, 6608, 6784, 6800, 6992, 7088, 7232, 7248, 42528, 43216, 43264, 43472, 43504, 43600, 44016, 65296, 66720, 68912, 69734, 69872, 69942, 70096, 70384, 70736, 70864, 71248, 71360, 71472, 71904, 72016, 72784, 73040, 73120, 73552, 92768, 92864, 93008, 120782, 120792, 120802, 120812, 120822, 123200, 123632, 124144, 125264, 130032];
const SELLER_DECIMAL_DIGITS = new Map(SELLER_DECIMAL_ZEROS.flatMap(z =>
  Array.from({length: 10}, (_, d) => [String.fromCodePoint(z + d), String(d)])));
function canonicalInn(value) {
  return Array.from(String(value || ""), c => SELLER_DECIMAL_DIGITS.get(c) || "").join("");
}
function validSellerInn(value) {
  const digits = canonicalInn(value);
  if (!/^(?:[0-9]{10}|[0-9]{12})$/.test(digits) || !/[1-9]/.test(digits)) return false;
  const d = Array.from(digits, Number);
  const sum = (weights) => weights.reduce((n, w, i) => n + w * d[i], 0) % 11 % 10;
  return d.length === 10 ? sum([2,4,10,3,5,9,4,6,8]) === d[9]
    : sum([7,2,4,10,3,5,9,4,6,8]) === d[10] && sum([3,7,2,4,10,3,5,9,4,6,8]) === d[11];
}
function offerSellerHost(o) {
  try { return new URL(o.url).hostname.toLowerCase().replace(/^www\./, ""); }
  catch { return ""; }
}
function offerSellerKey(o) {
  const inn = String(o.inn || o.requisites?.inn || "").trim();
  if (inn) return `inn:${canonicalInn(inn) || inn}`;
  const host = offerSellerHost(o);
  return host ? `host:${host}` : "";
}
function sellerOriginNotice(o) {
  if (o.from_letter) return "Цена из письма";
  const e = o.seller_evidence;
  if (e == null) return "Происхождение реквизитов не сохранено: прежние данные";
  if (e.version !== "seller-evidence-v1") return "Сведения о происхождении реквизитов повреждены";
  const status = e.decision?.status;
  return status === "role_confirmed" ? "Продавец указан в текущем источнике"
    : status === "reference_accepted" ? "Реквизиты из утверждённого справочника поставщиков"
    : status === "conflict" ? "Реквизиты продавца противоречат источнику или справочнику"
    : "Роль продавца не подтверждена источником";
}
function sellerEvidenceDetails(o) {
  const sources = o.seller_evidence?.sources;
  if (!Array.isArray(sources) || !sources.length) return "";
  const origin = {scoped_offer:"Карточка товара", published_seller_terms:"Условия продажи", approved_supplier_reference:"Справочник поставщиков", identity_enrichment:"Уточнение реквизитов"};
  const role = {seller:"Продавец", manufacturer:"Производитель", marketplace_operator:"Оператор площадки", unknown:"Роль не определена"};
  const rows = sources.slice(0, 8).map(s => {
    const text = value => esc(String(value || "").slice(0, 2048));
    let link = text(s.source_url);
    try { const url = new URL(s.source_url); if (["http:","https:"].includes(url.protocol)) link = `<a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">${text(s.source_url)}</a>`; } catch { /* escaped source text */ }
    const date = new Date(Number(s.source_observed_at) * 1000);
    const readAt = Number(s.source_observed_at) > 0 && Number.isFinite(date.getTime()) ? date.toISOString() : "дата не сохранена";
    return `<li>${text(origin[s.origin] || "Источник не определён")} · ${text(role[s.role] || "Роль не определена")}: ${text(s.raw_name)} · ИНН ${text(s.raw_inn)}<br>${link}<br>Источник прочитан: ${esc(readAt)}</li>`;
  }).join("");
  return `<details><summary>Источники реквизитов</summary><ul>${rows}</ul></details>`;
}
function sellerIdentityAllowed(o) {
  if (!o.from_letter && o.seller_evidence != null
      && (o.seller_evidence.version !== "seller-evidence-v1"
        || !["role_confirmed", "reference_accepted"].includes(o.seller_evidence.decision?.status))) return false;
  // Existing manual/letter inputs have a different authority than card data.
  const automatic = !o.from_letter && (o.listed_price_with_vat != null
    || (o.source_quote_evidence && Object.keys(o.source_quote_evidence).length > 0));
  return !automatic || (String(o.org_name || o.requisites?.name || "").trim()
    && validSellerInn(o.inn || o.requisites?.inn || ""));
}
function sellerHostContext(offers) {
  const known = new Map();
  for (const o of offers) {
    const key = offerSellerKey(o), host = offerSellerHost(o);
    if (!host || !key.startsWith("inn:")) continue;
    if (!known.has(host)) known.set(host, new Set());
    known.get(host).add(key);
  }
  return known;
}
function contextualSellerKey(o, known) {
  const key = offerSellerKey(o);
  if (!key.startsWith("host:")) return key;
  const identified = known.get(key.slice(5));
  return identified?.size > 1 ? "" : identified?.size === 1 ? identified.values().next().value : key;
}
function regionApproval(o) {
  if (typeof o.url !== "string" || /[\\\x00-\x20\x7f]/.test(o.url)) return false;
  const approval = o.region_approval;
  if (!approval || approval.policy !== "operator-approved-sites-moscow-v1"
      || approval.source_ref !== "approved-supplier-list" || !/^[a-f0-9]{64}$/.test(approval.source_sha256 || "")) return false;
  try {
    const url = new URL(o.url), host = url.hostname.toLowerCase().replace(/^www\./, "").replace(/\.$/, "");
    const domain = String(approval.host || "").toLowerCase();
    return !url.username && !url.password && ["http:", "https:"].includes(url.protocol) && !!domain && (host === domain || host.endsWith("." + domain));
  } catch (_) { return false; }
}
function regionStatus(o) {
  if (regionApproval(o)) return "confirmed";
  const evidence = String(o.region_evidence || "").trim();
  if (o.region_status === "unknown") return "unknown";
  if (o.region_status === "incompatible") return evidence ? "incompatible" : "unknown";
  if (o.region_status === "confirmed") return evidence || o.in_moscow_region === true ? "confirmed" : "unknown";
  if (o.in_moscow_region === true) return "confirmed";
  return o.in_moscow_region === false && evidence ? "incompatible" : "unknown";
}
function regionApprovalNotice(o) {
  return regionApproval(o) ? "Регион поставки допущен по согласованному перечню сайтов; не подтверждение наличия/самовывоза" : "";
}
function regionNotice(o) {
  if (o.from_letter || regionStatus(o) === "confirmed") return "";
  if (regionStatus(o) === "incompatible") return "Регион поставки не соответствует условиям: " + o.region_evidence;
  return "Регион поставки не подтверждён: " + (String(o.region_evidence || "").trim() || "нет подтверждения склада/самовывоза или доставки в Москву/МО");
}
function priceSaleUnit(value) {
  const u = String(value || "").trim().toLowerCase().replace(/[. ]+$/, "");
  if (["м", "метр", "метра", "метры", "м.п", "пог.м"].includes(u)) return "м";
  if (["шт", "штука", "штуки", "штук"].includes(u)) return "шт";
  if (["уп", "упак", "упаковка", "упаковки"].includes(u)) return "уп";
  return u;
}
function captureReviewReason(o) {
  // Attachments do not determine product/price/unit/seller eligibility.
  return "";
}

function unitReviewPending(o) {
  return !!o.unit_review_required || (!o.price_unit && o.listed_price_with_vat != null);
}
function researchPriceEligible(o) {
  return sellerIdentityAllowed(o) && !captureReviewReason(o) && !o.refresh_failed
    && regionStatus(o) !== "incompatible" && !(o.match_review_required && !o.model_review_accepted)
    && !unitReviewPending(o) && Number.isFinite(Number(o.price_with_vat)) && Number(o.price_with_vat) > 0;
}
function replacementCandidates(p, oi) {
  const candidates = (p._candidates || []).filter((o) => o.url && o.url !== p.offers[oi]?.url && researchPriceEligible(o));
  const selected = p.offers.filter(researchPriceEligible);
  const known = sellerHostContext([...selected, ...candidates]);
  const used = new Set(p.offers.filter((o, i) => i !== oi && researchPriceEligible(o))
    .map((o) => contextualSellerKey(o, known)).filter(Boolean));
  return candidates.filter((o) => {
    const key = contextualSellerKey(o, known);
    return key && !used.has(key);
  }).sort((a, b) => Number(b.price_with_vat) - Number(a.price_with_vat));
}
function refreshCoverage(p) {
  if (!p._searched || ["pending", "running", "skipped", "stale", "error"].includes(p._researchStatus)) return;
  const eligible = researchCandidates(p).filter((o) => !o.from_letter && researchPriceEligible(o));
  const known = sellerHostContext(eligible);
  const sources = new Set(eligible.map((o) => contextualSellerKey(o, known)).filter(Boolean));
  p._comparableSources = sources.size;
  p._coverage = sources.size >= 5 ? "wide" : sources.size >= 3 ? "limited" : "insufficient";
  if (!p._catalogueRead) p._researchStatus = sources.size >= 3 ? "complete" : p._found ? "partial" : "failed";
}
function renderPositions() {
  positions.forEach(refreshCoverage);
  const posCard = $("posCard");
  const buildCard = $("buildCard");
  const posCount = $("posCount");
  if (posCard) posCard.style.display = positions.length ? "block" : "none";
  if (buildCard) buildCard.style.display = positions.length ? "block" : "none";
  if (posCount) {
    const pending = positions.filter(reviewPending).length;
    const uncertain = positions.filter(reviewUncertain).length;
    posCount.textContent = positions.length
      ? `${positions.length} позиц.${pending ? ` · ${pending} требуют решения` : ""}${uncertain ? ` · ${uncertain} с замечанием` : ""}` : "";
  }
  const reviewSummary = $("reviewSummary");
  if (reviewSummary) {
    const open = positions.map((p, i) => ({ p, i })).filter(({ p }) => reviewPending(p));
    const accepted = positions.map((p, i) => ({ p, i })).filter(({ p }) => reviewUncertain(p));
    reviewSummary.style.display = open.length || accepted.length ? "block" : "none";
    reviewSummary.innerHTML = open.length || accepted.length
      ? `<strong>Проверка распознавания: ${open.length} требуют решения, ${accepted.length} используются с замечанием.</strong>
         <p>Если исходник не позволяет уточнить поле, запишите причину у позиции и явно выберите «Использовать с замечанием». Эти строки останутся в предупреждении перед сборкой.</p>
         ${open.slice(0, 15).map(({ p, i }) => `<button type="button" class="ghost sm" data-review-jump="${i}">№${p.number}${p.review_issue ? `: ${esc(p.review_issue)}` : " — сверить"}</button>`).join(" ")}
         ${open.length > 15 ? `<span>и ещё ${open.length - 15}</span>` : ""}
         ${accepted.length ? `<p>Используются с замечанием:</p>${accepted.map(({ p, i }) => `<button type="button" class="ghost sm" data-review-jump="${i}">№${p.number}: ${esc(p.review_issue)}</button>`).join(" ")}` : ""}` : "";
  }
  const researchIssues = $("researchIssues");
  if (researchIssues) {
    const issues = positions.map((p, i) => ({ p, i }));
    const failed = issues.filter(({ p }) => ["failed", "error", "stale"].includes(p._researchStatus)
      || (p._researchStatus === "skipped" && !p.is_unique));
    const partial = issues.filter(({ p }) => p._researchStatus === "partial");
    researchIssues.style.display = failed.length || partial.length ? "block" : "none";
    const jump = ({ p, i }) => `<button type="button" class="ghost sm" data-research-jump="${i}" title="${esc(p._researchReason)}">№${esc(p.number)}: ${esc(p._researchReason.split(";")[0] || "проверьте поиск")}</button>`;
    researchIssues.innerHTML = failed.length || partial.length
      ? `<strong>Поиск требует внимания: без цен или с ошибкой ${failed.length}, недостаточно источников ${partial.length}.</strong>
         ${failed.length ? `<div class="research-issue-buttons">${failed.map(jump).join(" ")}</div>` : ""}
         ${partial.length ? `<details${researchIssuesOpen ? " open" : ""}><summary>Показать ${partial.length} позиций с недостатком источников</summary><div class="research-issue-buttons">${partial.map(jump).join(" ")}</div></details>` : ""}` : "";
  }
  $("posList").innerHTML = positions.map(posHtml).join("");
  if (typeof onPositionsRendered === "function") onPositionsRendered();
}
const reviewSummary = $("reviewSummary");
if (reviewSummary) reviewSummary.addEventListener("click", (event) => {
  const button = event.target.closest("[data-review-jump]");
  if (!button) return;
  const index = Number(button.dataset.reviewJump);
  positions[index]._open = true;
  renderPositions();
  $("posList").children[index]?.scrollIntoView({ behavior: "smooth", block: "center" });
});
const researchIssues = $("researchIssues");
if (researchIssues) researchIssues.addEventListener("click", (event) => {
  if (event.target.closest("details summary")) {
    researchIssuesOpen = !event.target.closest("details").open;
    return;
  }
  const button = event.target.closest("[data-research-jump]");
  if (!button) return;
  const index = Number(button.dataset.researchJump);
  if (!positions[index]) return;
  positions[index]._open = true;
  positions[index]._diagnosticsOpen = true;
  renderPositions();
  $("posList").children[index]?.scrollIntoView({ behavior: "smooth", block: "center" });
});
function thumbsHtml(o, eng) {
  const cell = (name, label) => name
    ? `<img src="${eng}/cache/${esc(name)}" alt="${label}" title="${label}" class="zoom">`
    : `<span class="empty" title="нет скриншота"></span>`;
  return `<div class="thumbs">${cell(o.screenshot_product, "карточка")}${cell(o.screenshot_requisites, "реквизиты")}${cell(o.screenshot_context, "карточка — приложение")}</div>`;
}
const diagnosticLabels = {
  price_or_unit_unconfirmed: "Цена или единица не подтверждена",
  product_mismatch: "Не тот товар",
  brand_unconfirmed: "Не подтверждён производитель",
  article_mismatch: "Не тот артикул",
  page_article_mismatch: "Артикул карточки не совпал",
  seller_unconfirmed: "Не подтверждён поставщик",
  http_blocked: "Сайт заблокировал запрос",
  captcha: "Требуется капча",
  site_error: "Ошибка сайта или таймаут",
  supplier_paused: "Сайт временно пропущен после повторных сбоев",
  job_budget: "Исчерпан бюджет первого прохода; можно повторить поиск",
  minimum_price: "Указана только цена «от»",
  product_unconfirmed: "Соответствие товара не подтверждено",
  extract_failed: "Цена не извлечена",
  empty_page: "Пустая страница",
  listing: "Открылась категория вместо товара",
  card_not_found: "Точная карточка не найдена",
  out_of_stock: "Нет в наличии",
  not_direct_supplier: "Это не прямой поставщик",
  search_error: "Ошибка поискового API",
  article_search_error: "Ошибка поиска по артикулу",
  search_budget: "Поиск остановлен по лимиту времени",
};
function researchDiagnosticsHtml(diagnostics, positionIndex, isOpen) {
  if (!Array.isArray(diagnostics) || !diagnostics.length) return "";
  const rows = diagnostics.filter((item) => item && item.code !== "phase_timing");
  if (!rows.length) return "";
  const items = rows.map((item) => {
    let site = "";
    let safeUrl = "";
    try {
      const parsed = new URL(item.url);
      if (["http:", "https:"].includes(parsed.protocol)) {
        site = parsed.hostname;
        safeUrl = parsed.href;
      }
    } catch { /* URL may be absent */ }
    const source = safeUrl
      ? `<a href="${esc(safeUrl)}" target="_blank" rel="noopener noreferrer">${esc(site)}</a>`
      : esc(site || "Поиск");
    return `<li><strong>${source}</strong>: ${esc(diagnosticLabels[item.code] || item.code || "Причина неизвестна")}${item.reason ? ` — ${esc(item.reason)}` : ""}</li>`;
  }).join("");
  return `<details class="research-diagnostics" data-p="${positionIndex}"${isOpen ? " open" : ""}><summary>Подробности поиска: ${rows.length} замечаний</summary><ul>${items}</ul></details>`;
}
// Review evidence is separate from selected offers and never changes selection.
function reviewObservationKey(o) {
  const provenance = [o.url || "", o.catalogue_ref?.observation_id || "",
    o.catalogue_observed_at || "", o.catalogue_source_sha256 || ""];
  if (!provenance.slice(1).some(Boolean)) {
    provenance.push(o.org_name || "", o.inn || "", o.listed_price_with_vat ?? null,
      o.price_with_vat ?? null, o.price_unit || "", o.normalized_price_unit || "");
  }
  return JSON.stringify(provenance);
}
function reviewPriceCandidates(p) {
  const seen = new Set((p.offers || []).map(reviewObservationKey));
  return (Array.isArray(p._candidates) ? p._candidates : []).filter((o) => {
    if (!o || typeof o !== "object") return false;
    const key = reviewObservationKey(o);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
function reviewPricesHtml(candidates) {
  if (!candidates.length) return "";
  const amount = (v) => typeof v === "number" && Number.isFinite(v) && v > 0 ? `${esc(v)} ₽` : "цена не сохранена";
  const rows = candidates.map((o) => {
    let safeUrl = "";
    try {
      const parsed = new URL(o.url);
      if (["http:", "https:"].includes(parsed.protocol)) safeUrl = parsed.href;
    } catch { /* Keep invalid source URLs as escaped text only. */ }
    const source = safeUrl ? `<a href="${esc(safeUrl)}" target="_blank" rel="noopener noreferrer">${esc(o.url)}</a>` : esc(o.url || "Ссылка не сохранена");
    const date = new Date(Number(o.catalogue_observed_at) * 1000);
    const observed = Number(o.catalogue_observed_at) > 0 && Number.isFinite(date.getTime())
      ? date.toISOString() : "дата не сохранена";
    const notices = [sellerOriginNotice(o), o.catalogue_vat_notice || "Сведения об НДС не сохранены",
      regionNotice(o), regionApprovalNotice(o), o.unit_review_reason,
      unitReviewPending(o) ? "Единица/комплектация не подтверждена" : "",
      o.match_review_required && !o.model_review_accepted ? "Соответствие марки требует проверки" : "",
      o.refresh_failure_reason, o.reason, o.price_capture_review_reason,
      !o.org_name || !o.inn ? "Поставщик не подтверждён: нет наименования или ИНН" : "",
      !o.screenshot_product && !o.catalogue_image?.sha256 ? "Скриншот карточки отсутствует" : "",
      !o.screenshot_requisites ? "Скриншот реквизитов отсутствует" : "",
      !o.screenshot_context ? "Скриншот контекста отсутствует" : ""].filter(Boolean);
    return `<li><p><strong>${esc(o.org_name || "Поставщик не подтверждён")}</strong>${o.inn ? ` · ИНН ${esc(o.inn)}` : ""}</p>
      ${o.product_title ? `<p>${esc(o.product_title)}</p>` : ""}
      <p>На сайте: ${amount(o.listed_price_with_vat)} / ${esc(o.price_unit || "единица не указана")}</p>
      ${!unitReviewPending(o) && o.normalized_price_unit ? `<p>В единице позиции: ${amount(o.price_with_vat)} / ${esc(o.normalized_price_unit)}</p>` : ""}
      ${o.price_unit_evidence ? `<p class="muted">Единица на сайте: ${esc(o.price_unit_evidence)}</p>` : ""}
      <p>${source}</p><p class="muted">Дата наблюдения: ${esc(observed)}</p>
      ${o.catalogue_source_sha256 ? `<details><summary>Сведения об источнике</summary><p class="muted">Отпечаток источника: ${esc(o.catalogue_source_sha256)}</p></details>` : ""}
      <ul>${notices.map((notice) => `<li>${esc(notice)}</li>`).join("")}</ul>${sellerEvidenceDetails(o)}</li>`;
  }).join("");
  return `<details class="research-diagnostics review-prices" open><summary>Цены для проверки: ${candidates.length} · не выбраны для сметы</summary><ul>${rows}</ul></details>`;
}
function posHtml(p, i) {
  const reviewPrices = window.POS.showSearch ? reviewPriceCandidates(p) : [];
  const eng = engineUrl();
  const selectedReviewCount = p.offers.filter((o) => o.match_review_required && !o.model_review_accepted).length;
  const offers = p.offers.map((o, j) => {
    const badRegion = regionStatus(o) === "incompatible";
    const offerHtml = `
    <tr${badRegion ? ' style="background:rgba(210,153,34,.12)"' : ""}>
      <td><input type="number" step="0.01" value="${o.price_with_vat || ""}" data-p="${i}" data-o="${j}" data-f="price_with_vat" style="width:108px">
        <small class="muted">₽ / ${esc(o.unit_review_required ? "единица не подтверждена" : o.normalized_price_unit || p.unit)}</small>
        ${regionNotice(o) ? `<small class="review-badge">${esc(regionNotice(o))}</small>` : ""}
        ${regionApprovalNotice(o) ? `<small class="muted">${esc(regionApprovalNotice(o))}</small>` : ""}
        ${captureReviewReason(o) ? `<small class="review-badge">${esc(captureReviewReason(o))}</small>` : ""}
        ${o.unit_review_required ? `<small class="review-badge">${esc(o.unit_review_reason || "Проверьте единицу/комплектацию")}</small>` : ""}
        ${o.price_unit ? `<small class="muted">На сайте: ${o.listed_price_with_vat == null ? "цена не сохранена" : `${esc(o.listed_price_with_vat)} ₽`} / ${esc(o.price_unit)}</small>` : ""}</td>
      <td><input type="text" value="${esc(o.org_name)}" data-p="${i}" data-o="${j}" data-f="org_name"></td>
      <td><input type="text" value="${esc(o.inn)}" data-p="${i}" data-o="${j}" data-f="inn" style="width:102px"></td>
      <td><input type="text" value="${esc(o.city)}" data-p="${i}" data-o="${j}" data-f="city" style="width:96px" title="Сведения о городе; регион поставки проверяется отдельно" placeholder="город"></td>
      <td><input type="text" value="${esc(o.url)}" data-p="${i}" data-o="${j}" data-f="url">
        ${o.product_title ? `<small class="offer-product-title muted" title="Название на сайте">${esc(o.product_title)}</small>` : ""}
        ${o.match_review_required ? `<label class="offer-model-review review-badge" title="Марка не подтверждена точным совпадением с карточкой товара"><input type="checkbox" data-p="${i}" data-o="${j}" data-f="model_review_accepted" ${o.model_review_accepted ? "checked" : ""}> Марка сверена с карточкой</label>` : ""}</td>
      <td>${thumbsHtml(o, eng)}</td>
      <td><button class="ghost sm capture" data-p="${i}" data-o="${j}" title="снять скриншот по URL">${o._busy ? '<span class="spin"></span>' : "📷"}</button></td>
      ${window.POS.showSearch ? `<td><button class="ghost sm repl" data-p="${i}" data-o="${j}" title="заменить следующим кандидатом по цене">⤵</button></td>` : ""}
      <td><input type="checkbox" ${o.from_letter ? "checked" : ""} data-p="${i}" data-o="${j}" data-f="from_letter" title="из письма (не в ТКП)"></td>
      <td><a href="#" class="rmoffer link" data-p="${i}" data-o="${j}">✕</a></td>
    </tr>`;
    return ["pending", "running"].includes(p._researchStatus)
      ? offerHtml.replace(/<(input|button)\b/g, "<$1 disabled") : offerHtml;
  }).join("");
  const cols = window.POS.showSearch ? 10 : 9;
  const searchBtn = (window.POS.showSearch && !p.is_unique)
    ? `<button class="ghost sm search" data-p="${i}" title="найти цены онлайн">${p._searching ? '<span class="spin"></span> ищу…' : "🔎 найти цены"}</button>` : "";
  const badge = window.POS.showSearch && p._searched
    ? `<span class="pill">найдено ${p._found}; сопоставимых: ${p._comparableSources ?? "—"}; минимум для сметы: 3; покрытие: ${p._coverage === "wide" ? "широкое" : p._coverage === "limited" ? "ограниченное" : p._coverage === "insufficient" ? "недостаточное" : "не оценено"}</span>` : "";
  const researchBadge = window.POS.showSearch && p._researchStatus
    ? `<span class="pill" title="${esc(p._researchReason)}">${esc({ pending: "ожидает поиска", running: "поиск продолжается", complete: "цены найдены", partial: "частичный результат", failed: "цен нет", skipped: "поиск пропущен", stale: "данные изменены", error: "ошибка поиска" }[p._researchStatus] || p._researchStatus)}</span>` : "";
  const matchReviewBadge = window.POS.showSearch && selectedReviewCount
    ? `<span class="pill" style="border-color:var(--warn);color:var(--warn)" title="Отмеченные предложения требуют сверки перед сборкой">сверить марку: ${selectedReviewCount}</span>` : "";
  const baseBadge = window.POS.baseBadge
    ? (p._in_base === false ? `<span class="badge-missing" title="позиции нет в базе цен">нет в базе</span>`
      : p._in_base === true ? `<span class="badge-base">из базы</span>` : "") : "";
  const reviewBadge = reviewUncertain(p) ? '<span class="review-badge">⚠ с замечанием</span>'
    : reviewPending(p) ? '<span class="review-badge">⚠ требует решения</span>' : "";
  const tsn = window.POS.showTsn ? `
      <div class="tsn-row">
        <div><label>Базовая расценка ТСН-2001</label>
          <input type="number" step="0.01" value="${p.tsn_base_price ?? ""}" data-p="${i}" data-f="tsn_base_price" placeholder="из справочника если пусто"></div>
        <div><label>Коэффициент пересчёта</label>
          <input type="number" step="0.01" value="${p.tsn_coefficient ?? ""}" data-p="${i}" data-f="tsn_coefficient"></div>
      </div>` : "";
  return `
  <div class="pos ${p.is_unique ? "unique" : ""} ${p._in_base === false ? "missing" : ""}">
    <div class="pos-head">
      <span class="posnum" title="порядковый номер позиции">№${i + 1}</span>
      <input class="num" type="number" value="${p.number}" data-p="${i}" data-f="number" title="№ п.п. (графа А в КАЦ)">
      <input class="nm" type="text" value="${esc(p.name)}" data-p="${i}" data-f="name" placeholder="Наименование">
      <input class="mark" type="text" value="${esc(p.type_mark)}" data-p="${i}" data-f="type_mark" placeholder="Марка / артикул" title="тип, марка или артикул из исходной спецификации">
      <input class="u" type="text" value="${esc(p.unit)}" data-p="${i}" data-f="unit" title="ед.">
      <input class="q" type="number" step="0.001" value="${p.qty || ""}" data-p="${i}" data-f="qty" title="кол-во">
      <label class="muted" style="font-weight:500"><input type="checkbox" ${p.is_unique ? "checked" : ""} data-p="${i}" data-f="is_unique"> уник.</label>
      <label class="muted" style="font-weight:500"><input type="checkbox" ${p._sourceReviewed ? "checked" : ""} data-p="${i}" data-f="reviewed"> сверено с оригиналом</label>
      ${reviewBadge}
      ${p.source_index !== null && window.POS.openSource
        ? `<button class="ghost sm source-file" data-source="${p.source_index}" title="открыть исходный файл">📄 оригинал</button>` : ""}
      ${searchBtn}${badge}${researchBadge}${matchReviewBadge}${baseBadge}
      <span class="toggle" data-toggle="${i}">${p._open ? "свернуть ▲" : "цены ▼ (" + p.offers.length + ")" + (reviewPrices.length ? "; для проверки: " + reviewPrices.length : "")}</span>
      <a href="#" class="rmpos link" data-p="${i}" title="удалить позицию">🗑</a>
    </div>
    <div class="pos-body ${p._open ? "open" : ""}">
      ${p._coverageWarning ? `<div class="hint">${esc(p._coverageWarning)}</div>` : ""}
      ${p._researchReason ? `<div class="hint">Поиск: ${esc(p._researchReason)}</div>` : ""}
      ${p._sourceChecks?.length ? `<details><summary>Проверки сайтов: ${p._sourceChecks.filter(c => !['pending','running','incomplete'].includes(c.status)).length} из ${p._sourceChecks.length}</summary>${p._sourceChecks.map(c => `<p><strong>${esc(c.host)}</strong> (${esc(c.status)}): ${esc(c.reason || "Причина не сохранена") }</p>`).join('')}</details>` : ''}
      ${researchDiagnosticsHtml(p._researchDiagnostics, i, p._diagnosticsOpen)}
      ${researchCandidates(p).some((o) => regionNotice(o)) ? `<details class="research-diagnostics"><summary>Регион поставки: замечания по предложениям</summary><ul>${researchCandidates(p).filter((o) => regionNotice(o)).map((o) => `<li>${esc(o.org_name || o.url)} (${esc(o.url)}): ${esc(regionNotice(o))}</li>`).join("")}</ul></details>` : ""}
      ${researchCandidates(p).some((o) => o.unit_review_required) ? `<details class="research-diagnostics"><summary>Предложения с неподтверждённой единицей/комплектацией</summary><ul>${researchCandidates(p).filter((o) => o.unit_review_required).map((o) => `<li>${esc(o.org_name || o.url)}: ${esc(o.listed_price_with_vat ?? o.price_with_vat)} ₽ / ${esc(o.price_unit || "не указана")} — ${esc(o.unit_review_reason || "Требуется проверка")}</li>`).join("")}</ul></details>` : ""}
      ${researchCandidates(p).some((o) => captureReviewReason(o)) ? `<details class="research-diagnostics"><summary>Цена/снимок основной карточки требуют проверки</summary><ul>${researchCandidates(p).filter((o) => captureReviewReason(o)).map((o) => `<li>${esc(o.org_name || o.url)}: ${esc(o.listed_price_with_vat ?? o.price_with_vat)} ₽ / ${esc(o.price_unit || "не указана")} — ${esc(captureReviewReason(o))}</li>`).join("")}</ul></details>` : ""}
      ${p._ocr_source ? `<div class="review-detail">
        ${Object.keys(p.review_fields || {}).length ? `<ul>${Object.entries(p.review_fields).map(([field, reason]) => `<li>${esc(({name:"Наименование",type_mark:"Артикул/марка",unit:"Единица",qty:"Количество",source:"Исходная строка"})[field] || field)}: ${esc(reason)}</li>`).join("")}</ul>` : `<p class="muted">Автоматически распознано; конкретных замечаний нет. Это не отметка о проверке человеком.</p>`}
        <label>Что не удалось проверить по исходнику</label>
        <textarea data-p="${i}" data-f="review_issue" placeholder="Например: артикул закрыт штампом">${esc(p.review_issue)}</textarea>
        <label class="muted"><input type="checkbox" ${reviewUncertain(p) ? "checked" : ""} data-p="${i}" data-f="review_accepted"> Использовать текущие данные с этим замечанием</label>
      </div>` : ""}
      ${reviewPrices.length ? reviewPricesHtml(reviewPrices) + "\n      " : ""}<table class="offers">
        <thead><tr><th>Цена с НДС</th><th>Поставщик</th><th>ИНН</th><th>Город склада</th><th>Ссылка</th><th>Скриншоты</th><th></th>${window.POS.showSearch ? "<th></th>" : ""}<th>письмо</th><th></th></tr></thead>
        <tbody>${offers || `<tr><td colspan="${cols}" class="muted">${reviewPrices.length ? "Цены для сметы не выбраны — найденные цены доступны выше для проверки" : "нет цен — добавьте или нажмите «найти цены»"}</td></tr>`}</tbody>
      </table>
      <button class="ghost sm addoffer" data-p="${i}" style="margin-top:8px">+ цена</button>
      ${tsn}
    </div>
  </div>`;
}

/* ---------- события таблицы ---------- */
function _bindPosList() {
  const list = $("posList");
  if (!list) return;
  list.addEventListener("input", (e) => {
    const t = e.target, pi = +t.dataset.p;
    if (t.dataset.p === undefined) return;
    const f = t.dataset.f;
    let val = t.type === "checkbox" ? t.checked : t.value;
    if (t.dataset.o !== undefined) {
      const oi = +t.dataset.o;
      if (["price_with_vat"].includes(f)) val = parseFloat(val) || 0;
      const offer = positions[pi].offers[oi];
      const previousUrl = offer.url;
      offer[f] = val;
      if (f === "url") {
        offer.product_title = "";
        offer.page_title = "";
        offer.page_article = "";
        offer.identity_evidence = {};
        offer.price_unit = "";
        offer.price_unit_evidence = "";
        offer.normalized_price_unit = "";
        offer.listed_price_with_vat = null;
        offer.match_review_required = false;
        offer.model_review_accepted = false;
        offer.unit_review_required = true;
        offer.unit_review_reason = "Ссылка изменена: перепроверьте цену и единицу продажи";
        offer.in_moscow_region = null;
        offer.region_status = "unknown"; offer.region_evidence = "";
        offer.source_quote_evidence = {};
        offer.identity_evidence = {};
        offer.price_capture_evidence = {};
        offer.price_capture_review_reason = "";
        offer.screenshot_product = "";
        offer.screenshot_context = ""; offer.context_capture_evidence = {};
        offer.context_capture_review_reason = "Ссылка изменена: контекст карточки нужно снять заново";
      }
      const candidateIndex = (positions[pi]._candidates || []).findIndex((c) => previousUrl && c.url === previousUrl);
      if (candidateIndex >= 0) positions[pi]._candidates[candidateIndex] = { ...offer };
    } else if (f) {
      if (f === "qty") val = parseFloat(val) || 0;
      else if (f === "number") val = parseInt(val) || pi + 1;
      else if (["tsn_base_price", "tsn_coefficient"].includes(f)) val = val === "" ? null : parseFloat(val);
      if (f === "reviewed") {
        positions[pi].needs_review = !val;
        positions[pi]._sourceReviewed = val;
        if (val) positions[pi].review_fields = {};
        positions[pi].review_accepted = false;
      }
      else if (f === "review_accepted") {
        if (val && !positions[pi].review_issue.trim()) {
          alert("Сначала запишите, что именно не удалось проверить");
          t.checked = false;
          return;
        }
        positions[pi].review_accepted = val;
        if (val) positions[pi].needs_review = true;
      }
      else positions[pi][f] = val;
      if (["name", "type_mark", "unit", "qty", "discipline", "is_unique"].includes(f) && positions[pi]._researchStatus) {
        positions[pi]._researchStatus = "stale";
        positions[pi]._researchReason = "Данные позиции изменены; повторите поиск цен";
        positions[pi]._researchDiagnostics = [];
        positions[pi]._sourceChecks = [];
        applyCatalogueMetadata(positions[pi], {});
        positions[pi]._matchReviewCount = 0;
        positions[pi]._searched = false;
        positions[pi]._candidates = [];
        positions[pi].offers = positions[pi].offers.filter((o) => o.from_letter);
      }
      if (positions[pi]._ocr_source &&
          ["number", "name", "type_mark", "unit", "qty"].includes(f)) {
        positions[pi].needs_review = true;
        positions[pi]._sourceReviewed = false;
        positions[pi].review_accepted = false;
      }
      if (["is_unique", "reviewed", "review_accepted"].includes(f)) renderPositions();
    }
  });
  list.addEventListener("change", (e) => {
    const t = e.target;
    if (t.dataset.p === undefined) return;
    if (t.dataset.o !== undefined) {
      renderPositions();
      return;
    }
    const p = positions[+t.dataset.p];
    if (p?._ocr_source &&
        ["number", "name", "type_mark", "unit", "qty"].includes(t.dataset.f)) {
      renderPositions();
    } else if (t.dataset.f === "review_issue") {
      p.review_accepted = false;
      renderPositions();
    }
  });
  list.addEventListener("click", (e) => {
    const diagnosticSummary = e.target.closest("details.research-diagnostics summary");
    if (diagnosticSummary) {
      const details = diagnosticSummary.parentElement;
      positions[+details.dataset.p]._diagnosticsOpen = !details.open;
      return;
    }
    const t = e.target.closest("[data-toggle],.addoffer,.rmoffer,.rmpos,.capture,.repl,.search,.source-file,.zoom") || e.target;
    if (t.dataset.p !== undefined && ["pending", "running"].includes(positions[+t.dataset.p]?._researchStatus)
        && ["addoffer", "rmoffer", "capture", "repl"].some((name) => t.classList.contains(name))) {
      e.preventDefault();
      return alert("Дождитесь завершения поиска этой позиции перед изменением предложений");
    }
    if (t.dataset.toggle !== undefined) { const i = +t.dataset.toggle; positions[i]._open = !positions[i]._open; renderPositions(); }
    else if (t.classList.contains("addoffer")) { positions[+t.dataset.p].offers.push({ price_with_vat: 0, org_name: "", inn: "", kpp: "", url: "", city: "", status: 2, from_letter: false, screenshot_product: "", screenshot_requisites: "" }); renderPositions(); }
    else if (t.classList.contains("rmoffer")) { e.preventDefault(); positions[+t.dataset.p].offers.splice(+t.dataset.o, 1); renderPositions(); }
    else if (t.classList.contains("rmpos")) { e.preventDefault(); positions.splice(+t.dataset.p, 1); renderPositions(); }
    else if (t.classList.contains("capture")) { captureOffer(+t.dataset.p, +t.dataset.o); }
    else if (t.classList.contains("repl")) { replaceOffer(+t.dataset.p, +t.dataset.o); }
    else if (t.classList.contains("search")) { searchPosition(+t.dataset.p); }
    else if (t.classList.contains("source-file")) { window.POS.openSource(+t.dataset.source); }
    else if (t.classList.contains("zoom")) { openLightbox(t.src); }
  });
}

function replaceOffer(pi, oi) {
  const p = positions[pi];
  const next = replacementCandidates(p, oi)[0];
  if (!next) return alert("Нет других кандидатов для замены (запустите «найти цены»)");
  p.offers[oi] = { ...next, _busy: false };
  renderPositions();
}

async function captureOffer(pi, oi) {
  const o = positions[pi].offers[oi];
  if (o.catalogue_ref?.observation_id) {
    const reference = o.screenshot_product || o.screenshot_context;
    if (reference) {
      const filename = reference.split(/[\\/]/).pop();
      window.open(engineUrl() + "/cache/" + encodeURIComponent(filename), "_blank", "noopener");
    } else {
      alert("Снимок карточки не получен. Это отмечено в сводке; получить новый снимок можно при фоновом обновлении каталога.");
    }
    return;
  }
  if (!o.url) return alert("Укажите ссылку на карточку товара");
  o._busy = true; renderPositions();
  try {
    const r = await fetch(engineUrl() + "/capture", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: o.url, name: positions[pi].name, inn: o.inn, type_mark: positions[pi].type_mark, unit: positions[pi].unit, qty: positions[pi].qty }),
    });
    await requireOk(r);
    const d = await r.json();
    const freshPrice = Number(d.normalized_price_with_vat);
    const qualified = d.quote_qualified === true && Number.isFinite(freshPrice) && freshPrice > 0;
    if (qualified) {
      o.source_quote_evidence = d.source_quote_evidence || {};
      o.identity_evidence = d.identity_evidence || {};
      o.price_with_vat = freshPrice;
      o.refresh_failed = false; o.refresh_failure_reason = '';
    } else {
      o.refresh_failed = true;
      o.refresh_failure_reason = d.unit_review_reason || 'Текущая цена не подтверждена; сохранена прежняя датированная цена';
    }
    o.price_capture_evidence = d.price_capture_evidence || {};
    o.price_capture_review_reason = d.price_capture_review_reason ?? "";
    o.screenshot_product = d.screenshot_product || "";
    o.screenshot_context = d.screenshot_context || "";
    o.context_capture_evidence = d.context_capture_evidence || {};
    o.context_capture_review_reason = d.context_capture_review_reason || "";
    o.screenshot_requisites = d.screenshot_requisites || o.screenshot_requisites;
    const ex = d.extracted || {}, rq = d.requisites || {};
    if (qualified) {
      o.price_unit = d.price_unit || '';
      o.listed_price_with_vat = d.listed_price_with_vat ?? null;
      o.normalized_price_unit = d.normalized_price_unit || '';
      o.unit_review_required = false; o.unit_review_reason = '';
      o.in_moscow_region = typeof d.in_moscow_region === 'boolean' ? d.in_moscow_region : null;
      o.region_status = d.region_status || ''; o.region_evidence = d.region_evidence || '';
      o.region_approval = d.region_approval ? { ...d.region_approval } : null;
      if (ex.product_title) o.product_title = ex.product_title;
      o.page_title = d.page_title || ''; o.page_article = d.page_article || '';
      o.match_review_required = !!d.match_review_required;
      o.model_review_accepted = false;
    } else {
      // Keep the old dated money, denominator, product and seller together.
      o.unit_review_required = true;
      o.unit_review_reason = d.unit_review_reason || 'Новые сведения карточки не подтверждены; сохранено прежнее предложение';
      o.context_capture_review_reason = [o.context_capture_review_reason, o.unit_review_reason].filter(Boolean).join('; ');
    }

    if (qualified) {
      // A new seller is one legal-entity group, never mixed with old fields.
      o.inn = String(rq.inn || ''); o.org_name = rq.name || '';
      o.kpp = rq.kpp || ''; o.city = rq.city || 'уточнить';
      o.status = rq.status ?? 2;
    }

  } catch (e) {
    alert("Не удалось снять скриншот: " + e.message);
  } finally {
    o._busy = false; renderPositions();
  }
}

async function searchPosition(pi) {
  const p = positions[pi];
  if (p.is_unique) return alert("Уникальное оборудование не ищется у поставщиков");
  if (reviewPending(p)) return alert("Сначала сверьте позицию или примите её с замечанием");
  if (!p.name.trim()) return alert("Укажите наименование позиции");
  if (!String(p.unit).trim()) return alert("Укажите единицу измерения позиции по спецификации");
  p._searching = true; renderPositions();
  try {
    const r = await fetch(engineUrl() + "/search_position", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        number: p.number,
        qty: Number(p.qty),
        name: p.name,
        full_name: p.full_name || p.name,
        source_evidence: p.source_evidence || {},
        review_fields: p.review_fields || {},
        type_mark: p.type_mark,
        discipline: p.discipline,
        unit: p.unit || "шт",
      }),
    });
    await requireOk(r);
    const d = await r.json();
    applyCatalogueMetadata(p, d);
    p.offers = (d.offers || []).map((o) => ({ ...o, _busy: false }));
    p._candidates = d.candidates || d.offers || [];
    p._searched = true; p._found = d.found ?? p.offers.length;
    p._target = d.target ?? window._engineHealth?.min_sources ?? 5;
    p._matchReviewCount = d.match_review_count || 0;
    p._researchDiagnostics = Array.isArray(d.diagnostics) ? d.diagnostics : [];
    p._sourceChecks = Array.isArray(d.source_checks) ? d.source_checks : [];
    p._comparableSources = d.comparable_sources;
    p._coverage = d.coverage;
    p._researchStatus = d.catalogue_read ? d.status : d.quote_status === "sufficient" ? "complete" : p._found ? "partial" : "failed";
    p._searchStatus = p._found >= p._target ? "complete" : p._found ? "partial" : "failed";
    p._researchReason = d.reason || (p._found >= 3 ? "Цен для сметы достаточно" : `Найдено ${p._found} из 3 необходимых предложений`);
    if (p._matchReviewCount)
      p._researchReason += `${p._researchReason ? "; " : ""}у ${p._matchReviewCount} предложений марка не подтверждена точным совпадением — проверьте карточки`;
    p._open = true;
    return d;
  } catch (e) {
    p._researchStatus = "error";
    p._researchReason = `Поиск прерван: ${e.message}`;
    p._researchDiagnostics = [];
    p._sourceChecks = [];
    p._open = true;
    if (!window._bulk) alert("Поиск не удался: " + e.message);
    throw e;
  } finally {
    p._searching = false; renderPositions();
  }
}

async function bulkSearch(btnId) {
  const pending = positions.filter((p) => !p.is_unique && reviewPending(p)).map((p) => p.number);
  const idx = positions.map((p, i) => (!p.is_unique && !reviewPending(p) ? i : -1)).filter((i) => i >= 0);
  if (!idx.length) return alert(`Нет готовых позиций для поиска. Требуют решения: ${pending.join(", ") || "нет"}`);
  const invalid = idx.filter((i) => !positions[i].name.trim() || !String(positions[i].unit).trim());
  if (invalid.length) return alert(`Заполните наименование и единицу у позиций: ${invalid.map((i) => positions[i].number).join(", ")}`);
  if (pending.length) alert(`Поиск начнётся по ${idx.length} позициям. Пока пропущены: ${pending.join(", ")}. Они останутся в предупреждении.`);
  window._bulk = true;
  const btn = $(btnId); if (btn) btn.disabled = true;
  const label = btn?.textContent;
  let done = 0, fails = 0, shortfalls = 0;
  for (const i of idx) {
    if (btn) btn.textContent = `🔎 Поиск ${done + 1}/${idx.length}…`;
    try { const result = await searchPosition(i); if (result.found < result.target) shortfalls++; } catch { fails++; }
    done++;
  }
  window._bulk = false;
  if (btn) { btn.disabled = false; btn.textContent = label; }
  alert(`Поиск завершён: ${idx.length - fails} из ${idx.length} обработаны; ${shortfalls} с недостатком источников, ${fails} с ошибкой.${pending.length ? ` Пропущены до решения: ${pending.join(", ")}.` : ""}`);
}

/* ---------- lightbox ---------- */
const lightbox = $("lightbox");
function openLightbox(src) { if (lightbox) { $("lightboxImg").src = src; lightbox.classList.add("open"); } }
if (lightbox) lightbox.addEventListener("click", () => lightbox.classList.remove("open"));

/* ---------- джоб-прогресс ---------- */
async function pollJob(jobId, { bar, step, log, onDone, onError }) {
  try {
    const j = await (await fetch(`${engineUrl()}/jobs/${jobId}`)).json();
    if (bar && $(bar)) $(bar).style.width = Math.round((j.progress || 0) * 100) + "%";
    if (step && $(step)) $(step).textContent = j.step || (j.log || []).slice(-1)[0] || "обработка…";
    if (log && $(log)) { $(log).textContent = (j.log || []).join("\n"); $(log).scrollTop = $(log).scrollHeight; }
    if (j.status === "done") { onDone && onDone(j); return; }
    if (j.status === "error") { onError && onError(j); return; }
    setTimeout(() => pollJob(jobId, { bar, step, log, onDone, onError }), 1500);
  } catch { onError && onError({ error: "Потеряна связь с движком" }); }
}

_bindPosList();

// Same source-bound own-equipment identity as engine/equipment_output_policy.py.
function isGraftioEquipment(p) {
  const evidence=p.source_evidence, cells=evidence?.cells;
  if (evidence?.reader!=="native_pdf" || !cells || Array.isArray(cells)
      || typeof cells.maker!=="string" || !["графтио","graftio"].includes(cells.maker.trim().toLowerCase())) return false;
  const marks=[cells.model===undefined ? "" : cells.model,cells.article===undefined ? "" : cells.article];
  if (marks.some((v)=>typeof v!=="string") || typeof p.type_mark!=="string" || typeof p.name!=="string") return false;
  const mark=[...new Set(marks.map((v)=>v.trim()).filter(Boolean))].join(" / ");
  return mark===p.type_mark.trim() && String(cells.number ?? "").trim()===String(p.number)
    && String(cells.name ?? "").trim()===p.name.trim();
}
