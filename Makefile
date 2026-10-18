PY = .venv/bin/python
ORDERS = data/source/orders.parquet

.PHONY: data api test db-up reset-db demo freshness

data:
	$(PY) -m dqpipe.source.generator

$(ORDERS): src/dqpipe/source/generator.py
	$(PY) -m dqpipe.source.generator

api: $(ORDERS)
	$(PY) -m uvicorn dqpipe.source.api:app --port 8040

test: $(ORDERS) db-up
	$(PY) -m pytest

db-up:
	docker compose up -d --wait

reset-db: db-up
	$(PY) -m dqpipe.db --reset

# API гасится trap'ом при любом исходе
demo: $(ORDERS) reset-db
	$(PY) -m uvicorn dqpipe.source.api:app --port 8040 --log-level warning & pid=$$!; \
	trap 'kill $$pid' EXIT; \
	for i in $$(seq 50); do curl -sf localhost:8040/health >/dev/null && break; sleep 0.2; done; \
	$(PY) -m dqpipe.simulate --from 2026-08-01 --to 2026-09-30

# make freshness TODAY=2026-10-01 NOW=2026-10-01T12:00
freshness:
	$(PY) -m dqpipe.freshness $(if $(TODAY),--today $(TODAY)) $(if $(NOW),--now $(NOW))
