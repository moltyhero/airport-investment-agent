"""Public data sources: OurAirports (coords, runways), FAA NAS Status (live delays), OpenFlights (routes)."""
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
    "routes.dat": "https://raw.githubusercontent.com/jpatokal/openflights/master/data/routes.dat",
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


@lru_cache(maxsize=1)
def _routes() -> dict:
    """IATA -> list of (airline, dest, stops). Filters to non-stop routes where possible."""
    out: dict = {}
    with open(_download("routes.dat"), encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            if len(row) >= 8:
                origin = row[2].strip()
                dest = row[4].strip()
                airline = row[0].strip()
                stops_str = row[7].strip()
                try:
                    stops = int(stops_str)
                except ValueError:
                    stops = 0
                out.setdefault(origin, []).append((airline, dest, stops))
            elif len(row) >= 5:
                out.setdefault(row[2].strip(), []).append((row[0].strip(), row[4].strip(), 0))
    return out


def routes_from(iata: str, direct_only: bool = True) -> list:
    """[(airline, dest_iata)] for scheduled routes out of an airport (OpenFlights, ~2014 snapshot)."""
    raw = _routes().get(iata, [])
    if direct_only:
        return [(airline, dest) for (airline, dest, stops) in raw if stops == 0]
    return [(airline, dest) for (airline, dest, stops) in raw]


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
