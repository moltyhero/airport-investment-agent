"""Tools exposed to the agent. Every number returned comes from deterministic code + public data."""
from . import data_sources as ds
from . import scoring as sc

SCOPE = (f"Universe = all US commercial-service airports with >= {sc.MIN_ENPLANEMENTS:,} enplanements in the latest FAA "
         f"file. Data: FAA enplanements (2023-2025), BTS T-100 API (2024), BTS On-Time ({ds.BTS_LABEL}), OurAirports runways, "
         "FAA NAS live status. Structural constraints are curated for only 13 well-known airports.")


def _codes(args: dict) -> list:
    codes = []
    for c in args.get("airports", []) or []:
        r = sc.resolve(c)
        if not r:
            raise ValueError(f"Unknown airport '{c}'. Use a 3-letter IATA code or city name; only US commercial "
                             f"airports above the size cutoff are covered.")
        codes.append(r)
    return codes


def rank_airports(region: str = None, airports: list = None, top_n: int = 5, weights: dict = None):
    codes = _codes({"airports": airports})
    if region:
        states = sc.REGIONS.get(region.lower().strip())
        if not states:
            if len(region) == 2:
                states = [region.upper()]
            else:
                raise ValueError(f"Unknown region '{region}'. Known: {', '.join(sc.REGIONS)} or a 2-letter state.")
        codes = [k for k, a in sc.universe().items() if a["state"] in states and (not codes or k in codes)]
    rows = sc.rank(codes or None, weights, top_n)
    return {"ranking": rows, "metric": "opportunity_score (0-100)", "scope": SCOPE,
            "note": "Scores are percentiles relative to the whole US peer set, not absolute. Terminal expansion also "
                    "depends on terminal/gate utilisation, which is NOT in the public data used here."}


def compare_airports(airports: list):
    codes = _codes({"airports": airports})
    live = sc._live()
    rows = [sc.score(c, None, live) for c in codes]
    return {"comparison": rows, "scope": SCOPE,
            "note": "congestion_index = mean of capacity-pressure percentile and historical delay percentile "
                    "(BTS On-Time). Live FAA status is shown separately and is not part of the index."}


def long_haul_share(airport: str, threshold_miles: float = sc.LONG_HAUL_MI):
    code = sc.resolve(airport) or airport.upper()
    try:
        return sc.long_haul_share(code, threshold_miles)
    except ds.DataUnavailable as e:
        return {"error": str(e)}


def unmet_demand(airport: str):
    code = sc.resolve(airport)
    if not code:
        raise ValueError(f"Unknown airport '{airport}'.")
    out = sc.explain_unmet(code)
    out["note"] = ("Unmet demand cannot be observed directly in public data. This is a PROXY index combining capacity "
                   "pressure, curated structural constraints, growth, and historical delays.")
    return out


def live_status(airport: str):
    code = sc.resolve(airport) or airport.upper()
    try:
        st = ds.faa_status()
    except ds.DataUnavailable as e:
        return {"error": str(e)}
    return {"iata": code, "status": st.get(code) or "No active FAA delay program", "source": "FAA NAS Status (live)"}


def methodology():
    return {"weights": sc.DEFAULT_WEIGHTS,
            "components": {
                "growth": f"Percentile of {sc.BASE_YEAR}->latest-year enplanement CAGR (FAA)",
                "pressure": "0.6*percentile(est. ops per runway; 2x T-100 departures) + 0.4*percentile(enplanements per runway)",
                "scale": "Percentile of log(latest enplanements) - revenue base",
                "delay": "Rolling 12-month BTS On-Time: 0.5*pct(share of departures 15+ min late) + 0.3*pct(taxi-out) + 0.2*pct(cancel rate); "
                         "missing if the BTS cache is not built",
                "feasibility": "100*(1 - structural constraint score); curated for 13 airports only. Others are left out "
                               "of this component (weights renormalised), not assumed unconstrained"},
            "scope": SCOPE}


FUNCS = {"rank_airports": rank_airports, "compare_airports": compare_airports, "long_haul_share": long_haul_share,
         "unmet_demand": unmet_demand, "live_status": live_status, "methodology": methodology}


def _fn(name, desc, props, req=()):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": list(req)}}}


SCHEMAS = [
    _fn("rank_airports", "Rank US airports by terminal-expansion investment opportunity score. Filter by region "
        "(e.g. 'new england', 'california') or an explicit airports list.",
        {"region": {"type": "string"}, "airports": {"type": "array", "items": {"type": "string"}},
         "top_n": {"type": "integer"},
         "weights": {"type": "object", "description": "optional overrides for growth,pressure,scale,delay,feasibility"}}),
    _fn("compare_airports", "Compare airports on congestion, opportunity and unmet demand.",
        {"airports": {"type": "array", "items": {"type": "string"}}}, ["airports"]),
    _fn("long_haul_share", "Percent of an airport's actual departures that are long haul (BTS, rolling 12 months). "
        f"Default threshold is {sc.LONG_HAUL_MI:.0f} miles. Omit threshold_miles unless the user explicitly asks for a "
        "different distance. Always state the threshold used and the caveats from the result.",
        {"airport": {"type": "string"},
         "threshold_miles": {"type": "number", "description": "Only set if the user specifies a distance."}}, ["airport"]),
    _fn("unmet_demand", "Proxy index and reasons for unmet flight demand at an airport.",
        {"airport": {"type": "string"}}, ["airport"]),
    _fn("live_status", "Current FAA delay/ground-stop status for an airport.", {"airport": {"type": "string"}}, ["airport"]),
    _fn("methodology", "Return the scoring methodology and weights.", {}),
]


def call(name: str, args: dict):
    try:
        return FUNCS[name](**args)
    except (ValueError, TypeError, ds.DataUnavailable) as e:
        return {"error": str(e)}
