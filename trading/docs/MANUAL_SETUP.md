# Manual setup

The individual commands, for anyone who would rather not run `setup.ps1`, or
who is on macOS or Linux.

---

## Windows (PowerShell)

```powershell
cd path\to\trading

# 1. Create and activate the virtual environment
python -m venv backend\.venv
backend\.venv\Scripts\activate

# 2. Install the Python packages
python -m pip install --upgrade pip
pip install -r backend\requirements-dev.txt

# 3. Install the frontend packages
cd frontend
npm install
cd ..

# 4. Create your environment file
copy .env.example .env
notepad .env          # paste your Alpaca PAPER keys

# 5. Run the backend (leave this window open)
cd backend
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# 6. In a SECOND window, run the dashboard
cd path\to\trading\frontend
npm run dev
```

Open <http://localhost:5173>.

---

## macOS / Linux

```bash
cd path/to/trading

python3 -m venv backend/.venv
source backend/.venv/bin/activate
pip install -r backend/requirements-dev.txt

cd frontend && npm install && cd ..
cp .env.example .env

# backend
cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# frontend, in another terminal
cd frontend && npm run dev
```

Or just `make setup`, then `make backend` and `make frontend`.

---

## Verifying the install

```bash
cd backend
python -m pytest -q
```

419 tests should pass in about 20 seconds. They never touch the network.

A quick manual check that the backend is alive:

```bash
curl http://127.0.0.1:8000/api/system/health
```

You should see `"status":"ok"`.

---

## Useful commands

| What | Command |
|---|---|
| Run the backend | `uvicorn app.main:app --reload` (from `backend/`) |
| Run the dashboard | `npm run dev` (from `frontend/`) |
| Run the tests | `python -m pytest -q` (from `backend/`) |
| Run one test file | `python -m pytest tests/test_risk_bypass.py -v` |
| Lint | `ruff check app tests` |
| Format | `ruff format app tests` |
| Typecheck the frontend | `npm run typecheck` |
| Build the dashboard | `npm run build` |
| Emergency stop | create an empty file `data/KILL_SWITCH` |

---

## Where things live

| Path | What |
|---|---|
| `.env` | Your secrets. Never committed. |
| `config/*.yaml` | All trading behaviour. Edit and restart. |
| `data/atlas.db` | The SQLite database. |
| `data/KILL_SWITCH` | Present = all trading blocked. |
| `logs/atlas.jsonl` | Structured logs, one JSON object per line. |
