"""Data models for print jobs, queue status, and shop configuration."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    PENDING = "pending"
    PRINTING = "printing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ColorMode(str, Enum):
    BW = "bw"
    COLOR = "color"


class DuplexMode(str, Enum):
    SINGLE = "single"
    DUPLEX = "duplex"


class PrinterInfo(BaseModel):
    name: str
    status: str = "Ready"
    is_online: bool = True
    is_virtual: bool = False
    supports_color: bool = True
    supports_duplex: bool = False
    queued_jobs: int = 0
    is_exposed: bool = True


class PrintJob(BaseModel):
    id: str
    token: str  # 3-digit counter token e.g. "042"
    customer_secret: str  # Secret so only the uploader's browser can view/cancel
    filename: str
    file_ext: str
    source_path: str
    normalized_path: Optional[str] = None
    chunk_paths: list[str] = Field(default_factory=list)
    completed_chunks: int = 0
    total_pages: int = 1
    selected_pages_spec: str = "all"
    selected_page_count: int = 1
    color_mode: ColorMode = ColorMode.BW
    duplex_mode: DuplexMode = DuplexMode.SINGLE
    copies: int = 1
    estimated_price: float = 0.0
    currency: str = "₹"
    status: JobStatus = JobStatus.PENDING
    recommended_printer: Optional[str] = None
    assigned_printer: Optional[str] = None
    auto_fixes: list[str] = Field(default_factory=list)
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None


class ApproveJobRequest(BaseModel):
    printer_name: Optional[str] = None


class RerouteJobRequest(BaseModel):
    backup_printer: str


class OnboardingRequest(BaseModel):
    shop_name: str
    exposed_printers: list[str]
    bw_single: float = 2.0
    bw_duplex: float = 1.5
    color_single: float = 10.0
    color_duplex: float = 9.0
