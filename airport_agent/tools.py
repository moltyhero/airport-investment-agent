"""Tools exposed to the agent. Every number returned comes from deterministic code + public data."""
from . import data_sources as ds
from . import scoring as sc

SCOPE = ("Universe = 41 curated US commercial airports (NE + major hubs + CA/AK for the brief). "
         "Traffic volumes are approximate FAA/BTS-derived seed values; verify before investment decisions.")


def _codes(args: dict) -> list:
    codes = []
    for c in args.get("airports", []) or []:
        r = sc.resolve(c)
        if not r:
            raise ValueError(f"Unknown airport '{c}'. Supported: {', '.join(sorted(sc.universe()))}")
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
            "note": "Scores are relative to the 41-airport peer set, not absolute. Terminal expansion also depends "
                    "on terminal/gate utilisation, which is NOT in public data used here."}


def compare_airports(airports: list):
    codes = _codes({"airports": airports})
    live = sc._live()
    rows = [sc.score(c, None, live) for c in codes]
    return {"comparison": rows, "scope": SCOPE,
            "note": "congestion_index = 0.8*capacity pressure percentile + 0.2*live FAA delay score (live part dropped if FAA feed down)."}


def long_haul_share(airport: str, threshold_miles: float = 1500):
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
                   "pressure, structural constraints, growth, and live delays.")
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
                "growth": "Percentile of 2019->2023 enplanement CAGR",
                "pressure": "0.6*percentile(ops per runway) + 0.4*percentile(enplanements per runway)",
                "scale": "Percentile of log(2023 enplanements) - revenue base",
                "delay": "Live FAA delay/ground-delay score (snapshot, noisy)",
                "feasibility": "100*(1 - structural constraint score); curfews, caps, slot controls lower it"},
            "scope": SCOPE}


FUNCS = {"rank_airports": rank_airports, "compare_airports": compare_airports, "long_haul_share": long_haul_share,
         "unmet_demand": unmet_demand, "live_status": live_status, "methodology": methodology}


def _fn(name, desc, props, req=()):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": list(req)}}}


SCHEMAS = [
    _fn("rank_airports", "Rank airports by terminal-expansion investment opportunity score. Filter by region "
        "(e.g. 'new england', 'california') or an explicit airports list.",
        {"region": {"type": "string"}, "airports": {"type": "array", "items": {"type": "string"}},
         "top_n": {"type": "integer"},
         "weights": {"type": "object", "description": "optional overrides for growth,pressure,scale,delay,feasibility"}}),
    _fn("compare_airports", "Compare airports on congestion, opportunity and unmet demand.",
        {"airports": {"type": "array", "items": {"type": "string"}}}, ["airports"]),
    _fn("long_haul_share", "Percent of an airport's routes that are long haul (distance threshold).",
        {"airport": {"type": "string"}, "threshold_miles": {"type": "number"}}, ["airport"]),
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
