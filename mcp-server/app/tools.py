from __future__ import annotations

import logging
import os

from .models import CaregiverAlert, DoseLog, Medication, ToolResponse, course_window, medication_is_active
from .store import now_iso, store


logger = logging.getLogger(__name__)
MAX_PENDING_ATTEMPTS = 2


def _course_progress(user_id: str, med: Medication) -> dict:
    if not med.dias_tratamiento or not med.fecha_inicio or not med.fecha_fin:
        return {"tiene_curso": False}
    days_taken = {
        d.timestamp[:10]
        for d in store.doses.get(user_id, [])
        if d.med_id == med.med_id and d.status == "taken"
    }
    # días del curso que ya pasaron (incl. hoy) donde hubo al menos una toma
    active_days = sorted(
        d for d in days_taken if med.fecha_inicio <= d <= med.fecha_fin
    )
    return {
        "tiene_curso": True,
        "dias_tratamiento": med.dias_tratamiento,
        "fecha_inicio": med.fecha_inicio,
        "fecha_fin": med.fecha_fin,
        "dias_con_toma": len(active_days),
        "activo_hoy": medication_is_active(med),
        "completado": len(active_days) >= med.dias_tratamiento
        or (not medication_is_active(med) and len(active_days) > 0),
    }


def get_medications(user_id: str) -> ToolResponse:
    store.get_or_create_user(user_id)
    meds = store.list_medications(user_id)
    payload = []
    for m in meds:
        row = m.model_dump()
        row["curso"] = _course_progress(user_id, m)
        row["activo_hoy"] = medication_is_active(m)
        payload.append(row)
    activos = sum(1 for m in meds if medication_is_active(m))
    return ToolResponse(
        ok=True,
        data=payload,
        message=f"{activos} medicamento(s) activos hoy ({len(meds)} registrados)",
    )


def get_next_dose(user_id: str) -> ToolResponse:
    store.get_or_create_user(user_id)
    meds = [m for m in store.list_medications(user_id) if medication_is_active(m)]
    if not meds:
        return ToolResponse(
            ok=True,
            data=None,
            message="No hay medicamentos activos hoy (revisa si el curso de días ya terminó)",
        )

    next_med = sorted(meds, key=lambda m: (m.horarios[0] if m.horarios else "99:99"))[0]
    hora = next_med.horarios[0] if next_med.horarios else "sin horario"
    curso = _course_progress(user_id, next_med)
    msg = f"Próximo: {next_med.nombre} {next_med.dosis} a las {hora}"
    if curso.get("tiene_curso"):
        msg += f" (día {min(curso['dias_con_toma'] + 1, curso['dias_tratamiento'])} de {curso['dias_tratamiento']})"
    return ToolResponse(
        ok=True,
        data={
            "med_id": next_med.med_id,
            "nombre": next_med.nombre,
            "dosis": next_med.dosis,
            "hora": hora,
            "con_comida": next_med.con_comida,
            "curso": curso,
        },
        message=msg,
    )


def log_dose(user_id: str, med_id: str, status: str) -> ToolResponse:
    store.get_or_create_user(user_id)
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


def _apply_uso(med: Medication, dias_tratamiento: int | None, uso: str | None) -> None:
    """cotidiano borra el curso; temporal (o un número de días) lo define."""
    if uso == "cotidiano":
        med.dias_tratamiento = None
        med.fecha_inicio = None
        med.fecha_fin = None
        return
    if dias_tratamiento:
        inicio, fin = course_window(dias_tratamiento)
        med.dias_tratamiento = dias_tratamiento
        med.fecha_inicio = inicio
        med.fecha_fin = fin


def schedule_reminder(
    user_id: str,
    time: str,
    med_id: str | None = None,
    nombre: str | None = None,
    dosis: str | None = None,
    con_comida: bool = False,
    dias_tratamiento: int | None = None,
    uso: str | None = None,
) -> ToolResponse:
    store.get_or_create_user(user_id)

    if med_id:
        med = store.get_medication(user_id, med_id)
        if med is None:
            return ToolResponse(ok=False, message="Medicamento no encontrado")
        if time not in med.horarios:
            med.horarios.append(time)
            med.horarios.sort()
        _apply_uso(med, dias_tratamiento, uso)
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
            _apply_uso(existing, dias_tratamiento, uso)
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
            _apply_uso(med, dias_tratamiento, uso)
            store.upsert_medication(med)

    extra = ""
    if med.dias_tratamiento and med.fecha_fin:
        extra = f" por {med.dias_tratamiento} días (hasta {med.fecha_fin}), tratamiento temporal"
    elif uso == "cotidiano" or not med.dias_tratamiento:
        extra = ", de uso cotidiano"
    return ToolResponse(
        ok=True,
        data=med.model_dump(),
        message=f"Recordatorio de {med.nombre} a las {time}{extra} programado",
        logged_at=now_iso(),
    )


def notify_caregiver(user_id: str, message: str) -> ToolResponse:
    user = store.get_user(user_id)
    # Prioridad: Telegram del hogar (panel) > env global de Render
    destino = ""
    if user and user.cuidador_telegram:
        destino = user.cuidador_telegram.strip()
    if not destino:
        destino = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not destino:
        destino = "desconocido"

    enviado = False
    telegram_message_id = None

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = destino if destino != "desconocido" else ""

    if bot_token and chat_id:
        try:
            import httpx

            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            resp = httpx.post(
                url,
                json={"chat_id": chat_id, "text": message},
                timeout=10.0,
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("ok"):
                enviado = True
                telegram_message_id = str(payload.get("result", {}).get("message_id", ""))
            else:
                logger.warning("Telegram API not ok: %s", payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram send failed: %s", exc)

    alert = CaregiverAlert(
        user_id=user_id,
        timestamp=now_iso(),
        mensaje=message,
        canal="telegram",
        enviado=enviado,
    )
    store.add_alert(alert)
    return ToolResponse(
        ok=True,
        data={
            "alert": alert.model_dump(),
            "destino": str(destino),
            "telegram_message_id": telegram_message_id,
            "telegram_pending": not enviado,
        },
        message=(
            f"Alerta enviada al cuidador por Telegram ({destino})"
            if enviado
            else f"Alerta registrada para cuidador ({destino})"
        ),
        logged_at=alert.timestamp,
    )


def get_adherence_today(user_id: str) -> ToolResponse:
    store.get_or_create_user(user_id)
    meds = [m for m in store.list_medications(user_id) if medication_is_active(m)]
    doses = store.list_doses_today(user_id)
    taken = [d for d in doses if d.status == "taken"]
    pending = [d for d in doses if d.status == "pending"]
    skipped = [d for d in doses if d.status == "skipped"]

    expected = max(len([m for m in meds if m.horarios]), 1) if meds else 0
    taken_count = len({d.med_id for d in taken})
    cursos = [_course_progress(user_id, m) | {"nombre": m.nombre} for m in store.list_medications(user_id) if m.dias_tratamiento]

    return ToolResponse(
        ok=True,
        data={
            "expected": expected,
            "taken": taken_count,
            "pending": len(pending),
            "skipped": len(skipped),
            "doses": [d.model_dump() for d in doses],
            "cursos": cursos,
        },
        message=f"Llevas {taken_count} de {expected} hoy",
    )
