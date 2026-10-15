"""Public data sources: FAA enplanements (xlsx), BTS T-100 (ArcGIS REST API), BTS On-Time (monthly files),
FAA NAS Status (live API), OurAirports (coords, runways)."""
import csv
import math
import re
import time
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

import httpx

CACHE = Path(__file__).resolve().parent.parent / ".cache"
CACHE.mkdir(exist_ok=True)

URLS = {
    "airports.csv": "https://davidmegginson.github.io/ourairports-data/airports.csv",
    "runways.csv": "https://davidmegginson.github.io/ourairports-data/runways.csv",
}
FAA_URL = "https://nasstatus.faa.gov/api/airport-status-information"
PAVED = ("ASP", "CON", "BIT", "PEM", "MAC", "TAR")


class DataUnavailable(Exception):
    pass


def _download(name: str, ttl_hours: float = 24 * 14) -> Path:
    path = CACHE / name
    if path.exists() and time.time() - path.stat().st_mtime < ttl_hours * 3600:
        return path
    try:
        r = httpx.get(URLS[name], timeout=60, follow_redirects=True)
        r.raise_for_status()
        path.write_bytes(r.content)
    except Exception as e:  # fall back to stale cache if we have one
        if path.exists():
            return path
        raise DataUnavailable(f"Could not fetch {name}: {e}") from e
    return path


