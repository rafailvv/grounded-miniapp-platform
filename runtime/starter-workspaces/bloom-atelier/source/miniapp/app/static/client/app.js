const role = "client";
window.setupPreviewBridge?.(role);

const page = document.body.dataset.page || "home";
const app = document.querySelector("#app");
const nav = document.querySelector("#nav");
nav.innerHTML = Bloom.nav(role, page);

let state = null;
let cart = JSON.parse(localStorage.getItem("bloom_cart") || "[]");

function saveCart() {
  localStorage.setItem("bloom_cart", JSON.stringify(cart));
}

function cartTotal() {
  return cart.reduce((sum, item) => sum + item.price * item.qty, 0);
}

function findBouquet(id) {
  return state.bouquets.find((item) => item.id === Number(id));
}

function addToCart(id) {
  const bouquet = findBouquet(id);
  if (!bouquet || !bouquet.is_available) return;
  const existing = cart.find((item) => item.id === bouquet.id);
  if (existing) existing.qty += 1;
  else cart.push({ id: bouquet.id, name: bouquet.name, price: bouquet.price, qty: 1 });
  saveCart();
  Bloom.toast("Букет добавлен в корзину");
  render();
}

function setQty(id, qty) {
  cart = cart.map((item) => item.id === Number(id) ? { ...item, qty: Math.max(1, qty) } : item);
  saveCart();
  render();
}

function removeItem(id) {
  cart = cart.filter((item) => item.id !== Number(id));
  saveCart();
  render();
}

function bouquetCard(item) {
  return `
    <article class="bouquet">
      <img class="bouquet-photo" src="${item.image_url}" alt="${item.name}" loading="lazy" />
      <div>
        <div class="row">
          <h3>${item.name}</h3>
          <strong>${Bloom.money(item.price)}</strong>
        </div>
        <p>${item.description}</p>
        <div class="chips">
          <span>${item.mood_label}</span>
          <span>${item.palette_label}</span>
          <span>${item.stock_hint}</span>
        </div>
      </div>
      <button class="primary" data-add="${item.id}" ${item.is_available ? "" : "disabled"}>${item.is_available ? "В корзину" : "Нет в наличии"}</button>
    </article>
  `;
}

function orderCard(order) {
  const title = `${order.recipient_name}, ${order.delivery_window}`;
  return `
    <article class="card">
      <div class="row">
        <div>
          <span class="eyebrow">Заказ #${order.id}</span>
          <h3>${title}</h3>
        </div>
        <span class="badge">${Bloom.statusLabels[order.status]}</span>
      </div>
      ${Bloom.progress(order)}
      <p>${order.address}</p>
      <div class="row">
        <strong>${Bloom.money(order.total)}</strong>
        <span>${order.items.map((item) => `${item.name} x${item.qty}`).join(", ")}</span>
      </div>
      ${Bloom.timeline(order)}
      <div class="actions">
        <a class="secondary" href="/client/order?order=${order.id}">Открыть</a>
        <button class="secondary" data-repeat="${order.id}">Повторить</button>
        ${order.status === "delivered" ? `<button class="primary" data-complete="${order.id}">Все отлично</button>` : ""}
      </div>
    </article>
  `;
}

function hero() {
  return `
    <section class="hero">
      <div>
        <span class="eyebrow">Bloom Atelier</span>
        <h1>Букеты, которые приезжают вовремя и выглядят как на витрине.</h1>
        <p>Выберите настроение, оформите доставку в пару касаний и следите за сборкой флориста.</p>
      </div>
      <a class="primary" href="/client/catalog">Собрать заказ</a>
    </section>
  `;
}

function renderHome() {
  const activeOrder = state.orders.find((order) => !["completed"].includes(order.status));
  const picks = state.bouquets.filter((item) => item.is_available).slice(0, 4);
  app.innerHTML = `
    ${hero()}
    <section class="stats">
      <div><strong>${state.bouquets.filter((item) => item.is_available).length}</strong><span>доступно</span></div>
      <div><strong>${cart.length}</strong><span>в корзине</span></div>
      <div><strong>${activeOrder ? Bloom.statusLabels[activeOrder.status] : "Свободно"}</strong><span>последний статус</span></div>
    </section>
    <section class="stack">
      <div class="row"><h2>Подборка дня</h2><a href="/client/catalog">Все букеты</a></div>
      <div class="grid">${picks.map(bouquetCard).join("")}</div>
    </section>
    ${activeOrder ? `<section class="stack"><div class="row"><h2>Текущий заказ</h2><a href="/client/orders">История</a></div>${orderCard(activeOrder)}</section>` : ""}
  `;
}

function renderCatalog() {
  const selected = new URLSearchParams(location.search).get("mood") || "all";
  const moods = ["all", ...new Set(state.bouquets.map((item) => item.mood))];
  const visible = selected === "all" ? state.bouquets : state.bouquets.filter((item) => item.mood === selected);
  app.innerHTML = `
    <section class="toolbar">
      <div>
        <span class="eyebrow">Каталог</span>
        <h1>Выберите букет</h1>
      </div>
      <select id="mood-filter">${moods.map((mood) => `<option value="${mood}" ${mood === selected ? "selected" : ""}>${mood === "all" ? "Все настроения" : state.bouquets.find((item) => item.mood === mood)?.mood_label}</option>`).join("")}</select>
    </section>
    <section class="grid">${visible.map(bouquetCard).join("")}</section>
  `;
  document.querySelector("#mood-filter").addEventListener("change", (event) => {
    location.href = `/client/catalog?mood=${event.target.value}`;
  });
}

