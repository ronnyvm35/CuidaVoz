from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


DoseStatus = Literal["taken", "skipped", "pending"]


class User(BaseModel):
    user_id: str
    nombre: str
    cuidador_whatsapp: str = ""
    cuidador_telegram: str = ""
    timezone: str = "America/Mexico_City"


class Medication(BaseModel):
    user_id: str
    med_id: str
    nombre: str
    dosis: str
    horarios: list[str] = Field(default_factory=list)
    con_comida: bool = False


class DoseLog(BaseModel):
    user_id: str
    med_id: str
    timestamp: str
    status: DoseStatus
    intentos: int = 0
    confirmado_en: str | None = None


class CaregiverAlert(BaseModel):
    user_id: str
    timestamp: str
    mensaje: str
    canal: str = "telegram"
    enviado: bool = False


class LogDoseInput(BaseModel):
    user_id: str
    med_id: str
    status: DoseStatus


class ScheduleReminderInput(BaseModel):
    user_id: str
    med_id: str | None = None
    nombre: str | None = None
    dosis: str | None = None
    time: str
    con_comida: bool = False


class NotifyCaregiverInput(BaseModel):
    user_id: str
    message: str


class UserIdInput(BaseModel):
    user_id: str


class ToolResponse(BaseModel):
    ok: bool = True
    data: dict | list | str | None = None
    message: str | None = None
    logged_at: str | None = None
