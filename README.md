# NAS Annotation Platform

## Project Label
**Project Name:** NAS Annotation Platform

**GitHub Repository:** https://github.com/dhairyashil1012-ease/web-app

**Description:** Centralized NAS-based annotation platform for dataset labeling, assignment tracking, validation workflows, and team review across internal deployment environments.

This repository contains the central web platform, API layer, PostgreSQL-backed coordination service, and NAS sync worker used to manage image datasets, create annotation assignments, track ownership, validate submissions, and synchronize files between a NAS and local annotation workspaces.

The system is designed around one core idea: the central application owns the authoritative dataset catalog and assignment state, while the background worker handles heavy operations such as remote catalog scans, workspace preparation, transfer verification, and upload work.

---

## 1. What this project does

This platform is used to:

- register and track datasets stored on a remote NAS or mounted storage,
- scan remote folders and index images, directories, and metadata,
- assign image batches to annotators with managed leases,
- enforce single active ownership for each image while work is in progress,
- validate submitted annotation JSON before publishing it back to the NAS,
- maintain a durable queue for background work via PostgreSQL,
- expose a web dashboard and REST API for operations and review.

This is not just a simple uploader. It is a coordination platform for managed annotation pipelines.

---

## 2. System architecture

### Core services

- `controller/` — FastAPI app that hosts the dashboard, auth, dataset APIs, assignment APIs, upload logic, and the main web UI.
- `sync_worker/` — background worker that consumes durable jobs from PostgreSQL and performs catalog scans, assignment preparation, file transfer, and validation.
- `alembic/` — database migration scripts.
- `secrets/` — filesystem location for rclone credentials and other secrets.
- `scripts/` — operational helpers for startup, health checks, and repair.
- `docker-compose.yml` — orchestrates `postgres`, `migrate`, `controller`, and `sync_worker`.

### Runtime layout

```text
User / Admin / Annotator
         |
         v
Browser or API client
         |
         v
FastAPI Controller (controller/app)
         |   
         |   +--> PostgreSQL (users, datasets, assignments, jobs, audit)
         |   
         +--> Sync Jobs Queue (SyncJob table)
                 |
                 v
         Sync Worker (sync_worker/worker.py)
                 |
                 +--> NAS / remote storage via rclone
                 +--> Local staging workspaces
                 +--> Upload + verification workflow
```

### Workflow diagram

```mermaid
flowchart LR
    A[Admin / Manager registers dataset] --> B[Dataset + remote path stored in DB]
    B --> C[SyncJob: CATALOG_SCAN queued]
    C --> D[Sync Worker scans NAS via rclone]
    D --> E[DatasetDirectory + DatasetFile catalog indexed]
    E --> F[Manager creates assignment for annotator]
    F --> G[Assignment manifest + leased image set]
    G --> H[Annotator works in workspace]
    H --> I[Submission validated + ownership checked]
    I --> J[Sync Worker uploads verified annotations]
    J --> K[Audit trail + completion review]
```

---

## 3. Repository structure

```text
web_page/
├── .env.example                 # template for environment values
├── .env                        # local secrets and runtime config (do not commit)
├── docker-compose.yml          # orchestrates Postgres, migration, controller, worker
├── Dockerfile                  # application image
├── run_web.sh                  # one-click startup helper
├── requirements.txt            # Python dependencies
├── alembic/
│   ├── env.py
│   └── versions/
├── controller/
│   └── app/
│       ├── auth.py
│       ├── assignment_service.py
│       ├── cli.py
│       ├── config.py
│       ├── database.py
│       ├── dependencies.py
│       ├── main.py
│       ├── models.py
│       ├── schemas.py
│       ├── nas_service.py
│       ├── routers/
│       │   ├── auth.py
│       │   ├── assignments.py
│       │   ├── dashboard.py
│       │   ├── datasets.py
│       │   ├── health.py
│       │   ├── images.py
│       │   ├── nas.py
│       │   ├── submissions.py
│       │   ├── sync_jobs.py
│       │   └── users.py
│       └── static/
│           ├── css/
│           └── js/
├── sync_worker/
│   ├── catalog_adapter.py
│   ├── staging.py
│   ├── transfer.py
│   └── worker.py
├── scripts/
│   ├── manager.sh
│   ├── setup.sh
│   ├── health.sh
│   └── ...
├── secrets/
│   └── rclone.conf
└── README.md
```

---

## 4. How the system is connected end-to-end

### 4.1 Controller and API

The main application is the FastAPI controller. It exposes both API endpoints and dashboard routes.

Important integration points:

- `controller/app/main.py` mounts static assets and includes routers for health, auth, users, datasets, assignments, images, submissions, sync jobs, NAS access, and dashboard.
- `controller/app/database.py` creates the SQLAlchemy engine and session factory.
- `controller/app/config.py` centralizes environment settings and redacts secrets.
- `controller/app/models.py` defines the database schema and relationships.

### 4.2 PostgreSQL as the system of record

PostgreSQL is the durable source of truth for:

- users and device tokens,
- dataset and file catalogs,
- assignment manifests and item ownership,
- active image claims,
- annotation submissions and approval records,
- job queue state and audit traces.

