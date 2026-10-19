import os
from pathlib import Path
from typing import List, Set, Optional
from pydantic import BaseModel, Field


class Settings(BaseModel):
    # Application Environment
    APP_ENV: str = "production"
    APP_SECRET_KEY: str = Field(default_factory=lambda: os.getenv("APP_SECRET_KEY", "insecure-dev-secret-key-change-in-production"))
    PUBLIC_BASE_URL: str = Field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "http://localhost:8000"))
    ALLOWED_ORIGINS: List[str] = Field(default_factory=lambda: [x.strip() for x in os.getenv("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",") if x.strip()])

    # Database Settings
    POSTGRES_HOST: str = Field(default_factory=lambda: os.getenv("POSTGRES_HOST", "localhost"))
    POSTGRES_PORT: int = Field(default_factory=lambda: int(os.getenv("POSTGRES_PORT", "5432")))
    POSTGRES_DB: str = Field(default_factory=lambda: os.getenv("POSTGRES_DB", "annotation_db"))
    POSTGRES_USER: str = Field(default_factory=lambda: os.getenv("POSTGRES_USER", "annotation_user"))
    POSTGRES_PASSWORD: str = Field(default_factory=lambda: os.getenv("POSTGRES_PASSWORD", "annotation_pass"))
    DATABASE_URL: str = Field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            f"postgresql://{os.getenv('POSTGRES_USER', 'annotation_user')}:{os.getenv('POSTGRES_PASSWORD', 'annotation_pass')}@{os.getenv('POSTGRES_HOST', 'localhost')}:{os.getenv('POSTGRES_PORT', '5432')}/{os.getenv('POSTGRES_DB', 'annotation_db')}"
        )
    )

    # Security & Tokens
    BOOTSTRAP_ADMIN_TOKEN: str = Field(default_factory=lambda: os.getenv("BOOTSTRAP_ADMIN_TOKEN", ""))
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    SESSION_COOKIE_NAME: str = "nas_session"

    # Batch & Assignment Policies
    DEFAULT_BATCH_SIZE: int = Field(default_factory=lambda: int(os.getenv("DEFAULT_BATCH_SIZE", "100")))
    MAX_ASSIGNMENT_COUNT: int = Field(default_factory=lambda: int(os.getenv("MAX_ASSIGNMENT_COUNT", "10000")))
    HEARTBEAT_INTERVAL_SECONDS: int = Field(default_factory=lambda: int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "30")))
    LEASE_DURATION_SECONDS: int = Field(default_factory=lambda: int(os.getenv("LEASE_DURATION_SECONDS", "120")))
    LEASE_GRACE_SECONDS: int = Field(default_factory=lambda: int(os.getenv("LEASE_GRACE_SECONDS", "60")))

    # File Catalog
    SUPPORTED_IMAGE_EXTENSIONS: Set[str] = Field(
        default_factory=lambda: {
            "." + ext.strip().lower().lstrip(".")
            for ext in os.getenv("SUPPORTED_IMAGE_EXTENSIONS", "jpg,jpeg,png,bmp,webp,tif,tiff").split(",")
            if ext.strip()
        }
    )
    EXCLUDED_PATTERNS: List[str] = Field(
        default_factory=lambda: [p.strip() for p in os.getenv("EXCLUDED_PATTERNS", ".Trash/**,**/.Trash/**,**/.DS_Store,**/.thumbnails/**,**/tmp/**").split(",") if p.strip()]
    )

    # Storage Paths & Policies
    DEFAULT_REMOTE_NAME: str = Field(default_factory=lambda: os.getenv("DEFAULT_REMOTE_NAME", "annotation_nas"))
    DATASET_ROOT: str = Field(default_factory=lambda: os.getenv("DATASET_ROOT", "ai_data_collection"))
    ANNOTATION_WORKSPACE_ROOT: str = Field(default_factory=lambda: os.getenv("ANNOTATION_WORKSPACE_ROOT", "ai_annotation/workspaces"))
    ANNOTATION_OUTPUT_POLICY: str = Field(default_factory=lambda: os.getenv("ANNOTATION_OUTPUT_POLICY", "WORKSPACE"))

    # Rclone Transfer Settings
    RCLONE_CONFIG_PATH: str = Field(default_factory=lambda: os.getenv("RCLONE_CONFIG_PATH", "/secrets/rclone.conf"))
    RCLONE_BIN: str = Field(default_factory=lambda: os.getenv("RCLONE_BIN", "rclone"))
    RCLONE_TRANSFERS: int = Field(default_factory=lambda: int(os.getenv("RCLONE_TRANSFERS", "2")))
    RCLONE_CHECKERS: int = Field(default_factory=lambda: int(os.getenv("RCLONE_CHECKERS", "2")))
    RCLONE_SFTP_CONCURRENCY: int = Field(default_factory=lambda: int(os.getenv("RCLONE_SFTP_CONCURRENCY", "2")))
    RCLONE_BANDWIDTH: str = Field(default_factory=lambda: os.getenv("RCLONE_BANDWIDTH", "25M"))
    RCLONE_TIMEOUT: str = Field(default_factory=lambda: os.getenv("RCLONE_TIMEOUT", "30s"))
    RCLONE_CONNECT_TIMEOUT: str = Field(default_factory=lambda: os.getenv("RCLONE_CONNECT_TIMEOUT", "10s"))
    TRANSFER_TIMEOUT_SECONDS: int = Field(default_factory=lambda: int(os.getenv("TRANSFER_TIMEOUT_SECONDS", "1800")))

    # Storage & Upload Limits
    MAX_UPLOAD_SIZE_BYTES: int = Field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_SIZE_BYTES", str(10 * 1024 * 1024))))
    STAGING_MAX_DISK_PERCENT: int = Field(default_factory=lambda: int(os.getenv("STAGING_MAX_DISK_PERCENT", "90")))

    # NAS Connection Credentials (Configurable directly via .env)
    NAS_STORAGE_TYPE: str = Field(default_factory=lambda: os.getenv("NAS_STORAGE_TYPE", "sftp"))
    NAS_HOST: Optional[str] = Field(default_factory=lambda: os.getenv("NAS_HOST"))
    NAS_PORT: int = Field(default_factory=lambda: int(os.getenv("NAS_PORT", "22")))
    NAS_USER: Optional[str] = Field(default_factory=lambda: os.getenv("NAS_USER"))
    NAS_PASSWORD: Optional[str] = Field(default_factory=lambda: os.getenv("NAS_PASSWORD"))
    NAS_KEY_FILE: Optional[str] = Field(default_factory=lambda: os.getenv("NAS_KEY_FILE"))
    NAS_ROOT_PATH: str = Field(default_factory=lambda: os.getenv("NAS_ROOT_PATH", ""))

    def safe_dict(self) -> dict:
        """Return configuration dictionary with sensitive values redacted."""
        d = self.model_dump()
        redacted_keys = {"APP_SECRET_KEY", "POSTGRES_PASSWORD", "DATABASE_URL", "BOOTSTRAP_ADMIN_TOKEN", "NAS_PASSWORD"}
        for k in redacted_keys:
            if k in d and d[k]:
                d[k] = "******"
        # Convert set to list for JSON serialization
        if "SUPPORTED_IMAGE_EXTENSIONS" in d:
            d["SUPPORTED_IMAGE_EXTENSIONS"] = sorted(list(d["SUPPORTED_IMAGE_EXTENSIONS"]))
        return d


settings = Settings()

