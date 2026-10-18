import os
from datetime import date

START = date(2026, 8, 1)
WINDOW_DAYS = 7
TZ = "Europe/Moscow"
TIMEOUT = 10
PAUSES = (1, 2, 4)
DB_URL = os.environ.get("DQ_DATABASE_URL", "postgresql://dq:dq@localhost:5435/dq")
API_URL = os.environ.get("DQ_API_URL", "http://localhost:8040")
