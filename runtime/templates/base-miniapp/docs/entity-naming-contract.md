# Entity Naming Contract

Choose one dominant internal noun for the main persisted workflow entity and reuse it everywhere.

- Pick one singular name and one plural form.
- Derive one route stem from that noun.
- Reuse the same noun family across `db.py`, `schemas.py`, route modules, UI labels, and API paths.
- Do not mix parallel internal vocabularies such as `booking`, `request`, `submission`, and `appointment` for the same entity unless the prompt explicitly requires multiple entities.
- Prefer one canonical feature route module for the dominant entity instead of splitting the same lifecycle across near-duplicate route files.

Example pattern:

- singular label: `Booking`
- plural label: `Bookings`
- route stem: `bookings`
- route file: `miniapp/app/routes/bookings.py`
- API paths: `/api/bookings`, `/api/bookings/{item_id}`
- schemas: `BookingCreate`, `BookingRead`, `BookingListResponse`
- ORM model: `BookingRecord`

