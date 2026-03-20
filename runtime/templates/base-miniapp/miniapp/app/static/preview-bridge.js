function setupPreviewBridge(role) {
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

window.setupPreviewBridge = setupPreviewBridge;
