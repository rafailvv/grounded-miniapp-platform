from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, select, text
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship, selectinload

from app.db import Base, SessionLocal
from app.schemas import BouquetCreate, BouquetPatch, CheckoutRequest, InventoryPatch, OrderPatch, ShopState


router = APIRouter(tags=["flower-shop"])

STATUS_LABELS = {
    "new": "Новый заказ",
    "confirmed": "Подтвержден магазином",
    "assembling": "Флорист собирает",
    "ready": "Готов к передаче",
    "courier": "Передан курьеру",
    "delivered": "Доставлен",
    "completed": "Завершен клиентом",
    "issue": "Нужна помощь менеджера",
}


class Bouquet(Base):
    __tablename__ = "bouquets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    mood: Mapped[str] = mapped_column(String(40), default="seasonal")
    price: Mapped[int] = mapped_column(Integer)
    palette: Mapped[str] = mapped_column(String(80))
    stems: Mapped[str] = mapped_column(String(220))
    description: Mapped[str] = mapped_column(Text, default="")
    image_tone: Mapped[str] = mapped_column(String(40), default="rose")
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class FlowerOrder(Base):
    __tablename__ = "flower_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(80))
    recipient_name: Mapped[str] = mapped_column(String(120))
    address: Mapped[str] = mapped_column(String(200))
    delivery_window: Mapped[str] = mapped_column(String(100))
    card_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="new")
    total: Mapped[int] = mapped_column(Integer, default=0)
    florist_name: Mapped[str] = mapped_column(String(120), default="")
    courier_name: Mapped[str] = mapped_column(String(120), default="")
    specialist_note: Mapped[str] = mapped_column(Text, default="")
    manager_note: Mapped[str] = mapped_column(Text, default="")
    issue_note: Mapped[str] = mapped_column(Text, default="")
    replacement_stems: Mapped[str] = mapped_column(String(220), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.id")
    timeline: Mapped[list["OrderEvent"]] = relationship(back_populates="order", cascade="all, delete-orphan", order_by="OrderEvent.id")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("flower_orders.id", ondelete="CASCADE"))
    bouquet_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(120))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[int] = mapped_column(Integer)
    order: Mapped[FlowerOrder] = relationship(back_populates="items")


