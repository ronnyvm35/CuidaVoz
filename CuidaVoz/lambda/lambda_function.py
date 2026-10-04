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
        unit = parts[i + 1].lower() if i + 1 < len(parts) else ""
        if part.isdigit() and unit in {"mg", "mcg", "ml", "g", "miligramos", "miligramo", "mililitros", "mililitro"}:
            shown = "mg" if unit.startswith("miligram") else ("ml" if unit.startswith("mililit") else unit)
            dosis_text = f"{part} {shown}"
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


def _norm_forma(value: str | None) -> str:
    v = (value or "").lower()
    if any(p in v for p in ("inyec", "ampoll", "jering")):
        return "inyeccion"
    if any(p in v for p in ("jarab", "suspen", "mililit", " ml")):
        return "jarabe"
    if any(p in v for p in ("pastill", "tableta", "comprim", "capsul", "cápsul")):
        return "pastilla"
    return ""


def _hora_habla(hhmm: str) -> str:
    try:
        h_txt, m_txt = (hhmm or "").split(":")
        h, m = int(h_txt), int(m_txt)
    except ValueError:
        return hhmm or "esa hora"
    extra = f" y {m}" if m else ""
    if h == 0:
        return "12 de la noche" + extra
    if h < 12:
        return f"{h} de la mañana{extra}"
    if h == 12:
        return "12 del día" + extra
    if h < 19:
        return f"{h - 12} de la tarde{extra}"
    return f"{h - 12} de la noche{extra}"


