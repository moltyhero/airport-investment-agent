"""Conversational agent. LLM tool-calling when an API key is set; deterministic router otherwise."""
import json
import os
import re

from pathlib import Path

from . import scoring as sc
from . import tools

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

SYSTEM = """You are an investment-research analyst assistant for a firm investing in US airport modernization.
Rules:
- ALWAYS get numbers via the tools; never invent traffic, delay or route figures.
- Explain reasoning: cite the score components and raw metrics behind any ranking.
- Always state assumptions, data freshness, and uncertainty. The universe is a curated list of 41 US airports;
  traffic values are approximate seed data; 'unmet demand' is a proxy; long-haul share is by route count, not flights.
- If a tool returns an error or the question is out of scope, say so plainly rather than guessing.
- Be concise (use short tables/bullets). Support follow-ups using the conversation context."""


class Agent:
    def __init__(self):
        self.history = []   # LLM message history
        self.last_codes = []  # for follow-ups in offline mode
        self.client = None
        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL")
        if api_key:
            from openai import OpenAI
            self.client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
        self.model = os.getenv("OPENAI_MODEL", "gpt-6-luna")

    @property
    def mode(self):
        return f"llm:{self.model}" if self.client else "offline-router"

    def chat(self, text: str) -> dict:
        return self._llm(text) if self.client else self._offline(text)

    # ---------- LLM path ----------
    def _llm(self, text):
        self.history.append({"role": "user", "content": text})
        used = []
        for _ in range(6):
            resp = self.client.chat.completions.create(
                model=self.model, tools=tools.SCHEMAS,
                messages=[{"role": "system", "content": SYSTEM}] + self.history[-30:])
            msg = resp.choices[0].message
            self.history.append(msg.model_dump(exclude_none=True))
            if not msg.tool_calls:
                return {"reply": msg.content, "tools": used, "mode": self.mode}
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                used.append({"name": tc.function.name, "args": args})
                self.history.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": json.dumps(tools.call(tc.function.name, args), default=str)})
        return {"reply": "I couldn't finish that within the tool-call budget.", "tools": used, "mode": self.mode}

    # ---------- offline path ----------
    def _find_airports(self, text):
        found, up = [], text.upper()
        for alias, code in sorted(sc.ALIASES.items(), key=lambda x: -len(x[0])):
            if re.search(rf"\b{re.escape(alias)}\b", up) and code not in found:
                found.append(code)
        for tok in re.findall(r"\b[A-Z]{3}\b", text):
            if tok in sc.universe() and tok not in found:
                found.append(tok)
        return found

    def _offline(self, text):
        t = text.lower()
        codes = self._find_airports(text)
        region = next((r for r in sc.REGIONS if r in t), None)
        used = []

        def run(name, **args):
            used.append({"name": name, "args": args})
            return tools.call(name, args)

        if re.search(r"long[- ]?haul", t):
            code = (codes or self.last_codes or [None])[0]
            if not code:
                return self._out("Which airport? e.g. 'long haul share out of ANC'.", used)
            self.last_codes = [code]
            return self._out(fmt_longhaul(run("long_haul_share", airport=code)), used)
        if "unmet" in t:
            code = (codes or self.last_codes or [None])[0]
            if not code:
                return self._out("Which airport should I analyse for unmet demand?", used)
            self.last_codes = [code]
            return self._out(fmt_unmet(run("unmet_demand", airport=code)), used)
        if re.search(r"compar|congest|versus|\bvs\b", t):
            cs = codes if len(codes) >= 2 else (codes + self.last_codes)[:2] if codes else self.last_codes
            if len(cs) < 2:
                return self._out("Name at least two airports to compare.", used)
            self.last_codes = cs
            return self._out(fmt_compare(run("compare_airports", airports=cs)), used)
        if re.search(r"method|how .*(score|rank)|weights|assumption", t):
            return self._out(fmt_method(run("methodology")), used)
        if region or re.search(r"candidate|rank|best|top|expan|invest|promising", t):
            args = {"top_n": 5}
            if region:
                args["region"] = region
            elif codes:
                args["airports"] = codes
            res = run("rank_airports", **args)
            self.last_codes = [r["iata"] for r in res.get("ranking", [])][:5] or self.last_codes
            return self._out(fmt_rank(res, region), used)
        if codes:
            self.last_codes = codes
            return self._out(fmt_compare(run("compare_airports", airports=codes)), used)
        return self._out("I can rank airports by expansion opportunity (e.g. 'best candidates in New England'), "
                         "compare congestion, estimate long-haul share, or explain unmet demand. "
                         "(Offline mode: set OPENAI_API_KEY for free-form conversation.)", used)

    def _out(self, reply, used):
        return {"reply": reply, "tools": used, "mode": self.mode}


