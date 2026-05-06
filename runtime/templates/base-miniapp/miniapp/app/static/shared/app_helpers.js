function miniappEscapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function miniappQuery(selector, root = document) {
  return root.querySelector(selector);
}

function miniappQueryAll(selector, root = document) {
  return Array.from(root.querySelectorAll(selector));
}

function miniappFormData(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function miniappSetStatus(target, message, kind = "neutral") {
  const element = typeof target === "string" ? miniappQuery(target) : target;
  if (!element) {
    return;
  }
  element.textContent = message || "";
  element.dataset.status = kind;
}

async function miniappJson(input, init = {}, role = document.body?.dataset.role || "client") {
  const response = await window.miniappApiFetch(input, init, role);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof payload === "object" && payload !== null ? payload.detail || JSON.stringify(payload) : payload;
    throw new Error(message || `Request failed with ${response.status}`);
  }
  return payload;
}

function miniappRenderList(items, renderItem, emptyHtml) {
  if (!Array.isArray(items) || items.length === 0) {
    return emptyHtml || "";
  }
  return items.map((item, index) => renderItem(item, index)).join("");
}

function miniappRolePath(role, path = "") {
  const normalizedRole = role || document.body?.dataset.role || "client";
  const suffix = String(path || "").replace(/^\/+/, "");
  return suffix ? `/${normalizedRole}/${suffix}` : `/${normalizedRole}`;
}

window.MiniApp = {
  escapeHtml: miniappEscapeHtml,
  formData: miniappFormData,
  json: miniappJson,
  query: miniappQuery,
  queryAll: miniappQueryAll,
  renderList: miniappRenderList,
  rolePath: miniappRolePath,
  setStatus: miniappSetStatus,
};