def interpret_medicamento(raw: str) -> dict:
    """Saca nombre, dosis, presentación, cantidad, hora, intervalo, uso y días."""
    text = " ".join((raw or "").split())
    uso = None
    dias = None
    hora = None
    forma = ""
    cantidad = ""
    intervalo = None
    lower = text.lower()

    if any(p in lower for p in (
        "uso cotidiano", "todos los días", "todos los dias", "a diario", "de siempre",
        "permanente", "cotidiano", "indefinido", "de por vida", "sin fecha",
        "crónico", "cronico", "para siempre", "sin fin", "tratamiento continuo",
    )):
        uso = "cotidiano"
    if any(p in lower for p in ("temporal", "por unos días", "por unos dias", "tratamiento corto")):
        uso = "temporal"
    if re.search(r"una vez al d[ií]a|solo una vez", lower):
        intervalo = 24

    days_match = re.search(r"por\s+(\d+|[a-záéíóúñ]+)\s+d[ií]as", text, re.I)
    if days_match:
        count = _word_or_num(days_match.group(1))
        if count and 1 <= count <= 365:
            dias = count
            uso = "temporal"
        text = f"{text[:days_match.start()]} {text[days_match.end():]}"

    interval_match = re.search(r"cada\s+(\d+|[a-záéíóúñ]+)\s+horas?", text, re.I)
    if interval_match:
        count = _word_or_num(interval_match.group(1))
        if count in {4, 6, 8, 12, 24}:
            intervalo = count
        text = f"{text[:interval_match.start()]} {text[interval_match.end():]}"

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

    cant_match = re.search(
        r"\b(\d+|un|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\s+"
        r"(pastillas?|mililitros|ml|ampollas?|ampolletas?|tabletas?|comprimidos?|c[aá]psulas?)\b",
        text,
        re.I,
    )
    if cant_match:
        n = _word_or_num(cant_match.group(1)) or cant_match.group(1)
        unit = cant_match.group(2).lower()
        if unit.startswith("ml") or "mililitro" in unit:
            cantidad = f"{n} ml"
            forma = "jarabe"
        elif "ampoll" in unit:
            cantidad = "1 ampolla" if str(n) == "1" else f"{n} ampollas"
            forma = "inyeccion"
        else:
            cantidad = "1 pastilla" if str(n) == "1" else f"{n} pastillas"
            forma = "pastilla"
        text = f"{text[:cant_match.start()]} {text[cant_match.end():]}"
    if not forma:
        forma = _norm_forma(text)

    for phrase in (
        "de uso cotidiano", "uso cotidiano", "todos los días", "todos los dias",
        "de siempre", "a diario", "cotidiano", "permanente", "es temporal",
        "temporal", "por unos días", "por unos dias", "tratamiento corto",
        "de la mañana", "de la manana", "de la tarde", "de la noche",
        "indefinido", "de por vida", "sin fecha", "crónico", "cronico",
        "para siempre", "sin fin", "tratamiento continuo", "una vez al día",
        "una vez al dia", "pastilla", "pastillas", "jarabe", "inyección",
        "inyeccion", "ampolla", "ampolleta",
    ):
        text = re.sub(re.escape(phrase), " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" ,.")
    parts = text.split()
    while parts and parts[0].lower() in {"el", "la", "los", "las", "un", "una", "mi", "mis", "es", "en"}:
        parts.pop(0)
    text = " ".join(parts)
    nombre, dosis = _parse_dose(text)
    return {
        "nombre": nombre,
        "dosis": dosis,
        "hora": hora,
        "uso": uso,
        "dias": dias,
        "forma": forma,
        "cantidad": cantidad,
        "intervalo": intervalo,
    }


def split_medication_phrase(raw: str) -> list[str]:
    text = " ".join((raw or "").split())
    if (
        re.search(r"a\s+las\s", text, re.I)
        or re.search(r"por\s+\S+\s+d[ií]as", text, re.I)
        or re.search(r"cada\s+\S+\s+horas?", text, re.I)
    ):
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
    for key in (
        "setup_step", "pending_nombre", "pending_dosis", "pending_hora", "queue",
        "pending_forma", "pending_cantidad", "pending_intervalo", "pending_uso",
        "pending_dias", "cantidad_omitida", "pending_borrar",
    ):
        attrs.pop(key, None)


def _med_label(nombre: str, dosis: str, cantidad: str = "") -> str:
    if cantidad and dosis:
        return f"{cantidad} de {nombre} de {dosis}"
    if cantidad:
        return f"{cantidad} de {nombre}"
    if dosis:
        return f"{nombre} {dosis}".strip()
    return (nombre or "medicamento").strip()


def describe_medication(med: dict) -> str:
    label = _med_label(med.get("nombre") or "medicamento", med.get("dosis") or "", med.get("cantidad") or "")
    horas = med.get("horarios") or []
    if not horas and med.get("hora"):
        horas = [med["hora"]]
    intervalo = med.get("intervalo_horas")
    if intervalo and int(intervalo) < 24 and horas:
        cuando = f"cada {intervalo} horas, a las {' y '.join(_hora_habla(h) for h in horas)}"
    elif horas:
        cuando = "a las " + " y ".join(_hora_habla(h) for h in horas)
    else:
        cuando = "sin hora"
    curso = med.get("curso") or {}
    if curso.get("tiene_curso") or med.get("dias_tratamiento"):
        dias = curso.get("dias_tratamiento") or med.get("dias_tratamiento")
        tipo = f"por {dias} días"
    else:
        tipo = "tratamiento indefinido"
    return f"{label}, {cuando}, {tipo}"


def _apply_med_info(attrs: dict, info: dict) -> None:
    if info.get("dosis"):
        attrs["pending_dosis"] = info["dosis"]
    if info.get("hora"):
        attrs["pending_hora"] = info["hora"]
    if info.get("forma"):
        attrs["pending_forma"] = info["forma"]
    if info.get("cantidad"):
        attrs["pending_cantidad"] = info["cantidad"]
    if info.get("intervalo"):
        attrs["pending_intervalo"] = info["intervalo"]
    if info.get("uso"):
        attrs["pending_uso"] = info["uso"]
    if info.get("dias"):
        attrs["pending_dias"] = info["dias"]
        attrs["pending_uso"] = "temporal"


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
    attrs["pending_forma"] = info["forma"] or ""
    attrs["pending_cantidad"] = info["cantidad"] or ""
    attrs.pop("pending_intervalo", None)
    attrs.pop("pending_uso", None)
    attrs.pop("pending_dias", None)
    attrs.pop("cantidad_omitida", None)
    _apply_med_info(attrs, info)
    attrs["queue"] = queue
    return next_med_prompt(handler_input, prefix)


def next_med_prompt(handler_input: HandlerInput, prefix: str = "") -> Response:
    attrs = _session(handler_input)
    label = _med_label(
        attrs.get("pending_nombre") or "",
        attrs.get("pending_dosis") or "",
        attrs.get("pending_cantidad") or "",
    )
    if not attrs.get("pending_forma"):
        attrs["setup_step"] = "forma"
        return _ask(
            handler_input,
            prefix + f"{label}. ¿Es pastilla, jarabe o inyección?",
        )
    if not attrs.get("pending_cantidad") and not attrs.get("pending_dosis") and not attrs.get("cantidad_omitida"):
        attrs["setup_step"] = "cantidad"
        ejemplo = {
            "jarabe": "tomo 5 mililitros",
            "inyeccion": "tomo una ampolla",
        }.get(attrs.get("pending_forma") or "", "tomo una pastilla de 500 miligramos")
        return _ask(
            handler_input,
            prefix + f"¿Cuánto de {label}? Por ejemplo: {ejemplo}. Si no quieres precisarlo, di: sin cantidad.",
        )
    if not attrs.get("pending_hora"):
        attrs["setup_step"] = "horario"
        return _ask(
            handler_input,
            prefix + "¿A qué hora es la primera toma? Por ejemplo: a las ocho de la mañana.",
        )
    if not attrs.get("pending_intervalo"):
        attrs["setup_step"] = "horario"
        return _ask(
            handler_input,
            prefix + (
                f"{label} a las {_hora_habla(attrs.get('pending_hora') or '')}. "
                "¿Es una vez al día, o cada cuántas horas? "
                "Di: cada 8 horas, cada 12 horas, o una vez al día."
            ),
        )
    if not attrs.get("pending_uso"):
        attrs["setup_step"] = "tipo"
        return _ask(
            handler_input,
            prefix + "¿El tratamiento es indefinido, de todos los días, o solo por unos días?",
        )
    if attrs.get("pending_uso") == "temporal" and not attrs.get("pending_dias"):
        attrs["setup_step"] = "dias"
        return _ask(handler_input, prefix + "¿Por cuántos días? Por ejemplo: por cinco días.")
    return commit_medication(handler_input, prefix)


def commit_medication(handler_input: HandlerInput, prefix: str = "") -> Response:
    attrs = _session(handler_input)
    nombre = attrs.get("pending_nombre") or ""
    dosis = attrs.get("pending_dosis") or ""
    cantidad = attrs.get("pending_cantidad") or ""
    hora = attrs.get("pending_hora") or "08:00"
    intervalo = int(attrs.get("pending_intervalo") or 24)
    dias = attrs.get("pending_dias") if attrs.get("pending_uso") == "temporal" else None
    queue = list(attrs.get("queue") or [])
    payload = {
        "user_id": resolve_user_id(handler_input),
        "nombre": nombre,
        "dosis": dosis,
        "time": hora,
        "forma": attrs.get("pending_forma") or "",
        "cantidad": cantidad,
        "intervalo_horas": intervalo,
        "uso": "temporal" if dias else "cotidiano",
    }
    if dias:
        payload["dias_tratamiento"] = int(dias)
    result = call_tool("schedule_reminder", payload)
    _clear_setup(attrs)
    if not result.get("ok"):
        if result.get("message") == "sin_conexion":
            return _ask(handler_input, OFFLINE_SPEAK)
        return _ask(handler_input, "No pude registrar el medicamento. Intenta de nuevo diciendo: agrega, y el nombre.")

    label = _med_label(nombre, dosis, cantidad)
    if intervalo < 24:
        cuando = f"cada {intervalo} horas, desde las {_hora_habla(hora)}"
    else:
        cuando = f"una vez al día, a las {_hora_habla(hora)}"
    curso = f"por {dias} días" if dias else "con tratamiento indefinido"
    saved = f"Listo. Guardé {label}, {cuando}, {curso}. "
    if queue:
        nxt = queue.pop(0)
        return begin_medication(handler_input, nxt, queue, prefix + saved)
    attrs["setup_step"] = "otro"
    return _ask(handler_input, prefix + saved + "¿Quieres agregar otro medicamento?")


# --- Handlers --------------------------------------------------------------------


def _ask_remove(handler_input: HandlerInput) -> Response:
    raw = slot_value(handler_input, "medicamento") or ""
    nombre = (interpret_medicamento(raw).get("nombre") or raw).strip(" .")
    if not nombre:
        return _ask(handler_input, "¿Cuál medicamento ya no necesita? Di: quita, y el nombre.")
    attrs = _session(handler_input)
    attrs["setup_step"] = "borrar"
    attrs["pending_borrar"] = nombre
    return _ask(handler_input, f"¿Quito {nombre} de la lista? Dime sí para eliminarlo, o no para dejarlo.")


def _home(handler_input: HandlerInput) -> dict:
    return call_tool("get_home", {"user_id": resolve_user_id(handler_input)})


def _save_home(handler_input: HandlerInput, nombre: str | None = None, telefono: str | None = None) -> dict:
    payload: dict = {"user_id": resolve_user_id(handler_input)}
    if nombre:
        payload["nombre"] = nombre
    if telefono:
        payload["cuidador_telefono"] = telefono
    return call_tool("configure_home", payload)


_GUIA_PRIMERA_VEZ = (
    "Hola, soy CuidaVoz. Te recuerdo cada medicamento y, si no confirmas, aviso al celular de tu cuidador. "
    "La primera vez dejamos tres cosas: el nombre del paciente, el celular del cuidador, y cada medicina. "
    "De la medicina dime si es pastilla, jarabe o inyección, cada cuántas horas, y si el tratamiento es indefinido o por unos días. "
    "Después puedes cambiarlo cuando quieras. "
    "¿Cómo se llama el paciente? Di: el paciente se llama, y el nombre."
)


class LaunchRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        result = _home(handler_input)
        if not result.get("ok"):
            return _ask(handler_input, "Hola, soy CuidaVoz. " + OFFLINE_SPEAK)
        data = result.get("data") or {}
        user = data.get("user") or {}
        meds = data.get("medications") or []
        nombre = (user.get("nombre") or "").strip()
        telefono = (user.get("cuidador_telefono") or "").strip()
        attrs = _session(handler_input)
        if data.get("primera_vez"):
            attrs["setup_step"] = "paciente"
            return _ask(handler_input, _GUIA_PRIMERA_VEZ)
        if not nombre or nombre == "Usuario":
            attrs["setup_step"] = "paciente"
            return _ask(
                handler_input,
                "Hola, soy CuidaVoz. Falta el nombre del paciente. Di: el paciente se llama, y el nombre.",
            )
        if not telefono:
            attrs["setup_step"] = "telefono"
            return _ask(
                handler_input,
                f"Hola {nombre}. Falta el celular del cuidador, para avisarle si no confirmas una toma. "
                "Di: el celular es, y el número con lada. Si prefieres dejarlo después, di: ahora no.",
            )
        if not meds:
            attrs["setup_step"] = "nombre"
            return _ask(
                handler_input,
                f"Hola {nombre}. Ya tengo el celular del cuidador. "
                "Ahora el primer medicamento. Di: agrega, y el nombre. Por ejemplo: agrega paracetamol.",
            )
        speak = (
            f"Hola, soy CuidaVoz. {nombre} tiene {len(meds)} medicamento(s). "
            "Puedes decir: qué me toca ahora, ya lo tomé, cómo voy hoy, "
            "agrega un medicamento, el paciente se llama, o el celular del cuidador es."
        )
        return _ask(handler_input, speak)


class SetupInProgressHandler(AbstractRequestHandler):
    """Guía de primera vez y alta de un medicamento."""

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

        if step == "borrar":
            if name == "AMAZON.YesIntent":
                nombre = attrs.get("pending_borrar") or ""
                result = call_tool(
                    "remove_medication",
                    {"user_id": resolve_user_id(handler_input), "nombre": nombre},
                )
                _clear_setup(attrs)
                if result.get("message") == "sin_conexion":
                    return _ask(handler_input, OFFLINE_SPEAK)
                if not result.get("ok"):
                    return _ask(handler_input, "No encontré ese medicamento. Di: quita, y el nombre.")
                return _say(handler_input, f"Listo. Quité {nombre}. Ya no lo voy a recordar.")
            if name == "AMAZON.NoIntent":
                _clear_setup(attrs)
                return _say(handler_input, "De acuerdo. Lo dejo en la lista.")
            return _ask(handler_input, "¿Lo quito de la lista? Dime sí o no.")

        if name == "EliminarMedicamentoIntent":
            return _ask_remove(handler_input)

        if name == "IniciarConfiguracionIntent":
            attrs["setup_step"] = "paciente"
            return _ask(
                handler_input,
                "Vamos a configurar el hogar. ¿Cómo se llama el paciente? Di: el paciente se llama, y el nombre. "
                "Si solo quieres una medicina, di: agrega, y el nombre.",
            )

        if name == "RegistrarMedicamentoIntent":
            raw = slot_value(handler_input, "medicamento")
            names = split_medication_phrase(raw or "")
            if names:
                first, *rest = names
                return begin_medication(handler_input, first, rest)

        if name == "ConfigurarPacienteIntent" or step == "paciente":
            nombre = slot_value(handler_input, "paciente")
            if not nombre and step == "paciente":
                return _ask(handler_input, "Di: el paciente se llama, y el nombre.")
            if nombre:
                result = _save_home(handler_input, nombre=nombre)
                if result.get("message") == "sin_conexion":
                    return _ask(handler_input, OFFLINE_SPEAK)
                if not result.get("ok"):
                    return _ask(handler_input, "No pude guardar el nombre. Di: el paciente se llama, y el nombre.")
                if step == "paciente":
                    attrs["setup_step"] = "telefono"
                    return _ask(
                        handler_input,
                        f"Listo, el paciente es {nombre}. "
                        "¿A qué celular aviso por Telegram si no confirma la toma? "
                        "Di: el celular es, y el número con lada. "
                        "Esa persona abre el bot CuidaVoz, pulsa iniciar y comparte su contacto.",
                    )
                if attrs.get("pending_nombre"):
                    return next_med_prompt(handler_input, f"Actualicé el paciente a {nombre}. ")
                return _say(handler_input, f"Listo. El paciente es {nombre}.")

        if step == "telefono" and name in {"AMAZON.NoIntent", "OmitirCantidadIntent", "PosponerIntent"}:
            attrs["setup_step"] = "nombre"
            return _ask(
                handler_input,
                "De acuerdo, el celular queda pendiente. Ahora el medicamento. Di: agrega, y el nombre.",
            )

        if name == "ConfigurarCuidadorIntent" or step == "telefono":
            telefono = slot_value(handler_input, "telefono")
            if not telefono:
                if step == "telefono":
                    return _ask(
                        handler_input,
                        "Di: el celular es, y el número con lada. Por ejemplo: más cincuenta y dos. "
                        "Si lo dejas para después, di: ahora no.",
                    )
            else:
                result = _save_home(handler_input, telefono=telefono)
                if result.get("message") == "sin_conexion":
                    return _ask(handler_input, OFFLINE_SPEAK)
                if not result.get("ok"):
                    return _ask(handler_input, result.get("message") or "No pude guardar el celular. Dilo con lada.")
                if step in {"paciente", "telefono"}:
                    attrs["setup_step"] = "nombre"
                    return _ask(
                        handler_input,
                        "Guardé el celular del cuidador. Las alertas llegan a ese número. "
                        "Ahora el medicamento. Di: agrega, y el nombre.",
                    )
                if attrs.get("pending_nombre"):
                    return next_med_prompt(handler_input, "Actualicé el celular del cuidador. ")
                return _say(handler_input, "Listo. Las alertas llegan a ese celular.")

        if step == "forma":
            forma = _norm_forma(slot_value(handler_input, "forma") or slot_value(handler_input, "detalle") or "")
            if not forma:
                info = interpret_medicamento(slot_value(handler_input, "detalle") or slot_value(handler_input, "medicamento") or "")
                forma = info.get("forma") or ""
                _apply_med_info(attrs, info)
            if not forma:
                return _ask(handler_input, "Dime si es pastilla, jarabe o inyección.")
            attrs["pending_forma"] = forma
            return next_med_prompt(handler_input)

        if step == "cantidad":
            if name in {"OmitirCantidadIntent", "AMAZON.NoIntent"}:
                attrs["cantidad_omitida"] = True
                return next_med_prompt(handler_input)
            detalle = slot_value(handler_input, "detalle") or slot_value(handler_input, "medicamento") or ""
            info = interpret_medicamento(detalle)
            if info.get("cantidad") or info.get("dosis") or info.get("forma"):
                _apply_med_info(attrs, info)
                return next_med_prompt(handler_input)
            return _ask(
                handler_input,
                "Dime la cantidad. Por ejemplo: tomo una pastilla de 500 miligramos. O di: sin cantidad.",
            )

        if step in {"horario", "hora"}:
            if name == "IndicarFrecuenciaIntent" or slot_value(handler_input, "horas"):
                raw_horas = slot_value(handler_input, "horas")
                hora = _normalize_time(slot_value(handler_input, "hora"))
                if hora:
                    attrs["pending_hora"] = hora
                if raw_horas:
                    try:
                        n = int(float(raw_horas))
                    except ValueError:
                        n = 0
                    if n not in {4, 6, 8, 12, 24}:
                        return _ask(handler_input, "Puedo anotar cada 4, 6, 8 o 12 horas, o una vez al día.")
                    attrs["pending_intervalo"] = n
                else:
                    attrs["pending_intervalo"] = 24
                return next_med_prompt(handler_input)
            hora = _normalize_time(slot_value(handler_input, "hora"))
            if not hora:
                return _ask(
                    handler_input,
                    "Dime la hora y cada cuánto. Por ejemplo: cada 8 horas a las 8 de la mañana, o una vez al día a las 8.",
                )
            attrs["pending_hora"] = hora
            return next_med_prompt(handler_input)

        if step == "tipo":
            if name == "UsoCotidianoIntent":
                attrs["pending_uso"] = "cotidiano"
                attrs.pop("pending_dias", None)
                return next_med_prompt(handler_input)
            if name == "UsoTemporalIntent":
                attrs["pending_uso"] = "temporal"
                return next_med_prompt(handler_input)
            return _ask(handler_input, "Dime si el tratamiento es indefinido o si es solo por unos días.")

        if step == "dias":
            if name == "UsoCotidianoIntent":
                attrs["pending_uso"] = "cotidiano"
                attrs.pop("pending_dias", None)
                return next_med_prompt(handler_input)
            raw_days = slot_value(handler_input, "dias")
            try:
                dias = int(float(raw_days)) if raw_days else 0
            except ValueError:
                dias = 0
            if dias < 1 or dias > 365:
                return _ask(handler_input, "Dime un número de días. Por ejemplo: por siete días.")
            attrs["pending_dias"] = dias
            attrs["pending_uso"] = "temporal"
            return next_med_prompt(handler_input)

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
        label = _med_label(med.get("nombre", "medicamento"), med.get("dosis", ""), med.get("cantidad") or "")
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
                "Entendido. Como no hubo confirmación, ya avisé por Telegram al celular de tu cuidador. "
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
        label = _med_label(data.get("nombre") or "", data.get("dosis") or "", data.get("cantidad") or "")
        intervalo = data.get("intervalo_horas") or 24
        hora = _hora_habla(data.get("hora") or "")
        cuando = f"cada {intervalo} horas, la próxima a las {hora}" if int(intervalo) < 24 else f"a las {hora}"
        if curso.get("tiene_curso"):
            extra = f" Es un tratamiento por días, día {min(curso.get('dias_con_toma', 0) + 1, curso.get('dias_tratamiento', 1))} de {curso.get('dias_tratamiento')}."
        else:
            extra = " El tratamiento es indefinido."
        speak = f"Te toca {label}, {cuando}.{extra} ¿Ya lo tomaste?"
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
                "Luego te pregunto si es pastilla, jarabe o inyección, cada cuántas horas, "
                "y si el tratamiento es indefinido o por unos días.",
            )
        detalle = ". ".join(describe_medication(m) for m in meds[:6])
        speak = f"Tienes configurado: {detalle}. Si quieres otro, di: agrega, y el nombre. Si ya no lo necesita, di: quita, y el nombre."
        return _ask(handler_input, speak)


class IniciarConfiguracionIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("IniciarConfiguracionIntent")(handler_input)

    def handle(self, handler_input):
        _session(handler_input)["setup_step"] = "paciente"
        return _ask(
            handler_input,
            "Vamos a configurar el hogar. ¿Cómo se llama el paciente? Di: el paciente se llama, y el nombre. "
            "Si solo quieres agregar una medicina, di: agrega, y el nombre.",
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


class ConfigurarPacienteIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("ConfigurarPacienteIntent")(handler_input)

    def handle(self, handler_input):
        nombre = slot_value(handler_input, "paciente")
        if not nombre:
            _session(handler_input)["setup_step"] = "paciente"
            return _ask(handler_input, "Di: el paciente se llama, y el nombre.")
        result = _save_home(handler_input, nombre=nombre)
        if result.get("message") == "sin_conexion":
            return _ask(handler_input, OFFLINE_SPEAK)
        if not result.get("ok"):
            return _ask(handler_input, "No pude guardar el nombre. Intenta otra vez.")
        return _say(handler_input, f"Listo. El paciente es {nombre}.")


class ConfigurarCuidadorIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("ConfigurarCuidadorIntent")(handler_input)

    def handle(self, handler_input):
        telefono = slot_value(handler_input, "telefono")
        if not telefono:
            _session(handler_input)["setup_step"] = "telefono"
            return _ask(handler_input, "Di: el celular es, y el número con lada.")
        result = _save_home(handler_input, telefono=telefono)
        if result.get("message") == "sin_conexion":
            return _ask(handler_input, OFFLINE_SPEAK)
        if not result.get("ok"):
            return _ask(handler_input, result.get("message") or "No pude guardar el celular. Dilo con lada.")
        return _say(handler_input, "Listo. Las alertas llegan a ese celular.")


class IndicarPresentacionIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("IndicarPresentacionIntent")(handler_input)

    def handle(self, handler_input):
        return _ask(handler_input, "Primero dime el medicamento. Por ejemplo: agrega paracetamol.")


class IndicarFrecuenciaIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("IndicarFrecuenciaIntent")(handler_input)

    def handle(self, handler_input):
        return _ask(handler_input, "Primero dime el medicamento. Por ejemplo: agrega amoxicilina cada doce horas.")


class IndicarDetalleIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("IndicarDetalleIntent")(handler_input)

    def handle(self, handler_input):
        return _ask(handler_input, "Primero dime el medicamento. Por ejemplo: agrega paracetamol.")


class EliminarMedicamentoIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("EliminarMedicamentoIntent")(handler_input)

    def handle(self, handler_input):
        return _ask_remove(handler_input)


class OmitirCantidadIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("OmitirCantidadIntent")(handler_input)

    def handle(self, handler_input):
        return _ask(handler_input, "Cuando estemos configurando un medicamento, di: sin cantidad.")


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
            "Puedes dejar CuidaVoz a tu medida. "
            "Di: el paciente se llama, y el nombre. "
            "Di: el celular es, y el número del cuidador. "
            "Para una medicina: agrega amoxicilina, una pastilla de 500, cada doce horas, por cinco días. "
            "Si no tiene fecha de fin, di: indefinido. "
            "Si ya no lo necesita, di: quita, y el nombre. "
            "También: qué me toca ahora, ya lo tomé, todavía no, o cuáles son mis medicamentos."
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
        speak = (
            "No te entendí. Puedes decir: el paciente se llama Juan, "
            "el celular es y el número, o agrega paracetamol cada ocho horas, indefinido."
        )
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
sb.add_request_handler(ConfigurarPacienteIntentHandler())
sb.add_request_handler(ConfigurarCuidadorIntentHandler())
sb.add_request_handler(IndicarPresentacionIntentHandler())
sb.add_request_handler(IndicarFrecuenciaIntentHandler())
sb.add_request_handler(IndicarDetalleIntentHandler())
sb.add_request_handler(EliminarMedicamentoIntentHandler())
sb.add_request_handler(OmitirCantidadIntentHandler())
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
