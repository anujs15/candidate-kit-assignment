# Employee Attendance and Analytics API

FastAPI and MongoDB implementation of the supplied attendance contract. It includes race-safe punch-in and punch-out, audited manual corrections, MongoDB aggregation analytics, startup indexes, and execution-plan inspection.

## Run locally

Requirements: Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in the environment. To load the sample shapes into a disposable database, run `python sample_seed.py`; it replaces both sample collections in that database. Then start the API:

```bash
uvicorn app.main:app --port 8000
```

Interactive API documentation is available at `http://localhost:8000/docs`. The application creates all required indexes at startup. No known assignment functionality is missing.
