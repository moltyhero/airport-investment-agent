"""Deterministic scoring. No LLM involvement: same inputs -> same outputs.

Universe = every US airport in the FAA commercial-service enplanement files above MIN_ENPLANEMENTS.
"""
import csv
import math
from functools import lru_cache
from pathlib import Path

from . import data_sources as ds

MIN_ENPLANEMENTS = 250_000  # drop tiny commercial-service airports; percentiles are noisy there
BASE_YEAR = 2023            # growth is measured 2023 -> latest year, which avoids the 2020-21 pandemic baseline
LONG_HAUL_MI = 2000  # no official US definition; ~2,000-2,500 mi is common in EPA/EU/airline usage

DEFAULT_WEIGHTS = {"growth": 0.25, "pressure": 0.30, "scale": 0.20, "delay": 0.10, "feasibility": 0.15}

REGIONS = {
    "new england": ["CT", "MA", "ME", "NH", "RI", "VT"],
    "northeast": ["CT", "MA", "ME", "NH", "RI", "VT", "NY", "NJ", "PA"],
    "california": ["CA"],
    "alaska": ["AK"],
    "west coast": ["CA", "OR", "WA"],
    "southeast": ["FL", "GA", "NC", "TN", "SC", "AL"],
    "midwest": ["IL", "MI", "MN", "OH", "IN", "WI", "MO"],
    "southwest": ["TX", "AZ", "NM", "NV"],
    "mountain west": ["CO", "UT", "NV", "ID", "MT", "WY"],
    "mid-atlantic": ["NY", "NJ", "PA", "MD", "DC", "VA"],
}
# Nicknames only; city names are resolved automatically from the FAA data (see aliases()).
ALIASES = {"LA": "LAX", "SANTA ANA": "SNA", "ORANGE COUNTY": "SNA", "JOHN WAYNE": "SNA", "LOGAN": "BOS",
           "NYC": "JFK", "NEW YORK": "JFK", "PORTLAND ME": "PWM", "PORTLAND MAINE": "PWM",
           "WASHINGTON": "DCA", "DC": "DCA", "DALLAS": "DFW", "HOUSTON": "IAH", "CHICAGO": "ORD"}


@lru_cache(maxsize=1)
def constraints() -> dict:
    """Hand-curated structural constraints (curfews, caps, slots) for a few well-known airports only."""
    with open(Path(__file__).with_name("constraints.csv"), encoding="utf-8", newline="") as f:
        return {r["iata"]: {"constraint": float(r["constraint"]), "note": r["constraint_note"]}
                for r in csv.DictReader(f)}


@lru_cache(maxsize=1)
def universe() -> dict:
    t100 = _safe(ds.t100_totals) or {}
    out = {}
    for iata, a in ds.faa_enplanements().items():
        yrs = sorted(a["enpl"])
        if not yrs:
            continue
        latest = yrs[-1]
        if latest <= BASE_YEAR or BASE_YEAR not in a["enpl"] or a["enpl"][latest] < MIN_ENPLANEMENTS:
            continue
        t = t100.get(iata)
        out[iata] = {
            "iata": iata, "city": a["city"], "state": a["state"], "name": a["name"],
            "year": latest, "enpl": a["enpl"][latest], "enpl_base": a["enpl"][BASE_YEAR],
            "t100_departures": t["departures"] if t else None,
            "freight_lb": t["freight_lb"] if t else None,
        }
    return out


def aliases() -> dict:
    by_city: dict = {}
    for k, a in universe().items():
        by_city.setdefault(a["city"].upper(), []).append(k)
    out = {c: v[0] for c, v in by_city.items() if len(v) == 1}
    out.update(ALIASES)
    return out


def resolve(text: str):
    """Map a name/IATA code to an IATA code in the universe, else None."""
    t = text.strip().upper()
    if t in universe():
        return t
    return aliases().get(t)


def _pct(values: dict) -> dict:
    """Percentile rank (0-100) within the peer universe."""
    items = sorted(values.values())
    n = len(items)
    return {k: 100 * (sum(1 for x in items if x < v) + 0.5 * sum(1 for x in items if x == v)) / n
            for k, v in values.items()}


def _wmean(pairs):
    """Weighted mean over (value, weight) pairs, skipping None values (weights renormalised)."""
    pairs = [(v, w) for v, w in pairs if v is not None]
    tw = sum(w for _, w in pairs)
    return (sum(v * w for v, w in pairs) / tw) if tw else None


def _safe(fn):
    try:
        return fn()
    except ds.DataUnavailable:
        return None