function renderCart() {
  app.innerHTML = `
    <section class="toolbar">
      <div>
        <span class="eyebrow">Checkout</span>
        <h1>Корзина</h1>
      </div>
      <strong>${Bloom.money(cartTotal())}</strong>
    </section>
    <section class="stack">
      ${cart.length ? cart.map((item) => `
        <article class="card">
          <div class="row">
            <div><h3>${item.name}</h3><p>${Bloom.money(item.price)} за букет</p></div>
            <strong>${Bloom.money(item.price * item.qty)}</strong>
          </div>
          <div class="actions">
            <button class="secondary" data-dec="${item.id}">-</button>
            <span class="badge">${item.qty}</span>
            <button class="secondary" data-inc="${item.id}">+</button>
            <button class="secondary danger" data-remove="${item.id}">Убрать</button>
          </div>
        </article>
      `).join("") : `<article class="card"><h3>Корзина пустая</h3><p>Откройте каталог и добавьте один или несколько букетов.</p><a class="primary" href="/client/catalog">В каталог</a></article>`}
    </section>
    ${cart.length ? `
      <form id="checkout" class="form card">
        <h2>Доставка</h2>
        <input name="recipient_name" placeholder="Получатель" value="Анна Волкова" required />
        <input name="phone" placeholder="Телефон" value="+7 999 214-18-22" required />
        <input name="address" placeholder="Адрес" value="Патриаршие пруды, 8" required />
        <input name="delivery_window" placeholder="Окно доставки" value="Сегодня 18:00-20:00" required />
        <textarea name="message" placeholder="Открытка">С днем рождения, пусть будет нежно и светло.</textarea>
        <button class="primary">Оформить заказ</button>
      </form>
    ` : ""}
  `;
}

function renderOrders() {
  app.innerHTML = `
    <section class="toolbar">
      <div><span class="eyebrow">История</span><h1>Ваши заказы</h1></div>
      <a class="primary" href="/client/catalog">Новый букет</a>
    </section>
    <section class="stack">${state.orders.map(orderCard).join("")}</section>
  `;
}

function renderOrder() {
  const requested = Number(new URLSearchParams(location.search).get("order"));
  const order = state.orders.find((item) => item.id === requested) || state.orders[0];
  app.innerHTML = `
    <section class="toolbar">
      <div><span class="eyebrow">Заказ #${order.id}</span><h1>${Bloom.statusLabels[order.status]}</h1></div>
      <a class="secondary" href="/client/orders">Все заказы</a>
    </section>
    ${orderCard(order)}
  `;
}

async function checkout(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = {
    recipient_name: form.get("recipient_name"),
    phone: form.get("phone"),
    address: form.get("address"),
    delivery_window: form.get("delivery_window"),
    message: form.get("message"),
    items: cart.map((item) => ({ bouquet_id: item.id, qty: item.qty })),
  };
  await Bloom.api("/api/cart/checkout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  cart = [];
  saveCart();
  Bloom.toast("Заказ оформлен");
  location.href = "/client/orders";
}

async function completeOrder(id) {
  await Bloom.api(`/api/orders/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: "completed", actor_role: "client", note: "Клиент подтвердил получение" }),
  });
  await refresh();
}

function repeatOrder(id) {
  const order = state.orders.find((item) => item.id === Number(id));
  cart = order.items.map((item) => ({ id: item.bouquet_id, name: item.name, price: item.price, qty: item.qty }));
  saveCart();
  location.href = "/client/cart";
}

function bind() {
  document.querySelectorAll("[data-add]").forEach((node) => node.addEventListener("click", () => addToCart(node.dataset.add)));
  document.querySelectorAll("[data-inc]").forEach((node) => node.addEventListener("click", () => setQty(node.dataset.inc, cart.find((item) => item.id === Number(node.dataset.inc)).qty + 1)));
  document.querySelectorAll("[data-dec]").forEach((node) => node.addEventListener("click", () => setQty(node.dataset.dec, cart.find((item) => item.id === Number(node.dataset.dec)).qty - 1)));
  document.querySelectorAll("[data-remove]").forEach((node) => node.addEventListener("click", () => removeItem(node.dataset.remove)));
  document.querySelectorAll("[data-complete]").forEach((node) => node.addEventListener("click", () => completeOrder(node.dataset.complete)));
  document.querySelectorAll("[data-repeat]").forEach((node) => node.addEventListener("click", () => repeatOrder(node.dataset.repeat)));
  document.querySelector("#checkout")?.addEventListener("submit", checkout);
}

function render() {
  if (page === "catalog") renderCatalog();
  else if (page === "cart") renderCart();
  else if (page === "orders") renderOrders();
  else if (page === "order") renderOrder();
  else renderHome();
  bind();
}

async function refresh() {
  state = await Bloom.api();
  render();
}

refresh().catch((error) => {
  app.innerHTML = `<article class="card"><h1>Не удалось загрузить магазин</h1><p>${error.message}</p></article>`;
});
