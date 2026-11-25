/**
 * Russian research-result presentation shared by position and summary views.
 * Load before common.js. These pure reads never select, approve or convert prices;
 * malformed candidates cannot make an empty result look like a collected price.
 */
const researchPresentation = (() => {
  const labels = Object.freeze({
    pending: "ожидает поиска",
    running: "поиск продолжается",
    complete: "цены найдены",
    partial: "частичный результат",
    review: "есть предложения для проверки",
    failed: "цен нет",
    skipped: "поиск пропущен",
    stale: "данные изменены",
    error: "ошибка поиска",
  });

  /**
   * Recognize a saved positive supplier price for display only. Both legacy raw
   * price fields are accepted; missing, coerced or non-finite values stay empty.
   */
  function hasCandidatePrice(candidate) {
    if (!candidate || typeof candidate !== "object") return false;
    const amounts = [candidate.listed_price_with_vat, candidate.price_with_vat];
    // A numeric raw price is evidence of a candidate, never financial approval.
    return amounts.some((amount) => typeof amount === "number" && Number.isFinite(amount) && amount > 0);
  }

  /**
   * Map native/API or position-view state to a display state without mutation.
   * Only terminal failed-with-price becomes review; active work and actual
   * error/stale states retain their meaning. Existing coverage rules stay intact.
   */
  function displayStatus(record) {
    if (!record || typeof record !== "object") return "";
    let status = record._researchStatus ?? record.status ?? "";
    if (record._researchStatus == null && ["complete", "partial", "failed"].includes(status)) {
      if (record.quote_status === "sufficient") status = "complete";
      else if (record.quote_status === "insufficient" && status === "complete") status = "partial";
    }
    const candidates = Array.isArray(record._candidates) ? record._candidates
      : Array.isArray(record.candidates) ? record.candidates : [];
    return status === "failed" && candidates.some(hasCandidatePrice) ? "review" : status;
  }

  /** Return a centralized Russian label; an unknown native state remains visible. */
  function statusLabel(record) {
    const status = displayStatus(record);
    return labels[status] || String(status);
  }

  /**
   * Count the same displayed states used by row badges. Null/unpublished rows
   * remain uncounted instead of being presented as failures or successes.
   */
  function summaryText(rows) {
    const counts = { complete: 0, review: 0, partial: 0, failed: 0, skipped: 0 };
    for (const row of Array.isArray(rows) ? rows : []) {
      const status = displayStatus(row);
      if (Object.hasOwn(counts, status)) counts[status]++;
    }
    return `найдены ${counts.complete}, предложения для проверки ${counts.review}, мало источников ${counts.partial}, без цен ${counts.failed}, пропущены ${counts.skipped}`;
  }

  /** Format attention groups already counted by the view; no result state changes. */
  function attentionText(failedCount, reviewCount, partialCount) {
    return `Поиск требует внимания: без цен или с ошибкой ${failedCount}, предложения для проверки ${reviewCount}, недостаточно источников ${partialCount}.`;
  }

  return Object.freeze({ displayStatus, statusLabel, summaryText, attentionText });
})();
