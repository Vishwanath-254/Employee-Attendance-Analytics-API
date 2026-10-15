# Employee Attendance & Analytics API

FastAPI + MongoDB service for managing employee attendance, punch-in/punch-out rules, manual corrections (regularizations) with audit history, and MongoDB aggregation analytics.

## Prerequisites
- Python 3.11+
- MongoDB 6.0+ (Local Docker container or MongoDB Atlas)

## Setup & Running

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment Variables**:
   Set `MONGO_URI` and `MONGO_DB` in your environment (defaults to `mongodb://localhost:27017` and `attendance_db`).
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Seed Sample Data (Optional)**:
   ```bash
   python sample_seed.py
   ```

4. **Start the API Server**:
   ```bash
   python -m uvicorn app.main:app --port 8000
   ```

All database indexes are created idempotently at startup automatically.

## Endpoints Implemented

- `GET /health`: DB readiness check.
- `POST /employees`: Create employee (validates unique `emp_code`, `shift_start != shift_end`).
- `GET /employees`: Paginated employee list sorted by `emp_code`.
- `POST /attendance/punch-in`: Punch-in record creation (race-safe, handles R1 overnight shifts & R2 late minutes).
- `POST /attendance/punch-out`: Punch-out record update (handles R3 overtime, R4 work hours, R5 half day).
- `GET /attendance`: Paginated attendance list sorted by `date` desc, `emp_code` asc.
- `PATCH /attendance/{emp_code}/{date}`: Manual regularization with complete audit history trail.
- `GET /analytics/employees/{emp_code}/monthly`: Monthly employee attendance summary.
- `GET /analytics/departments/summary`: Monthly department summary across collections (headcount includes zero-log employees).
- `GET /analytics/leaderboard/late`: Competition ranking leaderboard with tie-handling.
- `GET /analytics/departments/{department}/trend`: Daily attendance trend with gap filling (`$densify`) and 7-day moving average (`$setWindowFields`).
- `GET /admin/explain/{endpoint}`: Returns MongoDB `executionStats` explain plan proving index usage (`IXSCAN`).

## Project Files
- `app/main.py`: Core FastAPI application and MongoDB handlers.
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Starter code defect analysis and fixes.
- `DECISIONS.md`: Architectural decisions and design trade-offs.
