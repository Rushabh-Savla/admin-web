# NAS Annotation Platform

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
web-app/
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

### Step 1 — Prepare the environment (.env configuration)

Follow these steps carefully to generate secure keys and configure `.env` before running the containers.

#### 1.1 Copy the template file
```bash
cd web-app
cp .env.example .env
```

#### 1.2 Generate `APP_SECRET_KEY` (64-character random key)
The application requires a 64-character cryptographically secure key for signing sessions and JWT tokens.
Generate it in your terminal:
```bash
openssl rand -hex 32
```
*(Or with Python: `python3 -c "import secrets; print(secrets.token_hex(32))"`)*

In `.env`, set:
```dotenv
APP_SECRET_KEY=<your_generated_64_character_hex_key>
```
*Example:* `APP_SECRET_KEY=9e4a8b72c6d5e1f0284719acb3d5e7f01248a6cb9e2d3f4a5b6c7d8e9f0a1b2c`

#### 1.3 Generate `POSTGRES_PASSWORD` and update `DATABASE_URL`
Generate a strong random password for PostgreSQL:
```bash
openssl rand -hex 16
```
*Example output:* `9a8f3b2c1d4e5f60718293a4b5c6d7e8`

In `.env`, set **both** `POSTGRES_PASSWORD` and the password in `DATABASE_URL` to the exact same generated string:
```dotenv
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=annotation_db
POSTGRES_USER=annotation_user
POSTGRES_PASSWORD=9a8f3b2c1d4e5f60718293a4b5c6d7e8
DATABASE_URL=postgresql://annotation_user:9a8f3b2c1d4e5f60718293a4b5c6d7e8@postgres:5432/annotation_db
```

> **Important Notes:**
> - The password inside `DATABASE_URL` (`postgresql://annotation_user:<password>@postgres:5432/annotation_db`) **must match** `POSTGRES_PASSWORD` exactly.
> - Generating a hexadecimal password via `openssl rand -hex 16` is strongly recommended because it uses characters `[0-9a-f]`, completely avoiding URL-encoding conflicts with special characters (like `@`, `:`, `/`, or `#`) in the connection string.

#### 1.4 Generate `BOOTSTRAP_ADMIN_TOKEN`
Generate a one-time random bootstrap token for administrative CLI setup:
```bash
openssl rand -hex 16
```
In `.env`, set:
```dotenv
BOOTSTRAP_ADMIN_TOKEN=<your_generated_token>
```

#### 1.5 Configure NAS storage connection details
Specify the connection details for the dataset storage NAS:
```dotenv
NAS_STORAGE_TYPE=sftp        # Protocol: sftp, smb, webdav, or local
NAS_HOST=192.168.1.100       # NAS hostname or IP address
NAS_PORT=22                  # Port (22 for SFTP, 445 for SMB, etc.)
NAS_USER=nas_user            # NAS login username
NAS_PASSWORD=nas_password    # NAS login password
NAS_KEY_FILE=                # Optional path to private SSH key (e.g. /secrets/id_rsa)
NAS_ROOT_PATH=/data          # Base root path on the NAS
```

#### 1.6 Ensure secrets directory exists
```bash
mkdir -p secrets
```
*(The platform will automatically generate `secrets/rclone.conf` on startup using the environment variables).*

---

### Step 2 — Configure & Verify Rclone (NAS Storage)

Before launching the Docker containers, configure how the background sync worker will communicate with your NAS storage.

You have two options:

#### Option A: Automatic Generation via `.env` (Fastest)
1. Ensure the `secrets/` directory exists on the host:
   ```bash
   mkdir -p secrets
   ```
2. When Docker Compose starts, the `controller` automatically reads `NAS_STORAGE_TYPE`, `NAS_HOST`, `NAS_PORT`, `NAS_USER`, and `NAS_PASSWORD` from `.env`, generates `secrets/rclone.conf`, and secures it with `0600` permissions.

#### Option B: Host Interactive Setup & Connectivity Verification (Recommended)
Configuring and testing Rclone directly on your host machine before starting Docker guarantees that network routes, firewall ports, and credentials work properly:

1. **Install Rclone on the host:**
   ```bash
   sudo apt update && sudo apt install -y rclone
   ```