class OrderEvent(Base):
    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("flower_orders.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(120))
    actor_role: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    order: Mapped[FlowerOrder] = relationship(back_populates="timeline")


class InventoryItem(Base):
    __tablename__ = "inventory_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    unit: Mapped[str] = mapped_column(String(40), default="stems")
    category: Mapped[str] = mapped_column(String(80), default="flowers")
    supplier: Mapped[str] = mapped_column(String(120), default="Bloom Market")
    reorder_level: Mapped[int] = mapped_column(Integer, default=24)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


def get_db():
    db = SessionLocal()
    try:
        _ensure_schema(db)
        yield db
    finally:
        db.close()


def _ensure_schema(db: Session) -> None:
    Base.metadata.create_all(bind=db.get_bind())
    if db.bind is None or db.bind.dialect.name != "sqlite":
        return
    existing = {row[1] for row in db.execute(text("PRAGMA table_info(inventory_items)")).all()}
    missing_columns = {
        "category": "ALTER TABLE inventory_items ADD COLUMN category VARCHAR(80) DEFAULT 'цветы'",
        "supplier": "ALTER TABLE inventory_items ADD COLUMN supplier VARCHAR(120) DEFAULT 'Bloom Market'",
        "reorder_level": "ALTER TABLE inventory_items ADD COLUMN reorder_level INTEGER DEFAULT 24",
    }
    for column, statement in missing_columns.items():
        if column not in existing:
            db.execute(text(statement))
    db.commit()


def _add_event(order: FlowerOrder, actor_role: str, label: str, note: str = "") -> None:
    order.timeline.append(OrderEvent(actor_role=actor_role, label=label, note=note))
    order.updated_at = datetime.now(timezone.utc)


def _bouquet(item: Bouquet) -> dict[str, Any]:
    mood_labels = {
        "romantic": "романтика",
        "minimal": "минимализм",
        "premium": "премиум",
        "seasonal": "сезонный",
        "bright": "яркий",
    }
    return {
        "id": item.id,
        "name": item.name,
        "mood": item.mood,
        "mood_label": mood_labels.get(item.mood, item.mood),
        "price": item.price,
        "palette": item.palette,
        "palette_label": item.palette,
        "stems": item.stems,
        "description": item.description,
        "image_tone": item.image_tone,
        "available": item.available,
        "is_available": item.available,
        "stock_hint": "доступен сегодня" if item.available else "уточнить у менеджера",
        "image_url": f"/static/assets/bouquets/bouquet-{item.id}.jpg",
        "created_at": item.created_at.isoformat(),
    }


def _order(item: FlowerOrder) -> dict[str, Any]:
    return {
        "id": item.id,
        "customer_name": item.customer_name,
        "phone": item.phone,
        "recipient_name": item.recipient_name,
        "address": item.address,
        "delivery_window": item.delivery_window,
        "card_text": item.card_text,
        "status": item.status,
        "status_label": STATUS_LABELS.get(item.status, "Обновлен"),
        "total": item.total,
        "florist_name": item.florist_name,
        "courier_name": item.courier_name,
        "specialist_note": item.specialist_note,
        "manager_note": item.manager_note,
        "issue_note": item.issue_note,
        "replacement_stems": item.replacement_stems,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "items": [
            {
                "id": row.id,
                "bouquet_id": row.bouquet_id,
                "name": row.name,
                "quantity": row.quantity,
                "qty": row.quantity,
                "unit_price": row.unit_price,
                "price": row.unit_price,
            }
            for row in item.items
        ],
        "timeline": [
            {"id": row.id, "label": row.label, "actor_role": row.actor_role, "note": row.note, "created_at": row.created_at.isoformat()}
            for row in item.timeline
        ],
    }


def _inventory(item: InventoryItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "quantity": item.quantity,
        "unit": item.unit,
        "category": item.category,
        "supplier": item.supplier,
        "reorder_level": item.reorder_level,
        "updated_at": item.updated_at.isoformat(),
    }


def _analytics(orders: list[FlowerOrder], bouquets: list[Bouquet], inventory: list[InventoryItem] | None = None) -> dict[str, Any]:
    revenue = sum(item.total for item in orders if item.status != "issue")
    orders_by_status: dict[str, int] = {}
    for order in orders:
        orders_by_status[order.status] = orders_by_status.get(order.status, 0) + 1
    low_stock = [_inventory(item) for item in (inventory or []) if item.quantity <= item.reorder_level]
    return {
        "bouquets": len(bouquets),
        "available_bouquets": sum(1 for item in bouquets if item.available),
        "orders": len(orders),
        "total_orders": len(orders),
        "active_orders": sum(1 for item in orders if item.status not in {"delivered", "completed"}),
        "issues": sum(1 for item in orders if item.status == "issue"),
        "ready": sum(1 for item in orders if item.status == "ready"),
        "revenue": revenue,
        "revenue_today": revenue,
        "average_order_value": round(revenue / len(orders)) if orders else 0,
        "orders_by_status": orders_by_status,
        "low_stock": low_stock,
    }


def _state(db: Session) -> ShopState:
    _seed_if_empty(db)
    bouquets = db.scalars(select(Bouquet).order_by(Bouquet.id)).all()
    orders = db.scalars(select(FlowerOrder).options(selectinload(FlowerOrder.items), selectinload(FlowerOrder.timeline)).order_by(FlowerOrder.id.desc())).all()
    inventory = db.scalars(select(InventoryItem).order_by(InventoryItem.name)).all()
    return ShopState(
        bouquets=[_bouquet(item) for item in bouquets],
        orders=[_order(item) for item in orders],
        inventory=[_inventory(item) for item in inventory],
        analytics=_analytics(orders, bouquets, inventory),
    )


def _seed_if_empty(db: Session) -> None:
    if db.scalar(select(Bouquet.id).limit(1)):
        return
    bouquets = [
        ("Пионовая акварель", "romantic", 5900, "розовый / кремовый", "пионы, ранункулюсы, эвкалипт", "Воздушный букет для дня рождения и признания", "rose"),
        ("Лавандовое утро", "minimal", 4200, "лаванда / белый", "маттиола, лаванда, белая роза", "Легкий ароматный букет для нежного подарка", "lilac"),
        ("Солнечный рынок", "bright", 4800, "желтый / оранжевый", "герберы, тюльпаны, солидаго", "Яркая композиция с настроением выходного дня", "sun"),
        ("Белая галерея", "premium", 7600, "белый / зеленый", "орхидея, эустома, роза, писташ", "Чистая премиальная композиция для важного жеста", "white"),
        ("Сад после дождя", "seasonal", 5300, "сирень / зелень", "гортензия, фрезия, зелень", "Свежий сезонный букет с мягкой фактурой", "leaf"),
        ("Красная линия", "romantic", 6500, "красный / бордо", "розы explorer, скиммия, эвкалипт", "Классический романтичный букет без лишней драмы", "red"),
        ("Молочный раф", "minimal", 3900, "молочный / бежевый", "хризантема, диантус, хлопок", "Спокойная композиция для интерьера и заботы", "cream"),
        ("Вечерний сад", "premium", 8900, "слива / розовый", "пионовидные розы, клематис, озотамнус", "Глубокий авторский букет для особого повода", "plum"),
        ("Тюльпановый сет", "seasonal", 3100, "микс", "25 тюльпанов", "Лаконичный сезонный набор на каждый день", "tulip"),
    ]
    db.add_all([Bouquet(name=name, mood=mood, price=price, palette=palette, stems=stems, description=description, image_tone=tone) for name, mood, price, palette, stems, description, tone in bouquets])
    db.flush()
    inventory = [
        ("Пионы Coral Charm", 46, "stems", "цветы", "Dutch Garden", 30),
        ("Роза White O'Hara", 70, "stems", "цветы", "RoseLab", 36),
        ("Тюльпаны микс", 180, "stems", "цветы", "Local Field", 60),
        ("Эвкалипт", 32, "branches", "зелень", "Green Route", 28),
        ("Крафтовая упаковка", 120, "sheets", "упаковка", "Paper House", 40),
        ("Открытки Bloom", 85, "cards", "полиграфия", "Bloom Atelier", 25),
    ]
    db.add_all([InventoryItem(name=name, quantity=quantity, unit=unit, category=category, supplier=supplier, reorder_level=reorder_level) for name, quantity, unit, category, supplier, reorder_level in inventory])
    bouquet_by_name = {item.name: item for item in db.scalars(select(Bouquet)).all()}

    def order(customer: str, bouquet_name: str, status: str, total: int, florist: str, note: str, address: str) -> FlowerOrder:
        bouquet = bouquet_by_name[bouquet_name]
        item = FlowerOrder(
            customer_name=customer,
            phone="+7 900 000-00-00",
            recipient_name=customer,
            address=address,
            delivery_window="Сегодня 18:00-20:00",
            card_text="С любовью и теплом",
            status=status,
            total=total,
            florist_name=florist,
            specialist_note=note,
            manager_note="Приоритетное окно доставки" if status == "courier" else "",
        )
        item.items.append(OrderItem(bouquet_id=bouquet.id, name=bouquet.name, quantity=1, unit_price=bouquet.price))
        _add_event(item, "client", STATUS_LABELS["new"], f"Заказан букет {bouquet.name}")
        if status in {"assembling", "ready", "courier", "delivered", "completed"}:
            _add_event(item, "specialist", STATUS_LABELS["assembling"], note or "Флорист начал сборку")
        if status in {"ready", "courier", "delivered", "completed"}:
            _add_event(item, "specialist", STATUS_LABELS["ready"], "Букет собран и сфотографирован")
        if status in {"courier", "delivered", "completed"}:
            _add_event(item, "manager", STATUS_LABELS["courier"], "Курьер назначен")
        return item

    db.add_all([
        order("Анна", "Пионовая акварель", "courier", 5900, "Мария", "Пионы раскрыты, упаковка кремовая", "Патриаршие пруды, 12"),
        order("Елена", "Лавандовое утро", "assembling", 4200, "София", "Проверяю свежесть маттиолы", "Большая Никитская, 9"),
        order("Михаил", "Белая галерея", "ready", 7600, "Мария", "Орхидея заменена на более стойкую ветку", "Садовая-Кудринская, 18"),
    ])
    db.commit()


@router.get("/api/shop", response_model=ShopState)
def read_shop(db: Session = Depends(get_db)) -> ShopState:
    return _state(db)


@router.get("/api/bouquets")
def list_bouquets(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"status": "ok", "items": _state(db).bouquets}


@router.post("/api/bouquets")
def create_bouquet(payload: BouquetCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    data = payload.model_dump(exclude={"is_available", "palette_label", "stock_hint", "image_url"})
    if payload.is_available is not None:
        data["available"] = payload.is_available
    item = Bouquet(**data)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"status": "ok", "item": _bouquet(item)}


@router.patch("/api/bouquets/{bouquet_id}")
def update_bouquet(bouquet_id: int, payload: BouquetPatch, db: Session = Depends(get_db)) -> dict[str, Any]:
    item = db.get(Bouquet, bouquet_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Bouquet not found")
    data = payload.model_dump(exclude_unset=True)
    if "is_available" in data:
        data["available"] = data.pop("is_available")
    for key, value in data.items():
        setattr(item, key, value)
    db.commit()
    db.refresh(item)
    return {"status": "ok", "item": _bouquet(item)}


@router.post("/api/cart/checkout")
def checkout(payload: CheckoutRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    _seed_if_empty(db)
    bouquets = {item.id: item for item in db.scalars(select(Bouquet)).all()}
    order = FlowerOrder(
        customer_name=payload.customer_name,
        phone=payload.phone,
        recipient_name=payload.recipient_name,
        address=payload.address,
        delivery_window=payload.delivery_window,
        card_text=payload.message or payload.card_text,
        status="new",
    )
    total = 0
    for row in payload.items:
        bouquet = bouquets.get(row.bouquet_id)
        if bouquet is None or not bouquet.available:
            raise HTTPException(status_code=422, detail=f"Bouquet {row.bouquet_id} unavailable")
        quantity = row.qty or row.quantity
        total += bouquet.price * quantity
        order.items.append(OrderItem(bouquet_id=bouquet.id, name=bouquet.name, quantity=quantity, unit_price=bouquet.price))
    order.total = total
    _add_event(order, "client", STATUS_LABELS["new"], f"Оформлен заказ на {total} ₽")
    db.add(order)
    db.commit()
    db.refresh(order)
    return {"status": "ok", "order": _order(order)}


@router.get("/api/orders/{order_id}")
def get_order(order_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    item = db.scalar(select(FlowerOrder).options(selectinload(FlowerOrder.items), selectinload(FlowerOrder.timeline)).where(FlowerOrder.id == order_id))
    if item is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return {"status": "ok", "order": _order(item)}


@router.patch("/api/orders/{order_id}")
def patch_order(order_id: int, payload: OrderPatch, db: Session = Depends(get_db)) -> dict[str, Any]:
    item = db.scalar(select(FlowerOrder).options(selectinload(FlowerOrder.items), selectinload(FlowerOrder.timeline)).where(FlowerOrder.id == order_id))
    if item is None:
        raise HTTPException(status_code=404, detail="Order not found")
    notes: list[str] = []
    for field in ("florist_name", "courier_name", "specialist_note", "manager_note", "issue_note", "replacement_stems"):
        value = getattr(payload, field)
        if value is not None:
            setattr(item, field, value)
            if value:
                notes.append(value)
    if payload.note:
        notes.append(payload.note)
    if payload.status:
        item.status = payload.status
    if payload.complete:
        item.status = "completed"
        notes.append("Клиент подтвердил получение")
    if payload.repeat_order:
        notes.append("Клиент запросил повтор заказа")
    if payload.issue_note:
        item.status = "issue"
    _add_event(item, payload.actor_role, STATUS_LABELS.get(item.status, "Заказ обновлен"), " · ".join(notes) or "Статус обновлен")
    db.commit()
    db.refresh(item)
    return {"status": "ok", "order": _order(item)}


@router.get("/api/inventory")
def list_inventory(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"status": "ok", "items": _state(db).inventory}


@router.patch("/api/inventory")
def patch_inventory(payload: InventoryPatch, db: Session = Depends(get_db)) -> dict[str, Any]:
    _seed_if_empty(db)
    item = db.get(InventoryItem, payload.item_id) if payload.item_id else None
    if item is None and payload.name:
        item = db.scalar(select(InventoryItem).where(InventoryItem.name == payload.name))
    if item is None:
        if not payload.name:
            raise HTTPException(status_code=422, detail="Inventory name or item_id is required")
        item = InventoryItem(name=payload.name, quantity=payload.quantity, unit=payload.unit)
        db.add(item)
    else:
        item.quantity = payload.quantity
        item.unit = payload.unit
        item.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(item)
    return {"status": "ok", "item": _inventory(item)}


@router.get("/api/analytics")
def analytics(db: Session = Depends(get_db)) -> dict[str, Any]:
    return _state(db).analytics
