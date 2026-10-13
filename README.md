# Airport Investment Intelligence Agent

Chat agent that ranks/compares US airports for modernization investment. See [DESIGN.md](DESIGN.md).

```powershell
pip install -r requirements.txt
# Copy .env.example to .env to configure your free LLM API (OmniRoute, Gemini, Groq, etc.)
# Run the app (using python -m uvicorn avoids Windows PATH issues):
python -m uvicorn server:app --port 8000 --reload
# Open http://localhost:8000  (mic button = voice input, checkbox = read aloud)

# Run tests:
python -m pytest -q
```
First run downloads ~17 MB of public data (OurAirports, OpenFlights) into `.cache/`. Without an API key the app
runs in offline-router mode (deterministic answers to the sample question types + follow-ups).

**Caveat:** `airport_agent/seed_airports.csv` holds approximate enplanement/operations values entered from memory of
public FAA/BTS figures and hand-assigned constraint scores; verify before relying on them.
