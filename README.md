# PCU Hackthon

PCU Hackthon is an AI-powered classroom engagement and attendance monitoring project built with Django, HTML, CSS, and JavaScript.

## Features

- Teacher login and dashboard
- Live classroom monitoring with camera feed
- Face-based attendance support
- Student management
- Engagement and emotion tracking
- Analytics and reporting

## Tech Stack

- Backend: Django, Django REST Framework
- Frontend: HTML, CSS, JavaScript
- Database: SQLite
- AI/CV: OpenCV, MediaPipe, FER, NumPy, Pandas

## Project Structure

- `manage.py`
- `smartclass_backend/`
- `engagement/`
- `index.html`
- `login.html`
- `dashboard.html`
- `live_class.html`
- `attendance.html`
- `student.html`
- `reports.html`
- `analytics.html`
- `settings.html`
- `styles.css`
- `main.js`
- `api.js`
- `requirements.txt`

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
py manage.py migrate
py manage.py runserver
```

Open:

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/login.html`
- `http://127.0.0.1:8000/dashboard.html`
- `http://127.0.0.1:8000/live_class.html`

## Notes

- Internal package names like `smartclass_backend` and browser globals like `SmartClassAPI` are kept unchanged so the project continues working correctly.
- This repository branding has been updated for GitHub presentation as `PCU Hackthon`.
