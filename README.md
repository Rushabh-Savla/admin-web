# Central Annotation Platform & Admin Webpage (`admin_webpage`)

High-concurrency centralized web dashboard, REST API controller, and asynchronous NAS staging worker powering the multi-PC annotation platform.

---

## 🌟 Highlights & Features

- **FastAPI Central Controller (`controller/app/`):**
  - **Project & Dataset Explorer:** Browse network-attached storage (NAS) folders, register active projects, trigger recursive catalog scans, and track total images.
  - **Batch Assignment Dispatcher:** Assign atomic image batches with idempotency keys, batch size limits, and concurrency lease management.
  - **Master Review & Approval Queue:** Multi-stage review interface for annotator submissions, background declarations, and batch sign-offs with denial/approval feedback loops.
  - **Real-Time Notification Bell:** In-header live notifications for pending review requests and background declarations awaiting master sign-off.
  - **System Operations & Audit Trail (`operations.html`):** Real-time monitoring of sync worker queues, background transfer jobs, and immutable security audit logs.
  - **Role-Based Access Control (RBAC):** Admin, Manager, Annotator, and Reviewer roles with Argon2 password hashing and JWT sessions.
- **Async NAS Sync Worker (`sync_worker/`):**
  - Robust worker handling background NAS cataloging, file transfers via rclone, batch tar bundling, and deduplication staging.
- **Database Migrations (`alembic/`):**
  - Version-controlled PostgreSQL schemas for zero-downtime database upgrades.

---

## 📂 Repository Structure

```
admin_webpage/
├── controller/                   # Central API and web application
│   ├── app/
│   │   ├── main.py               # FastAPI application entrypoint & lifespan
│   │   ├── models.py             # SQLAlchemy models (Datasets, Assignments, Items, Users)
│   │   ├── database.py           # DB connection & sessionmaker (PostgreSQL / SQLite)
│   │   ├── config.py             # Application settings & environment parsing
│   │   ├── auth.py               # Password hashing & JWT token services
│   │   ├── assignment_service.py # Core logic for atomic claims & assignment lifecycle
│   │   ├── nas_service.py        # NAS filesystem scanning & rclone abstraction
│   │   ├── routers/              # API and UI router endpoints
│   │   │   ├── dashboard.py      # Project views, metrics & review workflows
│   │   │   ├── assignments.py    # Assignment creation, item claiming & completion
│   │   │   ├── images.py         # Image serving, claim verification & uploads
│   │   │   ├── nas.py            # NAS catalog exploration & sync triggers
│   │   │   ├── users.py          # User management & authentication
│   │   │   └── sync_jobs.py      # Background worker sync job status
│   │   ├── templates/            # Jinja2 HTML templates
│   │   └── static/               # CSS styles & client JavaScript
├── alembic/                      # Database migration scripts
│   ├── versions/                 # Versioned migration revisions
│   └── env.py
├── alembic.ini                   # Alembic configuration
├── sync_worker/                  # NAS synchronization worker daemon
│   ├── worker.py                 # Worker process loop
│   ├── staging.py                # Local staging cache manager
│   ├── transfer.py               # Rclone transfer execution
│   ├── catalog_adapter.py        # Direct NAS tree scanner
│   └── batch_staging.py          # Bulk batch tarball packager
├── tests_central/                # Comprehensive test suite
├── run_web.ps1                   # Web launcher script (PowerShell)
├── run_web.bat                   # Web launcher script (Batch)
├── run_web.sh                    # Web launcher script (Bash)
├── requirements.txt              # Server Python dependencies
├── .env.example                  # Environment configuration template
├── pytest.ini                    # Pytest configuration
└── .github/workflows/
    └── web_tests.yml             # Automated CI pipeline
```

---

## 🚀 Quickstart Installation & Local Run

### 1. Prerequisites
- Python 3.10+
- PostgreSQL 14+ (or SQLite fallback for testing)
- Rclone (for production NAS integration)

### 2. Setup Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate   # On Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3. Configure Environment

Copy `.env.example` to `.env` and configure your settings:
```bash
cp .env.example .env
```

### 4. Run Migrations & Start Server

```bash
# Run database migrations
alembic upgrade head

# Start FastAPI web server on port 8080
uvicorn controller.app.main:app --host 0.0.0.0 --port 8080 --reload
```

Or run via the helper script:
```powershell
.\run_web.ps1
```

Access the Web Dashboard at: **`http://localhost:8080/dashboard`** (or your server's LAN IP, e.g., `http://192.168.1.103:8080/dashboard`).

---

## 🧪 Testing

Run all central unit and integration tests:
```bash
pytest tests_central/ -v
```

---

## 🔗 Related Repositories

- **Main Orchestration Repo:** [Rushabh-Savla/nas_annotation_automation](https://github.com/Rushabh-Savla/nas_annotation_automation)
- **Annotator Workstation App Subrepo:** [Rushabh-Savla/annotator_app](https://github.com/Rushabh-Savla/annotator_app)