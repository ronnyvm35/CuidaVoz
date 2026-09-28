from __future__ import annotations

import logging
import os

from .models import CaregiverAlert, DoseLog, Medication, ToolResponse
from .store import now_iso, store


logger = logging.getLogger(__name__)
MAX_PENDING_ATTEMPTS = 2


def get_medications(user_id: str) -> ToolResponse:
    meds = store.list_medications(user_id)
    return ToolResponse(
        ok=True,
        data=[m.model_dump() for m in meds],
        message=f"{len(meds)} medicamento(s) activos",
    )


def get_next_dose(user_id: str) -> ToolResponse:
    meds = store.list_medications(user_id)
    if not meds:
        return ToolResponse(ok=True, data=None, message="No hay medicamentos registrados")

    # MVP: el primero con horario definido; luego se mejora con timezone real
    next_med = sorted(meds, key=lambda m: (m.horarios[0] if m.horarios else "99:99"))[0]
    hora = next_med.horarios[0] if next_med.horarios else "sin horario"
    return ToolResponse(
        ok=True,
        data={
            "med_id": next_med.med_id,
            "nombre": next_med.nombre,
            "dosis": next_med.dosis,
            "hora": hora,
            "con_comida": next_med.con_comida,
        },
        message=f"Próximo: {next_med.nombre} {next_med.dosis} a las {hora}",
    )


def log_dose(user_id: str, med_id: str, status: str) -> ToolResponse:
    med = store.get_medication(user_id, med_id)
    if med is None:
        # permitir log por nombre corto en demo
        med = store.find_medication_by_name(user_id, med_id)
        if med is None and store.list_medications(user_id):
            med = store.list_medications(user_id)[0]
            med_id = med.med_id
        elif med is not None:
            med_id = med.med_id
        else:
            return ToolResponse(ok=False, message="Medicamento no encontrado")

    previous = store.pending_attempts(user_id, med_id)
    intentos = previous + 1 if status == "pending" else previous
    if status == "taken":
        intentos = max(previous, 1)

    logged_at = now_iso()
    dose = DoseLog(
        user_id=user_id,
        med_id=med_id,
        timestamp=logged_at,
        status=status,  # type: ignore[arg-type]
        intentos=intentos,
        confirmado_en=logged_at if status == "taken" else None,
    )
    store.add_dose(dose)

    caregiver_notified = False
    if status == "pending" and intentos >= MAX_PENDING_ATTEMPTS:
        user = store.get_user(user_id)
        nombre = user.nombre if user else user_id
        med_label = f"{med.nombre} {med.dosis}" if med else med_id
        notify = notify_caregiver(
            user_id,
            f"{nombre} no confirmó su {med_label}. ¿Llamarlo?",
        )
        caregiver_notified = notify.ok

    return ToolResponse(
        ok=True,
        logged_at=logged_at,
        data={
            "med_id": med_id,
            "status": status,
            "intentos": intentos,
            "caregiver_notified": caregiver_notified,
            "medication": med.model_dump() if med else None,
        },
        message="Dosis registrada",
    )


def schedule_reminder(
    user_id: str,
    time: str,
    med_id: str | None = None,
    nombre: str | None = None,
    dosis: str | None = None,
    con_comida: bool = False,
) -> ToolResponse:
    if med_id:
        med = store.get_medication(user_id, med_id)
        if med is None:
            return ToolResponse(ok=False, message="Medicamento no encontrado")
        if time not in med.horarios:
            med.horarios.append(time)
            med.horarios.sort()
        store.upsert_medication(med)
    else:
        if not nombre:
            return ToolResponse(ok=False, message="Falta nombre del medicamento")
        existing = store.find_medication_by_name(user_id, nombre)
        if existing:
            if time not in existing.horarios:
                existing.horarios.append(time)
                existing.horarios.sort()
            if dosis:
                existing.dosis = dosis
            store.upsert_medication(existing)
            med = existing
        else:
            med = Medication(
                user_id=user_id,
                med_id=store.new_med_id(),
                nombre=nombre,
                dosis=dosis or "",
                horarios=[time],
                con_comida=con_comida,
            )
            store.upsert_medication(med)

    return ToolResponse(
        ok=True,
        data=med.model_dump(),
        message=f"Recordatorio de {med.nombre} a las {time} programado",
        logged_at=now_iso(),
    )


def notify_caregiver(user_id: str, message: str) -> ToolResponse:
    user = store.get_user(user_id)
    destino = user.cuidador_whatsapp if user else "desconocido"
    enviado = False
    twilio_sid = None

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_whatsapp = os.getenv("TWILIO_WHATSAPP_FROM")
    to_whatsapp = os.getenv("TWILIO_WHATSAPP_TO", destino)

    if account_sid and auth_token and from_whatsapp and to_whatsapp:
        try:
            from twilio.rest import Client

            client = Client(account_sid, auth_token)
            to_number = to_whatsapp if to_whatsapp.startswith("whatsapp:") else f"whatsapp:{to_whatsapp}"
            msg = client.messages.create(
                body=message,
                from_=from_whatsapp,
                to=to_number,
            )
            enviado = True
            twilio_sid = msg.sid
        except Exception as exc:  # noqa: BLE001
            logger.warning("Twilio send failed: %s", exc)

    alert = CaregiverAlert(
        user_id=user_id,
        timestamp=now_iso(),
        mensaje=message,
        canal="whatsapp",
        enviado=enviado,
    )
    store.add_alert(alert)
    return ToolResponse(
        ok=True,
        data={
            "alert": alert.model_dump(),
            "destino": destino,
            "twilio_sid": twilio_sid,
            "twilio_pending": not enviado,
        },
        message=(
            f"Alerta enviada al cuidador ({destino})"
            if enviado
            else f"Alerta registrada para cuidador ({destino})"
        ),
        logged_at=alert.timestamp,
    )


def get_adherence_today(user_id: str) -> ToolResponse:
    meds = store.list_medications(user_id)
    doses = store.list_doses_today(user_id)
    taken = [d for d in doses if d.status == "taken"]
    pending = [d for d in doses if d.status == "pending"]
    skipped = [d for d in doses if d.status == "skipped"]

    # MVP: 1 dosis esperada por medicamento con al menos un horario
    expected = max(len([m for m in meds if m.horarios]), 1) if meds else 0
    taken_count = len({d.med_id for d in taken})

    return ToolResponse(
        ok=True,
        data={
            "expected": expected,
            "taken": taken_count,
            "pending": len(pending),
            "skipped": len(skipped),
            "doses": [d.model_dump() for d in doses],
        },
        message=f"Llevas {taken_count} de {expected} hoy",
    )
