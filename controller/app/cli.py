import argparse
import getpass
import sys
import shutil
from pathlib import Path
from sqlalchemy import text
from controller.app.database import SessionLocal, engine
from controller.app.models import User
from controller.app.auth import hash_password
from controller.app.config import settings


def create_admin():
    parser = argparse.ArgumentParser(description="Create initial administrator user")
    parser.add_argument("--username", help="Administrator username (default: admin)", default=None)
    parser.add_argument("--password", help="Administrator password (insecure: prefers interactive prompt)", default=None)
    args = parser.parse_args(sys.argv[2:])

    username = args.username
    if not username:
        prompt_user = input("Enter admin username [admin]: ").strip()
        username = prompt_user if prompt_user else "admin"

    password = args.password
    if not password:
        password = getpass.getpass("Enter admin password: ")
        confirm = getpass.getpass("Confirm admin password: ")
        if password != confirm:
            print("[ERROR] Passwords do not match.")
            sys.exit(1)

    if len(password) < 8:
        print("[ERROR] Password must be at least 8 characters long.")
        sys.exit(1)

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            print(f"[ERROR] User '{username}' already exists.")
            sys.exit(1)

        hashed = hash_password(password)
        admin_user = User(
            username=username,
            password_hash=hashed,
            role="ADMIN",
            is_active=True,
        )
        db.add(admin_user)
        db.commit()
        print(f"[SUCCESS] Administrator user '{username}' created successfully.")
    finally:
        db.close()


def doctor():
    print("=== Running System Diagnostics (Doctor) ===")
    all_ok = True

    # 1. Database connection check
    print("[1/5] Testing database connectivity...")
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1")).scalar()
            if result == 1:
                print("      Database connection: OK")
            else:
                print("      Database connection: UNEXPECTED RESULT")
                all_ok = False
    except Exception as exc:
        print(f"      [ERROR] Database connection failed: {exc}")
        all_ok = False

    # 2. Check essential tables exist
    print("[2/5] Checking schema tables...")
    try:
        with engine.connect() as conn:
            tables = [
                "users", "devices", "datasets", "dataset_directories",
                "dataset_files", "assignments", "assignment_items",
                "active_image_claims", "annotation_submissions",
                "background_declarations", "sync_jobs", "audit_events"
            ]
            for t in tables:
                # Portable check
                try:
                    conn.execute(text(f"SELECT 1 FROM {t} LIMIT 1"))
                    print(f"      Table {t}: OK")
                except Exception:
                    print(f"      [WARNING] Table {t} not found or uninitialized.")
                    all_ok = False
    except Exception as exc:
        print(f"      [ERROR] Schema verification failed: {exc}")
        all_ok = False

    # 3. Rclone binary check
    print("[3/5] Checking Rclone binary...")
    rclone_path = shutil.which(settings.RCLONE_BIN)
    if rclone_path:
        print(f"      Rclone found: {rclone_path}")
    else:
        print(f"      [WARNING] Rclone binary '{settings.RCLONE_BIN}' not found in PATH.")

    # 4. Rclone config check
    print("[4/5] Checking Rclone configuration file...")
    conf_path = Path(settings.RCLONE_CONFIG_PATH)
    if conf_path.exists():
        print(f"      Rclone config found: {conf_path}")
    else:
        print(f"      [NOTICE] Rclone config not found at {conf_path} (mock mode will be used for testing).")

    # 5. Configuration review
    print("[5/5] Reviewing active configuration...")
    cfg = settings.safe_dict()
    print(f"      App Environment:  {cfg.get('APP_ENV')}")
    print(f"      Public Base URL:  {cfg.get('PUBLIC_BASE_URL')}")
    print(f"      Default Remote:   {cfg.get('DEFAULT_REMOTE_NAME')}")
    print(f"      Dataset Root:     {cfg.get('DATASET_ROOT')}")
    print(f"      Workspace Root:   {cfg.get('ANNOTATION_WORKSPACE_ROOT')}")
    print(f"      Output Policy:    {cfg.get('ANNOTATION_OUTPUT_POLICY')}")

    print("===========================================")
    if all_ok:
        print("[RESULT] System diagnostics: ALL HEALTHY")
    else:
        print("[RESULT] System diagnostics: ISSUES DETECTED")


def create_user():
    parser = argparse.ArgumentParser(description="Create a user account")
    parser.add_argument("--username", required=True, help="Username")
    parser.add_argument("--password", help="Password (interactive prompt if omitted)")
    parser.add_argument("--role", default="ANNOTATOR", choices=["ADMIN", "MANAGER", "ANNOTATOR"], help="Role (default: ANNOTATOR)")
    args = parser.parse_args(sys.argv[2:])

    password = args.password
    if not password:
        password = getpass.getpass(f"Enter password for '{args.username}': ")
        confirm = getpass.getpass(f"Confirm password for '{args.username}': ")
        if password != confirm:
            print("[ERROR] Passwords do not match.")
            sys.exit(1)

    if len(password) < 8:
        print("[ERROR] Password must be at least 8 characters long.")
        sys.exit(1)

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == args.username).first()
        if existing:
            print(f"[ERROR] User '{args.username}' already exists.")
            sys.exit(1)

        hashed = hash_password(password)
        new_user = User(
            username=args.username,
            password_hash=hashed,
            role=args.role.upper(),
            is_active=True,
        )
        db.add(new_user)
        db.commit()
        print(f"[SUCCESS] User '{args.username}' with role '{args.role.upper()}' created successfully.")
    finally:
        db.close()


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m controller.app.cli [create-admin | create-user | doctor]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "create-admin":
        create_admin()
    elif cmd == "create-user":
        create_user()
    elif cmd == "doctor":
        doctor()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()

