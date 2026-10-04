from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field


DoseStatus = Literal["taken", "skipped", "pending"]
FormaMedicamento = Literal["pastilla", "jarabe", "inyeccion", "otro"]


class User(BaseModel):
    user_id: str
    nombre: str
    cuidador_telefono: str = ""
    cuidador_whatsapp: str = ""
    cuidador_telegram: str = ""
    timezone: str = "America/Mexico_City"
    # True después de la primera apertura de la skill (guía de instalación).
    guia_vista: bool = False


class Medication(BaseModel):
    user_id: str
    med_id: str
    nombre: str
    dosis: str
    horarios: list[str] = Field(default_factory=list)
    con_comida: bool = False
    # pastilla, jarabe, inyeccion u otro
    forma: str = ""
    # "1 pastilla", "5 ml", "1 ampolla"
    cantidad: str = ""
    # 6, 8, 12 o 24. None = solo los horarios guardados.
    intervalo_horas: int | None = None
    # Curso de tratamiento. None = indefinido.
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
    canal: str = "sms"
    destino: str = ""
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
    # cotidiano = indefinido; temporal = curso de N días
    uso: str | None = None
    forma: str = ""
    cantidad: str = ""
    intervalo_horas: int | None = None


class NotifyCaregiverInput(BaseModel):
    user_id: str
    message: str


class UserIdInput(BaseModel):
    user_id: str


class SetupHouseholdInput(BaseModel):
    user_id: str = "demo-user"
    nombre: str
    cuidador_telefono: str = ""
    cuidador_telegram: str = ""
    timezone: str = "America/Mexico_City"


class ConfigureHomeInput(BaseModel):
    user_id: str
    nombre: str | None = None
    cuidador_telefono: str | None = None


class RemoveMedicationInput(BaseModel):
    user_id: str = "demo-user"
    med_id: str | None = None
    nombre: str | None = None


class AddMedicationInput(BaseModel):
    user_id: str = "demo-user"
    nombre: str
    dosis: str = ""
    time: str = "08:00"
    con_comida: bool = False
    dias_tratamiento: int | None = None
    uso: str | None = None
    forma: str = ""
    cantidad: str = ""
    intervalo_horas: int | None = None


class ToolResponse(BaseModel):
    ok: bool = True
    data: dict | list | str | None = None
    message: str | None = None
    logged_at: str | None = None


def phone_keys(raw: str) -> set[str]:
    """Claves para empatar un celular de México con el que comparte Telegram."""
    phone = normalize_phone(raw)
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    keys: set[str] = set()
    if phone:
        keys.add(phone)
    if len(digits) >= 10:
        keys.add(digits[-10:])
    if digits.startswith("521") and len(digits) >= 13:
        keys.add("+52" + digits[3:13])
        keys.add(digits[3:13])
    return keys


def normalize_phone(raw: str) -> str:
    """Deja el celular en formato +lada. Un número de 10 dígitos se asume México (+52)."""
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("00"):
        text = "+" + text[2:]
    has_plus = text.startswith("+")
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) < 8:
        return ""
    if has_plus or (digits.startswith("52") and len(digits) >= 12):
        return "+" + digits
    if len(digits) == 10:
        return "+52" + digits
    return "+" + digits


def horarios_por_intervalo(hora_inicio: str, intervalo: int | None) -> list[str]:
    """Genera las tomas del día a partir de la primera hora y el intervalo."""
    hora = hora_inicio if _valid_hora(hora_inicio) else "08:00"
    if not intervalo or intervalo >= 24 or intervalo <= 0:
        return [hora]
    step = intervalo * 60
    hh, mm = (int(part) for part in hora.split(":"))
    cursor = hh * 60 + mm
    times: list[str] = []
    seen: set[int] = set()
    while cursor % (24 * 60) not in seen:
        minute = cursor % (24 * 60)
        seen.add(minute)
        times.append(f"{minute // 60:02d}:{minute % 60:02d}")
        cursor += step
    return times


def _valid_hora(value: str) -> bool:
    parts = (value or "").split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return False
    hh, mm = int(parts[0]), int(parts[1])
    return 0 <= hh <= 23 and 0 <= mm <= 59


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
