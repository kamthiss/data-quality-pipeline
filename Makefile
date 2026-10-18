PY = .venv/bin/python
ORDERS = data/source/orders.parquet

.PHONY: data api test

data:
	$(PY) -m dqpipe.source.generator

$(ORDERS): src/dqpipe/source/generator.py
	$(PY) -m dqpipe.source.generator

api: $(ORDERS)
	$(PY) -m uvicorn dqpipe.source.api:app --port 8040

test: $(ORDERS)
	$(PY) -m pytest
