from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    Index,
    JSON,
)
from sqlalchemy.orm import relationship, Mapped, mapped_column
from controller.app.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


PK_TYPE = BigInteger().with_variant(Integer, "sqlite")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="ANNOTATOR")  # ADMIN, MANAGER, ANNOTATOR
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    devices: Mapped[List["Device"]] = relationship("Device", back_populates="user", cascade="all, delete-orphan")
    assignments: Mapped[List["Assignment"]] = relationship("Assignment", back_populates="annotator", foreign_keys="[Assignment.annotator_id]")


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    device_identifier: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="devices")


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    assigned_under: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    project_status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)
    remote_name: Mapped[str] = mapped_column(String(64), nullable=False)
    remote_path: Mapped[str] = mapped_column(Text, nullable=False)
    catalog_state: Mapped[str] = mapped_column(String(32), default="IDLE", nullable=False)  # IDLE, SCANNING, READY, ERROR
    current_catalog_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    directories: Mapped[List["DatasetDirectory"]] = relationship("DatasetDirectory", back_populates="dataset", cascade="all, delete-orphan")
    files: Mapped[List["DatasetFile"]] = relationship("DatasetFile", back_populates="dataset", cascade="all, delete-orphan")
    assignments: Mapped[List["Assignment"]] = relationship("Assignment", back_populates="dataset", order_by="desc(Assignment.id)")


class DatasetDirectory(Base):
    __tablename__ = "dataset_directories"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    normalized_path: Mapped[str] = mapped_column(Text, nullable=False)
    parent_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    direct_image_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    direct_json_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    direct_txt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    direct_other_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    recursive_image_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recursive_json_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recursive_txt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recursive_other_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    dataset: Mapped["Dataset"] = relationship("Dataset", back_populates="directories")

    __table_args__ = (
        UniqueConstraint("dataset_id", "normalized_path", name="uq_dataset_dir_path"),
        Index("idx_dataset_dir_parent", "dataset_id", "parent_path"),
    )


class DatasetFile(Base):
    __tablename__ = "dataset_files"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    normalized_path: Mapped[str] = mapped_column(Text, nullable=False)
    basename: Mapped[str] = mapped_column(String(255), nullable=False)
    extension: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)  # IMAGE, JSON, TXT, XML, OTHER
    size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    remote_mtime: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    catalog_version: Mapped[str] = mapped_column(String(64), nullable=False)
    file_state: Mapped[str] = mapped_column(String(32), default="ELIGIBLE", nullable=False)  # ELIGIBLE, EXCLUDED, MISSING, CHANGED
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    dataset: Mapped["Dataset"] = relationship("Dataset", back_populates="files")
    active_claim: Mapped[Optional["ActiveImageClaim"]] = relationship("ActiveImageClaim", back_populates="file", uselist=False)

    __table_args__ = (
        UniqueConstraint("dataset_id", "normalized_path", name="uq_dataset_file_path"),
        Index("idx_dataset_file_lookup", "dataset_id", "category", "file_state"),
        Index("idx_dataset_file_cat_ver", "dataset_id", "catalog_version"),
    )


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("datasets.id", ondelete="RESTRICT"), nullable=False)
    directory_scope: Mapped[str] = mapped_column(Text, nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), default="RECURSIVE", nullable=False)  # DIRECT, RECURSIVE
    annotator_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    creator_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False)
    allocated_count: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_size: Mapped[int] = mapped_column(Integer, default=100, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="PREPARING", nullable=False)  # PREPARING, READY, IN_PROGRESS, PAUSED, COMPLETED, CANCELLED
    catalog_version: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lease_generation: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    dataset: Mapped["Dataset"] = relationship("Dataset", back_populates="assignments")
    annotator: Mapped["User"] = relationship("User", foreign_keys=[annotator_id], back_populates="assignments")
    creator: Mapped["User"] = relationship("User", foreign_keys=[creator_id])
    items: Mapped[List["AssignmentItem"]] = relationship("AssignmentItem", back_populates="assignment", cascade="all, delete-orphan")


class AssignmentItem(Base):
    __tablename__ = "assignment_items"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    assignment_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False)
    file_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("dataset_files.id", ondelete="RESTRICT"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    item_state: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    assignment: Mapped["Assignment"] = relationship("Assignment", back_populates="items")
    file: Mapped["DatasetFile"] = relationship("DatasetFile")

    __table_args__ = (
        UniqueConstraint("assignment_id", "file_id", name="uq_assignment_item_file"),
        UniqueConstraint("assignment_id", "ordinal", name="uq_assignment_item_ordinal"),
        Index("idx_assignment_items_state", "assignment_id", "item_state"),
    )


class ActiveImageClaim(Base):
    """
    CRITICAL DATABASE CONSTRAINT:
    Primary key is file_id. At most ONE active claim can exist globally for any image file.
    """
    __tablename__ = "active_image_claims"

    file_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("dataset_files.id", ondelete="CASCADE"), primary_key=True)
    assignment_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False)
    annotator_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    claim_generation: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    file: Mapped["DatasetFile"] = relationship("DatasetFile", back_populates="active_claim")
    assignment: Mapped["Assignment"] = relationship("Assignment")
    annotator: Mapped["User"] = relationship("User")


class AnnotationSubmission(Base):
    __tablename__ = "annotation_submissions"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    assignment_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("assignments.id", ondelete="RESTRICT"), nullable=False)
    file_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("dataset_files.id", ondelete="RESTRICT"), nullable=False)
    annotator_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    ownership_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    validation_state: Mapped[str] = mapped_column(String(32), nullable=False)  # VALID, INVALID_SYNTAX, INVALID_SCHEMA, CONFLICT
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class BackgroundDeclaration(Base):
    __tablename__ = "background_declarations"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    assignment_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("assignments.id", ondelete="RESTRICT"), nullable=False)
    file_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("dataset_files.id", ondelete="RESTRICT"), nullable=False)
    annotator_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    approval_state: Mapped[str] = mapped_column(String(32), default="PENDING_APPROVAL", nullable=False)  # PENDING_APPROVAL, APPROVED, REJECTED
    approved_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class SyncJob(Base):
    """
    PostgreSQL-backed durable work queue for background transfers.
    Atomic claiming via SELECT FOR UPDATE SKIP LOCKED.
    """
    __tablename__ = "sync_jobs"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)  # CATALOG_SCAN, PREPARE_ASSIGNMENT, UPLOAD_ANNOTATION, CLEANUP_EXPIRED
    status: Mapped[str] = mapped_column(String(32), default="QUEUED", nullable=False)  # QUEUED, CLAIMED, RETRYING, FAILED, COMPLETED
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    claimed_worker: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        Index("idx_sync_jobs_queue", "status", "next_attempt_at"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
