back:
	cd backend && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

front:
	cd frontend && npm run dev

pre-commit:
	cd backend && uv add --dev pre-commit
	uv run pre-commit install --install-hooks --overwrite

uv-install:
	cd backend && uv sync

# --- debugging exercise: revenue dashboard ---------------------------------
# Postgres and Redis from docker-compose; the app code reaches them on the
# host ports the compose file publishes.
E2E_ENV = PYTHONPATH=. \
          DATABASE_URL=postgresql://postgres:postgres@localhost:5433/propertyflow \
          REDIS_URL=redis://localhost:6380/0

up:
	docker compose up -d db redis

down:
	docker compose down

test:
	cd backend && .venv/bin/python -m pytest tests/ -q

test-e2e: up
	cd backend && REVENUE_INTEGRATION_DB=1 $(E2E_ENV) .venv/bin/python -m pytest tests/ -v

demo: up
	cd backend && $(E2E_ENV) .venv/bin/python scripts/demo_fixes.py

seed-check: up
	docker compose exec -T db psql -U postgres -d propertyflow -c \
	  "select property_id, tenant_id, count(*) n, sum(total_amount) total \
	   from reservations group by 1,2 order by 2,1;"
