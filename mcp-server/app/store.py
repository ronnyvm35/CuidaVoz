from __future__ import annotations

import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Lock
from uuid import uuid4

from .models import CaregiverAlert, DoseLog, Medication, User, phone_keys


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class InMemoryStore:
    """Almacén en memoria para el MVP. Interfaz lista para DynamoDB."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.users: dict[str, User] = {}
        self.medications: dict[str, list[Medication]] = {}
        self.doses: dict[str, list[DoseLog]] = {}
        self.alerts: dict[str, list[CaregiverAlert]] = {}
        # Celular normalizado → chat_id. Lo llena el bot cuando el cuidador comparte su contacto.
        self.telegram_by_phone: dict[str, str] = {}
        self.telegram_update_offset: int = 0
        self._seed()

    def _seed(self) -> None:
        user_id = "demo-user"
        self.users[user_id] = User(
            user_id=user_id,
            nombre="Juan",
            cuidador_telefono="",
            cuidador_whatsapp="",
            cuidador_telegram=os.getenv("TELEGRAM_CHAT_ID", ""),
            timezone="America/Mexico_City",
            guia_vista=True,
        )
        self.medications[user_id] = [
            Medication(
                user_id=user_id,
                med_id="med-losartan",
                nombre="Losartán",
                dosis="50 mg",
                forma="pastilla",
                cantidad="1 pastilla",
                intervalo_horas=24,
                horarios=["08:00"],
                con_comida=False,
                # crónico: sin curso (indefinido)
            ),
            Medication(
                user_id=user_id,
                med_id="med-amoxicilina",
                nombre="Amoxicilina",
                dosis="500 mg",
                forma="pastilla",
                cantidad="1 pastilla",
                intervalo_horas=12,
                horarios=["08:00", "20:00"],
                con_comida=True,
                dias_tratamiento=5,
                fecha_inicio=datetime.now(timezone.utc).date().isoformat(),
                fecha_fin=(datetime.now(timezone.utc).date() + timedelta(days=4)).isoformat(),
            ),
        ]
        self.doses[user_id] = []
        self.alerts[user_id] = []

    def get_user(self, user_id: str) -> User | None:
        return self.users.get(user_id)

    def upsert_user(self, user: User) -> User:
        with self._lock:
            self.users[user.user_id] = user
            self.medications.setdefault(user.user_id, [])
            self.doses.setdefault(user.user_id, [])
            self.alerts.setdefault(user.user_id, [])
            return deepcopy(user)

    def get_or_create_user(self, user_id: str, nombre: str = "Usuario") -> User:
        existing = self.get_user(user_id)
        if existing:
            return existing
        user = User(
            user_id=user_id,
            nombre=nombre,
            cuidador_telefono="",
            cuidador_telegram=os.getenv("TELEGRAM_CHAT_ID", ""),
            timezone="America/Mexico_City",
            guia_vista=False,
        )
        self.upsert_user(user)
        # Cada hogar empieza sin medicamentos. El seed de Losartán/Amoxicilina
        # queda solo en demo-user, para el panel de la demo.
        return deepcopy(user)

    def clear_doses_today(self, user_id: str) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            self.doses[user_id] = [
                d for d in self.doses.get(user_id, []) if not d.timestamp.startswith(today)
            ]

    def list_medications(self, user_id: str) -> list[Medication]:
        return deepcopy(self.medications.get(user_id, []))

    def get_medication(self, user_id: str, med_id: str) -> Medication | None:
        for med in self.medications.get(user_id, []):
            if med.med_id == med_id:
                return deepcopy(med)
        return None

    def find_medication_by_name(self, user_id: str, nombre: str) -> Medication | None:
        needle = nombre.strip().lower()
        for med in self.medications.get(user_id, []):
            if med.nombre.lower() == needle or needle in med.nombre.lower():
                return deepcopy(med)
        return None

    def upsert_medication(self, med: Medication) -> Medication:
        with self._lock:
            meds = self.medications.setdefault(med.user_id, [])
            for idx, existing in enumerate(meds):
                if existing.med_id == med.med_id:
                    meds[idx] = med
                    return deepcopy(med)
            meds.append(med)
            return deepcopy(med)

    def delete_medication(self, user_id: str, med_id: str) -> Medication | None:
        with self._lock:
            meds = self.medications.get(user_id, [])
            for idx, existing in enumerate(meds):
                if existing.med_id == med_id:
                    return deepcopy(meds.pop(idx))
        return None

    def add_dose(self, dose: DoseLog) -> DoseLog:
        with self._lock:
            self.doses.setdefault(dose.user_id, []).append(dose)
            return deepcopy(dose)

    def list_doses_today(self, user_id: str) -> list[DoseLog]:
        today = datetime.now(timezone.utc).date().isoformat()
        return [
            deepcopy(d)
            for d in self.doses.get(user_id, [])
            if d.timestamp.startswith(today)
        ]

    def pending_attempts(self, user_id: str, med_id: str) -> int:
        today_doses = [
            d
            for d in self.doses.get(user_id, [])
            if d.med_id == med_id and d.timestamp.startswith(datetime.now(timezone.utc).date().isoformat())
        ]
        if not today_doses:
            return 0
        return today_doses[-1].intentos

    def add_alert(self, alert: CaregiverAlert) -> CaregiverAlert:
        with self._lock:
            self.alerts.setdefault(alert.user_id, []).append(alert)
            return deepcopy(alert)

    def list_alerts(self, user_id: str) -> list[CaregiverAlert]:
        return deepcopy(self.alerts.get(user_id, []))

    def new_med_id(self) -> str:
        return f"med-{uuid4().hex[:8]}"

    def remember_telegram(self, phone_keys: set[str], chat_id: str) -> None:
        with self._lock:
            for key in phone_keys:
                if key:
                    self.telegram_by_phone[key] = chat_id

    def chat_for_phone(self, phone: str) -> str:
        for key in phone_keys(phone):
            found = self.telegram_by_phone.get(key)
            if found:
                return found
        return ""


store = InMemoryStore()
now_iso = _now_iso