@lru_cache(maxsize=1)
def airport_ref() -> dict:
    """IATA -> {ident, name, type, lat, lon, country, region, municipality, scheduled}."""
    out = {}
    type_priority = {"large_airport": 3, "medium_airport": 2, "small_airport": 1}
    with open(_download("airports.csv"), encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            iata = (row.get("iata_code") or "").strip()
            atype = row.get("type", "")
            if iata and atype in type_priority:
                prev = out.get(iata)
                # Deduplicate by preferring higher airport type (large > medium > small)
                if prev and type_priority.get(prev["type"], 0) >= type_priority.get(atype, 0):
                    continue
                out[iata] = {
                    "ident": row["ident"],
                    "name": row["name"],
                    "type": atype,
                    "lat": float(row["latitude_deg"]),
                    "lon": float(row["longitude_deg"]),
                    "country": row.get("iso_country", ""),
                    "region": row.get("iso_region", ""),  # e.g. US-MA, US-CA
                    "municipality": row.get("municipality", ""),
                    "scheduled_service": row.get("scheduled_service", "no") == "yes",
                }
    return out


@lru_cache(maxsize=1)
def _runways_by_ident() -> dict:
    out: dict = {}
    with open(_download("runways.csv"), encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                length = float(row["length_ft"] or 0)
            except ValueError:
                length = 0
            surface = row["surface"].upper()
            if row["closed"] == "1" or length < 3500 or not any(s in surface for s in PAVED):
                continue
            out.setdefault(row["airport_ident"], []).append(
                {"id": f"{row['le_ident']}/{row['he_ident']}", "length_ft": int(length)})
    return out


def runways(iata: str) -> list:
    ref = airport_ref().get(iata)
    return _runways_by_ident().get(ref["ident"], []) if ref else []


def haversine_mi(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(a))


FAA_ENPL_URL = ("https://www.faa.gov/airports/planning_capacity/passenger_allcargo_stats/passenger/"
                "arp-cy{y}-commercial-service-enplanements.xlsx")
T100_URL = ("https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/"
            "T100_Domestic_Market_and_Segment_Data/FeatureServer/1/query")
BTS_URL = ("https://www.transtats.bts.gov/PREZIP/"
           "On_Time_Reporting_Carrier_On_Time_Performance_1987_present_{y}_{m}.zip")
# Latest 12 published months (BTS lags ~2 months; Aug 2026 was not yet released on 2026-10-15).
# A full 12-month window avoids seasonality bias. Update when newer months are published.
BTS_WINDOW = [(2025, m) for m in range(8, 13)] + [(2026, m) for m in range(1, 8)]
BTS_LABEL = "Aug 2025-Jul 2026"
DIST_BIN = 250   # miles; long-haul thresholds are resolved to this granularity


@lru_cache(maxsize=1)
def faa_enplanements() -> dict:
    """LOCID -> {city, state, name, hub, enpl: {year: passengers}}.  FAA CY2025 + CY2024 workbooks (2023-2025)."""
    import io
    import warnings
    import openpyxl
    out: dict = {}
    for y in (2025, 2024):
        path = CACHE / f"faa_enplanements_{y}.xlsx"
        if not path.exists() or time.time() - path.stat().st_mtime > 30 * 86400:
            try:
                r = httpx.get(FAA_ENPL_URL.format(y=y), timeout=60, follow_redirects=True)
                r.raise_for_status()
                path.write_bytes(r.content)
            except Exception as e:
                if not path.exists():
                    raise DataUnavailable(f"Could not fetch FAA enplanements CY{y}: {e}") from e
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ws = openpyxl.load_workbook(io.BytesIO(path.read_bytes()), read_only=True).worksheets[0]
            rows = list(ws.iter_rows(values_only=True))
        hdr = next(i for i, r in enumerate(rows) if r and "Locid" in r)
        cols = {name: i for i, name in enumerate(rows[hdr]) if name}
        year_cols = {2000 + int(m.group(1)): i for name, i in cols.items()
                     if (m := re.match(r"CY (\d\d) Enplanements", str(name)))}
        for r in rows[hdr + 1:]:
            loc = r[cols["Locid"]] if r else None
            if not loc:
                continue
            e = out.setdefault(loc, {"city": r[cols["City"]], "state": r[cols["ST"]],
                                     "name": r[cols["Airport Name"]], "hub": r[cols["Hub"]], "enpl": {}})
            for yr, i in year_cols.items():
                if isinstance(r[i], (int, float)):
                    e["enpl"].setdefault(yr, int(r[i]))
    return out


@lru_cache(maxsize=1)
def t100_totals() -> dict:
    """IATA -> {departures, enplanements, freight_lb} for BTS T-100 (all domestic carriers incl. cargo), via ArcGIS REST API."""
    out, offset = {}, 0
    try:
        while True:
            r = httpx.get(T100_URL, timeout=60, params={
                "where": "1=1", "outFields": "year,origin,departures,enplanements,freight",
                "returnGeometry": "false", "f": "json", "resultOffset": offset, "resultRecordCount": 1000})
            r.raise_for_status()
            js = r.json()
            feats = js.get("features", [])
            for f in feats:
                a = f["attributes"]
                out[a["origin"]] = {"year": a["year"], "departures": a["departures"],
                                    "enplanements": a["enplanements"], "freight_lb": a["freight"]}
            if not feats or not js.get("exceededTransferLimit"):
                break
            offset += len(feats)
    except Exception as e:
        raise DataUnavailable(f"BTS T-100 API unavailable: {e}") from e
    return out


def _bts_month(year: int, month: int) -> dict:
    """Download one BTS On-Time month and aggregate departures per origin airport (cached as small JSON)."""
    import io
    import json
    import zipfile
    path = CACHE / f"bts_ontime_{year}_{month:02d}.json"
    if path.exists():
        return json.loads(path.read_text())
    zpath = CACHE / f"bts_{year}_{month}.zip"
    with httpx.stream("GET", BTS_URL.format(y=year, m=month), timeout=300, follow_redirects=True) as r:
        r.raise_for_status()
        with open(zpath, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    agg: dict = {}
    with zipfile.ZipFile(zpath) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        with z.open(name) as raw:
            for row in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline="")):
                a = agg.setdefault(row["Origin"], {"deps": 0, "cancelled": 0, "flown": 0, "delayed15": 0,
                                                   "taxi_sum": 0.0, "taxi_n": 0, "dist": {}})
                a["deps"] += 1
                if row["Cancelled"] in ("1", "1.00", "1.0"):
                    a["cancelled"] += 1
                    continue
                a["flown"] += 1
                if row["DepDel15"] in ("1", "1.00", "1.0"):
                    a["delayed15"] += 1
                if row["TaxiOut"]:
                    a["taxi_sum"] += float(row["TaxiOut"])
                    a["taxi_n"] += 1
                if row["Distance"]:
                    b = int(float(row["Distance"]) // DIST_BIN) * DIST_BIN
                    a["dist"][str(b)] = a["dist"].get(str(b), 0) + 1
    zpath.unlink(missing_ok=True)
    path.write_text(json.dumps(agg))
    return agg


def bts_cached_months() -> int:
    return sum((CACHE / f"bts_ontime_{y}_{m:02d}.json").exists() for y, m in BTS_WINDOW)


def build_bts_cache(progress=print):
    for y, m in BTS_WINDOW:
        progress(f"BTS On-Time {y}-{m:02d} ...")
        _bts_month(y, m)
    bts_ontime.cache_clear()


@lru_cache(maxsize=1)
def bts_ontime() -> dict:
    """IATA -> year-aggregated departure stats from whichever cached BTS months exist; {} if none."""
    import json
    total: dict = {}
    months = 0
    for y, m in BTS_WINDOW:
        p = CACHE / f"bts_ontime_{y}_{m:02d}.json"
        if not p.exists():
            continue
        months += 1
        for ap, a in json.loads(p.read_text()).items():
            t = total.setdefault(ap, {"deps": 0, "cancelled": 0, "flown": 0, "delayed15": 0,
                                      "taxi_sum": 0.0, "taxi_n": 0, "dist": {}})
            for k in ("deps", "cancelled", "flown", "delayed15", "taxi_sum", "taxi_n"):
                t[k] += a[k]
            for b, n in a["dist"].items():
                t["dist"][b] = t["dist"].get(b, 0) + n
    for t in total.values():
        t["months"] = months
    return total


_faa_cache: dict = {"t": 0, "data": {}}


def _minutes(text: str) -> int:
    h = re.search(r"(\d+)\s*hour", text or "")
    m = re.search(r"(\d+)\s*minute", text or "")
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)


def faa_status(ttl_s: int = 120) -> dict:
    """IATA -> {events: [...], primary_program, reason, avg_delay_min, severity_level}.

    Live snapshot of FAA NAS status. Preserves all events per airport and does NOT fabricate
    arbitrary numerical delay minutes for qualitative programs like Ground Stops.
    """
    if time.time() - _faa_cache["t"] < ttl_s:
        return _faa_cache["data"]
    try:
        r = httpx.get(FAA_URL, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.text)
    except Exception as e:
        raise DataUnavailable(f"FAA NAS status unavailable: {e}") from e

    # Collect list of events per airport to avoid overwriting multiple alerts
    events_by_arpt: dict = {}
    for dt in root.findall("Delay_type"):
        kind = dt.findtext("Name") or ""
        if "Closure" in kind:
            continue
        for node in dt.iter():
            arpt = node.findtext("ARPT")
            if not arpt:
                continue
            reason = (node.findtext("Reason") or "").strip()
            end_time = (node.findtext("End_Time") or "").strip()

            if "Ground Stop" in kind:
                events_by_arpt.setdefault(arpt, []).append({
                    "program": "Ground Stop",
                    "reason": reason,
                    "avg_delay_min": None,  # Ground stops halt departures at origin; delay duration is unquantified
                    "severity": "CRITICAL",
                    "end_time": end_time or None,
                })
            elif "Ground Delay" in kind:
                events_by_arpt.setdefault(arpt, []).append({
                    "program": "Ground Delay Program",
                    "reason": reason,
                    "avg_delay_min": _minutes(node.findtext("Avg")),
                    "max_delay_min": _minutes(node.findtext("Max")),
                    "severity": "HIGH",
                })
            elif "Arrival/Departure" in kind:
                lo = _minutes(node.findtext(".//Min"))
                hi = _minutes(node.findtext(".//Max"))
                avg = (lo + hi) // 2 if (lo or hi) else None
                events_by_arpt.setdefault(arpt, []).append({
                    "program": "Arrival/Departure Delay",
                    "reason": reason,
                    "avg_delay_min": avg,
                    "severity": "MODERATE",
                })

    # Consolidate per airport
    severity_order = {"CRITICAL": 3, "HIGH": 2, "MODERATE": 1}
    data: dict = {}
    for arpt, evts in events_by_arpt.items():
        # Pick primary event by highest operational severity
        primary = max(evts, key=lambda e: severity_order.get(e["severity"], 0))
        data[arpt] = {
            "program": primary["program"],
            "reason": primary["reason"],
            "avg_delay_min": primary["avg_delay_min"],
            "severity": primary["severity"],
            "all_events": evts,
            "source": "FAA NAS Status (live operational snapshot)",
        }

    _faa_cache.update(t=time.time(), data=data)
    return data
