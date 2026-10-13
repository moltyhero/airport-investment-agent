"""Deterministic scoring. No LLM involvement: same inputs -> same outputs."""
import csv
import math
from functools import lru_cache
from pathlib import Path

from . import data_sources as ds

DEFAULT_WEIGHTS = {"growth": 0.25, "pressure": 0.30, "scale": 0.20, "delay": 0.10, "feasibility": 0.15}

REGIONS = {
    "new england": ["CT", "MA", "ME", "NH", "RI", "VT"],
    "northeast": ["CT", "MA", "ME", "NH", "RI", "VT", "NY", "NJ", "PA"],
    "california": ["CA"],
    "west coast": ["CA", "OR", "WA"],
    "southeast": ["FL", "GA", "NC", "TN", "SC", "AL"],
    "midwest": ["IL", "MI", "MN", "OH", "IN", "WI", "MO"],
    "southwest": ["TX", "AZ", "NM", "NV"],
    "mountain west": ["CO", "UT", "NV", "ID", "MT", "WY"],
    "mid-atlantic": ["NY", "NJ", "PA", "MD", "DC", "VA"],
}
ALIASES = {"LA": "LAX", "LOS ANGELES": "LAX", "SANTA ANA": "SNA", "ORANGE COUNTY": "SNA", "JOHN WAYNE": "SNA",
           "SAN FRANCISCO": "SFO", "ANCHORAGE": "ANC", "BOSTON": "BOS", "LOGAN": "BOS", "HARTFORD": "BDL",
           "PROVIDENCE": "PVD", "MANCHESTER": "MHT", "PORTLAND ME": "PWM", "BURLINGTON": "BTV", "BANGOR": "BGR",
           "NEW YORK": "JFK", "NYC": "JFK", "CHICAGO": "ORD", "LAS VEGAS": "LAS", "SAN DIEGO": "SAN"}


@lru_cache(maxsize=1)
def universe() -> dict:
    path = Path(__file__).with_name("seed_airports.csv")
    out = {}
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            out[r["iata"]] = {
                "iata": r["iata"], "city": r["city"], "state": r["state"],
                "enpl_2019_m": float(r["enpl_2019_m"]), "enpl_2023_m": float(r["enpl_2023_m"]),
                "ops_2023": float(r["ops_2023_k"]) * 1000,
                "constraint": float(r["constraint"]), "constraint_note": r["constraint_note"],
            }
    return out


def resolve(text: str):
    """Map a name/IATA code to an IATA code in the universe, else None."""
    t = text.strip().upper()
    if t in universe():
        return t
    return ALIASES.get(t)


def _pct(values: dict) -> dict:
    """Percentile rank (0-100) within the peer universe."""
    items = sorted(values.values())
    n = len(items)
    return {k: 100 * (sum(1 for x in items if x < v) + 0.5 * sum(1 for x in items if x == v)) / n
            for k, v in values.items()}


def _delay_score(status) -> float:
    if not status:
        return 0.0
    # Score based on reported operational disruption severity rather than inventing fake delay minutes
    sev = status.get("severity", "MODERATE")
    if sev == "CRITICAL":  # Ground Stop (halting operations)
        return 90.0
    if sev == "HIGH":  # Ground Delay Program (formal traffic management program)
        avg = status.get("avg_delay_min")
        return max(70.0, min(100.0, (avg / 90 * 100) if avg else 75.0))
    # Arrival/Departure delays
    avg = status.get("avg_delay_min")
    return max(30.0, min(65.0, (avg / 60 * 50) if avg else 35.0))


@lru_cache(maxsize=1)
def _metrics() -> dict:
    """Static (non-live) metrics and peer percentiles for every airport."""
    u = universe()
    m = {}
    warnings = {}
    for k, a in u.items():
        n_rwy = len(ds.runways(k)) if _safe(lambda: ds.airport_ref()) else 0
        if n_rwy == 0:
            n_rwy = 1
            warnings[k] = "Runway count unavailable from OurAirports; assumed 1 (congestion may be overstated)."
        m[k] = {
            "runways": n_rwy,
            "cagr_pct": ((a["enpl_2023_m"] / a["enpl_2019_m"]) ** 0.25 - 1) * 100,
            "ops_per_runway": a["ops_2023"] / n_rwy,
            "enpl_per_runway_m": a["enpl_2023_m"] / n_rwy,
        }
    g = _pct({k: v["cagr_pct"] for k, v in m.items()})
    o = _pct({k: v["ops_per_runway"] for k, v in m.items()})
    e = _pct({k: v["enpl_per_runway_m"] for k, v in m.items()})
    s = _pct({k: math.log(u[k]["enpl_2023_m"]) for k in m})
    for k in m:
        m[k]["growth"] = g[k]
        m[k]["pressure"] = 0.6 * o[k] + 0.4 * e[k]
        m[k]["scale"] = s[k]
        m[k]["feasibility"] = 100 * (1 - u[k]["constraint"])
        m[k]["warning"] = warnings.get(k)
    return m


def _safe(fn):
    try:
        return fn()
    except ds.DataUnavailable:
        return None


def _live() -> tuple:
    try:
        return ds.faa_status(), True
    except ds.DataUnavailable:
        return {}, False


