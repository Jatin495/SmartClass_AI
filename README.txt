PCU Hackthon - Project README
===================================

1) Project Overview
-------------------
PCU Hackthon is a classroom engagement monitoring web application.
It provides:
- Teacher login/logout
- Real-time classroom monitoring using camera feed
- Face-based attendance marking
- Student management
- Dashboard analytics (stats, timeline, heatmap)
- Engagement and emotion tracking
- Reports generation and download

Current stack:
- Backend: Django + Django REST Framework
- Frontend: HTML + CSS + JavaScript (served by Django)
- Database: SQLite (smartclass.db)
- CV/AI libraries: OpenCV, MediaPipe, FER, NumPy, Pandas


2) Main Project Structure
-------------------------
- manage.py
- smartclass_backend/
  - settings.py
  - urls.py
- engagement/
  - models.py
  - views.py
  - urls.py
  - camera.py
  - video_stream.py
- Frontend pages:
  - index.html
  - login.html
  - dashboard.html
  - live_class.html
  - attendance.html
  - student.html
  - reports.html
  - analytics.html
  - settings.html
- Shared frontend files:
  - styles.css
  - main.js
  - api.js
  - charts.js
- requirements.txt
- smartclass.db


3) Prerequisites
----------------
Install:
- Python 3.10+ (recommended 3.10)
- pip
- Camera/webcam access (for live monitoring)

Windows note:
- If MediaPipe/OpenCV install fails, upgrade pip first:
  py -m pip install --upgrade pip


4) Setup and Installation
-------------------------
Option A: Use existing virtual environment (if already present)
1. Open terminal in project root.
2. Activate venv:
   venv_310\Scripts\activate
3. Install/refresh dependencies:
   pip install -r requirements.txt

Option B: Create new virtual environment
1. py -m venv .venv
2. .venv\Scripts\activate
3. pip install -r requirements.txt


5) Database Setup
-----------------
Run migrations:
1. py manage.py makemigrations
2. py manage.py migrate

Optional admin user:
- py manage.py createsuperuser


6) Run the Project
------------------
Start Django server:
- py manage.py runserver

Open in browser:
- Main app: http://127.0.0.1:8000/
- Login page: http://127.0.0.1:8000/login.html
- Dashboard: http://127.0.0.1:8000/dashboard.html
- Live monitor: http://127.0.0.1:8000/live_class.html


7) API Base and Key Endpoints
-----------------------------
Base API URL:
- /api/

Health:
- GET /api/health/

Auth:
- POST /api/auth/login/
- POST /api/auth/logout/

Students:
- GET /api/students/
- POST /api/students/add/
- GET/PUT/DELETE /api/students/<student_id>/

Sessions:
- GET /api/sessions/
- POST /api/sessions/start/
- POST /api/sessions/<session_id>/end/

Live monitoring:
- GET /api/live/feed/
- GET /api/live/data/
- POST /api/live/stop/

Reports:
- POST /api/reports/generate/
- GET /api/reports/list/
- GET /api/reports/download/<report_id>/
- DELETE /api/reports/delete/<report_id>/

Attendance and analytics:
- GET /api/attendance/
- GET /api/analytics/

Demo/seed:
- POST /api/setup/seed/


8) Frontend Pages Served by Django
----------------------------------
The backend serves static HTML/CSS/JS directly from project root via URL routes.
Examples:
- /index.html
- /dashboard.html
- /reports.html


9) Common Development Commands
------------------------------
- Run server:
  py manage.py runserver
- Apply migrations:
  py manage.py migrate
- Create migrations:
  py manage.py makemigrations
- Django checks:
  py manage.py check
- Django tests:
  py manage.py test


10) Troubleshooting
-------------------
A) Camera not opening
- Ensure camera is not used by another app.
- Try restarting browser and server.
- Use /api/start_simple_camera/ and /api/stop_simple_camera/ paths if needed by the page flow.

B) Live feed request hangs in terminal/browser tools
- MJPEG stream is continuous by design; request may appear pending.

C) Module import errors
- Verify active virtual environment.
- Re-run: pip install -r requirements.txt

D) CORS/CSRF issues in browser
- For local dev, settings.py currently allows broad CORS.
- Confirm requests are sent to same host (127.0.0.1:8000).


11) Production Notes
--------------------
Before production deployment:
- Set DEBUG = False
- Replace SECRET_KEY with environment variable
- Restrict ALLOWED_HOSTS
- Use PostgreSQL instead of SQLite
- Configure secure CSRF/SESSION cookies
- Serve static/media with proper web server setup


12) Quick Start (Minimal)
-------------------------
1. venv_310\Scripts\activate
2. pip install -r requirements.txt
3. py manage.py migrate
4. py manage.py runserver
5. Open http://127.0.0.1:8000/login.html


End of README