def _hist(iata: str):
    """Historical delay stats from BTS On-Time (departures of large reporting carriers), or None."""
    b = ds.bts_ontime().get(iata)
    if not b or b["flown"] < 1000:
        return None
    return {"delay15_pct": 100 * b["delayed15"] / b["flown"], "cancel_pct": 100 * b["cancelled"] / b["deps"],
            "taxi_out_min": b["taxi_sum"] / b["taxi_n"] if b["taxi_n"] else None, "months": b["months"],
            "deps": b["deps"]}


@lru_cache(maxsize=1)
def _metrics() -> dict:
    """Metrics and peer percentiles for every airport in the universe."""
    u = universe()
    refs = _safe(ds.airport_ref) or {}
    m = {}
    for k, a in u.items():
        n_rwy = len(ds.runways(k)) if refs else 0
        warn = None
        if n_rwy == 0:
            n_rwy = 1
            warn = "Runway count unavailable from OurAirports; assumed 1 (pressure may be overstated)."
        ops = 2 * a["t100_departures"] if a["t100_departures"] else None  # departures + arrivals
        m[k] = {
            "runways": n_rwy, "warning": warn, "hist": _hist(k),
            "cagr_pct": ((a["enpl"] / a["enpl_base"]) ** (1 / (a["year"] - BASE_YEAR)) - 1) * 100,
            "ops_est": ops, "ops_per_runway": ops / n_rwy if ops else None,
            "enpl_per_runway_m": a["enpl"] / 1e6 / n_rwy,
        }
    g = _pct({k: v["cagr_pct"] for k, v in m.items()})
    e = _pct({k: v["enpl_per_runway_m"] for k, v in m.items()})
    o = _pct({k: v["ops_per_runway"] for k, v in m.items() if v["ops_per_runway"]})
    s = _pct({k: math.log(u[k]["enpl"]) for k in m})
    with_hist = {k: v["hist"] for k, v in m.items() if v["hist"]}
    d15 = _pct({k: h["delay15_pct"] for k, h in with_hist.items()}) if with_hist else {}
    tx = _pct({k: h["taxi_out_min"] for k, h in with_hist.items() if h["taxi_out_min"]}) if with_hist else {}
    cx = _pct({k: h["cancel_pct"] for k, h in with_hist.items()}) if with_hist else {}
    cons = constraints()
    for k in m:
        m[k]["growth"] = g[k]
        m[k]["pressure"] = _wmean([(o.get(k), 0.6), (e[k], 0.4)])
        m[k]["scale"] = s[k]
        m[k]["delay"] = _wmean([(d15.get(k), 0.5), (tx.get(k), 0.3), (cx.get(k), 0.2)]) if k in with_hist else None
        m[k]["feasibility"] = 100 * (1 - cons[k]["constraint"]) if k in cons else None
    return m


def _live() -> tuple:
    try:
        return ds.faa_status(), True
    except ds.DataUnavailable:
        return {}, False


