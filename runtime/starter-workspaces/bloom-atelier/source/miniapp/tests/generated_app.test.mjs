import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { test } from "node:test";

const root = new URL("../app/static/", import.meta.url);
const pages = [
  "client/index.html",
  "client/catalog/index.html",
  "client/cart/index.html",
  "client/orders/index.html",
  "client/order/index.html",
  "specialist/index.html",
  "specialist/queue/index.html",
  "specialist/order/index.html",
  "specialist/stock/index.html",
  "manager/index.html",
  "manager/catalog/index.html",
  "manager/orders/index.html",
  "manager/analytics/index.html",
];

test("all role pages are routeable and connected to the shared app shell", () => {
  for (const page of pages) {
    const html = readFileSync(new URL(page, root), "utf8");
    assert.match(html, /\/static\/shared\/shop\.css/);
    assert.match(html, /\/static\/shared\/shop\.js/);
    assert.match(html, /id="nav"/);
    assert.match(html, /id="app"/);
  }
});

test("role scripts call real app api endpoints", () => {
  const client = readFileSync(new URL("client/app.js", root), "utf8");
  const specialist = readFileSync(new URL("specialist/app.js", root), "utf8");
  const manager = readFileSync(new URL("manager/app.js", root), "utf8");

  assert.match(client, /\/api\/cart\/checkout/);
  assert.match(specialist, /\/api\/orders\/\$\{id\}/);
  assert.match(specialist, /\/api\/inventory/);
  assert.match(manager, /\/api\/bouquets/);
  assert.match(manager, /\/api\/analytics/);
});

test("ui uses localized status labels instead of raw status output", () => {
  const shared = readFileSync(new URL("shared/shop.js", root), "utf8");
  const bundle = [
    readFileSync(new URL("client/app.js", root), "utf8"),
    readFileSync(new URL("specialist/app.js", root), "utf8"),
    readFileSync(new URL("manager/app.js", root), "utf8"),
  ].join("\n");

  assert.match(shared, /statusLabels/);
  assert.doesNotMatch(bundle, /status\}\<\/span\>/);
});

test("bouquet cards use generated photo assets", () => {
  const client = readFileSync(new URL("client/app.js", root), "utf8");
  const manager = readFileSync(new URL("manager/app.js", root), "utf8");

  assert.match(client, /class="bouquet-photo"/);
  assert.match(manager, /class="bouquet-photo"/);
  for (let index = 1; index <= 9; index += 1) {
    assert.equal(existsSync(new URL(`assets/bouquets/bouquet-${index}.jpg`, root)), true);
  }
});
