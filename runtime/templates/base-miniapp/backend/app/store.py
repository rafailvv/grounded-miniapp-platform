from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from app.schemas import AppRole, RoleDashboardResponse, RoleProfile


BASE_DIR = Path(__file__).resolve().parent
GENERATED_DIR = BASE_DIR / "generated"
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = GENERATED_DIR / "runtime_state.json"
ROLE_SEED_PATH = GENERATED_DIR / "role_seed.json"


DEFAULT_ROLE_SEED = {
    "roles": {
        "client": {
            "title": "Кабинет клиента",
            "description": "Запись и отслеживание своих заявок.",
            "feature_text": "Оформите обращение и следите за ответом специалиста.",
            "primary_action_label": "Создать заявку",
            "secondary_action_label": "Редактировать профиль",
            "metrics": [
                {"metric_id": "client_active", "label": "Активные заявки", "value": "2"},
                {"metric_id": "client_done", "label": "Закрыто", "value": "7"},
            ],
            "profile": {
                "first_name": "Клиент",
                "last_name": "Демо",
                "email": "client@example.local",
                "phone": "+7 (999) 111-22-33",
                "photo_url": None,
            },
        },
        "specialist": {
            "title": "Кабинет специалиста",
            "description": "Рабочая очередь и обработка клиентских запросов.",
            "feature_text": "Просматривайте очередь, принимайте заявки и обновляйте статус.",
            "primary_action_label": "Открыть очередь",
            "secondary_action_label": "Редактировать профиль",
            "metrics": [
                {"metric_id": "specialist_queue", "label": "В очереди", "value": "5"},
                {"metric_id": "specialist_today", "label": "Сегодня", "value": "3"},
            ],
            "profile": {
                "first_name": "Специалист",
                "last_name": "Демо",
                "email": "specialist@example.local",
                "phone": "+7 (999) 222-33-44",
                "photo_url": None,
            },
        },
        "manager": {
            "title": "Кабинет менеджера",
            "description": "Контроль ролей, SLA и общей загрузки.",
            "feature_text": "Следите за SLA, нагрузкой команды и распределением заявок.",
            "primary_action_label": "Открыть контрольную панель",
            "secondary_action_label": "Редактировать профиль",
            "metrics": [
                {"metric_id": "manager_sla", "label": "SLA", "value": "94%"},
                {"metric_id": "manager_load", "label": "Загрузка", "value": "68%"},
            ],
            "profile": {
                "first_name": "Менеджер",
                "last_name": "Демо",
                "email": "manager@example.local",
                "phone": "+7 (999) 333-44-55",
                "photo_url": None,
            },
        },
    }
}


def ensure_state() -> dict:
    if not ROLE_SEED_PATH.exists():
        ROLE_SEED_PATH.write_text(json.dumps(DEFAULT_ROLE_SEED, ensure_ascii=False, indent=2), encoding="utf-8")
    if not STATE_PATH.exists():
        STATE_PATH.write_text(json.dumps(DEFAULT_ROLE_SEED, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_state()


def load_state() -> dict:
    ensure_state()
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def get_role_dashboard(role: AppRole) -> RoleDashboardResponse:
    state = load_state()
    role_state = deepcopy(state["roles"][role])
    return RoleDashboardResponse(role=role, **{k: role_state[k] for k in ["title", "description", "feature_text", "metrics", "primary_action_label", "secondary_action_label"]})


def get_role_profile(role: AppRole) -> RoleProfile:
    state = load_state()
    payload = deepcopy(state["roles"][role]["profile"])
    return RoleProfile.model_validate(payload)


def save_role_profile(role: AppRole, profile: RoleProfile) -> RoleProfile:
    state = load_state()
    payload = profile.model_dump(mode="json")
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["roles"][role]["profile"] = payload
    save_state(state)
    return RoleProfile.model_validate(payload)


def register_submission(payload: dict) -> dict:
    state = load_state()
    state["roles"]["client"]["metrics"][0]["value"] = str(int(state["roles"]["client"]["metrics"][0]["value"]) + 1)
    state["roles"]["specialist"]["metrics"][0]["value"] = str(int(state["roles"]["specialist"]["metrics"][0]["value"]) + 1)
    save_state(state)
    return {
        "submission_id": f"submission-{int(datetime.now(timezone.utc).timestamp())}",
        "status": "stored",
        "payload": payload,
    }