def score(iata: str, weights=None, live=None) -> dict:
    """Full score record for one airport."""
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in w})
    status_map, live_ok = live if live is not None else _live()
    a, m = universe()[iata], _metrics()[iata]
    status = status_map.get(iata)
    comp = {k: m[k] for k in ("growth", "pressure", "scale", "feasibility")}
    comp["delay"] = _delay_score(status)
    used = {k: v for k, v in w.items() if live_ok or k != "delay"}  # drop live weight if feed is down
    total_w = sum(used.values()) or 1
    opportunity = sum(comp[k] * used[k] for k in used) / total_w
    congestion = 0.8 * m["pressure"] + 0.2 * comp["delay"] if live_ok else m["pressure"]
    unmet = (0.40 * m["pressure"] + 0.30 * a["constraint"] * 100 + 0.15 * m["growth"]
             + 0.15 * comp["delay"]) if live_ok else \
            (0.47 * m["pressure"] + 0.35 * a["constraint"] * 100 + 0.18 * m["growth"])
    return {
        "iata": iata, "city": a["city"], "state": a["state"],
        "opportunity_score": round(opportunity, 1),
        "congestion_index": round(congestion, 1),
        "unmet_demand_index": round(unmet, 1),
        "components": {k: round(v, 1) for k, v in comp.items()},
        "weights_used": {k: round(v / total_w, 3) for k, v in used.items()},
        "raw": {
            "enplanements_2023_m": a["enpl_2023_m"], "enplanements_2019_m": a["enpl_2019_m"],
            "recovery_vs_2019_pct": round(100 * a["enpl_2023_m"] / a["enpl_2019_m"], 1),
            "cagr_2019_2023_pct": round(m["cagr_pct"], 2),
            "operations_2023": int(a["ops_2023"]), "runways": m["runways"],
            "ops_per_runway": int(m["ops_per_runway"]),
            "enplanements_per_runway_m": round(m["enpl_per_runway_m"], 2),
        },
        "constraint": a["constraint"], "constraint_note": a["constraint_note"] or None,
        "live_status": status, "live_feed_ok": live_ok, "warning": m["warning"],
    }


def rank(codes=None, weights=None, top_n=10) -> list:
    live = _live()
    codes = codes or list(universe())
    rows = [score(c, weights, live) for c in codes]
    rows.sort(key=lambda r: r["opportunity_score"], reverse=True)
    return rows[:top_n]


def bucket(x: float) -> str:
    return "high" if x >= 66 else "moderate" if x >= 40 else "low"


def explain_unmet(iata: str) -> dict:
    s = score(iata)
    r, c = s["raw"], s["components"]
    reasons = []
    
    # Core domain reality: Unmet demand is unobservable in direct counts
    s["data_availability_verdict"] = (
        "Unmet flight demand is NOT directly measurable from any public API because aviation databases "
        "(BTS, FAA) record only realized flights and ticketed passengers, never rejected schedules or unaccommodated travelers. "
        "The metrics below represent an analytical proxy based on observed operational bottlenecks."
    )
    
    if s["constraint"] >= 0.4:
        reasons.append(f"Physical/Regulatory supply bottlenecks: {s['constraint_note']}.")
    if c["pressure"] >= 50:
        reasons.append(f"High airside utilization: {r['ops_per_runway']:,} operations/runway "
                       f"across {r['runways']} runway(s), leaving negligible buffer during peak scheduling banks.")
    if r["recovery_vs_2019_pct"] < 90:
        reasons.append(f"Traffic recovery context: Throughput is currently {100 - r['recovery_vs_2019_pct']:.0f}% below 2019 baseline, "
                       "meaning suppressed schedule volume is partly driven by post-pandemic business/transpacific route mix, "
                       "not purely terminal/runway exhaustion.")
    ls = s["live_status"]
    if ls:
        dur_str = f", ~{ls['avg_delay_min']} min" if ls.get("avg_delay_min") else ""
        reasons.append(f"Live operational friction: FAA currently reports active {ls['program']} ({ls['reason']}{dur_str}) "
                       "- demonstrating how weather/construction immediately triggers demand rationing.")
    if not reasons:
        reasons.append("Airport exhibits moderate capacity buffer without prominent physical supply constraints.")
        
    s["unmet_level"] = bucket(s["unmet_demand_index"])
    s["reasons"] = reasons
    return s


def long_haul_share(iata: str, threshold_mi: float = 1500) -> dict:
    ref = ds.airport_ref()
    if iata not in ref:
        raise ds.DataUnavailable(f"{iata} not found in OurAirports")
    src = ref[iata]
    routes = ds.routes_from(iata)
    if not routes:
        raise ds.DataUnavailable(f"No route data for {iata}")
    dists = {}
    for airline, dest in routes:
        if dest in ref:
            dists.setdefault(dest, []).append(ds.haversine_mi(src["lat"], src["lon"], ref[dest]["lat"], ref[dest]["lon"]))
    dest_d = {d: v[0] for d, v in dists.items()}
    n_route = sum(len(v) for v in dists.values())
    lh_route = sum(len(v) for d, v in dists.items() if v[0] >= threshold_mi)
    lh_dest = sorted((d for d, x in dest_d.items() if x >= threshold_mi), key=lambda d: -dest_d[d])
    return {
        "iata": iata, "threshold_mi": threshold_mi,
        "pct_long_haul_routes": round(100 * lh_route / n_route, 1),
        "pct_long_haul_destinations": round(100 * len(lh_dest) / len(dest_d), 1),
        "airline_routes": n_route, "destinations": len(dest_d),
        "long_haul_destinations": lh_dest[:15],
        "caveats": [
            "Counts distinct airline-destination routes, NOT flight frequencies or passengers; a daily "
            "ANC-SEA flight and a weekly bush route weigh the same.",
            "OpenFlights route data is a community-maintained snapshot (~2014); recent route changes are missing.",
            "Passenger routes only; cargo flights (a large share of Anchorage traffic) are excluded.",
            f"'Long haul' = great-circle distance >= {threshold_mi:.0f} mi (adjustable; no single official definition).",
        ],
    }
