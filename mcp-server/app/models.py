from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    # Curso de tratamiento (ej. antibiótico 5 días)
    dias_tratamiento: int | None = None
    fecha_inicio: str | None = None  # YYYY-MM-DD
    fecha_fin: str | None = None  # YYYY-MM-DD inclusive


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
    dias_tratamiento: int | None = None


class NotifyCaregiverInput(BaseModel):
    user_id: str
    message: str


class UserIdInput(BaseModel):
    user_id: str


class SetupHouseholdInput(BaseModel):
    user_id: str = "demo-user"
    nombre: str
    cuidador_telegram: str
    timezone: str = "America/Mexico_City"


class AddMedicationInput(BaseModel):
    user_id: str = "demo-user"
    nombre: str
    dosis: str = ""
    time: str = "08:00"
    con_comida: bool = False
    dias_tratamiento: int | None = 5


class ToolResponse(BaseModel):
    ok: bool = True
    data: dict | list | str | None = None
    message: str | None = None
    logged_at: str | None = None


def course_window(dias: int | None, inicio: str | None = None) -> tuple[str | None, str | None]:
    """Calcula fecha_inicio / fecha_fin para un curso de N días."""
    if not dias or dias < 1:
        return None, None
    start = datetime.strptime(inicio, "%Y-%m-%d").date() if inicio else datetime.now(timezone.utc).date()
    end = start + timedelta(days=dias - 1)
    return start.isoformat(), end.isoformat()


def medication_is_active(med: Medication, on_date: str | None = None) -> bool:
    """True si el medicamento aplica hoy (sin curso = siempre activo)."""
    if not med.fecha_inicio or not med.fecha_fin:
        return True
    day = on_date or datetime.now(timezone.utc).date().isoformat()
    return med.fecha_inicio <= day <= med.fecha_fin
