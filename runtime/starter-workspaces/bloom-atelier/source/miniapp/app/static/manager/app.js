const role = "manager";
window.setupPreviewBridge?.(role);

const page = document.body.dataset.page || "home";
const app = document.querySelector("#app");
document.querySelector("#nav").innerHTML = Bloom.nav(role, page);

let state = null;
let analytics = null;

function orderRow(order) {
  return `
    <article class="card">
      <div class="row">
        <div>
          <span class="eyebrow">Заказ #${order.id}</span>
          <h3>${order.recipient_name} • ${order.delivery_window}</h3>
        </div>
        <span class="badge">${Bloom.statusLabels[order.status]}</span>
      </div>
      <p>${order.address}</p>
      <div class="row">
        <strong>${Bloom.money(order.total)}</strong>
        <span>${order.items.map((item) => `${item.name} x${item.qty}`).join(", ")}</span>
      </div>
      ${Bloom.timeline(order)}
      <div class="actions">
        ${order.status === "ready" ? `<button class="primary" data-order="${order.id}" data-next="courier">Передать курьеру</button>` : ""}
        ${order.status === "courier" ? `<button class="primary" data-order="${order.id}" data-next="delivered">Доставлен</button>` : ""}
        <button class="secondary danger" data-order="${order.id}" data-next="issue">Нужна помощь</button>
      </div>
    </article>
  `;
}

function bouquetManagerCard(item) {
  return `
    <article class="bouquet">
      <img class="bouquet-photo" src="${item.image_url}" alt="${item.name}" loading="lazy" />
      <div>
        <div class="row">
          <h3>${item.name}</h3>
          <strong>${Bloom.money(item.price)}</strong>
        </div>
        <p>${item.description}</p>
        <div class="chips"><span>${item.mood_label}</span><span>${item.palette_label}</span><span>${item.stock_hint}</span></div>
      </div>
      <form class="inline-form" data-bouquet="${item.id}">
        <input name="price" type="number" value="${item.price}" min="500" />
        <label class="toggle"><input type="checkbox" name="is_available" ${item.is_available ? "checked" : ""} /> в продаже</label>
        <button class="primary">Сохранить</button>
      </form>
    </article>
  `;
}

function renderHome() {
  app.innerHTML = `
    <section class="hero manager">
      <div>
        <span class="eyebrow">Manager Console</span>
        <h1>Операционный пульт интернет-магазина цветов.</h1>
        <p>Каталог, заказы, склад и метрики синхронизированы с витриной клиента и рабочим местом флориста.</p>
      </div>
      <a class="primary" href="/manager/orders">Управлять заказами</a>
    </section>
    <section class="stats">
      <div><strong>${Bloom.money(analytics.revenue_today)}</strong><span>выручка</span></div>
      <div><strong>${analytics.orders_by_status.ready || 0}</strong><span>готово</span></div>
      <div><strong>${analytics.low_stock.length}</strong><span>низкий склад</span></div>
    </section>
    <section class="split">
      <div class="stack">
        <div class="row"><h2>Заказы</h2><a href="/manager/orders">Все</a></div>
        ${state.orders.slice(0, 3).map(orderRow).join("")}
      </div>
      <div class="stack">
        <div class="row"><h2>Складовые риски</h2><a href="/manager/analytics">Метрики</a></div>
        ${analytics.low_stock.map((item) => `<article class="card"><h3>${item.name}</h3><p>${item.quantity} ${item.unit}, порог ${item.reorder_level}</p></article>`).join("") || `<article class="card"><h3>Риски закрыты</h3><p>Все ключевые позиции выше порога заказа.</p></article>`}
      </div>
    </section>
  `;
}

