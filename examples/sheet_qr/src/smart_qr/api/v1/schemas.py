from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from smart_qr.config.constants import (
    DEFAULT_QR_BACK,
    DEFAULT_QR_BORDER,
    DEFAULT_QR_BOX,
    DEFAULT_QR_ECC,
    DEFAULT_QR_FILL,
    DEFAULT_QR_LOGO_SCALE,
)


class ConfigCreate(BaseModel):
    worksheet_id: int
    worksheet_name: Optional[str] = None
    name: str = "SmartQR config"
    content_mode: str = "identify"
    form_base_url: Optional[str] = None
    content_column_ids: list[int] = Field(default_factory=list)
    content_column_titles: list[str] = Field(default_factory=list)
    template: Optional[str] = None
    form_label_overrides: dict[str, str] = Field(default_factory=dict)
    watched_column_ids: Optional[list[int]] = None
    target_column_id: int
    target_column_title: Optional[str] = None
    write_mode: str = "replace"
    code_type: str = "qr"
    qr_ecc: str = DEFAULT_QR_ECC
    qr_box_size: int = DEFAULT_QR_BOX
    qr_border: int = DEFAULT_QR_BORDER
    qr_fill_color: str = DEFAULT_QR_FILL
    qr_back_color: str = DEFAULT_QR_BACK
    qr_logo_scale: float = DEFAULT_QR_LOGO_SCALE
    attach_to_row: bool = False
    pause: bool = False
    runner_user_id: Optional[UUID] = None


class ConfigUpdate(BaseModel):
    """Editable fields — worksheet_id is immutable after create."""

    name: Optional[str] = None
    content_mode: Optional[str] = None
    form_base_url: Optional[str] = None
    content_column_ids: Optional[list[int]] = None
    content_column_titles: Optional[list[str]] = None
    watched_column_ids: Optional[list[int]] = None
    target_column_id: Optional[int] = None
    target_column_title: Optional[str] = None
    worksheet_name: Optional[str] = None
    write_mode: Optional[str] = None
    code_type: Optional[str] = None
    template: Optional[str] = None
    form_label_overrides: Optional[dict[str, str]] = None
    qr_box_size: Optional[int] = None
    qr_border: Optional[int] = None
    qr_fill_color: Optional[str] = None
    qr_back_color: Optional[str] = None
    qr_logo_scale: Optional[float] = None
    attach_to_row: Optional[bool] = None


class ConfigOut(BaseModel):
    id: UUID
    organization_id: UUID
    worksheet_id: int
    worksheet_name: Optional[str] = None
    name: str
    content_mode: str
    form_base_url: Optional[str]
    content_column_ids: list[int]
    content_column_titles: list[str]
    template: Optional[str]
    form_label_overrides: dict[str, str]
    watched_column_ids: list[int]
    target_column_id: int
    target_column_title: Optional[str] = None
    write_mode: str
    code_type: str
    qr_ecc: str
    qr_box_size: int
    qr_border: int
    qr_fill_color: str = DEFAULT_QR_FILL
    qr_back_color: str = DEFAULT_QR_BACK
    qr_logo_scale: float = DEFAULT_QR_LOGO_SCALE
    attach_to_row: bool = False
    pause: bool
    runner_user_id: Optional[UUID]
    has_logo: bool = False
    logo_filename: Optional[str] = None

    @classmethod
    def from_record(cls, r: Any) -> "ConfigOut":
        return cls(
            id=r.id,
            organization_id=r.organization_id,
            worksheet_id=r.worksheet_id,
            worksheet_name=getattr(r, "worksheet_name", None),
            name=r.name,
            content_mode=r.content_mode,
            form_base_url=r.form_base_url,
            content_column_ids=r.content_column_ids,
            content_column_titles=r.content_column_titles,
            template=r.template,
            form_label_overrides=r.form_label_overrides,
            watched_column_ids=r.watched_column_ids,
            target_column_id=r.target_column_id,
            target_column_title=getattr(r, "target_column_title", None),
            write_mode=r.write_mode,
            code_type=r.code_type,
            qr_ecc=r.qr_ecc,
            qr_box_size=r.qr_box_size,
            qr_border=r.qr_border,
            qr_fill_color=getattr(r, "qr_fill_color", None) or DEFAULT_QR_FILL,
            qr_back_color=getattr(r, "qr_back_color", None) or DEFAULT_QR_BACK,
            qr_logo_scale=float(
                getattr(r, "qr_logo_scale", None)
                if getattr(r, "qr_logo_scale", None) is not None
                else DEFAULT_QR_LOGO_SCALE
            ),
            attach_to_row=bool(getattr(r, "attach_to_row", False)),
            pause=r.pause,
            runner_user_id=r.runner_user_id,
            has_logo=bool(getattr(r, "has_logo", False)),
            logo_filename=getattr(r, "logo_filename", None),
        )


class GenerateRowRequest(BaseModel):
    row_id: int
    force: bool = True


class InjectEventRequest(BaseModel):
    organization_id: UUID
    config_id: UUID
    row_id: int
    changed_column_ids: list[int] = Field(default_factory=list)
    change_agent: Optional[str] = None
    force: bool = False


class EstimateBackfillRequest(BaseModel):
    row_ids: list[int] = Field(default_factory=list)


class BackfillRequest(BaseModel):
    row_ids: list[int] = Field(default_factory=list)
    force: bool = False
