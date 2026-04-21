# Entity Naming Contract

Choose one dominant internal noun for the main persisted workflow entity and reuse it everywhere.

- Pick one singular name and one plural form.
- Derive one route stem from that noun.
- Reuse the same noun family across `db.py`, `schemas.py`, route modules, UI labels, and API paths.
- Do not mix parallel internal vocabularies such as `record`, `item`, `entry`, and `case` for the same entity unless the prompt explicitly requires multiple entities.
- Prefer one canonical feature route module for the dominant entity instead of splitting the same lifecycle across near-duplicate route files.

Example pattern:

- singular label: `Record`
- plural label: `Records`
- route stem: `records`
- route file: `miniapp/app/routes/records.py`
- API paths: `/api/records`, `/api/records/{item_id}`
- schemas: `RecordCreate`, `RecordRead`, `RecordListResponse`
- ORM model: `RecordEntry`
