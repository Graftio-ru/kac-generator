// Durable key survives transport-unknown retry of the same logical research action.
// A successful acknowledged submission settles the key; a new action gets one.
const _nativeFetch = window.fetch.bind(window);
window.fetch = async function(input, options) {
  const url = typeof input === 'string' ? input : input?.url;
  if (!options?.method || options.method.toUpperCase()==='GET') {
    const response=await _nativeFetch(input,options);
    if (/\/research\/[a-f0-9]{12}$/.test(url||'') && response.ok) {
      try {
        const snapshot=await response.clone().json();
        if(snapshot.dispatch_state) setTimeout(()=>{
          const summary=document.getElementById('baseSummary');
          const label=snapshot.dispatch_state==='queued' ? 'Задание в очереди; runner может быть выключен.' : snapshot.runner_snapshot ? 'Сохранённый снимок результата runner; цены повторно не проверялись.' : 'Задание передано runner.';
          if(summary) summary.textContent=label+' '+summary.textContent;
          let links=document.getElementById('runnerPublishedDownloads');
          if(!links && summary) { links=document.createElement('div'); links.id='runnerPublishedDownloads'; summary.after(links); }
          if(links) { links.replaceChildren(); for(const item of snapshot.downloads||[]) {
            if(!/^\/runner-results\/[a-f0-9]{12}\/[a-f0-9]{64}$/.test(item.url||'')) continue;
            const anchor=document.createElement('a'); anchor.href=item.url;anchor.textContent=item.name;anchor.download=item.name;links.append(anchor,document.createTextNode(' '));
          }}
        },0);
      } catch { /* native polling retains its own explicit error handling */ }
    }
    return response;
  }
  if (options.method.toUpperCase() !== 'POST' || !/\/research$/.test(url || '') || typeof options.body !== 'string') return _nativeFetch(input, options);
  const storageKey='estimator-research-pending-submission';
  if (remoteCommands()) {
    const previous=storedResearchCommand();
    if(previous && previous.body===options.body && !localStorage.getItem(storageKey)) {
      const response=await _nativeFetch(`${engineUrl()}/research/${previous.job_id}`);
      if(!response.ok) throw new Error('Предыдущее задание поиска не подтверждено; новая отправка запрещена');
      const state=await response.json();
      if(state.job_id!==previous.job_id) throw new Error('Не удалось подтвердить предыдущее задание поиска');
      if(state.status!=='done' && !(state.dispatch_state==='error' && state.error_code==='runner_queued_source_changed')) return new Response(JSON.stringify({job_id:previous.job_id}),{status:200,headers:{'Content-Type':'application/json'}});
    }
  }
  let pending;
  try { pending=JSON.parse(localStorage.getItem(storageKey)||'null'); } catch { pending=null; }
  if (!pending || pending.body!==options.body) {
    if (pending) throw new Error('Предыдущая отправка не подтверждена; проверьте её состояние до новой отправки');
    pending={body:options.body,key:crypto.randomUUID(),generation:nextCommandGeneration()};
    localStorage.setItem(storageKey,JSON.stringify(pending));
  }
  const headers=new Headers(options.headers||{}); headers.set('Idempotency-Key',pending.key);
  const response=await _nativeFetch(input,{...options,headers});
  if(response.ok) {
    // Headers alone do not acknowledge dispatch: a truncated response can hide
    // an already queued paid job. Keep the key until its exact ID is readable.
    try {
      const acknowledgement=await response.clone().json();
      if(acknowledgement && typeof acknowledgement==='object' && !Array.isArray(acknowledgement) && typeof acknowledgement.job_id==='string' && /^[a-f0-9]{12}$/.test(acknowledgement.job_id)) { localStorage.setItem('estimator-research-last-command',JSON.stringify({key:pending.key,job_id:acknowledgement.job_id,body:pending.body,generation:pending.generation})); localStorage.removeItem(storageKey); }
    } catch { /* transport/body uncertainty retains the same submission key */ }
  } else if([400,413,422].includes(response.status)) localStorage.removeItem(storageKey);
  return response;
};
// Общая машинерия для обеих страниц «Генрих» (Сметчик и База-менеджер).
// Поведение настраивается через window.POS = { showSearch, showTsn, baseBadge }.
window.POS = Object.assign({ showSearch: false, showTsn: false, baseBadge: false }, window.POS || {});

const $ = (id) => document.getElementById(id);
const engineInput = $("engine");
const isServerDeployment = () => window.KAC_RUNTIME?.mode === "server";
const engineUrl = () => isServerDeployment() ? location.origin : (engineInput ? engineInput.value.replace(/\/$/, "") : location.origin);
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

/* ---------- подключение к сервису ---------- */
function applyDeploymentMode() {
  const server = isServerDeployment();
  document.querySelectorAll("[data-local-only]").forEach(el => { el.hidden = server; });
  document.querySelectorAll("[data-server-only]").forEach(el => { el.hidden = !server; });
  if (engineInput && server) { engineInput.disabled = true; engineInput.value = location.origin; }
}
applyDeploymentMode();

