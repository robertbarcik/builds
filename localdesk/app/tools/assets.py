"""Asset lookup tool implementation."""

import json
import sqlite3

from app.config import DATABASE_PATH


def lookup_asset(employee_id: str) -> str:
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name, department, email FROM employees WHERE employee_id = ?", (employee_id.upper(),))
        emp = cur.fetchone()
        if not emp:
            return json.dumps({"error": f"No employee found with ID {employee_id}."})

        cur.execute(
            "SELECT asset_tag, asset_type, model, assigned_date FROM assets WHERE employee_id = ?",
            (employee_id.upper(),),
        )
        assets = [
            {"asset_tag": r[0], "type": r[1], "model": r[2], "assigned_date": r[3]}
            for r in cur.fetchall()
        ]
        return json.dumps(
            {
                "employee_id": employee_id.upper(),
                "name": emp[0],
                "department": emp[1],
                "email": emp[2],
                "assets": assets,
            }
        )
    finally:
        conn.close()
