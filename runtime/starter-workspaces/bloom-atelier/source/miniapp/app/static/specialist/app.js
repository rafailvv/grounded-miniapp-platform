const role = "specialist";
window.setupPreviewBridge?.(role);

const page = document.body.dataset.page || "home";
const app = document.querySelector("#app");
document.querySelector("#nav").innerHTML = Bloom.nav(role, page);

let state = null;

function activeOrders() {
  return state.orders.filter((order) => !["completed", "delivered"].includes(order.status));
}

function floristOrderCard(order) {
  return `
    <article class="card">
      <div class="row">
        <div>
          <span class="eyebrow">Заказ #${order.id}</span>
          <h3>${order.items.map((item) => `${item.name} x${item.qty}`).join(", ")}</h3>
        </div>
        <span class="badge">${Bloom.statusLabels[order.status]}</span>
      </div>
      <p>${order.recipient_name} • ${order.delivery_window} • ${order.address}</p>
      ${Bloom.progress(order)}
      ${Bloom.timeline(order)}
      <div class="actions">
        <a class="secondary" href="/specialist/order?order=${order.id}">Открыть</a>
        ${["new", "confirmed"].includes(order.status) ? `<button class="primary" data-status="${order.id}" data-next="assembling">Принять</button>` : ""}
        ${order.status === "assembling" ? `<button class="primary" data-status="${order.id}" data-next="ready">Готов к доставке</button>` : ""}
      </div>
    </article>
  `;
}

function renderHome() {
  const orders = activeOrders();
  const lowStock = state.inventory.filter((item) => item.quantity <= item.reorder_level);
  app.innerHTML = `
    <section class="hero florist">
      <div>
        <span class="eyebrow">Florist Desk</span>
        <h1>Очередь сборки, замены и готовность заказов в одном месте.</h1>
        <p>Статусы сразу видят клиент и менеджер, склад обновляется через общий API.</p>
      </div>
      <a class="primary" href="/specialist/queue">Открыть очередь</a>
    </section>
    <section class="stats">
      <div><strong>${orders.length}</strong><span>в работе</span></div>
      <div><strong>${orders.filter((item) => item.status === "assembling").length}</strong><span>собирается</span></div>
      <div><strong>${lowStock.length}</strong><span>низкий остаток</span></div>
    </section>
    <section class="stack">
      <div class="row"><h2>Следующие заказы</h2><a href="/specialist/stock">Склад</a></div>
      ${orders.slice(0, 3).map(floristOrderCard).join("")}
    </section>
  `;
}

function renderQueue() {
  app.innerHTML = `
    <section class="toolbar">
      <div><span class="eyebrow">Сборка</span><h1>Очередь флориста</h1></div>
      <span class="badge">${activeOrders().length} активных</span>
    </section>
    <section class="stack">${activeOrders().map(floristOrderCard).join("")}</section>
  `;
}

function renderOrder() {
  const requested = Number(new URLSearchParams(location.search).get("order"));
  const order = state.orders.find((item) => item.id === requested) || activeOrders()[0] || state.orders[0];
  app.innerHTML = `
    <section class="toolbar">
      <div><span class="eyebrow">Работа с заказом #${order.id}</span><h1>${Bloom.statusLabels[order.status]}</h1></div>
      <a class="secondary" href="/specialist/queue">К очереди</a>
    </section>
    ${floristOrderCard(order)}
    <form id="note-form" class="form card">
      <h2>Заметка флориста</h2>
      <textarea name="note" placeholder="Например: заменили пионовидную розу на садовую розу Juliet">Букет собран пышнее, добавлен эвкалипт для объема.</textarea>
      <input name="photo_url" placeholder="Ссылка на фото сборки" value="https://images.unsplash.com/photo-1518895949257-7621c3c786d7" />
      <button class="primary">Добавить в timeline</button>
    </form>
  `;
}

function renderStock() {
  app.innerHTML = `
    <section class="toolbar">
      <div><span class="eyebrow">Склад</span><h1>Цветы и упаковка</h1></div>
      <button class="secondary" id="reload-stock">Обновить</button>
    </section>
    <section class="stack">
      ${state.inventory.map((item) => `
        <article class="card">
          <div class="row">
            <div><h3>${item.name}</h3><p>${item.category} • поставщик ${item.supplier}</p></div>
            <span class="badge">${item.quantity} ${item.unit}</span>
          </div>
          <div class="progress"><i class="done"></i><i class="${item.quantity > item.reorder_level ? "done" : ""}"></i><i class="${item.quantity > item.reorder_level * 2 ? "done" : ""}"></i></div>
          <form class="inline-form" data-stock="${item.id}">
            <input type="number" name="quantity" value="${item.quantity}" min="0" />
            <input name="note" value="Обновлено после утренней приемки" />
            <button class="primary">Сохранить</button>
          </form>
        </article>
      `).join("")}
    </section>
  `;
}

async function patchOrder(id, status, note = "") {
  const labels = { assembling: "Флорист принял заказ", ready: "Букет готов к доставке" };
  await Bloom.api(`/api/orders/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      status,
      actor_role: "specialist",
      florist_name: "Мария",
      note: note || labels[status],
    }),
  });
  Bloom.toast("Заказ обновлен");
  await refresh();
}

async function addNote(event) {
  event.preventDefault();
  const requested = Number(new URLSearchParams(location.search).get("order"));
  const order = state.orders.find((item) => item.id === requested) || activeOrders()[0] || state.orders[0];
  const form = new FormData(event.currentTarget);
  await patchOrder(order.id, order.status, `${form.get("note")} Фото: ${form.get("photo_url")}`);
}

async function updateStock(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  await Bloom.api("/api/inventory", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      item_id: Number(event.currentTarget.dataset.stock),
      quantity: Number(form.get("quantity")),
      note: form.get("note"),
    }),
  });
  Bloom.toast("Остаток сохранен");
  await refresh();
}

function bind() {
  document.querySelectorAll("[data-status]").forEach((node) => {
    node.addEventListener("click", () => patchOrder(node.dataset.status, node.dataset.next));
  });
  document.querySelector("#note-form")?.addEventListener("submit", addNote);
  document.querySelectorAll("[data-stock]").forEach((node) => node.addEventListener("submit", updateStock));
  document.querySelector("#reload-stock")?.addEventListener("click", refresh);
}

function render() {
  if (page === "queue") renderQueue();
  else if (page === "order") renderOrder();
  else if (page === "stock") renderStock();
  else renderHome();
  bind();
}

async function refresh() {
  state = await Bloom.api();
  render();
}

refresh().catch((error) => {
  app.innerHTML = `<article class="card"><h1>Очередь недоступна</h1><p>${error.message}</p></article>`;
});
