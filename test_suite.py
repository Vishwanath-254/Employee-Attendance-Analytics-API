"""
Comprehensive Test Suite for Employee Attendance & Analytics API
"""
import os
os.environ["MONGO_DB"] = "test_attendance_db"

from datetime import datetime, timedelta, timezone
import unittest

from fastapi.testclient import TestClient
from app.main import app, db

client = TestClient(app)


class TestAttendanceAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.employees.drop()
        db.attendance_logs.drop()
        from app.main import init_db
        init_db()

    def setUp(self):
        db.employees.delete_many({})
        db.attendance_logs.delete_many({})

    def test_01_health(self):
        res = client.get("/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ok"})

    def test_02_employee_crud(self):
        res = client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Alice",
            "email": "alice@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "09:30",
            "joined_on": "2026-01-01"
        })
        self.assertEqual(res.status_code, 422)

        res = client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Alice",
            "email": "alice@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["emp_code"], "EMP0001")
        self.assertIn("created_at", data)
        self.assertIsInstance(data["created_at"], int)

        res = client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Alice Duplicate",
            "email": "alice2@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        self.assertEqual(res.status_code, 409)

        res = client.get("/employees")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["total"], 1)

    def test_03_punch_in_and_rules(self):
        client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Alice",
            "email": "alice@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })

        ts_ontime = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
        res = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": ts_ontime,
            "status": "PRESENT"
        })
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["date"], "2026-07-06")
        self.assertEqual(data["late_minutes"], 0)

        res = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": ts_ontime + 1000,
            "status": "PRESENT"
        })
        self.assertEqual(res.status_code, 409)

        ts_late = int(datetime(2026, 7, 7, 4, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
        res = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": ts_late,
            "status": "PRESENT"
        })
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["late_minutes"], 10)

    def test_04_overnight_shift_and_punch_out(self):
        client.post("/employees", json={
            "emp_code": "EMP0005",
            "name": "Night Owl",
            "email": "owl@example.com",
            "department": "Ops",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-01-01"
        })

        ts_in = int(datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc).timestamp() * 1000)
        res = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0005",
            "punched_at": ts_in,
            "status": "PRESENT"
        })
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["date"], "2026-07-06")

        ts_out = int(datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
        res = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0005",
            "punched_at": ts_out
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["work_hours"], 8.75)
        self.assertEqual(data["overtime_minutes"], 40)
        self.assertFalse(data["half_day"])

    def test_05_regularization(self):
        client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Alice",
            "email": "alice@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        ts_in = int(datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc).timestamp() * 1000)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})

        ts_fixed = int(datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
        res = client.patch("/attendance/EMP0001/2026-07-07", json={
            "punch_in": ts_fixed,
            "reason": "Biometric glitch",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["late_minutes"], 0)
        self.assertEqual(len(data["history"]), 1)
        self.assertEqual(data["history"][0]["by"], "hr.admin")
        self.assertIn("late_minutes", data["history"][0]["changes"])
        self.assertEqual(data["history"][0]["changes"]["late_minutes"]["from"], 35)
        self.assertEqual(data["history"][0]["changes"]["late_minutes"]["to"], 0)

    def test_06_analytics_leaderboard_ties(self):
        codes = ["EMP0001", "EMP0002", "EMP0003"]
        for i, code in enumerate(codes):
            res = client.post("/employees", json={
                "emp_code": code,
                "name": f"User {i+1}",
                "email": f"user{i+1}@example.com",
                "department": "Engineering",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01"
            })
            self.assertEqual(res.status_code, 201)

        db.attendance_logs.insert_many([
            {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 30},
            {"emp_code": "EMP0002", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 20},
            {"emp_code": "EMP0003", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 20},
        ])

        res = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
        self.assertEqual(res.status_code, 200)
        items = res.json()["items"]
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["rank"], 1)
        self.assertEqual(items[1]["rank"], 2)
        self.assertEqual(items[2]["rank"], 2)

    def test_07_analytics_trend(self):
        res1 = client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "User 1",
            "email": "u1@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01"
        })
        self.assertEqual(res1.status_code, 201)

        res2 = client.post("/employees", json={
            "emp_code": "EMP0002",
            "name": "User 2",
            "email": "u2@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-03"
        })
        self.assertEqual(res2.status_code, 201)

        db.attendance_logs.insert_many([
            {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 0, "half_day": False},
            {"emp_code": "EMP0001", "date": "2026-07-02", "status": "PRESENT", "late_minutes": 15, "half_day": True},
            {"emp_code": "EMP0002", "date": "2026-07-03", "status": "PRESENT", "late_minutes": 0, "half_day": False},
        ])

        res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
        self.assertEqual(res.status_code, 200)
        items = res.json()["items"]
        self.assertEqual(len(items), 7)
        self.assertEqual(items[0]["date"], "2026-07-01")
        self.assertEqual(items[0]["attendance_rate"], 1.0)
        self.assertEqual(items[1]["attendance_rate"], 0.5)

    def test_08_explain(self):
        client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Alice",
            "email": "alice@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        for ep in ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]:
            params = "emp_code=EMP0001&month=2026-07&department=Engineering&from=2026-07-01&to=2026-07-07"
            res = client.get(f"/admin/explain/{ep}?{params}")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertIn("explain", data)


if __name__ == "__main__":
    unittest.main()