function renderCatalog() {
  app.innerHTML = `
    <section class="toolbar">
      <div><span class="eyebrow">Каталог</span><h1>Управление витриной</h1></div>
      <span class="badge">${state.bouquets.length} букетов</span>
    </section>
    <form id="new-bouquet" class="form card">
      <h2>Новый букет</h2>
      <input name="name" placeholder="Название" value="Лунная Камелия" required />
      <textarea name="description" placeholder="Описание">Белая камелия, ранункулюсы и серебристый эвкалипт в матовой кальке.</textarea>
      <input name="price" type="number" min="500" value="6800" />
      <select name="mood">
        <option value="romantic">Романтика</option>
        <option value="minimal">Минимализм</option>
        <option value="premium">Премиум</option>
        <option value="seasonal">Сезонный</option>
        <option value="bright">Яркий</option>
      </select>
      <input name="palette" value="ivory" placeholder="Палитра" />
      <input name="palette_label" value="слоновая кость" placeholder="Название палитры" />
      <input name="stock_hint" value="соберем сегодня" placeholder="Наличие" />
      <button class="primary">Добавить на витрину</button>
    </form>
    <section class="grid">${state.bouquets.map(bouquetManagerCard).join("")}</section>
  `;
}

function renderOrders() {
  app.innerHTML = `
    <section class="toolbar">
      <div><span class="eyebrow">Операции</span><h1>Заказы магазина</h1></div>
      <a class="secondary" href="/manager/analytics">Метрики</a>
    </section>
    <section class="stack">${state.orders.map(orderRow).join("")}</section>
  `;
}

function renderAnalytics() {
  const statusBlocks = Object.entries(analytics.orders_by_status)
    .map(([status, count]) => `<article class="card"><h3>${Bloom.statusLabels[status]}</h3><strong>${count}</strong></article>`)
    .join("");
  app.innerHTML = `
    <section class="toolbar">
      <div><span class="eyebrow">Analytics</span><h1>Метрики Bloom Atelier</h1></div>
      <strong>${Bloom.money(analytics.average_order_value)} средний чек</strong>
    </section>
    <section class="stats">
      <div><strong>${Bloom.money(analytics.revenue_today)}</strong><span>выручка сегодня</span></div>
      <div><strong>${analytics.total_orders}</strong><span>заказов</span></div>
      <div><strong>${analytics.available_bouquets}</strong><span>в продаже</span></div>
    </section>
    <section class="grid compact">${statusBlocks}</section>
    <section class="stack">
      <div class="row"><h2>Низкие остатки</h2><a href="/specialist/stock">Склад флориста</a></div>
      ${analytics.low_stock.map((item) => `<article class="card"><div class="row"><h3>${item.name}</h3><span class="badge">${item.quantity} ${item.unit}</span></div><p>Порог пополнения: ${item.reorder_level}</p></article>`).join("")}
    </section>
  `;
}

async function patchOrder(id, status) {
  await Bloom.api(`/api/orders/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      status,
      actor_role: "manager",
      note: status === "issue" ? "Менеджер поставил заказ на контроль" : "Менеджер обновил логистический статус",
    }),
  });
  Bloom.toast("Статус обновлен");
  await refresh();
}

async function patchBouquet(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  await Bloom.api(`/api/bouquets/${event.currentTarget.dataset.bouquet}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      price: Number(form.get("price")),
      is_available: form.get("is_available") === "on",
    }),
  });
  Bloom.toast("Букет сохранен");
  await refresh();
}

async function createBouquet(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  await Bloom.api("/api/bouquets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: form.get("name"),
      description: form.get("description"),
      price: Number(form.get("price")),
      mood: form.get("mood"),
      palette: form.get("palette"),
      palette_label: form.get("palette_label"),
      stock_hint: form.get("stock_hint"),
      image_url: "",
      is_available: true,
    }),
  });
  event.currentTarget.reset();
  Bloom.toast("Букет добавлен");
  await refresh();
}

function bind() {
  document.querySelectorAll("[data-order]").forEach((node) => node.addEventListener("click", () => patchOrder(node.dataset.order, node.dataset.next)));
  document.querySelectorAll("[data-bouquet]").forEach((node) => node.addEventListener("submit", patchBouquet));
  document.querySelector("#new-bouquet")?.addEventListener("submit", createBouquet);
}

function render() {
  if (page === "catalog") renderCatalog();
  else if (page === "orders") renderOrders();
  else if (page === "analytics") renderAnalytics();
  else renderHome();
  bind();
}

async function refresh() {
  [state, analytics] = await Promise.all([Bloom.api(), Bloom.api("/api/analytics")]);
  render();
}

refresh().catch((error) => {
  app.innerHTML = `<article class="card"><h1>Пульт недоступен</h1><p>${error.message}</p></article>`;
});