def score(iata: str, weights=None, live=None) -> dict:
    """Full score record for one airport. Live FAA status is attached for display but NOT part of any index."""
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in w})
    status_map, live_ok = live if live is not None else _live()
    a, m = universe()[iata], _metrics()[iata]
    cons = constraints().get(iata)
    comp = {k: m[k] for k in ("growth", "pressure", "scale", "delay", "feasibility")}
    used = {k: w[k] for k in w if comp[k] is not None}
    opportunity = _wmean([(comp[k], w[k]) for k in w])
    congestion = _wmean([(m["pressure"], 0.5), (m["delay"], 0.5)])
    unmet = _wmean([(m["pressure"], 0.40), (cons["constraint"] * 100 if cons else None, 0.30),
                    (m["growth"], 0.15), (m["delay"], 0.15)])
    h = m["hist"]
    tw = sum(used.values()) or 1
    missing = [k for k in w if comp[k] is None]
    return {
        "iata": iata, "city": a["city"], "state": a["state"], "airport": a["name"],
        "opportunity_score": round(opportunity, 1),
        "congestion_index": round(congestion, 1),
        "unmet_demand_index": round(unmet, 1),
        "components": {k: (round(v, 1) if v is not None else None) for k, v in comp.items()},
        "weights_used": {k: round(v / tw, 3) for k, v in used.items()},
        "missing_components": missing,
        "raw": {
            "year": a["year"], "enplanements_m": round(a["enpl"] / 1e6, 2),
            f"enplanements_{BASE_YEAR}_m": round(a["enpl_base"] / 1e6, 2),
            "growth_cagr_pct": round(m["cagr_pct"], 2),
            "t100_departures_2024": a["t100_departures"],
            "ops_est": m["ops_est"], "runways": m["runways"],
            "ops_per_runway": int(m["ops_per_runway"]) if m["ops_per_runway"] else None,
            "enplanements_per_runway_m": round(m["enpl_per_runway_m"], 2),
            "delayed_15min_pct": round(h["delay15_pct"], 1) if h else None,
            "cancelled_pct": round(h["cancel_pct"], 2) if h else None,
            "taxi_out_min": round(h["taxi_out_min"], 1) if h and h["taxi_out_min"] else None,
        },
        "constraint": cons["constraint"] if cons else None,
        "constraint_note": cons["note"] if cons else None,
        "live_status": status_map.get(iata), "live_feed_ok": live_ok, "warning": m["warning"],
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
    s["data_availability_verdict"] = (
        "Unmet flight demand is NOT directly measurable from any public API: BTS and FAA record only realized "
        "flights and ticketed passengers, never rejected schedules or travelers who did not fly. "
        "The index below is an analytical proxy built from observed pressure and documented constraints."
    )
    if s["constraint"] and s["constraint"] >= 0.4:
        reasons.append(f"Documented structural constraint (curated, not API data): {s['constraint_note']}.")
    elif s["constraint"] is None:
        reasons.append("No curated constraint data for this airport, so structural limits are unknown (not assumed absent).")
    if c["pressure"] is not None and c["pressure"] >= 50:
        reasons.append(f"High airside utilization vs peers: {r['ops_per_runway']:,} est. operations per runway "
                       f"across {r['runways']} runway(s)." if r["ops_per_runway"] else
                       f"High passenger throughput per runway ({r['enplanements_per_runway_m']}M enplanements/runway).")
    if c["delay"] is not None and c["delay"] >= 50:
        reasons.append(f"Persistent delays ({ds.BTS_LABEL}): {r['delayed_15min_pct']}% of departures 15+ min late, "
                       f"{r['taxi_out_min']} min average taxi-out (peer percentile {c['delay']}).")
    if c["growth"] >= 60:
        reasons.append(f"Passenger demand is growing fast ({r['growth_cagr_pct']}%/yr since {BASE_YEAR}), "
                       "so pressure on current capacity is rising.")
    ls = s["live_status"]
    if ls:
        reasons.append(f"Live FAA status right now: {ls['program']} ({ls['reason']}). Snapshot only; not in the index.")
    if not reasons:
        reasons.append("No strong capacity-pressure, delay or constraint signal found in the available data.")
    s["unmet_level"] = bucket(s["unmet_demand_index"])
    s["reasons"] = reasons
    return s


def long_haul_share(iata: str, threshold_mi: float = LONG_HAUL_MI) -> dict:
    """Share of actual scheduled departures (BTS On-Time, full year) with distance >= threshold."""
    b = ds.bts_ontime().get(iata)
    if not b:
        n = ds.bts_cached_months()
        if not n:
            raise ds.DataUnavailable("BTS On-Time data is not cached yet - run `python -m airport_agent.build_cache` "
                                     "once (about 10 minutes).")
        raise ds.DataUnavailable(f"{iata} has no flights in the cached BTS On-Time data.")
    bins = {int(k): v for k, v in b["dist"].items()}
    cut = int(threshold_mi // ds.DIST_BIN) * ds.DIST_BIN
    total = sum(bins.values())
    lh = sum(v for k, v in bins.items() if k >= cut)
    t100 = _safe(ds.t100_totals) or {}
    caveats = [
        f"Based on {b['months']} month(s) of BTS On-Time data ({ds.BTS_LABEL}): domestic flights of large US reporting "
        "carriers only. International, cargo-only, charter and small-carrier flights are NOT included.",
        f"'Long haul' = scheduled distance >= {cut} mi (threshold rounded to {ds.DIST_BIN}-mi bins; no single official definition; common usage is ~2,000-2,500 mi, and the share is sensitive to it).",
    ]
    if b["months"] < 12:
        caveats.append(f"Only {b['months']}/12 months are cached, so seasonality may bias the share.")
    if iata in t100 and t100[iata]["freight_lb"] > 1e9:
        caveats.append(f"{iata} handles {t100[iata]['freight_lb'] / 1e9:.1f}B lb of freight (T-100); cargo flights are "
                       "excluded here, so the passenger long-haul share can differ from the airport's total operations.")
    return {"iata": iata, "threshold_mi": cut, "pct_long_haul_flights": round(100 * lh / total, 1),
            "flights_counted": total, "long_haul_flights": lh, "months": b["months"], "caveats": caveats}
