# -*- coding: utf-8 -*-
"""CuidaVoz — handlers Alexa que invocan el servidor MCP vía REST."""

from __future__ import annotations

import json
import logging
import os
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


# --- Cliente MCP / fallback local -------------------------------------------------

_LOCAL = {
    "medications": [
        {
            "med_id": "med-losartan",
            "nombre": "Losartán",
            "dosis": "50 mg",
            "horarios": ["08:00"],
        },
        {
            "med_id": "med-metformina",
            "nombre": "Metformina",
            "dosis": "850 mg",
            "horarios": ["08:00", "20:00"],
        },
    ],
    "taken_today": set(),
    "pending_attempts": 0,
}


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
        with urllib.request.urlopen(req, timeout=4) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("MCP call failed %s: %s", url, exc)
        return None


def call_tool(tool_name: str, payload: dict) -> dict:
    remote = _http_post(f"/api/tools/{tool_name}", payload)
    if remote is not None:
        return remote
    return _local_tool(tool_name, payload)


def _local_tool(tool_name: str, payload: dict) -> dict:
    """Fallback para Alexa Hosted mientras el MCP no esté desplegado públicamente."""
    if tool_name == "get_next_dose":
        med = _LOCAL["medications"][0]
        return {
            "ok": True,
            "data": {
                "med_id": med["med_id"],
                "nombre": med["nombre"],
                "dosis": med["dosis"],
                "hora": med["horarios"][0],
            },
            "message": f"Próximo: {med['nombre']} {med['dosis']} a las {med['horarios'][0]}",
        }

    if tool_name == "get_adherence_today":
        expected = len(_LOCAL["medications"])
        taken = len(_LOCAL["taken_today"])
        return {
            "ok": True,
            "data": {"expected": expected, "taken": taken, "pending": _LOCAL["pending_attempts"]},
            "message": f"Llevas {taken} de {expected} hoy",
        }

    if tool_name == "log_dose":
        status = payload.get("status", "taken")
        med = _LOCAL["medications"][0]
        if status == "taken":
            _LOCAL["taken_today"].add(med["med_id"])
            _LOCAL["pending_attempts"] = 0
            return {
                "ok": True,
                "data": {
                    "status": "taken",
                    "caregiver_notified": False,
                    "medication": med,
                },
                "message": "Dosis registrada",
            }
        _LOCAL["pending_attempts"] += 1
        notified = _LOCAL["pending_attempts"] >= 2
        return {
            "ok": True,
            "data": {
                "status": "pending",
                "intentos": _LOCAL["pending_attempts"],
                "caregiver_notified": notified,
                "medication": med,
            },
            "message": "Dosis pospuesta",
        }

    if tool_name == "schedule_reminder":
        nombre = payload.get("nombre") or "Medicamento"
        dosis = payload.get("dosis") or ""
        time = payload.get("time") or "08:00"
        med = {
            "med_id": f"med-{len(_LOCAL['medications']) + 1}",
            "nombre": nombre,
            "dosis": dosis,
            "horarios": [time],
        }
        _LOCAL["medications"].append(med)
        return {
            "ok": True,
            "data": med,
            "message": f"Recordatorio de {nombre} a las {time} programado",
        }

    if tool_name == "get_medications":
        return {"ok": True, "data": _LOCAL["medications"], "message": "OK"}

    return {"ok": False, "message": "Herramienta no disponible"}


def slot_value(handler_input: HandlerInput, name: str) -> str | None:
    slots = handler_input.request_envelope.request.intent.slots  # type: ignore[attr-defined]
    if not slots or name not in slots:
        return None
    value = slots[name].value
    return value.strip() if value else None


# --- Handlers --------------------------------------------------------------------


class LaunchRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        speak = (
            "Hola, soy CuidaVoz, tu asistente de medicamentos. "
            "Puedes decir: qué me toca ahora, ya lo tomé, todavía no, "
            "o cómo voy hoy."
        )
        return handler_input.response_builder.speak(speak).ask(speak).response


class TomarMedicamentoIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("TomarMedicamentoIntent")(handler_input)

    def handle(self, handler_input):
        med_name = slot_value(handler_input, "medicamento") or "próximo"
        result = call_tool(
            "log_dose",
            {"user_id": DEFAULT_USER_ID, "med_id": med_name, "status": "taken"},
        )
        adherence = call_tool("get_adherence_today", {"user_id": DEFAULT_USER_ID})
        data = adherence.get("data") or {}
        taken = data.get("taken", 0)
        expected = data.get("expected", 0)
        med = (result.get("data") or {}).get("medication") or {}
        label = f"{med.get('nombre', 'medicamento')} {med.get('dosis', '')}".strip()
        speak = f"Perfecto, registré {label}. Llevas {taken} de {expected} hoy."
        return handler_input.response_builder.speak(speak).response


class PosponerIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("PosponerIntent")(handler_input)

    def handle(self, handler_input):
        result = call_tool(
            "log_dose",
            {"user_id": DEFAULT_USER_ID, "med_id": "próximo", "status": "pending"},
        )
        data = result.get("data") or {}
        intentos = data.get("intentos", 1)
        notified = data.get("caregiver_notified", False)
        if notified:
            speak = (
                "Entendido. Como no hubo confirmación, ya avisé a tu cuidador por Telegram. "
                "Cuando la tomes, dime: ya lo tomé."
            )
        elif intentos >= 1:
            speak = (
                "De acuerdo, te recuerdo en unos minutos. "
                "Cuando la tomes, dime: ya lo tomé."
            )
        else:
            speak = "Quedó pendiente. Te vuelvo a preguntar pronto."
        return handler_input.response_builder.speak(speak).ask("¿Ya lo tomaste?").response


class ConsultarProximoIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("ConsultarProximoIntent")(handler_input)

    def handle(self, handler_input):
        result = call_tool("get_next_dose", {"user_id": DEFAULT_USER_ID})
        data = result.get("data")
        if not data:
            speak = "No tienes medicamentos registrados todavía."
            return handler_input.response_builder.speak(speak).ask(
                "¿Quieres agregar uno?"
            ).response
        speak = (
            f"Te toca {data.get('nombre')} {data.get('dosis')} a las {data.get('hora')}. "
            "¿Ya lo tomaste?"
        )
        return handler_input.response_builder.speak(speak).ask("¿Ya lo tomaste?").response


class ConsultarAdherenciaIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("ConsultarAdherenciaIntent")(handler_input)

    def handle(self, handler_input):
        result = call_tool("get_adherence_today", {"user_id": DEFAULT_USER_ID})
        data = result.get("data") or {}
        speak = (
            f"Hoy llevas {data.get('taken', 0)} de {data.get('expected', 0)} medicamentos. "
            f"Pendientes: {data.get('pending', 0)}."
        )
        return handler_input.response_builder.speak(speak).response


class RegistrarMedicamentoIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("RegistrarMedicamentoIntent")(handler_input)

    def handle(self, handler_input):
        nombre = slot_value(handler_input, "medicamento")
        hora = slot_value(handler_input, "hora") or "08:00"
        if not nombre:
            speak = "¿Qué medicamento quieres agregar y a qué hora?"
            return handler_input.response_builder.speak(speak).ask(speak).response

        # AMAZON.TIME puede venir como "08:00" o "morning"
        time_value = hora
        if hora == "morning":
            time_value = "08:00"
        elif hora == "evening":
            time_value = "20:00"
        elif hora == "night":
            time_value = "21:00"

        # Extrae dosis simple si el usuario dijo "Losartán 50 mg"
        dosis_text = ""
        parts = nombre.split()
        for i, part in enumerate(parts):
            if part.isdigit() and i + 1 < len(parts) and parts[i + 1].lower() in {"mg", "mcg", "ml"}:
                dosis_text = f"{part} {parts[i + 1]}"
                nombre = " ".join(parts[:i] + parts[i + 2 :]).strip() or nombre
                break
            if part.isdigit() and "mg" in "".join(parts[i:]).lower():
                dosis_text = f"{part} mg"
                nombre = " ".join(p for j, p in enumerate(parts) if j != i and p.lower() != "mg").strip() or nombre
                break

        result = call_tool(
            "schedule_reminder",
            {
                "user_id": DEFAULT_USER_ID,
                "nombre": nombre,
                "dosis": dosis_text,
                "time": time_value,
            },
        )
        if not result.get("ok"):
            speak = "No pude registrar el medicamento. Intenta de nuevo."
            return handler_input.response_builder.speak(speak).ask(speak).response

        label = f"{nombre} {dosis_text}".strip()
        speak = f"Listo. Agregué {label} a las {time_value}."
        return handler_input.response_builder.speak(speak).response


class HelpIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        speak = (
            "Puedes decir: qué me toca ahora, ya lo tomé, todavía no, "
            "cómo voy hoy, o agrega Losartán cincuenta miligramos a las ocho."
        )
        return handler_input.response_builder.speak(speak).ask(speak).response


class CancelOrStopIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.CancelIntent")(handler_input) or ask_utils.is_intent_name(
            "AMAZON.StopIntent"
        )(handler_input)

    def handle(self, handler_input):
        return handler_input.response_builder.speak("Hasta luego. Cuídate.").response


class FallbackIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.FallbackIntent")(handler_input)

    def handle(self, handler_input):
        speak = "No te entendí. Prueba con: qué me toca ahora, o ya lo tomé."
        return handler_input.response_builder.speak(speak).ask(speak).response


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
        return handler_input.response_builder.speak(speak).ask(speak).response


sb = SkillBuilder()
sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(TomarMedicamentoIntentHandler())
sb.add_request_handler(PosponerIntentHandler())
sb.add_request_handler(ConsultarProximoIntentHandler())
sb.add_request_handler(ConsultarAdherenciaIntentHandler())
sb.add_request_handler(RegistrarMedicamentoIntentHandler())
sb.add_request_handler(HelpIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(FallbackIntentHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_exception_handler(CatchAllExceptionHandler())

lambda_handler = sb.lambda_handler()
