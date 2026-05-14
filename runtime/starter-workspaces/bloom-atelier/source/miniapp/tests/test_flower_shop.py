from __future__ import annotations

import os
import tempfile
from pathlib import Path

db_path = Path(tempfile.mkdtemp()) / "bloom-test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_three_role_order_workflow_persists() -> None:
    with TestClient(app) as client:
        shop = client.get("/api/shop").json()
        assert shop["status"] == "ok"
        assert len(shop["bouquets"]) >= 8
        assert len(shop["orders"]) >= 3

        created = client.post(
            "/api/bouquets",
            json={
                "name": "Ночная Роза",
                "mood": "premium",
                "price": 7200,
                "palette": "orchid",
                "stems": "орхидея, роза, озотамнус",
                "description": "Глубокий вечерний букет для премиального заказа",
                "image_tone": "orchid",
                "is_available": True,
            },
        )
        assert created.status_code == 200
        bouquet_id = created.json()["item"]["id"]

        edited = client.patch(f"/api/bouquets/{bouquet_id}", json={"price": 7400, "is_available": True})
        assert edited.status_code == 200
        assert edited.json()["item"]["price"] == 7400

        checkout = client.post(
            "/api/cart/checkout",
            json={
                "customer_name": "Ирина",
                "recipient_name": "Ирина",
                "phone": "+7 999 100-20-30",
                "address": "Тверская, 15",
                "delivery_window": "Завтра 11:00-13:00",
                "message": "Для нового дома",
                "items": [{"bouquet_id": bouquet_id, "qty": 2}],
            },
        )
        assert checkout.status_code == 200
        order = checkout.json()["order"]
        assert order["total"] == 14800
        assert order["status_label"] == "Новый заказ"

        accepted = client.patch(
            f"/api/orders/{order['id']}",
            json={
                "actor_role": "specialist",
                "status": "assembling",
                "florist_name": "Мария",
                "note": "Флорист принял заказ и проверил орхидею",
            },
        )
        assert accepted.status_code == 200
        assert accepted.json()["order"]["status"] == "assembling"

        ready = client.patch(
            f"/api/orders/{order['id']}",
            json={"actor_role": "specialist", "status": "ready", "replacement_stems": "добавлен эвкалипт"},
        )
        assert ready.status_code == 200

        analytics = client.get("/api/analytics").json()
        assert analytics["total_orders"] >= 4
        assert analytics["orders_by_status"]["ready"] >= 1
        assert analytics["average_order_value"] > 0

        client.patch(
            f"/api/orders/{order['id']}",
            json={"actor_role": "manager", "status": "courier", "note": "Передан курьеру"},
        )
        client.patch(
            f"/api/orders/{order['id']}",
            json={"actor_role": "client", "status": "completed", "note": "Клиент подтвердил получение"},
        )

    with TestClient(app) as client:
        reloaded = client.get(f"/api/orders/{order['id']}").json()["order"]
        assert reloaded["status"] == "completed"
        labels = [event["label"] for event in reloaded["timeline"]]
        assert "Флорист собирает" in labels
        assert "Завершен клиентом" in labels


def test_inventory_can_be_updated_by_item_id() -> None:
    with TestClient(app) as client:
        item = client.get("/api/shop").json()["inventory"][0]
        response = client.patch(
            "/api/inventory",
            json={"item_id": item["id"], "quantity": item["quantity"] + 5, "note": "Приемка поставки"},
        )
        assert response.status_code == 200
        assert response.json()["item"]["quantity"] == item["quantity"] + 5
