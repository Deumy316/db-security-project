"use strict";
(() => {
  const byId = id => document.getElementById(id);
  const tabs = [byId("dbAuditTab"), byId("authAuditTab")];
  const form = byId("authFilterForm"), body = byId("authEventBody"), status = byId("authStatus");
  const number = value => Number(value).toLocaleString("ko-KR");
  let loaded = false, pending = false, page = 1, totalPages = 1;
  let activeFilters = new URLSearchParams();

  function selectTab(index) {
    tabs.forEach((tab, position) => {
      const selected = position === index;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      byId(tab.getAttribute("aria-controls")).hidden = !selected;
    });
    if (index === 1 && !loaded) loadPage(1);
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectTab(index));
    tab.addEventListener("keydown", event => {
      let next;
      if (event.key === "ArrowRight" || event.key === "ArrowLeft") next = 1 - index;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = 1;
      else return;
      event.preventDefault(); selectTab(next); tabs[next].focus();
    });
  });

  function agentSummary(agent) {
    if (!agent) return "정보 없음";
    const browser = /Edg\//i.test(agent) ? "Edge" : /Firefox\//i.test(agent) ? "Firefox"
      : /Chrome\//i.test(agent) ? "Chrome" : /Safari\//i.test(agent) ? "Safari"
      : /curl\//i.test(agent) ? "curl" : /Werkzeug\//i.test(agent) ? "테스트 클라이언트" : "기타 클라이언트";
    const os = /Android/i.test(agent) ? "Android" : /iPhone|iPad/i.test(agent) ? "iOS"
      : /Windows/i.test(agent) ? "Windows" : /Macintosh|Mac OS X/i.test(agent) ? "macOS"
      : /Linux/i.test(agent) ? "Linux" : "";
    return browser + (os ? " · " + os : "");
  }
  function cell(row, value, className = "") {
    const node = document.createElement("td");
    node.textContent = value ?? "—"; node.className = className; row.appendChild(node);
    return node;
  }
  function renderEvent(event) {
    const row = document.createElement("tr");
    cell(row, event.event_time_kst.replace("T", " ").replace("+09:00", ""), "timestamp mono");
    cell(row, event.portal_user || "미입력", "portal-account mono");
    const badge = document.createElement("span");
    badge.className = "badge " + (event.event_type === "LOGIN_FAILED" ? "auth-failed"
      : event.event_type === "LOGIN_SUCCESS" ? "excel" : "neutral");
    badge.textContent = event.event_type; cell(row, "").appendChild(badge);
    cell(row, event.client_ip, "mono");
    const forwarded = cell(row, "", "mono muted");
    if (event.forwarded_for_untrusted.length) {
      event.forwarded_for_untrusted.forEach(ip => {
        const value = document.createElement("div"); value.textContent = ip; forwarded.appendChild(value);
      });
    } else forwarded.textContent = event.forwarded_for_present ? "유효한 IP 정보 없음" : "—";
    const agent = cell(row, "", "auth-ua");
    const details = document.createElement("details"), summary = document.createElement("summary");
    summary.textContent = agentSummary(event.user_agent);
    const raw = document.createElement("pre"); raw.textContent = event.user_agent || "정보 없음";
    details.append(summary, raw); agent.appendChild(details);
    cell(row, event.session_id_prefix ? event.session_id_prefix + "…" : "—", "mono");
    body.appendChild(row);
  }
  function busy(value) {
    pending = value; form.setAttribute("aria-busy", String(value));
    byId("authApply").disabled = value; byId("authReset").disabled = value;
    byId("authPrev").disabled = value || page <= 1;
    byId("authNext").disabled = value || page >= totalPages;
    byId("authPageNumbers").querySelectorAll("button").forEach(button => { button.disabled = value; });
  }
  function renderPages(total) {
    const numbers = byId("authPageNumbers"); numbers.replaceChildren();
    const pages = new Set([1, totalPages]);
    for (let p = Math.max(1, page - 2); p <= Math.min(totalPages, page + 2); p++) pages.add(p);
    let previous = 0;
    [...pages].sort((a, b) => a - b).forEach(p => {
      if (p - previous > 1) { const gap = document.createElement("span"); gap.textContent = "…"; numbers.appendChild(gap); }
      const button = document.createElement("button"); button.type = "button"; button.className = "page-number";
      button.textContent = p; button.setAttribute("aria-label", "로그인 이력 " + p + " 페이지");
      if (p === page) button.setAttribute("aria-current", "page");
      button.addEventListener("click", () => loadPage(p)); numbers.appendChild(button); previous = p;
    });
    byId("authPageInfo").textContent = `총 ${number(total)}건 · ${page} / ${totalPages} 페이지`;
  }
  async function loadPage(requestedPage) {
    if (pending) return;
    busy(true); status.className = "viewer-status"; status.textContent = "로그인 이력을 조회하고 있습니다…";
    const params = new URLSearchParams(activeFilters); params.set("page", requestedPage);
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch("/api/viewer/auth-events?" + params, {
        credentials: "same-origin", cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) throw new Error(response.status === 401 ? "로그인이 만료되었습니다. 다시 로그인하세요."
        : response.status === 403 ? "감사 관리자 권한이 필요합니다."
        : response.status === 400 ? "사용자 필터와 이벤트 종류를 확인하세요."
        : "인증 감사 저장소를 읽을 수 없습니다. 잠시 후 다시 조회하세요.");
      const data = await response.json(); body.replaceChildren(); data.events.forEach(renderEvent);
      page = data.page; totalPages = data.total_pages; loaded = true;
      renderPages(data.total); byId("authEmpty").hidden = data.events.length > 0;
      byId("authEmpty").querySelector("h3").textContent = "조건에 맞는 로그인 이력이 없습니다";
      status.textContent = `현재 페이지 ${number(data.count)}건 · 한국 시간(KST)`;
    } catch (error) {
      loaded = false; page = totalPages = 1; body.replaceChildren(); byId("authPageNumbers").replaceChildren();
      byId("authPageInfo").textContent = "조회 불가"; byId("authEmpty").hidden = false;
      byId("authEmpty").querySelector("h3").textContent = "로그인 이력을 불러오지 못했습니다";
      status.className = "viewer-status error";
      status.textContent = error.name === "AbortError" ? "조회 시간이 초과되었습니다. 다시 시도하세요."
        : error instanceof TypeError ? "서버에 연결할 수 없습니다. 다시 조회하세요." : error.message;
    } finally { clearTimeout(timeout); busy(false); }
  }
  form.addEventListener("submit", event => {
    event.preventDefault(); if (pending) return;
    activeFilters = new URLSearchParams(new FormData(form)); loadPage(1);
  });
  byId("authReset").addEventListener("click", () => {
    form.reset(); activeFilters = new URLSearchParams(); loadPage(1);
  });
  byId("authPrev").addEventListener("click", () => loadPage(page - 1));
  byId("authNext").addEventListener("click", () => loadPage(page + 1));
})();
