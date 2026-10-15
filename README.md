# Employee Attendance & Analytics API

FastAPI and MongoDB backend for employee attendance tracking, regularization, and multi-dimensional analytics.

## Prerequisites
- Python 3.10+
- MongoDB 6.0+ (MongoDB 7.0 recommended, or MongoDB Atlas free tier M0)

## Quick Start

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   Create a `.env` file (or set environment variables in your deployment environment):
   ```bash
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```

3. **(Optional) Seed sample data:**
   ```bash
   python sample_seed.py
   ```

4. **Run the server:**
   ```bash
   uvicorn app.main:app --port 8000
   ```

Interactive API documentation will be available at `http://localhost:8000/docs`.

## Key Implementations & Highlights
- **Single-File Architecture:** All application code resides in [app/main.py](file:///c:/Users/ANKIT%20PRAMANICK/Desktop/HROne%20Assignment/candidate_kit/app/main.py) with zero external module dependencies beyond `requirements.txt`.
- **Automatic Index Creation:** Database indexes are created idempotently on startup, including unique constraints for concurrency safety on `emp_code` and `(emp_code, date)`.
- **Business Rules (R1–R10):**
  - Truncation of instants to whole seconds and IST timezone handling (including overnight shifts).
  - 10-minute strict grace period calculation for late arrivals.
  - 30-minute minimum threshold for overtime.
  - Half-up rounding (`ROUND_HALF_UP`) via `Decimal` for all calculated work hours and rates.
- **Aggregation Pipelines:** Analytics endpoints (`/monthly`, `/summary`, `/leaderboard`, `/trend`) are computed directly within MongoDB using native aggregation stages (`$densify`, `$setWindowFields` with `$rank`, `$lookup`, and `$group`) ensuring optimal query plans (`IXSCAN`) without Python-level iteration.
- **Explain Endpoint:** `GET /admin/explain/{endpoint}` executes `explain` with `executionStats` verbosity on the exact query/pipeline used by the endpoint.

## Project Structure
- `app/main.py`: Complete FastAPI application, models, helpers, endpoints, and indexing logic.
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Detailed defect analysis and fixes for starter code defects.
- `DECISIONS.md`: Design and architectural decisions (indexes, concurrency, ranking, scaling).
- `README.md`: Setup and run instructions.
