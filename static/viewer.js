"use strict";
(() => {
  const byId = id => document.getElementById(id);
  const form = byId("filterForm"), status = byId("status"), body = byId("logBody");
  const number = value => Number(value).toLocaleString("ko-KR");
  let page = 1, totalPages = 1, pending = false;
  let activeFilters = new URLSearchParams();
  function textCell(row, value, className = "") {
    const cell = document.createElement("td");
    cell.textContent = value ?? "—";
    cell.className = className;
    row.appendChild(cell);
    return cell;
  }
  function renderLog(log) {
    const row = document.createElement("tr");
    textCell(row, log.event_time.replace("T", " "), "timestamp mono");
    textCell(row, log.portal_user ?? "기존 로그(식별 불가)", log.portal_user ? "portal-account mono" : "legacy-label");
    textCell(row, log.db_user ?? log.user_name, "mono muted");
    const action = textCell(row, ""), badge = document.createElement("span");
    badge.className = "badge " + (log.action === "EXCEL_DOWNLOAD" ? "excel" : "select");
    badge.textContent = log.action;
    action.appendChild(badge);
    textCell(row, log.target_table, "mono");
    textCell(row, number(log.rows_affected) + "건", "numeric");
    const sql = textCell(row, "", "sql-cell"), details = document.createElement("details");
    const summary = document.createElement("summary"), preview = document.createElement("span");
    preview.className = "sql-preview mono";
    preview.textContent = log.query_string;
    summary.appendChild(preview);
    const full = document.createElement("pre");
    full.textContent = log.query_string;
    details.append(summary, full);
    sql.appendChild(details);
    body.appendChild(row);
  }
  function setBusy(busy) {
    pending = busy;
    form.setAttribute("aria-busy", String(busy));
    byId("applyFilters").disabled = busy;
    byId("resetFilters").disabled = busy;
    byId("prevPage").disabled = busy || page <= 1;
    byId("nextPage").disabled = busy || page >= totalPages;
    byId("pageNumbers").querySelectorAll("button").forEach(button => { button.disabled = busy; });
  }
  function clearResults(message) {
    body.replaceChildren();
    ["total", "select", "excel", "users"].forEach(key => { byId("stat-" + key).textContent = "—"; });
    byId("pageNumbers").replaceChildren();
    byId("pageInfo").textContent = "조회 대기";
    byId("logEmpty").hidden = false;
    byId("logEmpty").querySelector("h3").textContent = message;
    page = totalPages = 1;
  }
  function renderPages(data) {
    const numbers = byId("pageNumbers"); numbers.replaceChildren();
    const pages = new Set([1, totalPages]);
    for (let p = Math.max(1, page - 2); p <= Math.min(totalPages, page + 2); p++) pages.add(p);
    let previous = 0;
    [...pages].sort((a, b) => a - b).forEach(p => {
      if (p - previous > 1) {
        const gap = document.createElement("span"); gap.textContent = "…"; numbers.appendChild(gap);
      }
      const button = document.createElement("button"); button.type = "button";
      button.className = "page-number"; button.textContent = p;
      button.setAttribute("aria-label", p + " 페이지");
      if (p === page) button.setAttribute("aria-current", "page");
      button.addEventListener("click", () => loadPage(p));
      numbers.appendChild(button); previous = p;
    });
    byId("pageInfo").textContent = `총 ${number(data.total)}건 · ${page} / ${totalPages} 페이지`;
  }
  async function loadPage(requestedPage) {
    if (pending) return;
    setBusy(true); status.className = "viewer-status";
    status.textContent = "감사 로그를 조회하고 있습니다…";
    const params = new URLSearchParams(activeFilters); params.set("page", requestedPage);
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch("/api/viewer/db-logs?" + params, {
        credentials: "same-origin", cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) throw new Error(response.status === 401 ? "로그인이 만료되었습니다. 다시 로그인하세요."
        : response.status === 403 ? "감사 관리자 권한이 필요합니다."
        : response.status === 400 ? "필터 또는 시간 범위를 확인하세요."
        : "감사 로그를 불러오지 못했습니다. 잠시 후 다시 조회하세요.");
      const data = await response.json(); body.replaceChildren(); data.logs.forEach(renderLog);
      page = data.page; totalPages = data.total_pages;
      byId("stat-total").textContent = number(data.summary.total);
      byId("stat-select").textContent = number(data.summary.db_select);
      byId("stat-excel").textContent = number(data.summary.excel_download);
      byId("stat-users").textContent = number(data.summary.portal_users);
      byId("logEmpty").hidden = data.logs.length > 0;
      byId("logEmpty").querySelector("h3").textContent = "조건에 맞는 감사 로그가 없습니다";
      renderPages(data);
      status.textContent = `현재 페이지 ${number(data.count)}건 · 조회 완료 ${new Date().toLocaleTimeString("ko-KR")}`;
    } catch (error) {
      clearResults("조회 결과를 표시할 수 없습니다"); status.className = "viewer-status error";
      status.textContent = error.name === "AbortError" ? "조회 시간이 초과되었습니다. 다시 시도하세요." : error.message;
    } finally { clearTimeout(timeout); setBusy(false); }
  }
  form.addEventListener("submit", event => {
    event.preventDefault(); if (pending) return;
    activeFilters = new URLSearchParams(new FormData(form)); loadPage(1);
  });
  byId("resetFilters").addEventListener("click", () => {
    form.querySelectorAll("select").forEach(select => { select.selectedIndex = 0; });
    byId("start").value = ""; byId("end").value = "";
    activeFilters = new URLSearchParams(new FormData(form));
    loadPage(1);
  });
  byId("prevPage").addEventListener("click", () => loadPage(page - 1));
  byId("nextPage").addEventListener("click", () => loadPage(page + 1));
})();
