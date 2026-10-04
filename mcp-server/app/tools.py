from __future__ import annotations

import logging
import os

from .models import (
    CaregiverAlert,
    DoseLog,
    Medication,
    ToolResponse,
    course_window,
    horarios_por_intervalo,
    medication_is_active,
    normalize_phone,
    phone_keys,
)
from .store import now_iso, store


logger = logging.getLogger(__name__)
MAX_PENDING_ATTEMPTS = 2
_INTERVALOS = {4, 6, 8, 12, 24}


def medication_label(med: Medication) -> str:
    if med.cantidad and med.dosis:
        return f"{med.cantidad} de {med.nombre} de {med.dosis}"
    if med.cantidad:
        return f"{med.cantidad} de {med.nombre}"
    if med.dosis:
        return f"{med.nombre} {med.dosis}"
    return med.nombre


def _freq_text(med: Medication) -> str:
    horas = ", ".join(med.horarios) if med.horarios else "sin hora"
    if med.intervalo_horas and med.intervalo_horas < 24:
        return f"cada {med.intervalo_horas} horas ({horas})"
    if med.horarios:
        return f"a las {horas}"
    return "sin horario"


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
    msg = f"Próximo: {medication_label(next_med)}, {_freq_text(next_med)}"
    if curso.get("tiene_curso"):
        msg += f" (día {min(curso['dias_con_toma'] + 1, curso['dias_tratamiento'])} de {curso['dias_tratamiento']})"
    elif not next_med.dias_tratamiento:
        msg += " (tratamiento indefinido)"
    return ToolResponse(
        ok=True,
        data={
            "med_id": next_med.med_id,
            "nombre": next_med.nombre,
            "dosis": next_med.dosis,
            "forma": next_med.forma,
            "cantidad": next_med.cantidad,
            "intervalo_horas": next_med.intervalo_horas,
            "hora": hora,
            "horarios": next_med.horarios,
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
        med_label = medication_label(med) if med else med_id
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


def _apply_presentacion(
    med: Medication,
    forma: str | None,
    cantidad: str | None,
    intervalo_horas: int | None,
    time: str,
) -> None:
    if forma:
        med.forma = forma.strip().lower()
    if cantidad:
        med.cantidad = cantidad.strip()
    if intervalo_horas in _INTERVALOS:
        med.intervalo_horas = intervalo_horas
        med.horarios = horarios_por_intervalo(time, intervalo_horas)
        return
    if time and time not in med.horarios:
        med.horarios.append(time)
        med.horarios.sort()


def schedule_reminder(
    user_id: str,
    time: str,
    med_id: str | None = None,
    nombre: str | None = None,
    dosis: str | None = None,
    con_comida: bool = False,
    dias_tratamiento: int | None = None,
    uso: str | None = None,
    forma: str | None = None,
    cantidad: str | None = None,
    intervalo_horas: int | None = None,
) -> ToolResponse:
    store.get_or_create_user(user_id)

    if med_id:
        med = store.get_medication(user_id, med_id)
        if med is None:
            return ToolResponse(ok=False, message="Medicamento no encontrado")
        if dosis:
            med.dosis = dosis
        _apply_presentacion(med, forma, cantidad, intervalo_horas, time)
        _apply_uso(med, dias_tratamiento, uso)
        store.upsert_medication(med)
    else:
        if not nombre:
            return ToolResponse(ok=False, message="Falta nombre del medicamento")
        existing = store.find_medication_by_name(user_id, nombre)
        if existing:
            if dosis:
                existing.dosis = dosis
            _apply_presentacion(existing, forma, cantidad, intervalo_horas, time)
            _apply_uso(existing, dias_tratamiento, uso)
            store.upsert_medication(existing)
            med = existing
        else:
            med = Medication(
                user_id=user_id,
                med_id=store.new_med_id(),
                nombre=nombre,
                dosis=dosis or "",
                horarios=[],
                con_comida=con_comida,
                forma=(forma or "").strip().lower(),
                cantidad=(cantidad or "").strip(),
            )
            _apply_presentacion(med, forma, cantidad, intervalo_horas, time)
            _apply_uso(med, dias_tratamiento, uso)
            store.upsert_medication(med)

    if med.dias_tratamiento and med.fecha_fin:
        curso = f"por {med.dias_tratamiento} días, hasta {med.fecha_fin}"
    else:
        curso = "tratamiento indefinido"
    return ToolResponse(
        ok=True,
        data=med.model_dump(),
        message=f"Recordatorio de {medication_label(med)}, {_freq_text(med)}, {curso}",
        logged_at=now_iso(),
    )


def _bot_token() -> str:
    return (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()


def _bot_username() -> str:
    return (os.getenv("TELEGRAM_BOT_USERNAME") or "CuidaVoz_bot").strip().lstrip("@")


def _send_telegram(chat_id: str, message: str, reply_markup: dict | None = None) -> str | None:
    token = _bot_token()
    if not (token and chat_id):
        return None
    body: dict = {"chat_id": chat_id, "text": message}
    if reply_markup:
        body["reply_markup"] = reply_markup
    try:
        import httpx

        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=body,
            timeout=10.0,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("ok"):
            return str(payload.get("result", {}).get("message_id", ""))
        logger.warning("Telegram API not ok: %s", payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram send failed: %s", exc)
    return None


def _ask_share_contact(chat_id: str) -> None:
    _send_telegram(
        chat_id,
        "Hola, soy CuidaVoz. Toca el botón y comparte tu celular para recibir las alertas. "
        "No necesitas buscar un chat id.",
        {
            "keyboard": [[{"text": "Compartir mi celular", "request_contact": True}]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        },
    )


def _bind_user_telegram(user) -> str:
    """Si el celular ya compartió contacto con el bot, guarda su chat id."""
    if not user or not user.cuidador_telefono:
        return user.cuidador_telegram if user else ""
    chat_id = store.chat_for_phone(user.cuidador_telefono)
    if chat_id:
        user.cuidador_telegram = chat_id
        store.upsert_user(user)
    return chat_id


def sync_telegram_links() -> ToolResponse:
    """Lee getUpdates y asocia el celular que el cuidador comparte con su chat id."""
    token = _bot_token()
    if not token:
        return ToolResponse(ok=False, message="Falta TELEGRAM_BOT_TOKEN en el servidor")
    body: dict = {"timeout": 0, "allowed_updates": ["message"]}
    if store.telegram_update_offset:
        body["offset"] = store.telegram_update_offset
    try:
        import httpx

        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/getUpdates",
            json=body,
            timeout=15.0,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram getUpdates failed: %s", exc)
        return ToolResponse(ok=False, message="No pude consultar Telegram")
    if not payload.get("ok"):
        return ToolResponse(ok=False, message="Telegram no aceptó la consulta")

    linked: list[str] = []
    for update in payload.get("result") or []:
        store.telegram_update_offset = int(update.get("update_id", 0)) + 1
        msg = update.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        if not chat_id:
            continue
        contact = msg.get("contact") or {}
        phone = normalize_phone(str(contact.get("phone_number") or ""))
        text = (msg.get("text") or "").strip()
        if phone:
            store.remember_telegram(phone_keys(phone), chat_id)
            _send_telegram(
                chat_id,
                "Listo. Este celular queda ligado a CuidaVoz y recibirá las alertas por Telegram.",
                {"remove_keyboard": True},
            )
            linked.append(phone)
        elif text.startswith("/start"):
            _ask_share_contact(chat_id)

    bound = 0
    for user in list(store.users.values()):
        if user.cuidador_telefono and _bind_user_telegram(user):
            bound += 1
    if linked:
        message = f"Telegram vinculó {len(linked)} celular(es). Hogares listos: {bound}."
    else:
        message = (
            "Todavía no hay un contacto nuevo. El cuidador debe abrir el bot, "
            "pulsar Iniciar y compartir su celular."
        )
    return ToolResponse(
        ok=True,
        data={
            "linked": linked,
            "bound_households": bound,
            "bot_url": f"https://t.me/{_bot_username()}",
        },
        message=message,
    )


def notify_caregiver(user_id: str, message: str) -> ToolResponse:
    user = store.get_user(user_id)
    phone = normalize_phone(user.cuidador_telefono) if user and user.cuidador_telefono else ""
    chat_id = ""
    if user and phone:
        chat_id = _bind_user_telegram(user)
        if not chat_id and _bot_token():
            sync_telegram_links()
            user = store.get_user(user_id)
            chat_id = _bind_user_telegram(user) if user else ""
    if not chat_id and user and not phone and user.cuidador_telegram:
        chat_id = user.cuidador_telegram.strip()
    if not chat_id and not phone:
        chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

    enviado = False
    telegram_message_id = None
    destino = phone or chat_id or "sin celular"
    if chat_id:
        telegram_message_id = _send_telegram(chat_id, message)
        enviado = telegram_message_id is not None

    alert = CaregiverAlert(
        user_id=user_id,
        timestamp=now_iso(),
        mensaje=message,
        canal="telegram",
        destino=destino,
        enviado=enviado,
    )
    store.add_alert(alert)
    if enviado:
        estado = f"Alerta enviada por Telegram al {destino}"
    elif phone and not chat_id:
        estado = (
            f"Celular {phone} guardado. Falta que el cuidador abra @{_bot_username()}, "
            "pulse Iniciar y comparta su contacto."
        )
    else:
        estado = f"Alerta registrada para el cuidador ({destino})"
    return ToolResponse(
        ok=True,
        data={
            "alert": alert.model_dump(),
            "destino": destino,
            "canal": "telegram",
            "vinculado": bool(chat_id),
            "alerta_preview": message,
            "telegram_message_id": telegram_message_id,
            "telegram_pending": not enviado,
        },
        message=estado,
        logged_at=alert.timestamp,
    )


def get_home(user_id: str) -> ToolResponse:
    """Perfil del hogar. La primera llamada de una cuenta nueva marca la guía como vista."""
    user = store.get_or_create_user(user_id)
    primera_vez = not user.guia_vista
    if primera_vez:
        user.guia_vista = True
        store.upsert_user(user)
    meds = get_medications(user_id)
    return ToolResponse(
        ok=True,
        data={
            "primera_vez": primera_vez,
            "user": user.model_dump(),
            "medications": meds.data or [],
        },
        message="Hogar listo" if not primera_vez else "Primera vez: falta configurar el hogar",
    )


def configure_home(
    user_id: str,
    nombre: str | None = None,
    cuidador_telefono: str | None = None,
) -> ToolResponse:
    user = store.get_or_create_user(user_id, nombre or "Usuario")
    if nombre and nombre.strip():
        user.nombre = nombre.strip()
    if cuidador_telefono:
        phone = normalize_phone(cuidador_telefono)
        if not phone:
            return ToolResponse(ok=False, message="El celular necesita al menos 8 dígitos, con lada")
        user.cuidador_telefono = phone
        user.cuidador_telegram = store.chat_for_phone(phone)
    user.guia_vista = True
    store.upsert_user(user)
    destino = user.cuidador_telefono or "sin celular"
    if user.cuidador_telefono and user.cuidador_telegram:
        detalle = f"Alertas por Telegram al {destino}"
    elif user.cuidador_telefono:
        detalle = (
            f"Celular {destino} guardado. El cuidador abre @{_bot_username()}, "
            "pulsa Iniciar y comparte su contacto."
        )
    else:
        detalle = "Falta el celular del cuidador"
    return ToolResponse(
        ok=True,
        data=user.model_dump() | {"telegram_vinculado": bool(user.cuidador_telegram)},
        message=f"Hogar de {user.nombre}. {detalle}",
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
