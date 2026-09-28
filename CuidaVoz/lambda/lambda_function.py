# -*- coding: utf-8 -*-
"""CuidaVoz — handlers Alexa que invocan el servidor MCP vía REST."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request

import ask_sdk_core.utils as ask_utils
from ask_sdk_core.dispatch_components import AbstractExceptionHandler, AbstractRequestHandler
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_model import Response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DEFAULT_USER_ID = os.environ.get("CUIDAVOZ_USER_ID", os.environ.get("CUIDAMED_USER_ID", "demo-user"))
MCP_BASE_URL = os.environ.get(
    "MCP_BASE_URL",
    "https://cuidavoz-mcp.onrender.com",
).rstrip("/")

OFFLINE_SPEAK = (
    "No pude conectar con CuidaVoz. "
    "Espera unos veinte segundos y vuelve a decir: Alexa, abre cuida voz."
)

_NUM_WORDS = {
    "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
    "trece": 13, "catorce": 14, "quince": 15, "dieciseis": 16, "dieciséis": 16,
    "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20,
    "veintiuno": 21, "veintidos": 22, "veintidós": 22, "veintitres": 23,
    "veintitrés": 23, "veinticuatro": 24, "treinta": 30,
}

_SKIP_NAMES = {
    "un medicamento", "una medicina", "mis medicamentos", "mis medicinas",
    "medicamento", "medicina", "pastilla",
}


def resolve_user_id(handler_input: HandlerInput) -> str:
    """Usa el userId de Alexa cuando existe; si no, demo-user."""
    try:
        uid = handler_input.request_envelope.context.system.user.user_id
        if uid:
            return uid
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_USER_ID


def _http_post(path: str, payload: dict) -> dict | None:
    if not MCP_BASE_URL:
        return None
    url = f"{MCP_BASE_URL}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("MCP call failed %s: %s", url, exc)
        return None


def call_tool(tool_name: str, payload: dict) -> dict:
    remote = _http_post(f"/api/tools/{tool_name}", payload)
    if remote is not None:
        return remote
    return {"ok": False, "message": "sin_conexion"}


def slot_value(handler_input: HandlerInput, name: str) -> str | None:
    try:
        slots = handler_input.request_envelope.request.intent.slots  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None
    if not slots or name not in slots or slots[name] is None:
        return None
    value = slots[name].value
    return value.strip() if value else None


def intent_name(handler_input: HandlerInput) -> str:
    return handler_input.request_envelope.request.intent.name  # type: ignore[attr-defined]


def _session(handler_input: HandlerInput) -> dict:
    return handler_input.attributes_manager.session_attributes


def _ask(handler_input: HandlerInput, speak: str) -> Response:
    return handler_input.response_builder.speak(speak).ask(speak).response


def _say(handler_input: HandlerInput, speak: str) -> Response:
    return handler_input.response_builder.speak(speak).response


def _word_or_num(token: str) -> int | None:
    cleaned = token.lower().strip(".,")
    if cleaned.isdigit():
        return int(cleaned)
    return _NUM_WORDS.get(cleaned)


def _parse_dose(nombre: str) -> tuple[str, str]:
    dosis_text = ""
    parts = nombre.split()
    for i, part in enumerate(parts):
        if part.isdigit() and i + 1 < len(parts) and parts[i + 1].lower() in {"mg", "mcg", "ml", "g"}:
            dosis_text = f"{part} {parts[i + 1]}"
            nombre = " ".join(parts[:i] + parts[i + 2 :]).strip() or nombre
            break
        if part.isdigit() and "mg" in "".join(parts[i:]).lower():
            dosis_text = f"{part} mg"
            nombre = " ".join(p for j, p in enumerate(parts) if j != i and p.lower() != "mg").strip() or nombre
            break
    return nombre.strip(" ,."), dosis_text


def _normalize_time(hora: str | None) -> str | None:
    if not hora:
        return None
    key = hora.strip().lower()
    mapping = {
        "morning": "08:00", "mo": "08:00",
        "afternoon": "14:00", "af": "14:00",
        "evening": "20:00", "ev": "20:00",
        "night": "21:00", "ni": "21:00",
    }
    if key in mapping:
        return mapping[key]
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", key)
    if not match:
        return None
    hh = int(match.group(1))
    mm = int(match.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return f"{hh:02d}:{mm:02d}"
    return None


def interpret_medicamento(raw: str) -> dict:
    """Saca nombre, dosis, hora, uso y días de una frase libre."""
    text = " ".join((raw or "").split())
    uso = None
    dias = None
    hora = None
    lower = text.lower()

    if any(p in lower for p in ("uso cotidiano", "todos los días", "todos los dias", "a diario", "de siempre", "permanente", "cotidiano")):
        uso = "cotidiano"
    if any(p in lower for p in ("temporal", "por unos días", "por unos dias", "tratamiento corto")):
        uso = "temporal"

    days_match = re.search(r"por\s+(\d+|[a-záéíóúñ]+)\s+d[ií]as", text, re.I)
    if days_match:
        count = _word_or_num(days_match.group(1))
        if count and 1 <= count <= 365:
            dias = count
            uso = "temporal"
        text = f"{text[:days_match.start()]} {text[days_match.end():]}"

    time_match = re.search(
        r"a\s+las\s+(\d{1,2}|[a-záéíóúñ]+)(?:\s*:\s*(\d{2})|\s+y\s+media)?(?:\s+de\s+la\s+(mañana|manana|tarde|noche))?",
        text,
        re.I,
    )
    if time_match:
        hh = _word_or_num(time_match.group(1))
        if hh is not None:
            mm = time_match.group(2) or ("30" if "y media" in time_match.group(0).lower() else "00")
            periodo = (time_match.group(3) or "").lower()
            if periodo in {"tarde", "noche"} and 1 <= hh <= 11:
                hh += 12
            if periodo in {"mañana", "manana"} and hh == 12:
                hh = 0
            if 0 <= hh <= 23:
                hora = f"{hh:02d}:{mm}"
        text = f"{text[:time_match.start()]} {text[time_match.end():]}"

    for phrase in (
        "de uso cotidiano", "uso cotidiano", "todos los días", "todos los dias",
        "de siempre", "a diario", "cotidiano", "permanente", "es temporal",
        "temporal", "por unos días", "por unos dias", "tratamiento corto",
        "de la mañana", "de la manana", "de la tarde", "de la noche",
    ):
        text = re.sub(re.escape(phrase), " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" ,.")
    parts = text.split()
    while parts and parts[0].lower() in {"el", "la", "los", "las", "un", "una", "mi", "mis"}:
        parts.pop(0)
    text = " ".join(parts)
    nombre, dosis = _parse_dose(text)
    return {"nombre": nombre, "dosis": dosis, "hora": hora, "uso": uso, "dias": dias}


def split_medication_phrase(raw: str) -> list[str]:
    text = " ".join((raw or "").split())
    if re.search(r"a\s+las\s", text, re.I) or re.search(r"por\s+\S+\s+d[ií]as", text, re.I):
        return [text] if text else []
    parts: list[str] = []
    normalized = re.sub(r"\s+e\s+", " y ", text, flags=re.I)
    for chunk in normalized.split(","):
        for piece in chunk.split(" y "):
            name = piece.strip(" .")
            if name and name.lower() not in _SKIP_NAMES:
                parts.append(name)
    return parts


def _clear_setup(attrs: dict) -> None:
    for key in ("setup_step", "pending_nombre", "pending_dosis", "pending_hora", "queue"):
        attrs.pop(key, None)


def _med_label(nombre: str, dosis: str) -> str:
    return f"{nombre} {dosis}".strip()


def describe_medication(med: dict) -> str:
    nombre = med.get("nombre") or "medicamento"
    dosis = med.get("dosis") or ""
    horas = med.get("horarios") or []
    if not horas and med.get("hora"):
        horas = [med["hora"]]
    hora_txt = " y ".join(horas) if horas else "sin hora"
    curso = med.get("curso") or {}
    if curso.get("tiene_curso") or med.get("dias_tratamiento"):
        dias = curso.get("dias_tratamiento") or med.get("dias_tratamiento")
        tipo = f"temporal, por {dias} días"
    else:
        tipo = "de uso cotidiano"
    return f"{_med_label(nombre, dosis)} a las {hora_txt}, {tipo}"


def begin_medication(handler_input: HandlerInput, raw: str, queue: list[str], prefix: str = "") -> Response:
    info = interpret_medicamento(raw)
    attrs = _session(handler_input)
    if not info["nombre"]:
        attrs["setup_step"] = "nombre"
        attrs["queue"] = queue
        return _ask(handler_input, prefix + "¿Cómo se llama el medicamento? Di, por ejemplo: agrega paracetamol.")

    attrs["pending_nombre"] = info["nombre"]
    attrs["pending_dosis"] = info["dosis"]
    attrs["pending_hora"] = info["hora"] or ""
    attrs["queue"] = queue
    label = _med_label(info["nombre"], info["dosis"])

    if info["hora"] and info["dias"]:
        return commit_medication(handler_input, info["dias"], prefix)
    if info["hora"] and info["uso"] == "cotidiano":
        return commit_medication(handler_input, None, prefix)
    if info["hora"] and info["uso"] == "temporal":
        attrs["setup_step"] = "dias"
        return _ask(
            handler_input,
            prefix + f"{label} a las {info['hora']}. ¿Por cuántos días es el tratamiento? Por ejemplo: por cinco días.",
        )
    if info["hora"]:
        attrs["setup_step"] = "tipo"
        return _ask(
            handler_input,
            prefix + (
                f"{label} a las {info['hora']}. "
                "¿Es de uso cotidiano, todos los días, o es temporal, solo por unos días?"
            ),
        )
    attrs["setup_step"] = "hora"
    return _ask(
        handler_input,
        prefix + f"Vamos a configurar {label}. ¿A qué hora lo tomas? Por ejemplo: a las ocho de la mañana.",
    )


def commit_medication(handler_input: HandlerInput, dias: int | None, prefix: str = "") -> Response:
    attrs = _session(handler_input)
    nombre = attrs.get("pending_nombre") or ""
    dosis = attrs.get("pending_dosis") or ""
    hora = attrs.get("pending_hora") or "08:00"
    queue = list(attrs.get("queue") or [])
    payload = {
        "user_id": resolve_user_id(handler_input),
        "nombre": nombre,
        "dosis": dosis,
        "time": hora,
        "uso": "temporal" if dias else "cotidiano",
    }
    if dias:
        payload["dias_tratamiento"] = dias
    result = call_tool("schedule_reminder", payload)
    _clear_setup(attrs)
    if not result.get("ok"):
        if result.get("message") == "sin_conexion":
            return _ask(handler_input, OFFLINE_SPEAK)
        return _ask(handler_input, "No pude registrar el medicamento. Intenta de nuevo diciendo: agrega, y el nombre.")

    label = _med_label(nombre, dosis)
    detalle = f"por {dias} días, como tratamiento temporal" if dias else "de uso cotidiano"
    saved = f"Listo. Guardé {label} a las {hora}, {detalle}. "
    if queue:
        nxt = queue.pop(0)
        return begin_medication(handler_input, nxt, queue, prefix + saved)
    attrs["setup_step"] = "otro"
    return _ask(handler_input, prefix + saved + "¿Quieres agregar otro medicamento?")


# --- Handlers --------------------------------------------------------------------


class LaunchRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        result = call_tool("get_medications", {"user_id": resolve_user_id(handler_input)})
        if not result.get("ok"):
            return _ask(handler_input, "Hola, soy CuidaVoz. " + OFFLINE_SPEAK)
        meds = result.get("data") or []
        if not meds:
            speak = (
                "Hola, soy CuidaVoz. Todavía no tienes medicamentos configurados. "
                "Dime, por ejemplo: agrega paracetamol. "
                "Después te pregunto la hora y si es de uso cotidiano o solo por unos días. "
                "También puedes decirlo de una vez: agrega paracetamol a las ocho, de uso cotidiano."
            )
            return _ask(handler_input, speak)
        speak = (
            f"Hola, soy CuidaVoz. Tienes {len(meds)} medicamento(s) configurado(s). "
            "Puedes decir: qué me toca ahora, ya lo tomé, cómo voy hoy, "
            "o agrega y el nombre de otro medicamento."
        )
        return _ask(handler_input, speak)


class SetupInProgressHandler(AbstractRequestHandler):
    """Sigue el alta de un medicamento: hora, tipo y días."""

    def can_handle(self, handler_input):
        if not ask_utils.is_request_type("IntentRequest")(handler_input):
            return False
        if not _session(handler_input).get("setup_step"):
            return False
        name = intent_name(handler_input)
        return name not in {
            "AMAZON.CancelIntent",
            "AMAZON.StopIntent",
            "AMAZON.HelpIntent",
            "AMAZON.NavigateHomeIntent",
        }

    def handle(self, handler_input):
        attrs = _session(handler_input)
        step = attrs.get("setup_step")
        name = intent_name(handler_input)

        if name == "IniciarConfiguracionIntent":
            attrs["setup_step"] = "nombre"
            return _ask(handler_input, "Dime: agrega, y el nombre. Por ejemplo: agrega paracetamol.")

        if name == "RegistrarMedicamentoIntent":
            raw = slot_value(handler_input, "medicamento")
            names = split_medication_phrase(raw or "")
            if names:
                first, *rest = names
                return begin_medication(handler_input, first, rest)

        if step == "hora":
            hora = _normalize_time(slot_value(handler_input, "hora"))
            if not hora:
                return _ask(handler_input, "Dime la hora. Por ejemplo: a las ocho de la mañana.")
            attrs["pending_hora"] = hora
            attrs["setup_step"] = "tipo"
            label = _med_label(attrs.get("pending_nombre") or "", attrs.get("pending_dosis") or "")
            return _ask(
                handler_input,
                f"{label} a las {hora}. ¿Es de uso cotidiano, todos los días, o es temporal, solo por unos días?",
            )

        if step == "tipo":
            if name == "UsoCotidianoIntent":
                return commit_medication(handler_input, None)
            if name == "UsoTemporalIntent":
                attrs["setup_step"] = "dias"
                return _ask(handler_input, "¿Por cuántos días es el tratamiento? Por ejemplo: por cinco días.")
            return _ask(handler_input, "Dime si es de uso cotidiano o si es temporal, solo por unos días.")

        if step == "dias":
            raw_days = slot_value(handler_input, "dias")
            try:
                dias = int(float(raw_days)) if raw_days else 0
            except ValueError:
                dias = 0
            if dias < 1 or dias > 365:
                return _ask(handler_input, "Dime un número de días. Por ejemplo: por siete días.")
            return commit_medication(handler_input, dias)

        if step == "otro":
            if name == "AMAZON.YesIntent":
                attrs["setup_step"] = "nombre"
                return _ask(handler_input, "Dime: agrega, y el nombre. Por ejemplo: agrega ibuprofeno.")
            if name == "AMAZON.NoIntent":
                _clear_setup(attrs)
                return _say(handler_input, "Quedó guardado. Cuando quieras, pregunta qué me toca ahora.")
            return _ask(handler_input, "¿Quieres agregar otro medicamento? Dime sí o no.")

        if step == "nombre":
            return _ask(handler_input, "Dime: agrega, y el nombre del medicamento. Por ejemplo: agrega paracetamol.")

        _clear_setup(attrs)
        return _ask(handler_input, "Empecemos de nuevo. Di: agrega, y el nombre del medicamento.")


class TomarMedicamentoIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("TomarMedicamentoIntent")(handler_input) or ask_utils.is_intent_name(
            "TomarNombradoIntent"
        )(handler_input)

    def handle(self, handler_input):
        uid = resolve_user_id(handler_input)
        med_name = slot_value(handler_input, "medicamento") or "próximo"
        result = call_tool("log_dose", {"user_id": uid, "med_id": med_name, "status": "taken"})
        if result.get("message") == "sin_conexion":
            return _ask(handler_input, OFFLINE_SPEAK)
        if not result.get("ok"):
            speak = "Todavía no tienes ese medicamento. Para configurarlo, di: agrega, y el nombre."
            return _ask(handler_input, speak)
        adherence = call_tool("get_adherence_today", {"user_id": uid})
        data = adherence.get("data") or {}
        med = (result.get("data") or {}).get("medication") or {}
        label = _med_label(med.get("nombre", "medicamento"), med.get("dosis", ""))
        speak = f"Perfecto, registré {label}. Llevas {data.get('taken', 0)} de {data.get('expected', 0)} hoy."
        return _say(handler_input, speak)


class PosponerIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("PosponerIntent")(handler_input)

    def handle(self, handler_input):
        uid = resolve_user_id(handler_input)
        result = call_tool("log_dose", {"user_id": uid, "med_id": "próximo", "status": "pending"})
        if result.get("message") == "sin_conexion":
            return _ask(handler_input, OFFLINE_SPEAK)
        if not result.get("ok"):
            return _ask(handler_input, "No tienes medicamentos configurados. Di: agrega, y el nombre.")
        data = result.get("data") or {}
        intentos = data.get("intentos", 1)
        notified = data.get("caregiver_notified", False)
        if notified:
            speak = (
                "Entendido. Como no hubo confirmación, ya avisé a tu cuidador por Telegram. "
                "Cuando la tomes, dime: ya lo tomé."
            )
        elif intentos >= 1:
            speak = "De acuerdo, te recuerdo en unos minutos. Cuando la tomes, dime: ya lo tomé."
        else:
            speak = "Quedó pendiente. Te vuelvo a preguntar pronto."
        return _ask(handler_input, speak)


class ConsultarProximoIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("ConsultarProximoIntent")(handler_input)

    def handle(self, handler_input):
        result = call_tool("get_next_dose", {"user_id": resolve_user_id(handler_input)})
        if not result.get("ok"):
            return _ask(handler_input, OFFLINE_SPEAK)
        data = result.get("data")
        if not data:
            speak = (
                "No tienes medicamentos configurados. "
                "Di: agrega, y el nombre. Por ejemplo: agrega paracetamol a las ocho, de uso cotidiano."
            )
            return _ask(handler_input, speak)
        curso = data.get("curso") or {}
        extra = ""
        if curso.get("tiene_curso"):
            extra = f" Es un tratamiento temporal, día {min(curso.get('dias_con_toma', 0) + 1, curso.get('dias_tratamiento', 1))} de {curso.get('dias_tratamiento')}."
        speak = f"Te toca {data.get('nombre')} {data.get('dosis')} a las {data.get('hora')}.{extra} ¿Ya lo tomaste?"
        return _ask(handler_input, speak)


class ConsultarAdherenciaIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("ConsultarAdherenciaIntent")(handler_input)

    def handle(self, handler_input):
        result = call_tool("get_adherence_today", {"user_id": resolve_user_id(handler_input)})
        if not result.get("ok"):
            return _ask(handler_input, OFFLINE_SPEAK)
        data = result.get("data") or {}
        if not data.get("expected"):
            return _ask(handler_input, "Hoy no tienes medicamentos activos. Si quieres agregar uno, di: agrega, y el nombre.")
        speak = (
            f"Hoy llevas {data.get('taken', 0)} de {data.get('expected', 0)} medicamentos. "
            f"Pendientes: {data.get('pending', 0)}."
        )
        return _say(handler_input, speak)


class ListarMedicamentosIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("ListarMedicamentosIntent")(handler_input)

    def handle(self, handler_input):
        result = call_tool("get_medications", {"user_id": resolve_user_id(handler_input)})
        if not result.get("ok"):
            return _ask(handler_input, OFFLINE_SPEAK)
        meds = result.get("data") or []
        if not meds:
            return _ask(
                handler_input,
                "No tienes medicamentos configurados. Di: agrega, y el nombre. "
                "Luego te pregunto si es de uso cotidiano o temporal.",
            )
        detalle = ". ".join(describe_medication(m) for m in meds[:6])
        speak = f"Tienes configurado: {detalle}. Si quieres otro, di: agrega, y el nombre."
        return _ask(handler_input, speak)


class IniciarConfiguracionIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("IniciarConfiguracionIntent")(handler_input)

    def handle(self, handler_input):
        _session(handler_input)["setup_step"] = "nombre"
        return _ask(
            handler_input,
            "Vamos a configurar tus medicamentos. Dime: agrega, y el nombre. Por ejemplo: agrega paracetamol.",
        )


class RegistrarMedicamentoIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("RegistrarMedicamentoIntent")(handler_input)

    def handle(self, handler_input):
        raw = slot_value(handler_input, "medicamento")
        names = split_medication_phrase(raw or "")
        if not names:
            attrs = _session(handler_input)
            attrs["setup_step"] = "nombre"
            return _ask(
                handler_input,
                "Vamos a configurar tus medicamentos. Dime: agrega, y el nombre. Por ejemplo: agrega paracetamol.",
            )
        first, *rest = names
        return begin_medication(handler_input, first, rest)


class IndicarHoraIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("IndicarHoraIntent")(handler_input)

    def handle(self, handler_input):
        return _ask(handler_input, "Primero dime qué medicamento. Por ejemplo: agrega paracetamol.")


class UsoCotidianoIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("UsoCotidianoIntent")(handler_input)

    def handle(self, handler_input):
        return _ask(handler_input, "Primero dime qué medicamento. Por ejemplo: agrega paracetamol a las ocho, de uso cotidiano.")


class UsoTemporalIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("UsoTemporalIntent")(handler_input)

    def handle(self, handler_input):
        return _ask(handler_input, "Primero dime qué medicamento y luego por cuántos días. Por ejemplo: agrega amoxicilina a las ocho, por cinco días.")


class IndicarDiasIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("IndicarDiasIntent")(handler_input)

    def handle(self, handler_input):
        return _ask(handler_input, "Primero dime qué medicamento. Por ejemplo: agrega amoxicilina por cinco días.")


class YesIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.YesIntent")(handler_input)

    def handle(self, handler_input):
        return _ask(handler_input, "Si ya tomaste la dosis, di: ya lo tomé. Si quieres agregar un medicamento, di: agrega, y el nombre.")


class NoIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.NoIntent")(handler_input)

    def handle(self, handler_input):
        return _ask(handler_input, "De acuerdo. Si aún no la tomas, di: todavía no.")


class HelpIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        speak = (
            "Puedes configurar tus propios medicamentos. "
            "Di: agrega paracetamol a las ocho, de uso cotidiano. "
            "O: agrega amoxicilina a las ocho, por cinco días. "
            "También: qué me toca ahora, ya lo tomé, todavía no, cómo voy hoy, "
            "o cuáles son mis medicamentos."
        )
        return _ask(handler_input, speak)


class CancelOrStopIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.CancelIntent")(handler_input) or ask_utils.is_intent_name(
            "AMAZON.StopIntent"
        )(handler_input)

    def handle(self, handler_input):
        _clear_setup(_session(handler_input))
        return _say(handler_input, "Hasta luego. Cuídate.")


class FallbackIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.FallbackIntent")(handler_input)

    def handle(self, handler_input):
        speak = "No te entendí. Para configurar uno, di: agrega paracetamol a las ocho, de uso cotidiano."
        return _ask(handler_input, speak)


class SessionEndedRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_request_type("SessionEndedRequest")(handler_input)

    def handle(self, handler_input):
        return handler_input.response_builder.response


class CatchAllExceptionHandler(AbstractExceptionHandler):
    def can_handle(self, handler_input, exception):
        return True

    def handle(self, handler_input, exception):
        logger.error(exception, exc_info=True)
        speak = "Tuve un problema. Intenta otra vez, por favor."
        return _ask(handler_input, speak)


sb = SkillBuilder()
sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(SetupInProgressHandler())
sb.add_request_handler(TomarMedicamentoIntentHandler())
sb.add_request_handler(PosponerIntentHandler())
sb.add_request_handler(ConsultarProximoIntentHandler())
sb.add_request_handler(ConsultarAdherenciaIntentHandler())
sb.add_request_handler(ListarMedicamentosIntentHandler())
sb.add_request_handler(IniciarConfiguracionIntentHandler())
sb.add_request_handler(RegistrarMedicamentoIntentHandler())
sb.add_request_handler(IndicarHoraIntentHandler())
sb.add_request_handler(UsoCotidianoIntentHandler())
sb.add_request_handler(UsoTemporalIntentHandler())
sb.add_request_handler(IndicarDiasIntentHandler())
sb.add_request_handler(YesIntentHandler())
sb.add_request_handler(NoIntentHandler())
sb.add_request_handler(HelpIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(FallbackIntentHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_exception_handler(CatchAllExceptionHandler())

lambda_handler = sb.lambda_handler()
