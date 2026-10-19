from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, ConfigDict


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    username: str


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    old_password: str
    new_password: str


class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "ANNOTATOR"  # ADMIN, MANAGER, ANNOTATOR


class UserUpdate(BaseModel):
    is_active: Optional[bool] = None
    role: Optional[str] = None
    password: Optional[str] = None


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DeviceEnrollRequest(BaseModel):
    device_identifier: str
    name: str


class DeviceOut(BaseModel):
    id: int
    user_id: int
    device_identifier: str
    name: str
    is_active: bool
    last_seen_at: Optional[datetime]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class HeartbeatRequest(BaseModel):
    device_identifier: str
    active_assignment_id: Optional[int] = None
    active_batch_progress: Optional[Dict[str, Any]] = None


class HeartbeatResponse(BaseModel):
    status: str
    lease_expires_at: Optional[datetime]
    lease_generation: Optional[int]


class DatasetCreate(BaseModel):
    title: Optional[str] = None
    name: str
    description: Optional[str] = None
    assigned_under: Optional[str] = None
    project_status: str = "ACTIVE"
    remote_name: str
    remote_path: str


class DatasetOut(BaseModel):
    id: int
    title: Optional[str] = None
    name: str
    description: Optional[str] = None
    assigned_under: Optional[str] = None
    project_status: str = "ACTIVE"
    remote_name: str
    remote_path: str
    catalog_state: str
    current_catalog_version: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DatasetDirectoryOut(BaseModel):
    id: int
    dataset_id: int
    normalized_path: str
    parent_path: Optional[str]
    direct_image_count: int
    direct_json_count: int
    direct_txt_count: int
    direct_other_count: int
    recursive_image_count: int
    recursive_json_count: int
    recursive_txt_count: int
    recursive_other_count: int

    model_config = ConfigDict(from_attributes=True)


class AssignmentCreate(BaseModel):
    dataset_id: int
    directory_scope: str = ""
    scope_type: str = "RECURSIVE"  # DIRECT, RECURSIVE
    annotator_id: int
    requested_count: int = Field(gt=0)
    batch_size: Optional[int] = Field(default=None, gt=0)
    idempotency_key: Optional[str] = None


class AssignmentItemOut(BaseModel):
    id: int
    assignment_id: int
    file_id: int
    ordinal: int
    item_state: str
    error: Optional[str]
    file_path: Optional[str] = None
    file_size: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class AssignmentOut(BaseModel):
    id: int
    dataset_id: int
    directory_scope: str
    scope_type: str
    annotator_id: int
    creator_id: int
    requested_count: int
    allocated_count: int
    batch_size: Optional[int] = None
    status: str
    catalog_version: str
    workspace_path: Optional[str]
    lease_generation: int
    lease_expires_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AssignmentManifestItem(BaseModel):
    item_id: int
    file_id: int
    ordinal: int
    relative_path: str
    size: int
    fingerprint: Optional[str] = None


class AssignmentManifest(BaseModel):
    assignment_id: int
    catalog_version: str
    lease_generation: int
    total_count: int
    items: List[AssignmentManifestItem]


class AnnotationSubmissionRequest(BaseModel):
    assignment_id: int
    file_id: int
    ownership_generation: int
    idempotency_key: str
    content_hash: str
    annotation_json: Dict[str, Any]


class AnnotationSubmissionOut(BaseModel):
    id: int
    assignment_id: int
    file_id: int
    validation_state: str
    error: Optional[str]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BackgroundDeclarationRequest(BaseModel):
    assignment_id: int
    file_id: int
    reason: Optional[str] = "Background/negative image without labels"


class BulkBackgroundDeclarationRequest(BaseModel):
    assignment_id: int
    file_ids: List[int]
    reason: Optional[str] = "Background/negative image without labels"


class BackgroundApprovalRequest(BaseModel):
    approved: bool


class SyncJobOut(BaseModel):
    id: int
    job_type: str
    status: str
    payload: Dict[str, Any]
    attempts: int
    max_retries: int
    error: Optional[str]
    next_attempt_at: datetime
    claimed_worker: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AuditEventOut(BaseModel):
    id: int
    user_id: Optional[int]
    action: str
    resource_type: str
    resource_id: str
    details: Dict[str, Any]
    ip_address: Optional[str]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class HealthStatus(BaseModel):
    status: str = "ok"
    version: str = "2.0.0"


class ReadinessStatus(BaseModel):
    status: str
    database: str
    worker_queue: str
    rclone: str

