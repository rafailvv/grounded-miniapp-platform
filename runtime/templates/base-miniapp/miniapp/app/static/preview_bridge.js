let currentRole = "client";
let bridgeInitialized = false;

function setupPreviewBridge(role) {
  currentRole = inferRole(role);

  if (bridgeInitialized) {
    notifyParent(window.location.pathname);
    return;
  }
  bridgeInitialized = true;

  notifyParent(window.location.pathname);
  window.addEventListener("pageshow", () => notifyParent(window.location.pathname));
  window.addEventListener("popstate", () => notifyParent(window.location.pathname));
  window.addEventListener("message", (event) => {
    const payload = event.data;
    if (!payload || typeof payload !== "object" || payload.type !== "runtime-preview-command") {
      return;
    }
    if (typeof payload.role === "string" && payload.role && payload.role !== currentRole) {
      return;
    }
    if (typeof payload.frameId === "string" && payload.frameId && payload.frameId !== getFrameId()) {
      return;
    }
    const rootPath = getRootPath();
    if (payload.command === "refresh") {
      window.location.reload();
      return;
    }
    if (payload.command === "close") {
      window.location.href = rootPath;
      return;
    }
    if (payload.command === "navigate") {
      window.location.href = buildPreviewPath(payload.path);
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

function inferRole(role) {
  if (typeof role === "string" && role) {
    return role;
  }
  const match = window.location.pathname.match(/^\/(client|specialist|manager)(?:\/|$)/);
  return match ? match[1] : "client";
}

function getRootPath() {
  currentRole = inferRole();
  return `/${currentRole}`;
}

function getFrameStorageKey(role = inferRole()) {
  return `miniapp-preview-frame-id:${role}`;
}

function rememberFrameId(frameId, role = inferRole()) {
  if (!frameId) {
    return null;
  }
  try {
    window.sessionStorage.setItem(getFrameStorageKey(role), frameId);
  } catch {
    // Ignore storage failures; bridge commands still work when URL carries the frame id.
  }
  return frameId;
}

function getFrameId() {
  const role = inferRole();
  try {
    const frameId = new URLSearchParams(window.location.search).get("preview_frame_id");
    if (frameId && frameId.trim()) {
      return rememberFrameId(frameId.trim(), role);
    }
  } catch {
    // Ignore malformed URLs and fall back to session storage.
  }
  try {
    const stored = window.sessionStorage.getItem(getFrameStorageKey(role));
    return stored && stored.trim() ? stored.trim() : null;
  } catch {
    return null;
  }
}

function buildPreviewPath(path) {
  if (typeof path !== "string" || !path.trim()) {
    return getRootPath();
  }
  const url = new URL(path, window.location.origin);
  const frameId = getFrameId();
  if (frameId && !url.searchParams.get("preview_frame_id")) {
    url.searchParams.set("preview_frame_id", frameId);
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

function notifyParent(path) {
  currentRole = inferRole();
  window.parent.postMessage(
    {
      type: "runtime-preview-route",
      path,
      role: currentRole,
      frameId: getFrameId(),
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

setupPreviewBridge();
