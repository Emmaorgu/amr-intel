# AMR-Sentinel — Deployment Guide
## amr-intel.com on Render

---

## Overview

| Service | Type | Plan | Cost |
|---|---|---|---|
| amr-sentinel-api | Web Service (FastAPI) | Starter | $7/mo |
| amr-sentinel-orchestrator | Background Worker | Starter | $7/mo |
| amr-sentinel-dashboard | Static Site (React) | Free | $0 |
| amr-sentinel-db | PostgreSQL | Starter | $7/mo |
| **Total** | | | **$21/mo** |

---

## Step 1 — Prepare the repo

```bash
# In the project root
git init  # if not already done
git remote add origin https://github.com/Emmaorgu/amr-intel.git

# Make sure .gitignore excludes .env, data/, .venv/, node_modules/
# Then add everything
git add .
git commit -m "Initial deployment: AMR-Sentinel v8.1"
git push -u origin main
```

**Before pushing, verify these are NOT being committed:**
- `.env` (contains your API keys)
- `.venv/` (Python virtual environment)
- `data/` (large data files)
- `node_modules/`
- `*.ckpt`, `*.pkl` (ML model files)

---

## Step 2 — Create Render PostgreSQL

1. Go to render.com → New → PostgreSQL
2. Name: `amr-sentinel-db`
3. Region: Oregon
4. Plan: Starter ($7/mo)
5. Click Create — note the connection details

---

## Step 3 — Run migrations against hosted DB

On your local machine, temporarily set your .env to point at the Render DB:

```bash
# Set these in your .env temporarily
DB_HOST=<render-db-host>.oregon-postgres.render.com
DB_PASSWORD=<render-db-password>
DB_USER=<render-db-user>
DB_NAME=amr_sentinel
DB_PORT=5432

# Run migrations
python -m alembic upgrade head

# Restore your local .env after
```

---

## Step 4 — Seed the hosted database

Run your ingestors against the Render DB (same .env override as above):

```bash
python -m amr_sentinel.ingestion.glass_ingestor
python -m amr_sentinel.ingestion.ecdc_ingestor
python -m amr_sentinel.ingestion.ndaro_ingestor
```

Then seed the signal validations and emergence scores:
```bash
python -m amr_sentinel.db.seed_validations
python -m amr_sentinel.models.emergence_scorer
```

---

## Step 5 — Deploy via render.yaml

1. Go to render.com → New → Blueprint
2. Connect your GitHub repo: https://github.com/Emmaorgu/amr-intel
3. Render reads `render.yaml` automatically and creates all services
4. Set `ANTHROPIC_API_KEY` manually in each service's environment variables

---

## Step 6 — Connect amr-intel.com

In Namecheap DNS settings for amr-intel.com:

**For the dashboard (main site):**
```
Type: CNAME
Host: @
Value: amr-sentinel-dashboard.onrender.com
TTL: Auto
```

```
Type: CNAME
Host: www
Value: amr-sentinel-dashboard.onrender.com
TTL: Auto
```

**For the API (subdomain):**
```
Type: CNAME
Host: api
Value: amr-sentinel-api.onrender.com
TTL: Auto
```

Then in the dashboard's `src/App.jsx`, change the API constant:
```javascript
const API = "https://api.amr-intel.com";
```

---

## Step 7 — Update dashboard API URL

In `dashboard/src/App.jsx`, find:
```javascript
const API = "http://localhost:8000";
```
Change to:
```javascript
const API = import.meta.env.VITE_API_URL || "https://api.amr-intel.com";
```

Add `dashboard/.env.production`:
```
VITE_API_URL=https://api.amr-intel.com
```

---

## Important notes on TFT and continuous operation

The TFT model (PyTorch) is too large to run on Render's Starter plan.
The orchestrator on Render uses the **forecast cache** (`data/cache/forecasts_*.pkl`).

**To update forecasts:**
1. Run TFT locally: `python -m amr_sentinel.models.trajectory_forecaster`
2. Commit the updated cache file: `git add data/cache/ && git commit -m "Update forecast cache"`
3. Push — Render redeploys automatically and uses the new cache

The orchestrator on Render runs every 6 hours and:
- Skips ingestion (data is seeded once, updated manually)
- Loads forecast cache (no TFT retraining)
- Runs anomaly detection, triage, stewardship, evidence linking
- Generates new alerts and writes them to the DB
- The dashboard reflects new alerts immediately

This gives you autonomous, continuous intelligence generation without needing GPU infrastructure.

---

## Costs summary

| Item | Monthly |
|---|---|
| Render Starter × 2 (API + Worker) | $14 |
| Render PostgreSQL Starter | $7 |
| Render Static Site | $0 |
| Namecheap amr-intel.com (annual) | ~$1.25 |
| Anthropic API (≈20 alerts × 6 cycles/day) | ~$15-30 |
| **Total** | **~$37-52/mo** |