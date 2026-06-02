---
metadata_schema: grounded.skill.v2
description: Shop, catalog, cart, and product browsing workflow pack.
whenToUse:
  - магазин
  - каталог
  - товар
  - товары
  - корзина
  - заказ
  - shop
  - catalog
  - product
  - cart
  - checkout
paths:
  - miniapp/app/static/**
  - miniapp/app/routes/**
  - miniapp/tests/**
allowedTools:
  - read_files
  - search_files
  - apply_patch_to_draft
  - write_file
  - run_checks
  - browser_verify
model: default
effort: high
triggerRules:
  - Match prompts using this skill domain and path scope.
validation:
  - catalog_workflow
  - persisted_workflow
  - role_coverage
  - browser_flow_smoke
requiredProof:
  - Final readiness proof covers this skill.
incompatibleSkills:
  - ""
outputExpectations:
  - Produce working product changes and cite proof artifacts.
---
# Shop / Catalog

Use this skill when the product sells or displays products, bundles, menus, stock, subscriptions, or catalog items.

## Rules

- Persist product id, name, category, price, availability, image or visual marker, description, options, and stock state.
- Expose a client flow: browse categories, search or filter, open item details, add to cart or favorites, submit order.
- Expose a specialist flow: prepare or process ordered items, update item availability, and see option details.
- Expose a manager flow: catalog health, low stock, top items, revenue or order count, and unpublished or unavailable products.
- Make cart totals deterministic: quantity, options, subtotal, discounts or fees, and final total must match saved order data.
- Use empty states for no products, no search results, and unavailable items.

## Acceptance

- API proof creates or reads catalog items and persists an order or cart marker.
- Browser proof adds a product to cart and verifies the saved order appears in a fulfillment or manager surface.
- UI proof shows product details from persisted data, not hardcoded-only cards.
- Role coverage includes client shopping and manager catalog/order visibility.
- Tests pass for catalog read and order creation.