The `sync_jobs` table is especially important because the worker consumes queued jobs instead of doing work inline in the web request path.

### 4.3 Sync worker and queued jobs

The `sync_worker/worker.py` process repeatedly claims jobs using PostgreSQL-backed durable queues. Job classes include:

- `CATALOG_SCAN`
- `PREPARE_ASSIGNMENT`
- `UPLOAD_ANNOTATION`
- `BATCH_UPLOAD_ANNOTATIONS`
- `CLEANUP_EXPIRED`

This design keeps the web application responsive while allowing expensive NAS access and transfer tasks to run in the background.

### 4.4 NAS + rclone integration

The platform uses rclone and NAS credentials stored in `secrets/rclone.conf` plus values from `.env` such as:

- `NAS_HOST`
- `NAS_PORT`
- `NAS_USER`
- `NAS_PASSWORD`
- `NAS_KEY_FILE`
- `NAS_ROOT_PATH`
- `DEFAULT_REMOTE_NAME`

The `nas_service.py` code synchronizes a generated rclone config from environment values, and the worker uses that config to scan file trees and transfer outputs.

---

## 5. Database schema overview

The application uses SQLAlchemy models centered on dataset and assignment management.

### Primary tables

- `users` — login identities, role, active state, device ownership.
- `devices` — per-user device tokens / device registrations.
- `datasets` — top-level dataset definition, remote location, catalog state, versioning.
- `dataset_directories` — catalog tree counts and path hierarchy for each dataset.
- `dataset_files` — every discovered file with category, size, fingerprint, catalog_version, and current state.
- `assignments` — assignment header with scope, annotator, catalog version, lease, status.
- `assignment_items` — items assigned to a specific annotator, referencing dataset files.
- `active_image_claims` — global single-claim ownership table; one active claim per file.
- `annotation_submissions` — validated user outputs, hash, content, validation result.
- `background_declarations` — approval flow for background / exception declarations.
- `sync_jobs` — durable job queue for worker processing.
- `audit_events` — security and operational activity log.

### Relationship model

```text
users
  ├─ one-to-many -> devices
  ├─ one-to-many -> assignments (as annotator)
  └─ one-to-many -> audit_events

datasets
  ├─ one-to-many -> dataset_directories
  ├─ one-to-many -> dataset_files
  └─ one-to-many -> assignments

assignments
  ├─ one-to-many -> assignment_items
  └─ one-to-many -> annotation_submissions

dataset_files
  ├─ one-to-one -> active_image_claims
  └─ one-to-many -> assignment_items
```

### Important business rules in the schema

- `dataset_files` enforces unique dataset + path combinations.
- `assignment_items` enforces unique assignment + file and unique assignment + ordinal.
- `active_image_claims` is a critical global lock: each file may only have one active claim at a time.
- `sync_jobs` is TTL-style operational queue state with retries and exponential backoff.
- `audit_events` records user-triggered and system-triggered operations.

---

## 6. End-to-end operational workflow

### Step 1 — Prepare the environment

1. Copy the template:
   ```bash
   cp .env.example .env
   ```
2. Open `.env` and replace the placeholder values with your local setup.
3. At minimum, update the PostgreSQL section:
   ```dotenv
   POSTGRES_HOST=postgres
   POSTGRES_PORT=5432
   POSTGRES_DB=annotation_db
   POSTGRES_USER=annotation_user
   POSTGRES_PASSWORD=your_secure_password
   DATABASE_URL=postgresql://annotation_user:your_secure_password@postgres:5432/annotation_db
   ```
4. Update NAS values for your storage target, such as:
   ```dotenv
   NAS_STORAGE_TYPE=sftp
   NAS_HOST=192.168.1.100
   NAS_PORT=22
   NAS_USER=nas_user
   NAS_PASSWORD=nas_password
   NAS_ROOT_PATH=/data
   ```
5. Set `RCLONE_CONFIG_PATH` to `/secrets/rclone.conf` and update the remote config in that file.
6. Ensure Docker and Docker Compose are installed.

### Step 2 — Start the platform

```bash
cd web_page
./run_web.sh
```

or

```bash
cd web_page
docker compose up -d
```

Open:

- Web dashboard: http://localhost:8080/dashboard
- API docs: http://localhost:8080/docs

### Step 3 — Create the first admin user

Once the containers are running, create the first administrator account.

```bash
cd web_page
docker compose exec controller python -m controller.app.cli create-admin --username admin --password 'Admin@123'
```

Then log in using the browser:

- Dashboard: http://localhost:8080/dashboard
- Username: `admin`
- Password: `Admin@123`

This creates the first administrator account. You can also create additional manager or annotator accounts with:

```bash
cd web_page
docker compose exec controller python -m controller.app.cli create-user --username annotator1 --password 'StrongPass123' --role ANNOTATOR
```

### Step 4 — Register a dataset

Use the dashboard or API to register a dataset with:

- dataset name,
- remote name,
- remote path,
- optional description.

This writes a row to `datasets` and prepares the system for a scan.

### Step 5 — Scan the dataset catalog

