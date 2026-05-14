from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


OrderStatus = Literal["new", "confirmed", "assembling", "ready", "courier", "delivered", "completed", "issue"]
BouquetMood = Literal["romantic", "minimal", "premium", "seasonal", "bright"]


class BouquetCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    mood: BouquetMood = "seasonal"
    price: int = Field(ge=500, le=200000)
    palette: str = Field(min_length=2, max_length=80)
    stems: str = Field(default="сезонные цветы", min_length=2, max_length=220)
    description: str = Field(default="", max_length=500)
    image_tone: str = Field(default="rose", max_length=40)
    available: bool = True
    is_available: bool | None = None
    palette_label: str | None = Field(default=None, max_length=80)
    stock_hint: str | None = Field(default=None, max_length=120)
    image_url: str | None = Field(default=None, max_length=300)


class BouquetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    mood: BouquetMood | None = None
    price: int | None = Field(default=None, ge=500, le=200000)
    palette: str | None = Field(default=None, min_length=2, max_length=80)
    stems: str | None = Field(default=None, min_length=2, max_length=220)
    description: str | None = Field(default=None, max_length=500)
    image_tone: str | None = Field(default=None, max_length=40)
    available: bool | None = None
    is_available: bool | None = None


class CheckoutItem(BaseModel):
    bouquet_id: int
    quantity: int = Field(default=1, ge=1, le=12)
    qty: int | None = Field(default=None, ge=1, le=12)


class CheckoutRequest(BaseModel):
    customer_name: str = Field(default="Telegram client", min_length=2, max_length=120)
    phone: str = Field(min_length=5, max_length=80)
    recipient_name: str = Field(min_length=2, max_length=120)
    address: str = Field(min_length=4, max_length=200)
    delivery_window: str = Field(min_length=2, max_length=100)
    card_text: str = Field(default="", max_length=280)
    message: str | None = Field(default=None, max_length=280)
    items: list[CheckoutItem] = Field(min_length=1)


class OrderPatch(BaseModel):
    actor_role: Literal["client", "specialist", "manager"] = "manager"
    status: OrderStatus | None = None
    florist_name: str | None = Field(default=None, max_length=120)
    courier_name: str | None = Field(default=None, max_length=120)
    specialist_note: str | None = Field(default=None, max_length=400)
    manager_note: str | None = Field(default=None, max_length=400)
    issue_note: str | None = Field(default=None, max_length=400)
    replacement_stems: str | None = Field(default=None, max_length=220)
    note: str | None = Field(default=None, max_length=500)
    complete: bool = False
    repeat_order: bool = False


class InventoryPatch(BaseModel):
    name: str | None = None
    item_id: int | None = None
    quantity: int = Field(ge=0, le=10000)
    unit: str = Field(default="stems", max_length=40)
    note: str | None = Field(default=None, max_length=200)


class ShopState(BaseModel):
    status: Literal["ok"] = "ok"
    bouquets: list[dict]
    orders: list[dict]
    inventory: list[dict]
    analytics: dict
