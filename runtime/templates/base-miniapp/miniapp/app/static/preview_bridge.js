let currentRole = "client";

function setupPreviewBridge(role) {
  currentRole = role;
  const rootPath = `/${role}`;

  notifyParent(window.location.pathname);
  window.addEventListener("pageshow", () => notifyParent(window.location.pathname));
  window.addEventListener("popstate", () => notifyParent(window.location.pathname));
  window.addEventListener("message", (event) => {
    const payload = event.data;
    if (!payload || typeof payload !== "object" || payload.type !== "runtime-preview-command") {
      return;
    }
    if (payload.command === "refresh") {
      window.location.reload();
      return;
    }
    if (payload.command === "close") {
      window.location.href = rootPath;
      return;
    }
    if (payload.command === "back") {
      if (window.location.pathname !== rootPath && window.history.length > 1) {
        window.history.back();
        window.setTimeout(() => {
          if (window.location.pathname !== rootPath) {
            notifyParent(window.location.pathname);
          }
        }, 120);
        return;
      }
      window.location.href = rootPath;
    }
  });
}

function notifyParent(path) {
  window.parent.postMessage(
    {
      type: "runtime-preview-route",
      path,
    },
    "*",
  );
}

function buildPreviewHeaders(role = currentRole) {
  const normalizedRole = typeof role === "string" && role ? role : "client";
  const label = normalizedRole.charAt(0).toUpperCase() + normalizedRole.slice(1);
  return {
    "X-User-Id": `${normalizedRole}-demo`,
    "X-User-Role": normalizedRole,
    "X-User-Name": `${label} Demo`,
    Accept: "application/json",
  };
}

function miniappApiFetch(input, init = {}, role = currentRole) {
  const headers = new Headers(init.headers || {});
  const previewHeaders = buildPreviewHeaders(role);
  Object.entries(previewHeaders).forEach(([key, value]) => {
    if (!headers.has(key)) {
      headers.set(key, value);
    }
  });
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(input, {
    ...init,
    headers,
  });
}

window.setupPreviewBridge = setupPreviewBridge;
window.buildPreviewHeaders = buildPreviewHeaders;
window.miniappApiFetch = miniappApiFetch;
