PY ?= python3
SEASONS ?= 2021 2022 2023 2024

.PHONY: data pipeline test

data:
	$(PY) scripts/get_data.py --seasons $(SEASONS)

pipeline:
	$(PY) -m src.pipeline --db data/nfl.duckdb

test:
	$(PY) -m pytest -q
