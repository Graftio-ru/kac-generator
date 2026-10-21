// Сметчик: спецификация → распознать и найти цены → проверить → собрать документы.
// Общая машинерия (helpers, рендер позиций, lightbox, poll) — в common.js.

const specFiles = [];
const letterFiles = [];
let parsedSpecFiles = [];
let sourceOutcomes = [];
function sourceOutcomeSummary() {
  const reasons = {recognition_page_failed: "страница не прочитана",
    no_usable_positions: "пригодные позиции не выделены",
    source_image_unreadable: "изображение повреждено или недоступно",
    recognition_file_failed: "файл не прочитан", recognition_letter_failed: "письмо не прочитано",
    llm_auth_error: "провайдер отклонил доступ", llm_insufficient_balance: "недостаточно баланса",
    account_stopped: "не обработано после отказа провайдера"};
  return sourceOutcomes.filter((o) => o.status !== "done").map((o) =>
    `${o.filename || "Исходник"}${o.page ? `, страница ${o.page}` : ""}: ${reasons[o.code] || "обработка не завершена"}`).join("; ");
}
function sourceDocumentNoteSummary() {
  const notes = sourceOutcomes.flatMap((o) => Array.isArray(o.document_notes) ? o.document_notes : []);
  return notes.filter((n) => n && n.version === "native-document-notes-v1"
    && n.producer === "native_table_positions"
    && ((n.code === "native_name_only_fragment" && (n.original_cell == null || n.original_cell === "name"))
      || (n.code === "native_unnumbered_number_text" && n.original_cell === "number"))
    && n.scope === "document" && n.severity === "warning"
    && typeof n.text === "string" && n.text.trim()
    && Number.isInteger(n.page) && n.page > 0 && Number.isInteger(n.table) && n.table >= 0
    && Number.isInteger(n.row) && n.row >= 0)
    .map((n) => `${n.original_cell === "number" ? "безномерный текст из графы номера, назначение не определено; " : ""}стр. ${n.page}, таблица ${n.table}, строка ${n.row}: ${n.text}`).join("; ");
}
function renderSourceSummary() {
  const failures = sourceOutcomeSummary();
  const notes = sourceDocumentNoteSummary();
  $("sourceParseSummary").textContent = [failures ? `Проблемы исходников: ${failures}.` : "",
    notes ? `Предупреждения к документу (известные позиции обрабатываются): ${notes}` : ""].filter(Boolean).join(" ");
}
let sourceUrls = [];
let activeResearchJob = "";
async function pollResearch(jobId) {
  if (!jobId || jobId !== activeResearchJob) return;
  try {
    const response = await fetch(`${engineUrl()}/research/${jobId}`);
    if (jobId !== activeResearchJob) return;
    if (response.status === 404) {
      renderResearchContinuation(jobId, null);
      activeResearchJob = "";
      positions.forEach((p) => {
        if (["pending", "running"].includes(p._researchStatus)) {
          p._researchStatus = "error";
          p._researchReason = "Задание исчезло после перезапуска движка; повторите поиск этой позиции";
        }
      });
      renderPositions();
      $("baseSummary").textContent = "Задание поиска больше не найдено в движке. Уже полученные результаты сохранены; повторите поиск для незавершённых позиций.";
      return;
    }
    await requireOk(response);
    if (jobId !== activeResearchJob) return;
    const job = await response.json();
    if (jobId !== activeResearchJob) return;
    renderResearchContinuation(jobId, job);
    let updated = false;
    job.rows.forEach((row, index) => {
      const version = `${jobId}:${row?.status}:${row?.revision || 0}`;
      if (!row || !positions[index] || positions[index]._researchApplied === version) return;
      updated = true;
      const p = positions[index];
      if (p._researchStatus === "stale") return;
      p._researchApplied = version;
      if (p.name !== row.name || p.type_mark !== row.type_mark || p.unit !== row.unit
          || p.discipline !== row.discipline || p.is_unique !== row.is_unique
          || (row.qty !== undefined && Number(p.qty) !== Number(row.qty))) {
        p._researchStatus = "stale";
        p._researchReason = "Строка изменена во время поиска; повторите поиск для неё";
        p._researchDiagnostics = [];
        p._sourceChecks = [];
        return;
      }
      p._researchStatus = row.status;
      p._searchStatus = row.status;
      if (!row.catalogue_read && row.quote_status === "sufficient") p._researchStatus = "complete";
      else if (row.quote_status === "insufficient" && row.status === "complete") p._researchStatus = "partial";
      p._comparableSources = row.comparable_sources;
      p._coverage = row.coverage;
      p._researchReason = row.reason;
      p._researchDiagnostics = Array.isArray(row.diagnostics) ? row.diagnostics : [];
      p._sourceChecks = Array.isArray(row.source_checks) ? row.source_checks : [];
      applyCatalogueMetadata(p, row);
      p._searched = !["skipped"].includes(row.status);
      p._found = row.found;
      p._target = row.target;
      p._matchReviewCount = row.match_review_count || 0;
      p._in_base = row.from_base;
      if (!p.is_unique) {
        p.offers = (row.offers || []).map((o) => ({ ...o, _busy: false }));
        p._candidates = row.candidates || [];
      }
    });
    if (updated) renderPositions();
    const counts = { complete: 0, partial: 0, failed: 0, skipped: 0 };
    job.rows.forEach((row) => {
      if (!row) return;
      const status = row.quote_status === "sufficient" ? "complete"
        : row.quote_status === "insufficient" && row.status === "complete" ? "partial" : row.status;
      if (counts[status] !== undefined) counts[status]++;
    });
    const matchReviewRows = job.rows.filter((row) => row && row.match_review_count > 0).length;
    const work = job.rows.reduce((sum, row) => {
      if (!row) return sum;
      for (const key of ["sites_attempted", "sites_finished", "queries_attempted", "cards_attempted"])
        sum[key] += Number(row[key]) || 0;
      return sum;
    }, { sites_attempted: 0, sites_finished: 0, queries_attempted: 0, cards_attempted: 0 });
    const workText = `Запросы: ${work.queries_attempted}, карточки: ${work.cards_attempted}; начаты проверки позиция–сайт: ${work.sites_attempted}, зафиксированы результаты: ${work.sites_finished}`;
    $("baseSummary").textContent = `${workText}. Обработаны строки ${job.completed}/${job.total}: найдены ${counts.complete}, мало источников ${counts.partial}, без цен ${counts.failed}, пропущены ${counts.skipped}; сверить карточки: ${matchReviewRows}`;
    if (job.status === "cancelled") {
      activeResearchJob = "";
      $("baseSummary").textContent = `Поиск остановлен; непроверенные сайты остаются незавершёнными. ${workText}.`;
    } else if (job.status === "error") {
      activeResearchJob = "";
      positions.forEach((p) => {
        if (["pending", "running"].includes(p._researchStatus)) {
          p._researchStatus = "error";
          p._researchReason = job.error || "Поиск прерван; повторите эту позицию";
        }
      });
      renderPositions();
      $("baseSummary").textContent = `Поиск прерван: ${job.error || "ошибка движка"}. Сохраните черновик и повторите незавершённые позиции.`;
    } else if ((job.awaiting_provider === true || job.awaiting_supplier === true || job.awaiting_slice === true) && job.status === "running") {
      const reasons = [];
      if (job.awaiting_provider === true) reasons.push(job.awaiting_provider_reason || "поисковый сервис временно недоступен");
      if (job.awaiting_supplier === true) reasons.push(job.awaiting_supplier_reason || "обращение к поставщику временно отложено");
      if (job.awaiting_slice === true) reasons.push(job.awaiting_slice_reason || "завершён ограниченный проверочный запуск; оставшиеся проверки сохранены для продолжения");
      $("baseSummary").textContent = `Проверки отложены: ${reasons.join("; ")}. Полученные предложения и скриншоты доступны; незавершённые проверки отмечены в отчёте. ${workText}.`;
      setTimeout(() => pollResearch(jobId), 5000);
    } else if (job.status === "done") {
      activeResearchJob = "";
    } else {
      setTimeout(() => pollResearch(jobId), 1500);
    }
  } catch (error) {
    if (jobId !== activeResearchJob) return;
    $("baseSummary").textContent = `Связь с поиском прервана: ${error.message}. Повторяем подключение…`;
    setTimeout(() => pollResearch(jobId), 5000);
  }
}
let continuationView = null;
let continuationPending = "";
let continuationError = "";
function renderResearchContinuation(jobId, job) {
  if (jobId !== activeResearchJob) return;
  if (continuationView?.id !== jobId) continuationError = "";
  continuationView = { id: jobId, job };
  const button = $("researchContinueBtn"), message = $("researchContinueMessage");
  if (!button?.style || !message) return;
  const paused = /^[0-9a-f]{12}$/.test(jobId) && job && ["pending", "running"].includes(job.status)
    && job.awaiting_supplier === true && job.awaiting_provider !== true
    && !job.cancel_requested && !job.first_stop && !job.error && !job.error_code;
  const retry = job?.awaiting_supplier_retry_at;
  const known = typeof retry === "number" && Number.isFinite(retry) && retry > 0;
  button.style.display = paused || continuationPending === jobId ? "" : "none";
  button.disabled = !!continuationPending || !paused || !known || retry * 1000 > Date.now();
  button.onclick = continueResearchExplicitly;
  message.textContent = continuationError || (continuationPending === jobId
    ? "Продолжаем проверки…" : paused ? known
      ? `Пауза поставщика. Продолжение доступно после ${new Date(retry * 1000).toLocaleString()}.`
      : "Пауза поставщика: время продолжения пока неизвестно." : "");
}
async function continueResearchExplicitly() {
  const id = activeResearchJob;
  if (!id || continuationView?.id !== id || continuationPending) return;
  renderResearchContinuation(id, continuationView.job);
  if ($("researchContinueBtn").disabled) return;
  continuationPending = id; continuationError = "";
  renderResearchContinuation(id, continuationView.job);
  try {
    const response = await fetch(`${engineUrl()}/research/${encodeURIComponent(id)}/continue`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}),
    });
    await requireOk(response);
    if (activeResearchJob === id) await pollResearch(id);
  } catch (error) {
    if (activeResearchJob === id) continuationError = `Продолжение не выполнено: ${error.message}`;
  } finally {
    if (continuationPending === id) continuationPending = "";
    if (activeResearchJob === id && continuationView?.id === id)
      renderResearchContinuation(id, continuationView.job);
  }
}
window.POS.openSource = (index) => {
  const file = parsedSpecFiles[index];
  if (!file) return alert("Исходный файл недоступен — распознайте спецификацию повторно");
  if (!sourceUrls[index]) sourceUrls[index] = URL.createObjectURL(file);
  window.open(sourceUrls[index], "_blank", "noopener");
};
window.addEventListener("beforeunload", () => sourceUrls.forEach((url) => url && URL.revokeObjectURL(url)));
if ($("fixDate")) $("fixDate").valueAsDate = new Date();

