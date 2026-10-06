"""
Fetch City of Dubuque street condition data from the public ArcGIS REST service.

Source: City of Dubuque Engineering and Public Works Departments, GIS Office
        https://maps.cityofdubuque.org/arcgisweb/rest/services/Street_Ratings_for_Web/MapServer

Pulls two things:
  * Table 3  "Street Ratings"  -> data/raw/street_ratings.json       (attributes only)
  * Layer 2  "2021 Ratings"    -> data/raw/road_centerlines.geojson  (WGS84 polylines)

Design notes
  * The service caps responses at 2000 records, so we page with resultOffset.
  * The service intermittently returns HTTP 500 / an {"error": ...} body, even on
    simple count queries. Each page is retried with exponential backoff, and the
    page size is halved after repeated failures.
  * Records are de-duplicated on OBJECTID and the final count is checked against
    returnCountOnly. Re-running overwrites the output files, so it never appends
    duplicates.
  * where=1=1 is used deliberately; filtering happens later in pandas (a WHERE on
    Y2022 was observed to silently return nothing).

Usage:  python src/fetch_data.py
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://maps.cityofdubuque.org/arcgisweb/rest/services/Street_Ratings_for_Web/MapServer"
RATINGS_TABLE = 3
CENTERLINE_LAYER = 2

RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"

MAX_RECORD_COUNT = 2000      # server-side cap
START_PAGE_SIZE = 500        # 1500 with outFields=* was observed to 500 on table 3
MIN_PAGE_SIZE = 50
MAX_ATTEMPTS_PER_SIZE = 4    # retries before halving page size
TIMEOUT_SECONDS = 90


def get_json(url, params):
    """GET a URL and return parsed JSON. ArcGIS reports some errors as HTTP 200
    with an {"error": ...} body, so treat that as a failure too."""
    resp = requests.get(url, params=params, timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS error: {data['error']}")
    return data


def get_with_retry(url, params, attempts=6):
    """Retry a single request with exponential backoff (1, 2, 4, 8... seconds)."""
    for attempt in range(attempts):
        try:
            return get_json(url, params)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            wait = 2 ** attempt
            print(f"    request failed ({exc}); retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"Giving up on {url} after {attempts} attempts")


def live_count(layer_id):
    url = f"{BASE_URL}/{layer_id}/query"
    return get_with_retry(url, {"where": "1=1", "returnCountOnly": "true", "f": "json"})["count"]


def fetch_page(layer_id, offset, page_size, fmt, extra_params):
    """Fetch one page. On repeated failure, halve the page size and keep trying.
    Returns (features, page_size_that_worked)."""
    url = f"{BASE_URL}/{layer_id}/query"
    size = page_size
    while size >= MIN_PAGE_SIZE:
        params = {
            "where": "1=1",
            "outFields": "*",
            "resultOffset": offset,
            "resultRecordCount": size,
            "orderByFields": "OBJECTID" if fmt == "json" else None,
            "f": fmt,
            **extra_params,
        }
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(MAX_ATTEMPTS_PER_SIZE):
            try:
                data = get_json(url, params)
                return data["features"], size
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                wait = 2 ** attempt
                print(f"    offset={offset} size={size} failed ({exc}); retry in {wait}s")
                time.sleep(wait)
        size //= 2
        print(f"    shrinking page size to {size}")
    raise RuntimeError(f"Could not fetch layer {layer_id} at offset {offset}")


def fetch_all(layer_id, fmt, id_getter, extra_params=None):
    """Page through a layer/table until a short page comes back.
    De-duplicates on OBJECTID so overlapping pages cannot double-count."""
    extra_params = extra_params or {}
    by_id = {}
    rows_returned = 0
    offset = 0
    page_size = min(START_PAGE_SIZE, MAX_RECORD_COUNT)
    while True:
        features, used_size = fetch_page(layer_id, offset, page_size, fmt, extra_params)
        rows_returned += len(features)
        for feat in features:
            # Keep the first occurrence. Layer 2 is a one-to-many join of centerlines
            # to rating rows, so the same centerline can appear more than once.
            by_id.setdefault(id_getter(feat), feat)
        print(f"  layer {layer_id}: offset {offset:>5} -> {len(features)} records (total {len(by_id)})")
        if len(features) < used_size:
            break
        offset += len(features)
        page_size = used_size
    if rows_returned != len(by_id):
        print(f"  note: {rows_returned} rows returned, {len(by_id)} unique OBJECTIDs "
              f"({rows_returned - len(by_id)} duplicates from the service-side join dropped)")
    return [by_id[k] for k in sorted(by_id)], rows_returned


def service_metadata():
    meta = get_with_retry(BASE_URL, {"f": "json"})
    return {
        "mapName": meta.get("mapName"),
        "copyrightText": meta.get("copyrightText"),
        "serviceDescription": meta.get("serviceDescription"),
        "spatialReference_native": meta.get("spatialReference"),
    }


def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    pulled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print("Reading service metadata...")
    meta = service_metadata()

    # --- Table 3: street ratings (no geometry) ---------------------------------
    print("Fetching table 3 (Street Ratings)...")
    expected_ratings = live_count(RATINGS_TABLE)
    ratings, ratings_rows_returned = fetch_all(RATINGS_TABLE, "json", lambda f: f["attributes"]["OBJECTID"])
    rows = [f["attributes"] for f in ratings]
    (RAW_DIR / "street_ratings.json").write_text(json.dumps(rows, indent=1))

    # --- Layer 2: centerlines joined to ratings (geometry, WGS84) ---------------
    print("Fetching layer 2 (2021 Ratings centerlines)...")
    expected_lines = live_count(CENTERLINE_LAYER)
    oid = "DUBUQUE.RoadCenterline.OBJECTID"
    lines, lines_rows_returned = fetch_all(
        CENTERLINE_LAYER, "geojson",
        lambda f: f["properties"][oid],
        extra_params={"outSR": 4326, "orderByFields": oid},
    )
    geojson = {"type": "FeatureCollection", "features": lines}
    (RAW_DIR / "road_centerlines.geojson").write_text(json.dumps(geojson))

    # --- Verify counts ---------------------------------------------------------
    checks = {
        "street_ratings": (len(rows), expected_ratings),
        "road_centerlines": (len(lines), expected_lines),
    }
    for name, (got, expected) in checks.items():
        status = "OK" if got == expected else "MISMATCH"
        print(f"  {name}: fetched {got}, service reports {expected} -> {status}")

    manifest = {
        "pulled_at_utc": pulled_at,
        "service_url": BASE_URL,
        "attribution": meta["copyrightText"],
        "service_metadata": meta,
        "files": {
            "street_ratings.json": {
                "source": f"{BASE_URL}/{RATINGS_TABLE}",
                "records_fetched": len(rows),
                "rows_returned_before_dedup": ratings_rows_returned,
                "records_reported_by_service": expected_ratings,
            },
            "road_centerlines.geojson": {
                "source": f"{BASE_URL}/{CENTERLINE_LAYER}",
                "outSR": 4326,
                "records_fetched": len(lines),
                "rows_returned_before_dedup": lines_rows_returned,
                "records_reported_by_service": expected_lines,
            },
        },
    }
    (RAW_DIR / "_manifest.json").write_text(json.dumps(manifest, indent=2))

    # --- Summary: nulls per rating year ----------------------------------------
    year_cols = sorted(k for k in rows[0] if k.startswith("Y") and k[1:].isdigit())
    print("\nRating columns (table 3):  non-null / null / zero")
    for col in year_cols:
        vals = [r[col] for r in rows]
        nulls = sum(v is None for v in vals)
        zeros = sum(v == 0 for v in vals)
        print(f"  {col}: {len(vals) - nulls:>5} / {nulls:>5} / {zeros:>5}")

    if any(got != expected for got, expected in checks.values()):
        raise SystemExit("Fetched counts do not match the service; see above.")
    print(f"\nDone. Wrote raw files and manifest to {RAW_DIR}")


if __name__ == "__main__":
    main()