2. **Run interactive setup:**
   ```bash
   rclone config
   ```
   - Remote Name: `annotation_nas` *(must match `DEFAULT_REMOTE_NAME` in `.env`)*
   - Storage Type: `sftp` *(or `smb`, `webdav`, etc.)*
   - Host, Port, User, and Password as prompted.

3. **Verify connectivity directly before running Docker:**
   ```bash
   rclone lsd annotation_nas:
   ```
   *(Or test a specific folder: `rclone lsd annotation_nas:/data`)*.
   If this lists directories without error, your NAS connection is working.

4. **Copy the verified configuration into the project:**
   ```bash
   mkdir -p secrets
   cp ~/.config/rclone/rclone.conf secrets/rclone.conf
   chmod 600 secrets/rclone.conf
   ```

---

### Step 3 — Start the platform with Docker Compose

Once `.env` and `secrets/` are ready, start the services:

```bash
cd web-app
docker compose up -d
```
*(or run `./run_web.sh`)*

#### Verify Container Status:
```bash
docker compose ps
```

Expected healthy output:
```text
NAME                     SERVICE       STATUS                    PORTS
annotation_controller    controller    Up (healthy)              0.0.0.0:8080->8000/tcp
annotation_postgres      postgres      Up (healthy)              127.0.0.1:54321->5432/tcp
annotation_sync_worker   sync_worker   Up                        8000/tcp
annotation_migrate       migrate       Exited (0) [Completed]    
```

Open:
- Web dashboard: http://localhost:8080/dashboard
- API docs: http://localhost:8080/docs

---

### Troubleshooting: `Container annotation_migrate Error (exit 1)` / Password Authentication Failed

If you run `docker compose up -d` and see:
```text
[+] up 2/2
 ✔ Container annotation_postgres Healthy                                      1.1s
 ✘ Container annotation_migrate  Error service "migrate" didn't complete successfully: exit 1
```
And `docker compose logs migrate` shows:
```text
psycopg.OperationalError: connection failed: connection to server at "172.23.0.2", port 5432 failed: FATAL:  password authentication failed for user "annotation_user"
```

#### Why does this happen?
1. **PostgreSQL Volume Persistence:** The PostgreSQL Docker container only evaluates `POSTGRES_USER` and `POSTGRES_PASSWORD` when creating a brand-new database cluster. If the Docker volume (`postgres_data`) was initialized previously, PostgreSQL logs:
   `PostgreSQL Database directory appears to contain a database; Skipping initialization`
   and retains the password that was set when the volume was first created. Changing `POSTGRES_PASSWORD` or `DATABASE_URL` in `.env` afterwards does **not** automatically update the existing password stored in the volume.
2. **Healthcheck False Positive:** The `annotation_postgres` container reports `Healthy` because its health check runs `pg_isready -U annotation_user -d annotation_db`, which checks if the PostgreSQL server is accepting socket connections without verifying passwords.
3. **Migration Failure:** When `annotation_migrate` runs `alembic upgrade head`, it connects over the Docker bridge network using the new `DATABASE_URL` from `.env`. PostgreSQL rejects the password mismatch, causing `migrate` to exit with code 1 and preventing `controller` and `sync_worker` from starting.

#### How to fix:

- **Solution 1 — Update user password in the existing database (Preserves existing data):**
  Run this command to synchronize PostgreSQL's internal password to match the `POSTGRES_PASSWORD` currently in your `.env` file:
  ```bash
  docker exec annotation_postgres psql -U annotation_user -d annotation_db -c "ALTER USER annotation_user WITH PASSWORD '<YOUR_POSTGRES_PASSWORD_FROM_ENV>';"
  ```
  *(Or via Docker Compose:* `docker compose exec postgres psql -U annotation_user -d annotation_db -c "ALTER USER annotation_user WITH PASSWORD '<YOUR_POSTGRES_PASSWORD_FROM_ENV>';"`*)*

  Then bring up the containers again:
  ```bash
  docker compose up -d
  ```
  The `migrate` container will now connect, run migrations, exit cleanly with code 0, and allow `controller` and `sync_worker` to start.

- **Solution 2 — Clean Volume Reset (Recommended for fresh setup):**
  If you do not have existing data you need to preserve and want a clean start with your new `.env` settings:
  ```bash
  # 1. Stop all containers and delete the old database volume
  docker compose down -v

  # 2. Re-create containers cleanly (PostgreSQL will initialize with your current .env password)
  docker compose up -d
  ```

