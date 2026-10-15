# Airport Investment Intelligence Agent

Chat agent that ranks/compares **all US commercial airports** for modernization investment, using public data.
Design, scoring and tradeoffs: [DESIGN.md](DESIGN.md).

```powershell
pip install -r requirements.txt
# Optional: copy .env.example to .env and add a free LLM key (OmniRoute, Gemini, Groq, ...)

# One-time, resumable (~10-15 min, ~360 MB downloaded, only ~1 MB kept): BTS On-Time data, rolling 12 months (Aug 2025 - Jul 2026).
# Needed for historical delays and the long-haul share. The app still runs without it (those parts say "no data").
python -m airport_agent.build_cache

python -m uvicorn server:app --port 8000 --reload   # then open http://localhost:8000 (mic = voice input)
python -m pytest -q
```

Without an API key the app runs in an offline-router mode (deterministic answers to the sample question types and
simple follow-ups). With a key, an LLM picks tools and writes the explanation; all numbers still come from code.

## Data sources (all public, no keys)
| Source | Access | Used for |
|---|---|---|
| BTS T-100 (ArcGIS REST API) | live API | 2024 departures per airport (traffic volume / pressure); the API only offers 2024 |
| FAA enplanement workbooks 2025 + 2024 | file download | enplanements 2023-2025 (growth, scale) |
| BTS On-Time Performance, rolling 12 months (Aug 2025 - Jul 2026; BTS lags ~2 months) | monthly files, cached | delay / cancellation / taxi-out, long-haul share by real flights |
| FAA NAS Status | live API | current delays (shown, not scored) |
| OurAirports | file download | runway counts |

`airport_agent/constraints.csv` is the only hand-curated data: curfews/caps/slot limits for 13 well-known airports.
