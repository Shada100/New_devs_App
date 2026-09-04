# Revenue Dashboard — Investigation Notes

Three client-facing symptoms, four underlying bugs. All of them live in the
revenue path: `dashboard.py` → `cache.py` → `reservations.py` → `database_pool.py`.

---

## 1. Ocean Rentals seeing another company's revenue

**Symptom:** "Sometimes when we refresh the page, we see revenue numbers that
look like they belong to another company."

**Root cause:** `app/services/cache.py` built its cache key from the property ID
alone:

```python
cache_key = f"revenue:{property_id}"
```

Property IDs are only unique *within* a tenant. The schema says so explicitly —
`properties` has a composite primary key of `(id, tenant_id)` — and the seed data
uses it:

| property_id | tenant | name |
|---|---|---|
| `prop-001` | tenant-a (Sunset) | Beach House Alpha |
| `prop-001` | tenant-b (Ocean) | Mountain Lodge Beta |

So `revenue:prop-001` is a single Redis slot that both clients read and write.
Whoever loaded the dashboard first won the slot, and for the next five minutes
the other client was served their competitor's totals. The "sometimes" in the
report is the 5 minute TTL — the leak appears and disappears as the key expires.

**Fix:** the tenant is part of the key.

```python
return f"revenue:{tenant_id}:{property_id}"
```

The cached payload also carries its `tenant_id`, and a read that doesn't match
the caller is dropped instead of returned. That second check is belt-and-braces
for entries left in Redis under the old key format.

---

## 2. Sunset Properties' March total not matching their records

**Symptom:** "We're showing different totals for March."

**Root cause:** `calculate_monthly_revenue` built its month boundaries from
naive datetimes:

```python
start_date = datetime(year, month, 1)
```

`check_in_date` is `TIMESTAMP WITH TIME ZONE`, so a naive boundary gets compared
as UTC. But Sunset's properties are in `Europe/Paris`, and their March doesn't
start at midnight UTC — it starts at **23:00 UTC on 29 February**.

The seed data contains exactly the booking that falls in that gap:

```sql
('res-tz-1', 'prop-001', 'tenant-a', '2024-02-29 23:30:00+00', ..., 1250.000)
```

23:30 UTC on Feb 29 is **00:30 on 1 March in Paris**. The client counts it as
March revenue. The dashboard's UTC boundary put it before March and dropped it,
so Sunset's March was short by €1,250 — and it wasn't in February either,
because February's UTC boundary excluded it from the other end.

**Fix:** boundaries are built in the property's own timezone and converted to
UTC for the query.

```python
start_local = datetime(year, month, 1, tzinfo=ZoneInfo(timezone_name))
```

The property's timezone comes from the `properties` row, scoped to the tenant.
This also handles the DST edge: Paris is UTC+1 on 1 March and UTC+2 by 31 March,
so the two ends of the month have different offsets. A fixed offset would still
have been wrong at one end.

Same month, two properties, two different answers — which is correct:

| property | timezone | March 2024 starts (UTC) |
|---|---|---|
| Beach House Alpha | Europe/Paris | 2024-02-29 23:00 |
| Lakeside Cottage | America/New_York | 2024-03-01 05:00 |

---

## 3. Totals "slightly off by a few cents"

Two separate mechanisms, both in the money path.

**a. Rounding in the wrong order.** `total_amount` is `NUMERIC(10, 3)` —
deliberately sub-cent. prop-001's three March bookings are `333.333`, `333.333`,
`333.334`, which sum to exactly `1000.000`. Round each row to the cent first and
you get `999.99`. A cent per property, quietly, forever.

The fix sums at full precision and rounds once, half-up, at the edge.

**b. `float` at the API boundary.** `dashboard.py` did:

```python
total_revenue_float = float(revenue_data['total'])
```

Money in binary floating point doesn't hold. Four ordinary two-decimal bookings
are enough to show it:

```
4527.20 + 2119.79 + 4073.86 + 4767.44
  Decimal -> 15488.29
  float   -> 15488.289999999999
```

**Fix:** the total stays a `Decimal` end to end and is serialised as a decimal
string. `total_revenue` is now `"4975.50"` rather than `4975.5`. That's a
deliberate response-shape change — it's the only way to hand a client an exact
figure over JSON, and it's what the finance team needs to reconcile.

---

## 4. The revenue query never reached the database

Worth calling out on its own, because it's the reason both clients could see
*identical* numbers regardless of who they were.

`calculate_total_revenue` wrapped everything in a bare `except`, and on failure
returned a hardcoded table:

```python
mock_data = {
    'prop-001': {'total': '1000.00', 'count': 3},
    ...
}
```

That fallback is keyed on `property_id` only — no tenant — so both clients got
the same fabricated figure for `prop-001`.

And it was firing on *every* request, because the database path could not work:

- `DatabasePool.initialize()` built its URL from `settings.supabase_db_user`,
  `settings.supabase_db_host` and friends. Those settings don't exist; the
  config defines `database_url`. Every initialize raised `AttributeError`,
  which the method swallowed, leaving `session_factory` as `None`.
- `get_session()` was `async def` returning a session, but callers used
  `async with db_pool.get_session()`. A coroutine isn't a context manager.
- `calculate_total_revenue` constructed a brand new `DatabasePool()` per call
  instead of using the module-level singleton, so every request would have built
  its own engine and connection pool.

**Fix:** the pool is built from `settings.database_url` (with the scheme
rewritten to `postgresql+asyncpg://`, since the async engine needs an async
driver), `get_session` is a proper `@asynccontextmanager`, initialization is
idempotent, and the shared `db_pool` instance is used.

The mock table is gone. A dashboard that can't reach the database should say so,
not invent revenue figures — silently serving made-up financials to a client
preparing for a board meeting is worse than an error page.

---

## 5. Smaller things fixed along the way

- **`default_tenant` fallback.** `dashboard.py` did
  `getattr(current_user, "tenant_id", "default_tenant") or "default_tenant"`.
  Any account whose tenant didn't resolve was dropped into one shared bucket
  with every other such account. It now returns `403` — if we don't know who is
  asking, we don't answer.
- **No ownership check.** Any authenticated user could pass any `property_id`.
  The lookup is now scoped to the tenant and returns `404` for a property that
  isn't theirs.
- **`calculate_monthly_revenue` had no `tenant_id` parameter** at all, while its
  SQL referenced one. It also returned `Decimal('0')` unconditionally — the
  query was never executed. It's now implemented and tenant-scoped.
- **Mixed currencies.** The result was hardcoded to `"USD"` regardless of what
  was stored. The currency now comes from the data, and a property with
  reservations in more than one currency raises rather than adding EUR to USD to
  produce a meaningless number.

---

## Verifying

```bash
cd backend && python -m pytest tests/ -q
```

14 tests, one per symptom plus the edge cases (December year-rollover, the DST
shift across March, the poisoned-cache-entry path). Each was written to fail
against the original code.
