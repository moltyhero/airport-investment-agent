# Design & Architecture

## Overview
```
Browser chat (text + Web Speech voice in/out)
        |  POST /api/chat
FastAPI -- Agent --> LLM (OpenAI-compatible, tool calling)   [if OPENAI_API_KEY set]
              |        else: deterministic intent router + templated answers
              v
          tools.py  (rank / compare / long_haul / unmet_demand / live_status / methodology)
              v
          scoring.py (pure, deterministic) <-- data_sources.py
                                                 - BTS T-100 ArcGIS REST API  (2024 departures)   API
                                                 - FAA enplanement xlsx 2025+2024 (2023-25)       file, cached
                                                 - BTS On-Time rolling 12 mo (monthly files -> JSON) file, cached
                                                 - FAA NAS Status API                              live API
                                                 - OurAirports runways                             file, cached
                                                 - constraints.csv (13 airports, hand-curated)
```

## Where AI is used (and not)
- **LLM**: interprets the question, picks tools/arguments (region, airports, threshold, weight overrides), keeps
  conversational context, writes the explanation. The system prompt forbids inventing numbers or airport facts.
- **Not LLM**: every number. Scores, rankings and percentages come from `scoring.py`; tools return scope/caveats with results.
- With no API key an offline router handles the four sample question types and follow-ups, so the scoring is demonstrably LLM-independent.

## Scope
All US airports in the FAA commercial-service enplanement files with >= 250k enplanements in the latest year
(~185 airports; smaller ones have unstable percentiles). Region queries use the airport's state (e.g. New England = CT, MA, ME, NH, RI, VT).

## Scoring methodology
Components are **percentile ranks (0-100) within the national peer set**.

**Opportunity score** (terminal-expansion profitability proxy) = weighted mean of:

| Component | Weight | Definition | Rationale |
|---|---|---|---|
| growth | 25% | CAGR of enplanements 2023 -> 2025 (FAA) | Demand momentum, avoids the pandemic baseline |
| pressure | 30% | 0.6 pct(est. ops/runway) + 0.4 pct(enplanements/runway); ops = 2 x T-100 departures | Demand vs airside capacity |
| scale | 20% | pct(log enplanements) | Revenue base to amortize capex |
| delay | 10% | 0.5 pct(share of departures 15+ min late) + 0.3 pct(taxi-out) + 0.2 pct(cancel rate), BTS rolling 12 months | Persistent congestion |
| feasibility | 15% | 100 (1 - constraint), curated | Curfews, caps, slots block returns |

Missing components (no BTS cache, or no curated constraint) are **left out and weights renormalised**, never filled with a guess.
Weights can be overridden per query.

**Congestion index** = mean(pressure, delay). **Unmet-demand index** (proxy) = 0.40 pressure + 0.30 constraint*100 + 0.15 growth + 0.15 delay
(renormalised if parts are missing). **Long-haul share** = share of actual BTS departures with scheduled distance >= 2000 mi (adjustable per query; no official US definition exists, sources range from ~1,000 to ~2,500 mi, so the answer states the threshold and it can change the share a lot, e.g. ANC 68% at 1,000 mi vs 18% at 2,500 mi).
Live FAA status is displayed but is **not** in any index (a snapshot is not a persistent condition).

## Key tradeoffs and limits
- **API vs files**: only T-100 totals and FAA NAS status have real APIs. Flight-level data (delays, distances) and enplanement history exist only as
  bulk files, so those are downloaded once and reduced to small cached aggregates.
- **BTS On-Time covers domestic flights of large reporting carriers**: no international, cargo-only or small-carrier flights.
  Anchorage's long-haul share therefore describes passenger carriers, not its large cargo operation (shown as a caveat).
- **Time windows differ by source**: BTS On-Time uses the latest 12 published months (Aug 2025 - Jul 2026, a full window so there is no seasonality bias; BTS lags ~2 months). The T-100 API only offers 2024, and FAA enplanements run to 2025, so the sources are not perfectly aligned.
- **T-100 totals are domestic only**, so ops estimates understate international hubs (e.g. LAX, SFO, JFK).
- **Runway counts** come from OurAirports (paved, >= 3500 ft, open); crude for airports with crossing or short runways.
- **Terminal capacity is not public**: gate utilization and terminal square footage are not in any open dataset, so "terminal expansion" is inferred from airside and traffic pressure.
- **Constraints** are hand-curated for 13 well-known airports; elsewhere they are unknown, not zero.
- **Unmet demand** is not observable: BTS/FAA record realized flights only. The agent says so and reports a labelled proxy plus the signals behind it.
- **Percentile scores are relative**: "best in region" can still be weak nationally; the agent flags when the top score is < 50.
- Growth uses only 2 years of endpoints and ignores route-level or forecast (FAA TAF) information.

## Possible next sources
FAA Terminal Area Forecast (forward growth), airport master plans / ACIP (gates, terminal sizing, funded projects), FAA capacity profiles (airside rates).