---

### Step 4 — Create the first admin user

Once the containers are running, create the first administrator account.

```bash
cd web-app
docker compose exec controller python -m controller.app.cli create-admin --username admin --password 'Admin@123'
```

Then log in using the browser:

- Dashboard: http://localhost:8080/dashboard
- Username: `admin`
- Password: `Admin@123`

This creates the first administrator account. You can also create additional manager or annotator accounts with:

```bash
cd web-app
docker compose exec controller python -m controller.app.cli create-user --username annotator1 --password 'StrongPass123' --role ANNOTATOR
```

### Step 5 — Register a dataset

Use the dashboard or API to register a dataset with:

- dataset name,
- remote name,
- remote path,
- optional description.

This writes a row to `datasets` and prepares the system for a scan.

### Step 6 — Scan the dataset catalog

Trigger a `CATALOG_SCAN` job. The sync worker reads the configured remote path, filters by supported extensions, and updates:

- `dataset_directories`
- `dataset_files`
- `datasets.catalog_state`
- `datasets.current_catalog_version`

At this point, the platform knows what images are available and what is eligible for assignment.

### Step 7 — Create an assignment

A manager or admin creates an assignment by choosing:

- dataset,
- directory scope,
- scope type (`DIRECT` or `RECURSIVE`),
- annotator,
- requested count,
- optional batch size.

The assignment creation service picks eligible files, creates `assignment_items`, and updates assignment status and lease metadata.

### Step 8 — Prepare workspace and claim images

The worker prepares the staging workspace and creates the job payload for the assignment. Annotators work on the staged files, and `active_image_claims` ensures only one active owner exists per image globally.

This prevents overlapping ownership and duplicate work across annotators.

### Step 9 — Submit and validate output

When an annotator submits work:

- `annotation_submissions` records the result,
- file hash and raw content are stored,
- validation state is checked (`VALID`, `INVALID_SYNTAX`, `INVALID_SCHEMA`, `CONFLICT`),
- the worker uploads verified output back to the NAS or configured workspace target.

### Step 10 — Review and completion

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
cd web-app
docker compose ps

