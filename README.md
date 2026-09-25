# smart.park

A multi-role parking management system (Super Admin, Admin, Guard, Driver)
covering parking locations, slots, vehicle sessions, reservations, M-Pesa
payments, camera/OCR plate scanning, audit logging, and reporting —

```
python main.py
```

That's the entire startup command. No
app factory.

---

## 1. Folder structure

```
smart_parking/
├── main.py                  # Entrypoint — starts the HTTP server
├── config.py                # Settings (replaces app.config)
├── create_super_admin.py    # CLI script to bootstrap the first account
├── requirements.txt
├── .env.example
│
├── database/
│   └── db.py                 # sqlite3 connection pool + schema (replaces SQLAlchemy)
│
├── models/                   # One file per table — raw SQL, no ORM
│   ├── user.py
│   ├── location.py
│   ├── slot.py
│   ├── session.py
│   ├── reservation.py
│   └── audit_log.py
│
├── services/                  # Business logic shared across controllers
│   ├── parking.py     # check-in / check-out logic
│   ├── email.py       # smtplib-based email sending
│   └── notification.py
│
├── controllers/                # One file per role — replaces routes/
│   ├── auth.py
│   ├── driver.py
│   ├── admin.py
│   ├── super_admin.py
│   ├── guard.py
│   └── api.py       # JSON endpoints: slots, sessions, camera/OCR, M-Pesa
│
├── auth/
│   ├── session_manager.py      # Custom session/cookie/login/flash system
│   └── decorators.py           # @login_required, @admin_required, etc.
│
├── core/                       # The "framework" — built from stdlib only
│   ├── http.py                 # Request / Response objects
│   ├── router.py                # URL routing (Blueprint, Router, url_for)
│   └── templating.py            # Jinja2 environment and template globals
│
├── utils/
│   ├── security.py              # Password/PIN hashing (hashlib PBKDF2)
│   ├── fee_calculator.py        # No framework dependencies
│   └── plate_validator.py       # No framework dependencies
│
├── reports/
│   ├── revenue_report.py        # Admin Reports page data
│   └── congestion_report.py     # Super Admin Congestion page data
│
├── templates/                   # All 41 original .html files, byte-for-byte
├── static/                      # All CSS/JS/images, byte-for-byte
└── instance/
    └── smart_parking.db         # Created automatically on first run
```

---

## 3. Database schema

Every table below is a direct, column-for-column port of the original
SQLAlchemy model classes (`app/models/orm.py`), created via raw
`CREATE TABLE IF NOT EXISTS` statements in `database/db.py` instead of
`db.create_all()`.

| Table | Purpose | Key columns |
|---|---|---|
| `users` | All accounts (super_admin/admin/guard/driver) | `email` (unique), `password` (PBKDF2 hash), `role`, `location_id` |
| `locations` | Parking locations | `hourly_rate`, `mpesa_*` credentials |
| `slots` | Physical bays at a location | `status` (available/occupied/reserved/disabled), `sort_order` |
| `sessions` | One row per vehicle visit | `entry_time`/`exit_time` (ISO strings), `fee_kes`, `receipt_number` (unique), `mpesa_code` |
| `reservations` | Driver bookings + event/VIP reservations | `status` (pending/active/conflict/expired/...), `reserved_from`/`reserved_until` |
| `audit_logs` | Append-only action history | `details` (JSON string), `action`, `role` |
| `email_failures` | Failed outgoing emails, for visibility | `to`, `subject`, `error` |
| `system_state` | Misc throttling state (e.g. reservation sync timestamps) | `key`, `last_run` |
| `http_sessions` | **New** — server-side session store (see §4) | `session_id`, `data` (JSON), `expires_at` |

**Datetime storage convention:** every datetime is stored as a naive UTC
`datetime.isoformat()` string (no native SQLite datetime type exists).
Code that needs a real `datetime` object back calls
`datetime.fromisoformat(row["entry_time"])` explicitly — see §6 for why
this matters for templates.

---

## 4. How authentication & sessions work now

The session stores the logged-in user's ID in a server-side table, and
reloaded the full user object via a registered callback. This system
uses a **server-side session** instead:

1. On login, `auth/session_manager.login_user()` stores `user.id` under
   `request.session["_user_id"]`.
2. `save_session_from_request()` (called once per request, after the
   controller runs) writes that session dict as JSON into the
   `http_sessions` table, and sends the browser a cookie containing
   `session_id.hmac_signature`.
3. On the next request, `load_session_into_request()` verifies the
   HMAC signature (rejecting any cookie this server didn't issue),
   loads the session row, and calls `User.get_by_id()` to populate
   `request.current_user` is available in every controller and template.
   
4. `@login_required` / `@admin_required` / `@guard_required` / etc. in
   `auth/decorators.py` check `request.current_user` and `.role` before
   calling the wrapped controller function — replacing the original
   `middleware/role_required.py`.

Why server-side instead of a pure client-side cookie (closer to what
Server-side sessions keep cookies small, support arbitrary
session data (flash messages, the pricing-PIN "verified" flag) without
bloating every request's headers, and allows instant server-side
invalidation on logout.

---

## 5. How routing works now

`core/router.py`'s `Router` class keeps a list of `(methods, compiled
regex, handler, endpoint_name)` tuples — built from `Blueprint` objects
that group routes by role:

```python
guard_bp = Blueprint("guard")

@guard_bp.route("/dashboard")
@guard_required
def dashboard(request: Request) -> Response:
    ...
```

`main.py` registers every controller's blueprint with the exact same
URL prefix (`/admin`, `/guard`, `/driver`,
`/super-admin`, `/api`, and no prefix for auth — see
`app/__init__.py`'s `register_blueprint()` calls in the original code).

`Router.url_for("guard.dashboard")` resolves an endpoint name back to a
path string, including a special case for `url_for("static",
filename=...)` for CSS/JS/image links.
static-file endpoint.

---

## 8. Running tests

```bash
pip install -r requirements.txt
pytest
```

Tests live in `tests/`, one file per module (`test_fee_calculator.py`,
`test_plate_validator.py`, `test_security.py`, `test_slot_concurrency.py`,
`test_session_state_machine.py`, `test_reservations.py`,
`test_parking.py`). Each test opens its own temporary SQLite
file (see `tests/conftest.py`), so running the suite never touches
`instance/smart_parking.db`.

---

## 9. Running it

```bash
pip install -r requirements.txt
cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"   # paste into SECRET_KEY in .env
python create_super_admin.py
python main.py
```

Then open `http://127.0.0.1:5000/`.

**M-Pesa note:** if a location has no real Daraja API credentials
configured (Admin → Pricing & Payments), the system falls back to a
clearly-labelled simulation mode (`MPESA_SIMULATION=true` in `.env`),
so the payment flow can be exercised end-to-end without a real
Safaricom sandbox account.

**OCR note:** if `easyocr` is not installed, the camera scan endpoint
returns a friendly "OCR not available, enter manually" response instead
of crashing — the rest of the system is unaffected.