async function checkEngine() {
  const el = $(isServerDeployment() ? "serverStatus" : "engineStatus");
  try {
    const h = await (await fetch(engineUrl() + "/health")).json();
    if (isServerDeployment()) {
      if (el) {
        const processingUnavailable = (h.llm_key ?? h.anthropic_key) === false;
        el.textContent = processingUnavailable ? "Сервис на связи; распознавание и поиск временно недоступны. Сообщите администратору." : "Сервис на связи";
        el.style.color = processingUnavailable ? "var(--warn)" : "var(--ok)";
      }
      const reqHint = $("reqHint"), bulkHint = $("bulkHint");
      if (reqHint) reqHint.textContent = "";
      if (bulkHint) bulkHint.textContent = "3 проверенных предложения для сметы";
      window._engineHealth = h;
      return;
    }
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
    if (el) { el.textContent = isServerDeployment() ? "Сервис временно недоступен. Обновите страницу или сообщите администратору." : "движок недоступен — запустите engine (start.command)"; el.style.color = "var(--err)"; }
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
    ...positionAlternatives(p),
    type_mark: p.type_mark || "", full_name: p.full_name || "",
    offers: (p.offers || []).map((o) => ({
      price_with_vat: o.price_with_vat || 0, org_name: o.org_name || "", inn: o.inn || "",
      kpp: o.kpp || "", url: o.url || "", city: o.city || "",
      status: o.status || 2, from_letter: !!o.from_letter,
      product_title: o.product_title || "",
      page_title: o.page_title || "", page_article: o.page_article || "",
      identity_evidence: o.identity_evidence || {},
      article_match_evidence: o.article_match_evidence ? JSON.parse(JSON.stringify(o.article_match_evidence)) : null,
      price_unit: o.price_unit || "", listed_price_with_vat: o.listed_price_with_vat ?? null,
      normalized_price_unit: unitReviewPending(o) ? "" : o.normalized_price_unit || p.unit || "",
      price_unit_evidence: o.price_unit_evidence || "",
      source_quote_evidence: o.source_quote_evidence || {},
      catalogue_ref: { ...(o.catalogue_ref || {}) },
      estimator_approval_ref: o.estimator_approval_ref ? { ...o.estimator_approval_ref } : null,
      estimator_approval: o.estimator_approval ? JSON.parse(JSON.stringify(o.estimator_approval)) : null,
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

const ALTERNATIVE_FIELDS = ["alternative_articles", "alternative_names"];
const CASE_FOLD = {"\u00b5":"\u03bc","\u00df":"ss","\u0149":"\u02bcn","\u017f":"s","\u01f0":"j\u030c","\u0345":"\u03b9","\u0390":"\u03b9\u0308\u0301","\u03b0":"\u03c5\u0308\u0301","\u03c2":"\u03c3","\u03d0":"\u03b2","\u03d1":"\u03b8","\u03d5":"\u03c6","\u03d6":"\u03c0","\u03f0":"\u03ba","\u03f1":"\u03c1","\u03f5":"\u03b5","\u0587":"\u0565\u0582","\u13a0":"\u13a0","\u13a1":"\u13a1","\u13a2":"\u13a2","\u13a3":"\u13a3","\u13a4":"\u13a4","\u13a5":"\u13a5","\u13a6":"\u13a6","\u13a7":"\u13a7","\u13a8":"\u13a8","\u13a9":"\u13a9","\u13aa":"\u13aa","\u13ab":"\u13ab","\u13ac":"\u13ac","\u13ad":"\u13ad","\u13ae":"\u13ae","\u13af":"\u13af","\u13b0":"\u13b0","\u13b1":"\u13b1","\u13b2":"\u13b2","\u13b3":"\u13b3","\u13b4":"\u13b4","\u13b5":"\u13b5","\u13b6":"\u13b6","\u13b7":"\u13b7","\u13b8":"\u13b8","\u13b9":"\u13b9","\u13ba":"\u13ba","\u13bb":"\u13bb","\u13bc":"\u13bc","\u13bd":"\u13bd","\u13be":"\u13be","\u13bf":"\u13bf","\u13c0":"\u13c0","\u13c1":"\u13c1","\u13c2":"\u13c2","\u13c3":"\u13c3","\u13c4":"\u13c4","\u13c5":"\u13c5","\u13c6":"\u13c6","\u13c7":"\u13c7","\u13c8":"\u13c8","\u13c9":"\u13c9","\u13ca":"\u13ca","\u13cb":"\u13cb","\u13cc":"\u13cc","\u13cd":"\u13cd","\u13ce":"\u13ce","\u13cf":"\u13cf","\u13d0":"\u13d0","\u13d1":"\u13d1","\u13d2":"\u13d2","\u13d3":"\u13d3","\u13d4":"\u13d4","\u13d5":"\u13d5","\u13d6":"\u13d6","\u13d7":"\u13d7","\u13d8":"\u13d8","\u13d9":"\u13d9","\u13da":"\u13da","\u13db":"\u13db","\u13dc":"\u13dc","\u13dd":"\u13dd","\u13de":"\u13de","\u13df":"\u13df","\u13e0":"\u13e0","\u13e1":"\u13e1","\u13e2":"\u13e2","\u13e3":"\u13e3","\u13e4":"\u13e4","\u13e5":"\u13e5","\u13e6":"\u13e6","\u13e7":"\u13e7","\u13e8":"\u13e8","\u13e9":"\u13e9","\u13ea":"\u13ea","\u13eb":"\u13eb","\u13ec":"\u13ec","\u13ed":"\u13ed","\u13ee":"\u13ee","\u13ef":"\u13ef","\u13f0":"\u13f0","\u13f1":"\u13f1","\u13f2":"\u13f2","\u13f3":"\u13f3","\u13f4":"\u13f4","\u13f5":"\u13f5","\u13f8":"\u13f0","\u13f9":"\u13f1","\u13fa":"\u13f2","\u13fb":"\u13f3","\u13fc":"\u13f4","\u13fd":"\u13f5","\u1c80":"\u0432","\u1c81":"\u0434","\u1c82":"\u043e","\u1c83":"\u0441","\u1c84":"\u0442","\u1c85":"\u0442","\u1c86":"\u044a","\u1c87":"\u0463","\u1c88":"\ua64b","\u1e96":"h\u0331","\u1e97":"t\u0308","\u1e98":"w\u030a","\u1e99":"y\u030a","\u1e9a":"a\u02be","\u1e9b":"\u1e61","\u1e9e":"ss","\u1f50":"\u03c5\u0313","\u1f52":"\u03c5\u0313\u0300","\u1f54":"\u03c5\u0313\u0301","\u1f56":"\u03c5\u0313\u0342","\u1f80":"\u1f00\u03b9","\u1f81":"\u1f01\u03b9","\u1f82":"\u1f02\u03b9","\u1f83":"\u1f03\u03b9","\u1f84":"\u1f04\u03b9","\u1f85":"\u1f05\u03b9","\u1f86":"\u1f06\u03b9","\u1f87":"\u1f07\u03b9","\u1f88":"\u1f00\u03b9","\u1f89":"\u1f01\u03b9","\u1f8a":"\u1f02\u03b9","\u1f8b":"\u1f03\u03b9","\u1f8c":"\u1f04\u03b9","\u1f8d":"\u1f05\u03b9","\u1f8e":"\u1f06\u03b9","\u1f8f":"\u1f07\u03b9","\u1f90":"\u1f20\u03b9","\u1f91":"\u1f21\u03b9","\u1f92":"\u1f22\u03b9","\u1f93":"\u1f23\u03b9","\u1f94":"\u1f24\u03b9","\u1f95":"\u1f25\u03b9","\u1f96":"\u1f26\u03b9","\u1f97":"\u1f27\u03b9","\u1f98":"\u1f20\u03b9","\u1f99":"\u1f21\u03b9","\u1f9a":"\u1f22\u03b9","\u1f9b":"\u1f23\u03b9","\u1f9c":"\u1f24\u03b9","\u1f9d":"\u1f25\u03b9","\u1f9e":"\u1f26\u03b9","\u1f9f":"\u1f27\u03b9","\u1fa0":"\u1f60\u03b9","\u1fa1":"\u1f61\u03b9","\u1fa2":"\u1f62\u03b9","\u1fa3":"\u1f63\u03b9","\u1fa4":"\u1f64\u03b9","\u1fa5":"\u1f65\u03b9","\u1fa6":"\u1f66\u03b9","\u1fa7":"\u1f67\u03b9","\u1fa8":"\u1f60\u03b9","\u1fa9":"\u1f61\u03b9","\u1faa":"\u1f62\u03b9","\u1fab":"\u1f63\u03b9","\u1fac":"\u1f64\u03b9","\u1fad":"\u1f65\u03b9","\u1fae":"\u1f66\u03b9","\u1faf":"\u1f67\u03b9","\u1fb2":"\u1f70\u03b9","\u1fb3":"\u03b1\u03b9","\u1fb4":"\u03ac\u03b9","\u1fb6":"\u03b1\u0342","\u1fb7":"\u03b1\u0342\u03b9","\u1fbc":"\u03b1\u03b9","\u1fbe":"\u03b9","\u1fc2":"\u1f74\u03b9","\u1fc3":"\u03b7\u03b9","\u1fc4":"\u03ae\u03b9","\u1fc6":"\u03b7\u0342","\u1fc7":"\u03b7\u0342\u03b9","\u1fcc":"\u03b7\u03b9","\u1fd2":"\u03b9\u0308\u0300","\u1fd3":"\u03b9\u0308\u0301","\u1fd6":"\u03b9\u0342","\u1fd7":"\u03b9\u0308\u0342","\u1fe2":"\u03c5\u0308\u0300","\u1fe3":"\u03c5\u0308\u0301","\u1fe4":"\u03c1\u0313","\u1fe6":"\u03c5\u0342","\u1fe7":"\u03c5\u0308\u0342","\u1ff2":"\u1f7c\u03b9","\u1ff3":"\u03c9\u03b9","\u1ff4":"\u03ce\u03b9","\u1ff6":"\u03c9\u0342","\u1ff7":"\u03c9\u0342\u03b9","\u1ffc":"\u03c9\u03b9","\uab70":"\u13a0","\uab71":"\u13a1","\uab72":"\u13a2","\uab73":"\u13a3","\uab74":"\u13a4","\uab75":"\u13a5","\uab76":"\u13a6","\uab77":"\u13a7","\uab78":"\u13a8","\uab79":"\u13a9","\uab7a":"\u13aa","\uab7b":"\u13ab","\uab7c":"\u13ac","\uab7d":"\u13ad","\uab7e":"\u13ae","\uab7f":"\u13af","\uab80":"\u13b0","\uab81":"\u13b1","\uab82":"\u13b2","\uab83":"\u13b3","\uab84":"\u13b4","\uab85":"\u13b5","\uab86":"\u13b6","\uab87":"\u13b7","\uab88":"\u13b8","\uab89":"\u13b9","\uab8a":"\u13ba","\uab8b":"\u13bb","\uab8c":"\u13bc","\uab8d":"\u13bd","\uab8e":"\u13be","\uab8f":"\u13bf","\uab90":"\u13c0","\uab91":"\u13c1","\uab92":"\u13c2","\uab93":"\u13c3","\uab94":"\u13c4","\uab95":"\u13c5","\uab96":"\u13c6","\uab97":"\u13c7","\uab98":"\u13c8","\uab99":"\u13c9","\uab9a":"\u13ca","\uab9b":"\u13cb","\uab9c":"\u13cc","\uab9d":"\u13cd","\uab9e":"\u13ce","\uab9f":"\u13cf","\uaba0":"\u13d0","\uaba1":"\u13d1","\uaba2":"\u13d2","\uaba3":"\u13d3","\uaba4":"\u13d4","\uaba5":"\u13d5","\uaba6":"\u13d6","\uaba7":"\u13d7","\uaba8":"\u13d8","\uaba9":"\u13d9","\uabaa":"\u13da","\uabab":"\u13db","\uabac":"\u13dc","\uabad":"\u13dd","\uabae":"\u13de","\uabaf":"\u13df","\uabb0":"\u13e0","\uabb1":"\u13e1","\uabb2":"\u13e2","\uabb3":"\u13e3","\uabb4":"\u13e4","\uabb5":"\u13e5","\uabb6":"\u13e6","\uabb7":"\u13e7","\uabb8":"\u13e8","\uabb9":"\u13e9","\uabba":"\u13ea","\uabbb":"\u13eb","\uabbc":"\u13ec","\uabbd":"\u13ed","\uabbe":"\u13ee","\uabbf":"\u13ef","\ufb00":"ff","\ufb01":"fi","\ufb02":"fl","\ufb03":"ffi","\ufb04":"ffl","\ufb05":"st","\ufb06":"st","\ufb13":"\u0574\u0576","\ufb14":"\u0574\u0565","\ufb15":"\u0574\u056b","\ufb16":"\u057e\u0576","\ufb17":"\u0574\u056d"};
function normalizeAlternatives(values, field) {
  if (values === undefined) return [];
  if (!Array.isArray(values) || values.length > 20) throw new Error("Допускается не более 20 значений в каждом списке");
  const result = [], seen = new Set(), limit = field === "alternative_articles" ? 128 : 500;
  for (const raw of values) {
    if (typeof raw !== "string" || /[\p{Cc}\p{Cf}\p{Cs}]/u.test(raw)) throw new Error("Значения должны быть строками без управляющих символов");
    const value = raw.trim();
    if (!value || Array.from(value).length > limit) throw new Error(`Каждое значение должно содержать от 1 до ${limit} символов`);
    const key = Array.from(value).map(c => CASE_FOLD[c] ?? c.toLowerCase()).join("");
    if (!seen.has(key)) { seen.add(key); result.push(value); }
  }
  return result;
}
function positionAlternatives(p) {
  return Object.fromEntries(ALTERNATIVE_FIELDS.map(field => [field, normalizeAlternatives(p[field], field)]));
}
function alternativesLocked(p) { return !!p._searching || p.offers.some(o => o._busy) || ["pending", "running"].includes(p._researchStatus); }
function invalidatePositionResearch(p) {
  if (p._researchStatus || p._searched || p._searching || p.offers.some(o => !o.from_letter)) {
    p._researchStatus = "stale";
    p._researchReason = "Данные позиции изменены; повторите поиск цен";
  }
  p._researchDiagnostics = []; p._sourceChecks = [];
  applyCatalogueMetadata(p, {}); p._matchReviewCount = 0; p._searched = false;
  p._candidates = []; p.offers = p.offers.filter(o => o.from_letter);
}
function alternativeArticleNotice(o) {
  const evidence = o.article_match_evidence;
  return evidence?.match_basis === "estimator_alternative"
    ? `Разрешённый вариант: ${evidence.observed_article || evidence.matched_article || ""}; исходный артикул: ${evidence.original_article || ""}` : "";
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
/** Render existing positions and review groups; presentation never approves or converts a candidate. */
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
    // Display review candidates separately from genuine empty/error results.
    const failed = issues.filter(({ p }) => ["failed", "error", "stale"].includes(researchPresentation.displayStatus(p))
      || (p._researchStatus === "skipped" && !p.is_unique));
    // This group links to preserved candidate evidence, without selecting it.
    const review = issues.filter(({ p }) => researchPresentation.displayStatus(p) === "review");
    const partial = issues.filter(({ p }) => p._researchStatus === "partial");
    researchIssues.style.display = failed.length || review.length || partial.length ? "block" : "none";
    // Keep the existing jump action; review candidates receive a truthful label.
    const jump = ({ p, i }) => `<button type="button" class="ghost sm" data-research-jump="${i}" title="${esc(p._researchReason)}">№${esc(p.number)}: ${esc(researchPresentation.displayStatus(p) === "review" ? researchPresentation.statusLabel(p) : p._researchReason.split(";")[0] || "проверьте поиск")}</button>`;
    researchIssues.innerHTML = failed.length || review.length || partial.length
      ? `<strong>${esc(researchPresentation.attentionText(failed.length, review.length, partial.length))}</strong>
         ${failed.length || review.length ? `<div class="research-issue-buttons">${[...review, ...failed].map(jump).join(" ")}</div>` : ""}
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

// A server-bound reference is the only submission authority; display metadata never grants it.
const ESTIMATOR_MATERIAL_FIELDS = ["product_id", "identity", "title", "raw_amount", "sale_unit", "seller_inn", "seller_name", "price_with_vat", "listed_price_with_vat", "price_unit", "normalized_price_unit", "url", "product_title", "page_title", "page_article", "org_name", "inn", "kpp", "city", "currency", "catalogue_vat_status", "status", "catalogue_ref", "catalogue_source_sha256", "catalogue_observed_at", "source_quote_evidence", "identity_evidence", "manufacturer_code", "seller_evidence", "vat", "purchase_terms", "unit_equivalence", "region_status", "region_evidence", "state", "units_per_sale_unit", "conversion", "from_letter"];
function invalidateEstimatorApproval(o) {
  if (o.estimator_approval_ref) { o.unit_review_required = true; o.unit_review_reason = "Данные подтверждённой цены изменены: требуется новая проверка сметчиком"; }
  o.estimator_approval_ref = null; o.estimator_approval = null;
}
function estimatorLocked(p) { return !p || !!p._searching || ["pending", "running"].includes(p._researchStatus) || p.offers.some(o => o._busy); }
function estimatorPosition(p) {
  return {number:p.number,name:p.name,type_mark:p.type_mark,full_name:p.full_name,unit:p.unit,qty:p.qty,discipline:p.discipline,is_unique:p.is_unique,source_evidence:p.source_evidence,review_fields:p.review_fields,...positionAlternatives(p)};
}
function estimatorSnapshot(p, o) { return JSON.stringify([estimatorPosition(p), o.catalogue_ref, o.url, o.catalogue_source_sha256, o.catalogue_observed_at, o.listed_price_with_vat, o.price_with_vat, o.price_unit, o.normalized_price_unit, o.product_title, o.page_article, o.org_name, o.inn, o.kpp]); }
function estimatorDate(value) { const d = new Date(Number(value) * 1000); return Number(value) > 0 && Number.isFinite(d.getTime()) ? d.toISOString() : "дата не сохранена"; }
function estimatorBadgeHtml(o, pi, oi) {
  const a = o.estimator_approval;
  if (!o.estimator_approval_ref || a?.status !== "approved") return "";
  return `<div class="estimator-approval"><small class="review-badge">Подтверждено сметчиком</small><br><small class="muted">Исходная дата: ${esc(estimatorDate(a.original_observed_at))}</small>${a.original_machine_reasons?.length ? `<details><summary>Исходные замечания автоматической проверки</summary><p>${esc(a.original_machine_reasons.join("; "))}</p></details>` : ""}<button type="button" class="ghost sm estimator-revoke" data-p="${pi}" data-o="${oi}">Отозвать подтверждение</button></div>`;
}
function estimatorReviewHtml(p, pi) {
  const r = p._estimatorReview; if (!r) return "";
  if (!r.candidate) return `<section data-estimator-confirm-form><p>${esc(r.error || "Загрузка сохранённых данных…")}</p><button class="estimator-close" data-p="${pi}">Закрыть</button></section>`;
  const c = r.candidate, f = r.form, disabled = r.busy || estimatorLocked(p) ? "disabled" : "";
  const field = (name, label, value, type="text") => `<label>${esc(label)} <input type="${type}" data-p="${pi}" data-estimator-field="${name}" value="${esc(value)}" ${type === "number" ? 'step="any" min="0"' : ""} ${disabled}></label>`;
  return `<section data-estimator-confirm-form><h4>Подтверждение найденной цены</h4>
    <p>Позиция ${esc(c.position.number)}: ${esc(c.position.name)} · ${esc(c.position.type_mark)} · ${esc(c.position.qty)} ${esc(c.position.unit)}</p>
    <p>Товар: ${esc(c.product.title)} · ${esc(c.product.article)}</p><p>Поставщик: ${esc(c.seller.name)} · ИНН ${esc(c.seller.inn)}</p>
    <p>Источник: ${esc(c.url)}</p><p>Исходная дата: ${esc(estimatorDate(c.observed_at))}</p>
    <p>Сохранённая цена: ${esc(c.raw_amount)} ${esc(c.currency)} / ${esc(c.sale_unit || "единица не установлена")}</p>
    <p>Автоматическая проверка: ${esc((c.machine_reasons || []).join("; "))}</p>
    <p class="muted">Подтверждение относится только к этому товару и сохранённой цене. Оно не обновляет дату и не заменяет реквизиты, НДС или необходимые снимки.</p>
    ${field("confirmed_sale_unit", "Подтверждённая единица продажи", f.confirmed_sale_unit)}
    ${field("units_per_sale_unit", `Сколько единиц «${c.position.unit}» в одной оплачиваемой единице продажи`, f.units_per_sale_unit, "number")}
    ${field("reason", "Основание подтверждения и пересчёта", f.reason)}
    <label><input type="checkbox" data-p="${pi}" data-estimator-field="product_confirmed" ${f.product_confirmed ? "checked" : ""} ${disabled}> Я сверил товар, артикул, сохранённую цену и единицу/комплектацию с позицией</label>
    ${r.error ? `<p role="alert">${esc(r.error)}</p>` : ""}
    <button type="button" class="estimator-confirm" data-p="${pi}" ${disabled}>Подтвердить и использовать предложение</button>
    <button type="button" class="estimator-close" data-p="${pi}" ${disabled}>Закрыть</button></section>`;
}
async function estimatorPost(route, body) {
  if (remoteCommands()) {
    const kind='candidate_'+route, fingerprint=JSON.stringify(body), storage='estimator-command-'+kind;
    let pending=pendingCommand(kind);
    if(pending && pending.fingerprint!==fingerprint) {
      if(!pending.result && !pending.terminalError) throw new Error('Предыдущее подтверждение ещё не завершено. Восстановите его результат.');
      pending=null;
    }
    if(!pending) { pending={key:crypto.randomUUID(),fingerprint}; localStorage.setItem(storage,JSON.stringify(pending)); }
    if(pending.result) return pending.result;
    if(pending.terminalError) throw new Error(pending.terminalError);
    if(!pending.job_id) {
      const response=await requireOk(await fetch(engineUrl()+"/candidate-approvals/"+route,{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':pending.key},body:fingerprint}));
      const ack=await response.json();
      if(!ack || typeof ack.job_id!=='string' || !/^[a-f0-9]{12}$/.test(ack.job_id)) throw new Error('Отправка не подтверждена; ключ сохранён');
      pending.job_id=ack.job_id;localStorage.setItem(storage,JSON.stringify(pending));
    }
    const state=await awaitCommand(kind,pending);
    if(state.command!==kind || !state.result || typeof state.result!=='object') throw new Error('Результат подтверждения не прошёл проверку');
    pending.result=state.result;localStorage.setItem(storage,JSON.stringify(pending));
    if(route==='review') localStorage.removeItem(storage);
    if(route==='revoke') localStorage.removeItem('estimator-command-candidate_confirm');
    if(route==='confirm') localStorage.removeItem('estimator-command-candidate_revoke');
    return state.result;
  }
  const response = await fetch(engineUrl()+"/candidate-approvals/"+route, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if (!response.ok) { let detail; try { detail = (await response.json()).detail; } catch {} throw new Error(typeof detail === "string" ? detail : "Сервер не принял подтверждение. Проверьте данные и повторите проверку."); }
  return response.json();
}
async function reviewEstimatorCandidate(pi, ci) {
  const p = positions[pi], o = p?._candidates?.[ci]; if (!o || estimatorLocked(p) || p._estimatorReview?.busy) return;
  if (!o.catalogue_ref?.observation_id) return alert("Нет сохранённого наблюдения для подтверждения");
  const state = {busy:true,original:o,snapshot:estimatorSnapshot(p,o)}; p._estimatorReview = state; renderPositions();
  try {
    const data = await estimatorPost("review", {position:estimatorPosition(p),catalogue_ref:o.catalogue_ref});
    if (positions[pi] !== p || p._estimatorReview !== state || state.snapshot !== estimatorSnapshot(p,o)) return;
    if (!data.candidate?.material_sha256) throw new Error("Сервер не вернул данные сохранённой цены");
    state.candidate = data.candidate; state.form = {confirmed_sale_unit:data.candidate.sale_unit || "",units_per_sale_unit:"",reason:"",product_confirmed:false};
  } catch (e) { if (p._estimatorReview === state) state.error = e.message || "Не удалось открыть подтверждение"; }
  finally { state.busy = false; renderPositions(); }
}
async function confirmEstimatorCandidate(pi) {
  const p = positions[pi], state = p?._estimatorReview; if (!state?.candidate || state.busy || estimatorLocked(p)) return;
  const c = state.candidate, f = state.form, factor = Number(f.units_per_sale_unit);
  if (!f.product_confirmed || !f.confirmed_sale_unit.trim() || !(factor > 0 && Number.isFinite(factor)) || !f.reason.trim()) { state.error = "Подтвердите товар и цену, укажите единицу продажи, положительный коэффициент и основание"; renderPositions(); return; }
  if (state.snapshot !== estimatorSnapshot(p,state.original)) { state.error = "Данные изменились. Откройте проверку заново"; renderPositions(); return; }
  state.busy = true; state.error = ""; renderPositions();
  try {
    const data = await estimatorPost("confirm", {position:estimatorPosition(p),catalogue_ref:c.catalogue_ref,expected_material_sha256:c.material_sha256,confirmed_product_title:c.product.title,confirmed_article:c.product.article,confirmed_raw_amount:c.raw_amount,confirmed_sale_unit:f.confirmed_sale_unit.trim(),requested_unit:c.position.unit,units_per_sale_unit:factor,reason:f.reason.trim()});
    if (positions[pi] !== p || p._estimatorReview !== state || state.snapshot !== estimatorSnapshot(p,state.original) || estimatorLocked(p)) return;
    if (data.approval?.status !== "approved" || !data.offer || !data.estimator_approval_ref?.approval_id) throw new Error("Сервер не подтвердил предложение");
    const offer = data.offer; if (offer.estimator_approval_ref?.approval_id !== data.estimator_approval_ref.approval_id || offer.estimator_approval_ref?.revision !== data.estimator_approval_ref.revision || offer.estimator_approval?.status !== "approved") throw new Error("Сервер вернул несогласованное подтверждение");
    const index = p.offers.findIndex(x => reviewObservationKey(x) === reviewObservationKey(state.original));
    if (index < 0) p.offers.push(offer); else p.offers[index] = offer;
    p._estimatorReview = null;
  } catch (e) { if (p._estimatorReview === state) state.error = e.message || "Подтверждение не сохранено"; }
  finally { state.busy = false; renderPositions(); }
}
async function revokeEstimatorOffer(pi, oi) {
  const p = positions[pi], o = p?.offers?.[oi], ref = o?.estimator_approval_ref; if (!ref || estimatorLocked(p) || o._approvalBusy) return;
  o._approvalBusy = true;
  try {
    const data = await estimatorPost("revoke", {approval_id:ref.approval_id,expected_revision:ref.revision,reason:"Отозвано сметчиком в интерфейсе"});
    if (data.approval?.status !== "revoked") throw new Error("Сервер не подтвердил отзыв");
    if (positions[pi] === p && p.offers[oi] === o) {
      invalidateEstimatorApproval(o); o.unit_review_required = true; o.unit_review_reason = "Подтверждение сметчика отозвано";
      p.offers.splice(oi,1); if (!(p._candidates || []).some(x => reviewObservationKey(x) === reviewObservationKey(o))) (p._candidates ||= []).push(o);
    }
  } catch (e) { alert(e.message || "Не удалось отозвать подтверждение"); }
  finally { o._approvalBusy = false; renderPositions(); }
}

function reviewPricesHtml(candidates, pi) {
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
    const notices = [alternativeArticleNotice(o), sellerOriginNotice(o), o.catalogue_vat_notice || "Сведения об НДС не сохранены",
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
      <ul>${notices.map((notice) => `<li>${esc(notice)}</li>`).join("")}</ul>${sellerEvidenceDetails(o)}${o.catalogue_ref?.observation_id ? `<button type="button" class="ghost sm estimator-review" data-p="${pi}" data-c="${(positions[pi]._candidates || []).indexOf(o)}" ${estimatorLocked(positions[pi]) ? "disabled" : ""}>Проверить и подтвердить цену</button>` : `<p class="muted">Подтверждение недоступно: нет сохранённого наблюдения источника</p>`}</li>`;
  }).join("");
  return `<details class="research-diagnostics review-prices" open><summary>Цены для проверки: ${candidates.length} · не выбраны для сметы</summary><ul>${rows}</ul></details>`;
}
/** Build one existing position view, preserving selected offers and separate human-review candidates. */
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
        ${estimatorBadgeHtml(o, i, j)}
        ${captureReviewReason(o) ? `<small class="review-badge">${esc(captureReviewReason(o))}</small>` : ""}
        ${o.unit_review_required ? `<small class="review-badge">${esc(o.unit_review_reason || "Проверьте единицу/комплектацию")}</small>` : ""}
        ${o.price_unit ? `<small class="muted">На сайте: ${o.listed_price_with_vat == null ? "цена не сохранена" : `${esc(o.listed_price_with_vat)} ₽`} / ${esc(o.price_unit)}</small>` : ""}</td>
      <td><input type="text" value="${esc(o.org_name)}" data-p="${i}" data-o="${j}" data-f="org_name"></td>
      <td><input type="text" value="${esc(o.inn)}" data-p="${i}" data-o="${j}" data-f="inn" style="width:102px"></td>
      <td><input type="text" value="${esc(o.city)}" data-p="${i}" data-o="${j}" data-f="city" style="width:96px" title="Сведения о городе; регион поставки проверяется отдельно" placeholder="город"></td>
      <td><input type="text" value="${esc(o.url)}" data-p="${i}" data-o="${j}" data-f="url">
        ${alternativeArticleNotice(o) ? `<small class="offer-alternative muted">${esc(alternativeArticleNotice(o))}</small>` : ""}
        ${o.product_title ? `<small class="offer-product-title muted" title="Название на сайте">${esc(o.product_title)}</small>` : ""}
        ${o.match_review_required ? `<label class="offer-model-review review-badge" title="Марка не подтверждена точным совпадением с карточкой товара"><input type="checkbox" data-p="${i}" data-o="${j}" data-f="model_review_accepted" ${o.model_review_accepted ? "checked" : ""}> Марка сверена с карточкой</label>` : ""}</td>
      <td>${thumbsHtml(o, eng)}</td>
      <td><button class="ghost sm capture" data-p="${i}" data-o="${j}" title="снять скриншот по URL">${o._busy ? '<span class="spin"></span>' : "📷"}</button></td>
      ${window.POS.showSearch ? `<td><button class="ghost sm repl" data-p="${i}" data-o="${j}" title="заменить следующим кандидатом по цене">⤵</button></td>` : ""}
      <td><input type="checkbox" ${o.from_letter ? "checked" : ""} data-p="${i}" data-o="${j}" data-f="from_letter" title="из письма (не в ТКП)"></td>
      <td><a href="#" class="rmoffer link" data-p="${i}" data-o="${j}">✕</a></td>
    </tr>`;
    return (["pending", "running"].includes(p._researchStatus) || p._estimatorReview?.busy || o._approvalBusy)
      ? offerHtml.replace(/<(input|button)\b/g, "<$1 disabled") : offerHtml;
  }).join("");
  const cols = window.POS.showSearch ? 10 : 9;
  const searchBtn = (window.POS.showSearch && !p.is_unique)
    ? `<button class="ghost sm search" data-p="${i}" title="найти цены онлайн">${p._searching ? '<span class="spin"></span> ищу…' : "🔎 найти цены"}</button>` : "";
  const badge = window.POS.showSearch && p._searched
    ? `<span class="pill">найдено ${p._found}; сопоставимых: ${p._comparableSources ?? "—"}; минимум для сметы: 3; покрытие: ${p._coverage === "wide" ? "широкое" : p._coverage === "limited" ? "ограниченное" : p._coverage === "insufficient" ? "недостаточное" : "не оценено"}</span>` : "";
  const researchBadge = window.POS.showSearch && p._researchStatus
    ? `<span class="pill" title="${esc(p._researchReason)}">${esc(researchPresentation.statusLabel(p))}</span>` : "";
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
      <div class="position-alternatives">
        <label>Разрешённые альтернативные артикулы — по одному на строку
          <textarea data-p="${i}" data-f="alternative_articles" ${alternativesLocked(p) ? "disabled" : ""}>${esc(normalizeAlternatives(p.alternative_articles, "alternative_articles").join("\n"))}</textarea>
        </label>
        <small class="hint">Вы разрешаете использовать эти варианты вместо исходного артикула. Цена, единица и продавец проходят обычную проверку.</small>
        <label>Другие названия для поиска — по одному на строку
          <textarea data-p="${i}" data-f="alternative_names" ${alternativesLocked(p) ? "disabled" : ""}>${esc(normalizeAlternatives(p.alternative_names, "alternative_names").join("\n"))}</textarea>
        </label>
        <small class="hint">Названия помогают найти товар и не разрешают замену артикула. Не все подсказки обязательно попадут в поиск.</small>
      </div>
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
      ${reviewPrices.length ? reviewPricesHtml(reviewPrices, i) + "\n      " : ""}${estimatorReviewHtml(p, i)}<table class="offers">
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
    if (t.dataset.estimatorField !== undefined) {
      const p = positions[pi], review = p?._estimatorReview;
      if (!review || review.busy || estimatorLocked(p)) return;
      review.form[t.dataset.estimatorField] = t.type === "checkbox" ? t.checked : t.value;
      if (t.dataset.estimatorField !== "product_confirmed") {
        review.form.product_confirmed = false;
        const checkbox = t.closest?.("[data-estimator-confirm-form]")?.querySelector?.('[data-estimator-field="product_confirmed"]');
        if (checkbox) checkbox.checked = false;
      }
      review.error = ""; return;
    }
    let val = t.type === "checkbox" ? t.checked : t.value;
    if (positions[pi]?._estimatorReview?.busy || positions[pi]?.offers.some(o => o._approvalBusy)) return;
    if (ALTERNATIVE_FIELDS.includes(f) && t.dataset.o === undefined) {
      const p = positions[pi];
      if (alternativesLocked(p)) { t.value = normalizeAlternatives(p[f], f).join("\n"); return; }
      try {
        const values = normalizeAlternatives(t.value.split(/\r?\n/).filter(v => v.trim()), f);
        t.setCustomValidity?.("");
        if (JSON.stringify(values) !== JSON.stringify(normalizeAlternatives(p[f], f))) { p[f] = values; invalidatePositionResearch(p); }
      } catch (error) { t.setCustomValidity?.(error.message); }
      return;
    }
    if (t.dataset.o !== undefined) {
      const oi = +t.dataset.o;
      if (["price_with_vat"].includes(f)) val = parseFloat(val) || 0;
      const offer = positions[pi].offers[oi];
      const previousUrl = offer.url;
      if (ESTIMATOR_MATERIAL_FIELDS.includes(f)) invalidateEstimatorApproval(offer);
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
      if (["name", "type_mark", "unit", "qty", "alternative_articles", "alternative_names", "source_evidence", "full_name"].includes(f)) {
        positions[pi]._estimatorReview = null; positions[pi].offers.forEach(invalidateEstimatorApproval);
      }
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
    if (ALTERNATIVE_FIELDS.includes(t.dataset.f)) {
      if (t.validationMessage) alert(t.validationMessage);
      t.value = normalizeAlternatives(p[t.dataset.f], t.dataset.f).join("\n");
      t.setCustomValidity?.(""); renderPositions(); return;
    }
    if (p?._ocr_source &&
        ["number", "name", "type_mark", "unit", "qty"].includes(t.dataset.f)) {
      renderPositions();
    } else if (t.dataset.f === "review_issue") {
      p.review_accepted = false;
      renderPositions();
    }
  });
  list.addEventListener("click", (e) => {
    const estimatorAction = e.target.closest?.(".estimator-review,.estimator-confirm,.estimator-close,.estimator-revoke");
    if (estimatorAction) {
      e.preventDefault(); const pi = +estimatorAction.dataset.p;
      if (estimatorAction.classList.contains("estimator-close")) { if (!positions[pi]?._estimatorReview?.busy) { positions[pi]._estimatorReview = null; renderPositions(); } }
      else if (estimatorAction.classList.contains("estimator-review")) reviewEstimatorCandidate(pi, +estimatorAction.dataset.c);
      else if (estimatorAction.classList.contains("estimator-confirm")) confirmEstimatorCandidate(pi);
      else revokeEstimatorOffer(pi, +estimatorAction.dataset.o);
      return;
    }
    const diagnosticSummary = e.target.closest("details.research-diagnostics summary");
    if (diagnosticSummary) {
      const details = diagnosticSummary.parentElement;
      positions[+details.dataset.p]._diagnosticsOpen = !details.open;
      return;
    }
    if (positions[+e.target.dataset.p]?._estimatorReview?.busy || positions[+e.target.dataset.p]?.offers.some(o => o._approvalBusy)) { e.preventDefault(); return; }
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
      body: JSON.stringify({
        url: o.url, name: positions[pi].name, full_name: positions[pi].full_name,
        source_evidence: positions[pi].source_evidence, review_fields: positions[pi].review_fields,
        inn: o.inn, type_mark: positions[pi].type_mark, ...positionAlternatives(positions[pi]),
        unit: positions[pi].unit, qty: positions[pi].qty,
      }),
    });
    await requireOk(r);
    const d = await r.json();
    const freshPrice = Number(d.normalized_price_with_vat);
    const qualified = d.quote_qualified === true && Number.isFinite(freshPrice) && freshPrice > 0;
    if (qualified) {
      o.source_quote_evidence = d.source_quote_evidence || {};
      o.identity_evidence = d.identity_evidence || {};
      o.article_match_evidence = d.article_match_evidence || null;
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
  const alternativesSnapshot = JSON.stringify(positionAlternatives(p));
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
        ...positionAlternatives(p),
        discipline: p.discipline,
        unit: p.unit || "шт",
      }),
    });
    await requireOk(r);
    const d = await r.json();
    if (JSON.stringify(positionAlternatives(p)) !== alternativesSnapshot) { invalidatePositionResearch(p); return; }
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


// Only the control queues these commands. Keep key AND acknowledged ID until
// the caller consumes a complete terminal result; transport failures never retry a new key.
function remoteCommands() { return window.KAC_RUNTIME?.executionMode === 'remote'; }
function pendingCommand(kind) {
  try { return JSON.parse(localStorage.getItem('estimator-command-'+kind)||'null'); } catch { return null; }
}
async function awaitCommand(kind, pending) {
  if (!pending?.job_id || !/^[a-f0-9]{12}$/.test(pending.job_id)) throw new Error('Идентификатор задания не подтверждён');
  for (;;) {
    const response=await requireOk(await fetch(`${engineUrl()}/jobs/${pending.job_id}`));
    const state=await response.json();
    if (state.job_id!==pending.job_id) throw new Error('Ответ относится к другому заданию');
    if (state.status==='done') return state;
    if (['error','cancelled'].includes(state.status)) {
      if(state.dispatch_state==='error' && ['runner_build_validation_refused','runner_queued_source_changed','runner_approval_refused'].includes(state.error_code)) consumeCommand(kind);
      throw new Error(state.error||'Задание остановлено; автоматический повтор запрещён');
    }
    const summary=document.getElementById('baseSummary');
    if (summary) summary.textContent=state.dispatch_state==='queued'?'Задание в очереди; ожидаем runner.':state.step||'Runner обрабатывает задание…';
    await new Promise(resolve=>setTimeout(resolve,1500));
  }
}
async function submitCommand(kind,body,fingerprint) {
  let pending=pendingCommand(kind);
  if (pending && pending.fingerprint!==fingerprint) throw new Error('Предыдущее задание не завершено. Восстановите его результат перед новой отправкой.');
  if (!pending) {
    pending={key:crypto.randomUUID(),fingerprint,generation:nextCommandGeneration()};
    localStorage.setItem('estimator-command-'+kind,JSON.stringify(pending));
  }
  if (!pending.job_id) {
    const headers={'Idempotency-Key':pending.key};if(kind==='build')headers['Content-Type']='application/json';
    const response=await requireOk(await fetch(`${engineUrl()}/${kind}`,{method:'POST',headers,body}));
    const ack=await response.json();
    if (!ack || typeof ack.job_id!=='string'||!/^[a-f0-9]{12}$/.test(ack.job_id)) throw new Error('Отправка не подтверждена; ключ сохранён');
    pending.job_id=ack.job_id;localStorage.setItem('estimator-command-'+kind,JSON.stringify(pending));
  }
  return awaitCommand(kind,pending);
}
function consumeCommand(kind) { const pending=pendingCommand(kind); if(pending?.job_id) localStorage.setItem('estimator-command-last-'+kind,JSON.stringify(pending)); localStorage.removeItem('estimator-command-'+kind); }
function lastCommand(kind) { try { return JSON.parse(localStorage.getItem('estimator-command-last-'+kind)||'null'); } catch { return null; } }
async function uploadFingerprint(fields,groups) {
  const files=[];
  for (const [kind,items]of groups)for(let index=0;index<items.length;index++) {
    const file=items[index],bytes=await file.arrayBuffer();
    const sha=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(x=>x.toString(16).padStart(2,'0')).join('');
    files.push({kind,index,name:file.name,bytes:file.size,sha256:sha});
  }
  return JSON.stringify({fields,files});
}

function nextCommandGeneration() {
  const value=localStorage.getItem('estimator-command-generation');
  const current=value===null?0:Number(value);
  if(!Number.isSafeInteger(current)||current<0||current>=Number.MAX_SAFE_INTEGER)throw new Error('История заданий повреждена; новая отправка запрещена');
  localStorage.setItem('estimator-command-generation',String(current+1));return current+1;
}
function storedResearchCommand() {
  const raw=localStorage.getItem('estimator-research-last-command');
  if(!raw)return null;
  let value;try{value=JSON.parse(raw);}catch{throw new Error('Не удалось прочитать сохранённый поиск; новая отправка запрещена');}
  if(!value||typeof value.body!=='string'||!/^[a-f0-9]{12}$/.test(value.job_id||''))throw new Error('Сохранённый поиск не подтверждён');
  return value;
}
function researchCommandInput(value) {
  let body;try{body=JSON.parse(value.body);}catch{throw new Error('Входные позиции поиска повреждены');}
  if(!body||!Array.isArray(body.positions)||!body.positions.length||body.positions.some(p=>!p||typeof p!=='object'||Array.isArray(p)))throw new Error('Входные позиции поиска не подтверждены');
  return body.positions;
}
function remoteRestorePlan() {
  const raw=localStorage.getItem('estimator-research-pending-submission');
  if(raw) {
    let value;try{value=JSON.parse(raw);}catch{throw new Error('Отправка поиска не подтверждена; новая отправка запрещена');}
    return {kind:'research_unknown',record:value,positions:researchCommandInput(value)};
  }
  const research=storedResearchCommand(),parse=pendingCommand('parse')||lastCommand('parse');
  if(research&&parse) {
    const a=research.generation,b=parse.generation;
    if(!Number.isSafeInteger(a)||a<1||!Number.isSafeInteger(b)||b<1||a===b)throw new Error('Порядок сохранённых заданий не подтверждён. Автоматическое восстановление остановлено.');
    if(a>b)return {kind:'research',record:research,positions:researchCommandInput(research)};
  } else if(research)return {kind:'research',record:research,positions:researchCommandInput(research)};
  return parse?{kind:'parse',record:parse}:{kind:'none'};
}