docker compose logs -f controller
docker compose logs -f sync_worker
```

The project also contains a diagnostic CLI:

```bash
cd web-app
docker compose exec controller python -m controller.app.cli doctor
```

---

## 8. Required configuration files

### `.env`

Use `.env.example` as the baseline (`cp .env.example .env`) and configure your values. Never commit `.env` to version control.

Key variables to configure:

#### 1. Security & Application Secrets
- `APP_SECRET_KEY` = 64-character cryptographically secure key used for signing session cookies and JWT tokens.
  - Generate via: `openssl rand -hex 32` (or Python: `secrets.token_hex(32)`)
- `BOOTSTRAP_ADMIN_TOKEN` = One-time secure admin bootstrap token (CLI only, invalidated upon admin creation).
  - Generate via: `openssl rand -hex 16`

#### 2. PostgreSQL Connection Settings
```dotenv
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=annotation_db
POSTGRES_USER=annotation_user
POSTGRES_PASSWORD=replace_with_strong_random_password
DATABASE_URL=postgresql://annotation_user:replace_with_strong_random_password@postgres:5432/annotation_db
```

What this means:
- `POSTGRES_USER` = The database username your app logs in with.
- `POSTGRES_PASSWORD` = The raw password for that database user. Generate via `openssl rand -hex 16`.
- `POSTGRES_DB` = The database name to use (default: `annotation_db`).
- `DATABASE_URL` = Full SQLAlchemy connection string: `postgresql://<user>:<password>@postgres:5432/<db>`.
- **Note:** The password in `DATABASE_URL` **must match** `POSTGRES_PASSWORD`. Using hexadecimal strings avoids URL-encoding issues with symbols.
- **Volume Note:** PostgreSQL initializes the database password only on the first run of the persistent Docker volume (`postgres_data`). If changing the password later, refer to the [Troubleshooting section](#troubleshooting-container-annotation_migrate-error-exit-1--password-authentication-failed) to synchronize the database user password.

#### 3. NAS & Storage Settings
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

These tell the platform how to connect to the dataset NAS and where to stage workspace files for annotation.

### `secrets/rclone.conf` & NAS Setup Guide

The background worker and catalog synchronization service use **Rclone** to communicate with the remote NAS storage.

#### 1. Install Rclone (Host Machine)
```bash
sudo apt update
sudo apt install rclone
```

#### 2. Configure Rclone Remote Interactively
Run the interactive configuration wizard:
```bash
rclone config
```

Follow the prompts step-by-step:
1. `n/s/q>`: Type `n` (New remote)
2. `name>`: Type `annotation_nas` *(Must match `DEFAULT_REMOTE_NAME` in `.env`)*
3. `Storage>`: Type `sftp` *(for SSH/SFTP storage)*
4. `host>`: Enter your NAS IP address or hostname (e.g. `192.168.1.100`)
5. `user>`: Enter your NAS SSH username (e.g. `Annotation` or `nas_user`)
6. `port>`: Press Enter to accept default `22` (or specify custom port)
7. `y/g/n>`: Type `y` to type in your password manually
8. `SSH password>`: Enter password, then confirm when prompted
9. `key_pem>`: Press Enter (leave blank)
10. `key_file>`: Press Enter (leave blank unless using an SSH private key)
11. `key_file_pass>`: Type `n`
12. `pubkey_file>`: Press Enter (leave blank)
13. `key_use_agent>`: Press Enter (default `false`)
14. `use_insecure_cipher>`: Press Enter (default `false`)
15. `disable_hashcheck>`: Press Enter (default `false`)
16. `Edit advanced config? (y/n)>`: Type `n`
17. `Keep this "annotation_nas" remote? (y/e/d)>`: Type `y`
18. `e/n/d/r/c/s/q>`: Type `q` to quit config

#### 3. Test NAS Connectivity
Verify that the remote NAS is accessible and can list directories:
```bash
rclone lsd annotation_nas:
```
*If your datasets are located in a subfolder like `/data`, verify with:*
```bash
rclone lsd annotation_nas:/data
```

#### 4. Link Configuration to the Docker Container
You have two straightforward options:

- **Option A (Automatic Generation via `.env` - Recommended):**
  When you set `NAS_STORAGE_TYPE=sftp`, `NAS_HOST`, `NAS_PORT`, `NAS_USER`, and `NAS_PASSWORD` in `web-app/.env`, the central controller automatically obscures the password and writes `secrets/rclone.conf` on startup with secure `600` permissions.
- **Option B (Copy from Host):**
  If you ran `rclone config` on the host, copy the generated file directly:
  ```bash
  mkdir -p secrets
  cp ~/.config/rclone/rclone.conf secrets/rclone.conf
  chmod 600 secrets/rclone.conf
  ```

#### 5. Background NAS Synchronization & Offline Resilience
- **Durable Job Queue:** File operations (`CATALOG_SCAN`, `PREPARE_ASSIGNMENT`, `UPLOAD_ANNOTATION`) are queued in PostgreSQL and executed asynchronously by `annotation_sync_worker`.
- **Automatic Retries:** If the NAS is temporarily disconnected or unreachable, transfers automatically back off exponentially (10s, 20s, 40s... up to 900s) and resume once connectivity is restored.
- **Non-Destructive Publishing:** The worker verifies file integrity before copying and never deletes or overwrites canonical source datasets on the NAS.

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
cd web-app
cp .env.example .env
# 1. Edit .env (generate keys and set passwords as described in Step 1)
# 2. Prepare secrets directory
mkdir -p secrets

# 3. (Optional) Configure & verify rclone on host, or let controller auto-generate it from .env
# cp ~/.config/rclone/rclone.conf secrets/rclone.conf && chmod 600 secrets/rclone.conf

# 4. Start Docker Compose services
docker compose up -d

# NOTE: If annotation_migrate fails with password authentication error on an existing volume:
# docker exec annotation_postgres psql -U annotation_user -d annotation_db -c "ALTER USER annotation_user WITH PASSWORD '<YOUR_POSTGRES_PASSWORD>';" && docker compose up -d

# 5. Bootstrap admin user
docker compose exec controller python -m controller.app.cli create-admin --username admin --password 'Admin@123'
```

Access:

- Dashboard: http://localhost:8080/dashboard
- API docs: http://localhost:8080/docs

This is the recommended local startup and review flow for the project before publishing or sharing it externally.

