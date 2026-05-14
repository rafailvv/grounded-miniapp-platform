window.Bloom = (() => {
  const statusLabels = {
    new: "Новый",
    confirmed: "Подтвержден",
    assembling: "Собирается",
    ready: "Готов",
    courier: "У курьера",
    delivered: "Доставлен",
    completed: "Завершен",
    issue: "Нужна помощь",
  };
  async function api(path = "/api/shop", options) {
    const response = await fetch(path, options);
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }
  function money(value) {
    return `${Number(value || 0).toLocaleString("ru-RU")} ₽`;
  }
  function toneClass(value) {
    return `tone-${String(value || "rose").replace(/[^a-z0-9_-]/gi, "")}`;
  }
  function toast(message) {
    const node = document.querySelector(".toast");
    if (!node) return;
    node.textContent = message;
    node.classList.add("show");
    window.setTimeout(() => node.classList.remove("show"), 2200);
  }
  function nav(role, active) {
    const labels = {
      client: [["", "Дом"], ["catalog", "Каталог"], ["cart", "Корзина"], ["orders", "Заказы"]],
      specialist: [["", "Сводка"], ["queue", "Очередь"], ["order", "Заказ"], ["stock", "Склад"]],
      manager: [["", "Пульт"], ["catalog", "Каталог"], ["orders", "Заказы"], ["analytics", "Метрики"]],
    }[role];
    return labels.map(([slug, label]) => `<a class="${active === (slug || "home") ? "active" : ""}" href="/${role}${slug ? `/${slug}` : ""}">${label}</a>`).join("");
  }
  function timeline(order) {
    return `<ul class="timeline">${(order.timeline || []).map((event) => `<li><div><strong>${event.label}</strong><span>${event.note || event.actor_role}</span></div></li>`).join("")}</ul>`;
  }
  function progress(order) {
    const steps = ["new", "assembling", "ready", "courier", "completed"];
    const index = Math.max(0, steps.indexOf(order.status));
    return `<div class="progress">${steps.map((_, itemIndex) => `<i class="${itemIndex <= index ? "done" : ""}"></i>`).join("")}</div>`;
  }
  return { api, money, nav, progress, statusLabels, timeline, toast, toneClass };
})();