# ---------- formatters (offline mode) ----------
def _err(r):
    return f"⚠️ {r['error']}" if "error" in r else None


def fmt_rank(r, region):
    if _err(r):
        return _err(r)
    rows = r["ranking"]
    out = [f"**Top terminal-expansion candidates{' in ' + region.title() if region else ''}** (opportunity score 0-100)\n",
           "| # | Airport | Score | Growth | Pressure | Scale | Delay now | Feasibility |", "|--|--|--|--|--|--|--|--|"]
    for i, s in enumerate(rows, 1):
        c = s["components"]
        out.append(f"| {i} | {s['iata']} ({s['city']}) | **{s['opportunity_score']}** | {c['growth']} | "
                   f"{c['pressure']} | {c['scale']} | {c['delay']} | {c['feasibility']} |")
    top = rows[0]
    out.append(f"\n**Why {top['iata']} leads:** {top['raw']['ops_per_runway']:,} ops/runway over {top['raw']['runways']} "
               f"runway(s), {top['raw']['cagr_2019_2023_pct']}%/yr enplanement growth vs 2019, "
               f"{top['raw']['enplanements_2023_m']}M enplanements (2023).")
    if top["opportunity_score"] < 50:
        out.append("\n⚠️ **No airport here scores strongly** (best < 50 vs the national peer set). Scores are relative; "
                   "'best of a modest set' is not the same as 'strong candidate'. 2019->2023 growth is also depressed "
                   "by the pandemic baseline.")
    for s in rows:
        if s["constraint_note"]:
            out.append(f"- ⚠️ {s['iata']}: {s['constraint_note']}")
    out.append(f"\n_Assumptions:  {r['note']} {r['scope']}_")
    return "\n".join(out)


def fmt_compare(r):
    if _err(r):
        return _err(r)
    rows = r["comparison"]
    out = ["| Metric | " + " | ".join(s["iata"] for s in rows) + " |", "|--|" + "--|" * len(rows)]
    for label, f in [("Congestion index (0-100)", lambda s: s["congestion_index"]),
                     ("Ops per runway", lambda s: f"{s['raw']['ops_per_runway']:,}"),
                     ("Runways", lambda s: s["raw"]["runways"]),
                     ("Enplanements 2023 (M)", lambda s: s["raw"]["enplanements_2023_m"]),
                     ("Growth CAGR vs 2019 (%)", lambda s: s["raw"]["cagr_2019_2023_pct"]),
                     ("Unmet-demand index", lambda s: s["unmet_demand_index"]),
                     ("Opportunity score", lambda s: s["opportunity_score"]),
                     ("Live FAA status", lambda s: (s["live_status"] or {}).get("program", "normal"))]:
        out.append(f"| {label} | " + " | ".join(str(f(s)) for s in rows) + " |")
    hi = max(rows, key=lambda s: s["congestion_index"])
    out.append(f"\n**{hi['iata']} is more congested** on this index (capacity pressure vs peers, plus live delays).")
    for s in rows:
        if s["constraint_note"]:
            out.append(f"- {s['iata']}: {s['constraint_note']}")
    out.append(f"\n_{r['note']} Live delays are a point-in-time snapshot. {r['scope']}_")
    return "\n".join(out)


def fmt_longhaul(r):
    if _err(r):
        return _err(r)
    return (f"**{r['iata']}: {r['pct_long_haul_routes']}% of airline routes are long haul** (>= {r['threshold_mi']:.0f} mi; "
            f"{r['airline_routes']} routes to {r['destinations']} destinations; "
            f"{r['pct_long_haul_destinations']}% of destinations).\n\nLongest destinations: "
            f"{', '.join(r['long_haul_destinations']) or 'none'}\n\n**Caveats**\n"
            + "\n".join(f"- {c}" for c in r["caveats"]))


def fmt_unmet(r):
    if _err(r):
        return _err(r)
    verdict = r.get("data_availability_verdict", "")
    out = [
        f"**{r['iata']} Unmet Demand Analysis: Index {r['unmet_demand_index']} ({r['unmet_level']})**\n",
        f"[Data Limitation] {verdict}\n" if verdict else "",
        "**Why Unmet / Suppressed Demand Occurs at This Airport:**",
    ]
    for x in r["reasons"]:
        out.append(f"- {x}")
    out.append(f"\n_{r['note']}_")
    return "\n".join(line for line in out if line)


def fmt_method(r):
    return ("**Opportunity score** = weighted sum of percentile components:\n"
            + "\n".join(f"- **{k}** ({r['weights'][k]:.0%}): {v}" for k, v in r["components"].items())
            + f"\n\n_{r['scope']}_")
