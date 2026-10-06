# Reproduce the whole project:  make all
# (uses the uv-managed virtual environment in .venv)
PY := .venv/bin/python

.PHONY: all env fetch build analyze maps export dashboard reports

all: fetch build analyze maps export dashboard reports

env:
	uv sync

fetch:
	$(PY) src/fetch_data.py

build:
	$(PY) src/build_dataset.py

analyze:
	cd src && ../$(PY) analysis.py

maps:
	cd src && ../$(PY) maps.py

export:
	cd src && ../$(PY) export_powerbi.py

dashboard:
	cd src && ../$(PY) dashboard.py

reports:
	cd src && ../$(PY) write_reports.py