// Экспортируем также исходники: без них после восстановления нельзя сверить строки.
const DRAFT_VERSION = 1;
const DRAFT_SOURCE_TYPES = {
  pdf: "application/pdf", jpg: "image/jpeg", jpeg: "image/jpeg",
  png: "image/png", tif: "image/tiff", tiff: "image/tiff",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  xlsm: "application/vnd.ms-excel.sheet.macroEnabled.12", xls: "application/vnd.ms-excel",
};
async function downloadResearchReport(reportPositions = positions, button = $("researchReportBtn")) {
  if (!reportPositions.length) return alert("Нет позиций для сводки");
  button.disabled = true;
  try {
    const response = await fetch(engineUrl() + "/research/package", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        object_name: $("objectName").value.trim(),
        positions: reportPositions.map((p) => ({
          ...p,
          research_status: p._researchStatus || "",
          research_reason: p._researchReason || "",
          research_target: p._target || 0,
          candidates: researchCandidates(p),
          diagnostics: p._researchDiagnostics || [],
          source_checks: p._sourceChecks || [],
          catalogue_read: !!p._catalogueRead,
          catalogue_coverage_warning: p._coverageWarning || "",
          catalogue_product_id: p._catalogueProductId || "",
          catalogue_revision: p._catalogueRevision || 0,
          catalogue_refresh_attempts: p._catalogueRefreshAttempts || [],
        })),
      }),
    });
    await requireOk(response);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = `сводка-поиска-${new Date().toISOString().slice(0, 10)}.zip`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (error) {
    alert("Не удалось скачать сводку: " + error.message);
  } finally {
    button.disabled = false;
  }
}
$("researchReportBtn").onclick = () => downloadResearchReport();
function fileDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}
$("saveDraftBtn").onclick = async () => {
  if (!positions.length && !sourceOutcomes.length) return alert("Нет результатов для сохранения");
  const button = $("saveDraftBtn");
  button.disabled = true;
  try {
    const sources = await Promise.all(parsedSpecFiles.map(async (file) => ({
      name: file.name, type: file.type, data: await fileDataUrl(file),
    })));
    const draft = {
      version: DRAFT_VERSION,
      object_name: $("objectName").value,
      discipline: $("discipline").value,
      fix_date: $("fixDate").value,
      unique_names: $("uniqueNames").value,
      positions,
      sources,
      source_outcomes: sourceOutcomes,
      research_job_id: activeResearchJob || null,
    };
    const url = URL.createObjectURL(new Blob([JSON.stringify(draft)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `сметчик-черновик-${new Date().toISOString().slice(0, 10)}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (error) {
    alert("Не удалось сохранить черновик: " + error.message);
  } finally {
    button.disabled = false;
  }
};
$("loadDraftBtn").onclick = () => $("draftInput").click();
$("draftInput").onchange = async (event) => {
  const file = event.target.files[0];
  event.target.value = "";
  if (!file) return;
  try {
    const draft = JSON.parse(await file.text());
    if (draft.version !== DRAFT_VERSION || !Array.isArray(draft.positions)
        || (!draft.positions.length && !draft.source_outcomes?.length) || draft.positions.length > 5000
        || !Array.isArray(draft.sources))
      throw new Error("неверный формат или версия черновика");
    if (draft.source_outcomes != null && (!Array.isArray(draft.source_outcomes)
        || draft.source_outcomes.some((o) => !o || typeof o !== "object"
          || !["done", "partial", "failed", "not_attempted"].includes(o.status)
          || typeof o.code !== "string" || typeof o.filename !== "string")))
      throw new Error("повреждён отчёт об обработке исходников");
    const sources = await Promise.all(draft.sources.map(async (source) => {
      const ext = typeof source.name === "string" ? source.name.split(".").pop().toLowerCase() : "";
      if (!DRAFT_SOURCE_TYPES[ext] || typeof source.data !== "string"
          || !/^data:[^;]*;base64,/.test(source.data))
        throw new Error("повреждён исходный файл в черновике");
      const blob = await (await fetch(source.data)).blob();
      return new File([blob], source.name, { type: DRAFT_SOURCE_TYPES[ext] });
    }));
    if (draft.positions.some((p) => !p || typeof p !== "object"
        || !Number.isInteger(p.number) || typeof p.name !== "string"
        || !Array.isArray(p.offers)
        || (p.source_index != null &&
            (!Number.isInteger(p.source_index) || p.source_index < 0 || p.source_index >= sources.length))))
      throw new Error("повреждены позиции или ссылки на исходные файлы");
    const restored = draft.positions.map(normalizePos);
    sourceUrls.forEach((url) => url && URL.revokeObjectURL(url));
    sourceUrls = [];
    parsedSpecFiles = sources;
    sourceOutcomes = Array.isArray(draft.source_outcomes) ? draft.source_outcomes : [];
    const previousJob = activeResearchJob;
    activeResearchJob = typeof draft.research_job_id === "string" ? draft.research_job_id : "";
    renderResearchContinuation(activeResearchJob, null);
    positions = restored;
    $("objectName").value = String(draft.object_name || "");
    if (["ЭМ", "АК"].includes(draft.discipline)) $("discipline").value = draft.discipline;
    $("fixDate").value = String(draft.fix_date || "");
    $("uniqueNames").value = String(draft.unique_names || "");
    renderPositions();
    const waiting = positions.filter((p) => p._researchStatus === "pending").length;
    $("baseSummary").textContent = `Черновик восстановлен: ${positions.length} позиций; ${positions.filter(reviewPending).length} требуют решения`
      + (!activeResearchJob && waiting ? `; ${waiting} ждут поиска — запустите поиск цен вручную` : "");
    const failures = sourceOutcomeSummary();
    renderSourceSummary();
    if (activeResearchJob && activeResearchJob !== previousJob) pollResearch(activeResearchJob);
  } catch (error) {
    alert("Не удалось открыть черновик: " + error.message);
  }
};

wireDrop("dropSpec", "specInput", specFiles, "specFiles", [".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".docx", ".xlsx", ".xlsm", ".xls"]);
wireDrop("dropLetter", "letterInput", letterFiles, "letterFiles", [".pdf"]);

/* ---------- шаг 1 → 2: распознать + заполнить из базы ---------- */
$("parseBtn").onclick = async () => {
  if (!specFiles.length && !letterFiles.length)
    return alert("Добавьте файлы спецификации или письма");
  const fd = new FormData();
  fd.append("object_name", $("objectName").value.trim());
  fd.append("discipline", $("discipline").value);
  fd.append("unique_names", $("uniqueNames").value);
  specFiles.forEach((f) => fd.append("specs", f));
  letterFiles.forEach((f) => fd.append("letters", f));

  $("parseBtn").disabled = true; $("parseBtn").textContent = "Распознаём…";
  try {
    const r = await fetch(engineUrl() + "/parse", { method: "POST", body: fd });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const data = await r.json();
    sourceUrls.forEach((url) => url && URL.revokeObjectURL(url));
    sourceUrls = [];
    parsedSpecFiles = [...specFiles];
    sourceOutcomes = Array.isArray(data.source_outcomes) ? data.source_outcomes : [];
    positions = data.positions.map(normalizePos);
    activeResearchJob = data.research_job_id || "";
    renderResearchContinuation(activeResearchJob, null);
    positions.forEach((p) => { p._researchStatus = "pending"; });
    renderPositions();
    const imageSource = specFiles.some((f) => /\.(jpe?g|png|tiff?)$/i.test(f.name));
    const missingQty = positions.filter((p) => !(Number(p.qty) > 0)).map((p) => p.number);
    const missingUnit = positions.filter((p) => !String(p.unit).trim()).map((p) => p.number);
    const pending = positions.filter(reviewPending).map((p) => p.number);
    $("baseSummary").textContent = `Автоматический поиск запущен для ${positions.length} строк. Можно сверять распознавание во время поиска. Сохраните черновик, чтобы вернуться к нему после перезагрузки страницы.`;
    if (imageSource || pending.length || missingQty.length || missingUnit.length)
      $("baseSummary").title = `Сверьте OCR с оригиналом перед сборкой. Требуют решения: ${pending.length}; нет количества: ${missingQty.join(", ") || "нет"}; нет единицы: ${missingUnit.join(", ") || "нет"}.`;
    const failures = sourceOutcomeSummary();
    renderSourceSummary();
    if (!activeResearchJob) $("baseSummary").textContent = `Сохранено ${positions.length} строк. Автоматический поиск не запущен.`
      + (failures ? ` Проблемы исходников: ${failures}.` : "");
    if (activeResearchJob) pollResearch(activeResearchJob);
  } catch (e) {
    alert("Не удалось распознать: " + e.message + "\nМожно заполнить вручную.");
  } finally {
    $("parseBtn").disabled = false; $("parseBtn").textContent = "Распознать и найти цены →";
  }
};

$("manualBtn").onclick = () => { if (!positions.length) addPosition(); renderPositions(); };
$("addPosBtn").onclick = () => { addPosition(); renderPositions(); };
$("refillBtn").onclick = fillFromBase;
$("bulkPriceBtn").onclick = () => bulkSearch("bulkPriceBtn");

// Заполняет позиции готовыми блоками из базы (без онлайн-поиска), помечает «нет в базе».
async function fillFromBase() {
  if (!positions.length) return;
  const ready = positions.map((p, i) => (!p.is_unique && !reviewPending(p) ? i : -1)).filter((i) => i >= 0);
  const invalid = ready.filter((i) => !(Number(positions[i].qty) > 0) || !String(positions[i].unit).trim());
  if (invalid.length)
    return alert(`Сначала заполните количество и единицу у позиций: ${invalid.map((i) => positions[i].number).join(", ")}`);
  if (!ready.length) return alert("Пока нет проверенных позиций для поиска в базе");
  $("baseSummary").textContent = "запрос к базе…";
  try {
    const r = await fetch(engineUrl() + "/base/fill", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ positions: ready.map((i) => positions[i]).map((p) => ({
        number: p.number, name: p.name, full_name: p.full_name || p.name, type_mark: p.type_mark, unit: p.unit,
        qty: p.qty, discipline: p.discipline, needs_review: p.needs_review,
        review_issue: p.review_issue, review_accepted: p.review_accepted,
        review_fields: p.review_fields, source_is_image: p.source_is_image,
      source_evidence: p.source_evidence, source_index: p.source_index, offers: [],
      })) }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const d = await r.json();
    d.results.forEach((res, i) => {
      const position = positions[ready[i]];
      position.offers = (res.offers || []).map((o) => ({ ...o, _busy: false }));
      position._in_base = res.in_base;
      position._candidates = res.candidates || [];
      position._sourceChecks = res.source_checks || [];
      position._researchStatus = res.status || "not_started";
      position._researchReason = res.reason || "";
      position._researchDiagnostics = res.diagnostics || [];
      position._comparableSources = res.comparable_sources;
      position._coverage = res.coverage;
      applyCatalogueMetadata(position, res);
    });
    renderPositions();
    $("baseSummary").textContent = `в каталоге: ${d.found} из ${d.total} позиций; цены и причины показаны в результатах`;
  } catch (e) {
    $("baseSummary").textContent = "база недоступна: " + e.message;
  }
}

/* ---------- шаг 3: сборка (без ТСН, без онлайн-поиска) ---------- */
$("buildBtn").onclick = async () => {
  if (!$("objectName").value.trim()) return alert("Укажите наименование объекта (шаг 1)");
  if (!positions.length) return alert("Нет позиций");
  if (sourceOutcomes.some((o) => o.status !== "done"))
    return alert("Не все исходники обработаны. Сохраните результаты и повторите разбор исходников перед итоговой сборкой.");
  const pending = positions.filter(reviewPending).map((p) => p.number);
  if (pending.length)
    return alert(`Перед сборкой сверьте или примите с замечанием позиции: ${pending.join(", ")}`);
  const missingQty = positions.filter((p) => !(Number(p.qty) > 0)).map((p) => p.number);
  const missingUnit = positions.filter((p) => !String(p.unit).trim()).map((p) => p.number);
  if (missingQty.length || missingUnit.length)
    return alert(`Перед сборкой заполните количество: ${missingQty.join(", ") || "—"}; единицу измерения: ${missingUnit.join(", ") || "—"}`);
  const unfinished = positions.filter((p) => ["pending", "running", "stale", "error"].includes(p._researchStatus));
  if (unfinished.length)
    return alert(`Дождитесь завершения или повторите поиск по позициям: ${unfinished.map((p) => p.number).join(", ")}`);
  const unpriced = positions.filter((p) => !isGraftioEquipment(p) && !p.offers.some((o) => Number(o.price_with_vat) > 0));
  if (unpriced.length)
    return alert(`Нет подтверждённой цены у позиций: ${unpriced.map((p) => p.number).join(", ")}. Добавьте предложение или повторите поиск.`);
  const modelPending = positions.filter((p) => p.offers.some((o) => o.match_review_required && !o.model_review_accepted));
  const unitPending = positions.filter((p) => p.offers.some((o) => o.unit_review_required));
  if (unitPending.length)
    return alert(`Не подтверждены единицы продажи/комплектация в позициях: ${unitPending.map((p) => p.number).join(", ")}`);
  if (modelPending.length)
    return alert(`Перед сборкой сверьте марки у предложений в позициях: ${modelPending.map((p) => p.number).join(", ")}`);
  const uncertain = positions.filter(reviewUncertain);
  if (uncertain.length && !confirm(`В документ войдут ${uncertain.length} позиций с неустранённой неопределённостью:\n${uncertain.map((p) => `№${p.number}: ${p.review_issue}`).join("\n")}\n\nИспользовать текущие данные?`)) return;
  // The server validates final independent sources and explicit letter/own exceptions.
  const body = {
    object_name: $("objectName").value.trim(),
    fix_date: $("fixDate").value ? $("fixDate").value.split("-").reverse().join(".") : "",
    use_tsn: false,
    skip_search: true,
    source_outcomes: sourceOutcomes,
    positions: positions.map((p) => ({
      number: p.number, name: p.name, full_name: p.full_name, type_mark: p.type_mark,
      unit: p.unit, qty: p.qty, is_unique: p.is_unique, needs_review: p.needs_review,
      review_issue: p.review_issue, review_accepted: p.review_accepted,
      review_fields: p.review_fields, source_is_image: p.source_is_image,
      source_evidence: p.source_evidence, source_index: p.source_index,
      discipline: p.discipline,
      offers: p.offers,
    })),
  };
  const reportSnapshot = positions.map((p) => JSON.parse(JSON.stringify(p)));
  $("buildBtn").disabled = true;
  $("progressWrap").style.display = "block";
  $("downloads").innerHTML = "";
  setStatus("running", "Запуск…");
  try {
    const r = await fetch(engineUrl() + "/build", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const jobId = (await r.json()).job_id;
    pollJob(jobId, {
      bar: "barFill", step: "stepText", log: "logBox",
      onDone: (j) => {
        setStatus("done", j.coverage_warnings?.length ? "Документы готовы; ошибки и неопределённые источники сохранены в сводке" : "Готово ✓");
        $("downloads").innerHTML = (j.outputs || []).map((k) =>
          `<a href="${engineUrl()}/jobs/${jobId}/download/${k}">⬇ ${LABELS[k] || k}</a>`).join("");
        const reportButton = document.createElement("button");
        reportButton.className = "ghost sm";
        reportButton.textContent = "⬇ Сводка и скриншоты (ZIP)";
        reportButton.onclick = () => downloadResearchReport(reportSnapshot, reportButton);
        $("downloads").append(reportButton);
        $("buildBtn").disabled = false;
      },
      onError: (j) => { setStatus("error", "Ошибка: " + (j.error || "")); $("buildBtn").disabled = false; },
    });
  } catch (e) {
    setStatus("error", "Ошибка: " + e.message); $("buildBtn").disabled = false;
  }
};

const LABELS = { research_xlsx: "Проверки источников (Excel)", research_package: "Проверки и снимки (ZIP)", kac: "КАЦ (Excel)", tkp: "Том ТКП (PDF)", tsn: "ТСН-2001 (Excel)" };
function setStatus(cls, text) { const el = $("statusText"); el.className = "status " + cls; el.textContent = text; }