Trigger a `CATALOG_SCAN` job. The sync worker reads the configured remote path, filters by supported extensions, and updates:

- `dataset_directories`
- `dataset_files`
- `datasets.catalog_state`
- `datasets.current_catalog_version`

At this point, the platform knows what images are available and what is eligible for assignment.

### Step 6 — Create an assignment

A manager or admin creates an assignment by choosing:

- dataset,
- directory scope,
- scope type (`DIRECT` or `RECURSIVE`),
- annotator,
- requested count,
- optional batch size.

The assignment creation service picks eligible files, creates `assignment_items`, and updates assignment status and lease metadata.

### Step 7 — Prepare workspace and claim images

The worker prepares the staging workspace and creates the job payload for the assignment. Annotators work on the staged files, and `active_image_claims` ensures only one active owner exists per image globally.

This prevents overlapping ownership and duplicate work across annotators.

### Step 8 — Submit and validate output

When an annotator submits work:

- `annotation_submissions` records the result,
- file hash and raw content are stored,
- validation state is checked (`VALID`, `INVALID_SYNTAX`, `INVALID_SCHEMA`, `CONFLICT`),
- the worker uploads verified output back to the NAS or configured workspace target.

### Step 9 — Review and completion

Managers/admins can review submission state, approve or reject declarations, and close assignments after validation. `audit_events` keeps a full operational record of who did what and when.

---

## 7. Operational notes and expected workflows

### Roles

- `ADMIN` — full platform control and dataset/assignment administration.
- `MANAGER` — dataset and assignment management.
- `ANNOTATOR` — assignment execution and submission.

### Typical production flow

```text
Register dataset
  -> scan remote NAS
  -> review file catalog
  -> assign work to annotator
  -> worker prepares local workspace
  -> user annotates files
  -> submit output
  -> validate and upload
  -> manager approves completion
  -> state is closed and audited
```

### Useful health checks

```bash
cd web_page
docker compose ps

docker compose logs -f controller
docker compose logs -f sync_worker
```

The project also contains a diagnostic CLI:

```bash
cd web_page
docker compose exec controller python -m controller.app.cli doctor
```

---

## 8. Required configuration files

### `.env`

Use `.env.example` as the baseline and replace the example values with your own environment details.

The most important part for a new person is this section:

```dotenv
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=annotation_db
POSTGRES_USER=annotation_user
POSTGRES_PASSWORD=strong_password
DATABASE_URL=postgresql://annotation_user:strong_password@postgres:5432/annotation_db
```

What this means in plain English:

- `POSTGRES_USER` = the database username your app will log in with.
- `POSTGRES_PASSWORD` = the password for that database user.
- `POSTGRES_DB` = the database name the app should use.
- `DATABASE_URL` = the exact SQLAlchemy connection string built from the previous values.
- `postgres` in the URL is the Docker service name, not a local machine host name.

If you keep the default local Docker setup, these values are usually fine as-is, except that `POSTGRES_PASSWORD` must be changed to a real secure password.

For the NAS side, set values like:

```dotenv
NAS_STORAGE_TYPE=sftp
NAS_HOST=192.168.1.100
NAS_PORT=22
NAS_USER=nas_user
NAS_PASSWORD=nas_password
NAS_ROOT_PATH=/data

DEFAULT_REMOTE_NAME=annotation_nas
DATASET_ROOT=ai_data_collection
ANNOTATION_WORKSPACE_ROOT=annotation_workspaces
RCLONE_CONFIG_PATH=/secrets/rclone.conf
```

These tell the app where the remote dataset storage is and what workspace to use for assignment staging.

### `secrets/rclone.conf`

This file is used by rclone to connect to the NAS or remote storage. It should be configured for the remote you want the platform to scan and sync.

---

## 9. Recommended first-time checklist

Before pushing to GitHub or deploying to a shared environment, make sure you have:

- [ ] copied `.env.example` to `.env`
- [ ] set DB credentials and NAS endpoint values
- [ ] created `secrets/rclone.conf`
- [ ] started Docker services
- [ ] created the admin account
- [ ] registered at least one dataset
- [ ] ran a catalog scan
- [ ] created a test assignment
- [ ] checked logs for controller and worker health
- [ ] reviewed database tables and audit trail

---

## 10. Notes for GitHub

This project is best pushed to GitHub with:

- `.env` kept out of source control,
- `secrets/rclone.conf` kept out of source control,
- the `.env.example` template used as the safe reference for everyone else,
- clear setup instructions in this README,
- operational logs and health-check commands documented for review.

This README is intentionally written as a full project guide so reviewers can understand:

- how the services connect,
- what the database schema controls,
- how the workflow runs from dataset registration to annotation completion,
- what must be configured before the platform is usable.

---

## 11. Quick start summary

```bash
cd web_page
cp .env.example .env
# edit .env and secrets/rclone.conf
./run_web.sh

docker compose exec controller python -m controller.app.cli create-admin --username admin --password 'Admin@123'
```

Access:

- Dashboard: http://localhost:8080/dashboard
- API docs: http://localhost:8080/docs

This is the recommended local startup and review flow for the project before publishing or sharing it externally.

