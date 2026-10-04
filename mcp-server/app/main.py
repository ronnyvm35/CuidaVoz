from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import tools
from .models import (
    AddMedicationInput,
    ConfigureHomeInput,
    RemoveMedicationInput,
    LogDoseInput,
    NotifyCaregiverInput,
    ScheduleReminderInput,
    SetupHouseholdInput,
    ToolResponse,
    UserIdInput,
    normalize_phone,
)
from .store import now_iso, store

MCP_SPEC = "2025-11-25"
STATIC_DIR = Path(__file__).resolve().parent / "static"

_allowed_hosts = [
    "localhost",
    "localhost:*",
    "127.0.0.1",
    "127.0.0.1:*",
    "cuidavoz-mcp.onrender.com",
]
_extra_hosts = os.getenv("MCP_ALLOWED_HOSTS", "")
if _extra_hosts:
    _allowed_hosts.extend(h.strip() for h in _extra_hosts.split(",") if h.strip())

_transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=_allowed_hosts,
    allowed_origins=[
        "http://localhost:*",
        "http://127.0.0.1:*",
        "https://cuidavoz-mcp.onrender.com",
        "*",
    ],
)

mcp = MCPServer(
    name="CuidaVoz",
    instructions=(
        "Servidor MCP de CuidaVoz para adherencia a medicamentos. "
        "Usa las herramientas para consultar tratamiento, registrar dosis "
        "y escalar al cuidador cuando no hay confirmación."
    ),
)


@mcp.tool()
def get_medications(user_id: str) -> dict:
    """Lista los medicamentos activos del usuario."""
    return tools.get_medications(user_id).model_dump()


@mcp.tool()
def get_next_dose(user_id: str) -> dict:
    """Devuelve el próximo medicamento y hora programada."""
    return tools.get_next_dose(user_id).model_dump()


@mcp.tool()
def log_dose(user_id: str, med_id: str, status: str) -> dict:
    """Registra una dosis como taken, skipped o pending. Escala al cuidador tras 2 pending."""
    return tools.log_dose(user_id, med_id, status).model_dump()


@mcp.tool()
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
) -> dict:
    """Crea o actualiza un recordatorio. uso=cotidiano es indefinido; dias_tratamiento=5 es un curso. intervalo_horas: 6, 8, 12 o 24."""
    return tools.schedule_reminder(
        user_id=user_id,
        time=time,
        med_id=med_id,
        nombre=nombre,
        dosis=dosis,
        con_comida=con_comida,
        dias_tratamiento=dias_tratamiento,
        uso=uso,
        forma=forma,
        cantidad=cantidad,
        intervalo_horas=intervalo_horas,
    ).model_dump()


@mcp.tool()
def notify_caregiver(user_id: str, message: str) -> dict:
    """Avisa al cuidador por Telegram. El celular se usa para encontrar su chat, no hace falta el chat id."""
    return tools.notify_caregiver(user_id, message).model_dump()


@mcp.tool()
def get_home(user_id: str) -> dict:
    """Devuelve paciente, celular del cuidador y medicamentos. La primera vez marca la guía como vista."""
    return tools.get_home(user_id).model_dump()


@mcp.tool()
def configure_home(user_id: str, nombre: str | None = None, cuidador_telefono: str | None = None) -> dict:
    """Guarda el nombre del paciente y el celular del cuidador."""
    return tools.configure_home(user_id, nombre, cuidador_telefono).model_dump()


@mcp.tool()
def get_adherence_today(user_id: str) -> dict:
    """Resumen de adherencia del día."""
    return tools.get_adherence_today(user_id).model_dump()


@mcp.tool()
def remove_medication(user_id: str, med_id: str | None = None, nombre: str | None = None) -> dict:
    """Quita un medicamento de la lista del paciente."""
    return tools.remove_medication(user_id, med_id, nombre).model_dump()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield


mcp_http_app = mcp.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    transport_security=_transport_security,
)

app = FastAPI(
    title="CuidaVoz MCP Server",
    version="0.1.0",
    description=f"MCP Streamable HTTP (spec {MCP_SPEC}) + REST para Alexa",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "cuidavoz-mcp",
        "mcp_spec": MCP_SPEC,
        "mcp_endpoint": "/mcp/",
        "tools": [
            "get_medications",
            "get_next_dose",
            "log_dose",
            "schedule_reminder",
            "notify_caregiver",
            "get_adherence_today",
            "get_home",
            "configure_home",
            "remove_medication",
        ],
        "demo_user": "demo-user",
        "medications": len(store.list_medications("demo-user")),
    }


@app.post("/api/tools/get_medications", response_model=ToolResponse)
async def rest_get_medications(body: UserIdInput):
    return tools.get_medications(body.user_id)


@app.post("/api/tools/get_next_dose", response_model=ToolResponse)
async def rest_get_next_dose(body: UserIdInput):
    return tools.get_next_dose(body.user_id)


@app.post("/api/tools/log_dose", response_model=ToolResponse)
async def rest_log_dose(body: LogDoseInput):
    return tools.log_dose(body.user_id, body.med_id, body.status)


@app.post("/api/tools/schedule_reminder", response_model=ToolResponse)
async def rest_schedule_reminder(body: ScheduleReminderInput):
    return tools.schedule_reminder(
        user_id=body.user_id,
        time=body.time,
        med_id=body.med_id,
        nombre=body.nombre,
        dosis=body.dosis,
        con_comida=body.con_comida,
        dias_tratamiento=body.dias_tratamiento,
        uso=body.uso,
        forma=body.forma,
        cantidad=body.cantidad,
        intervalo_horas=body.intervalo_horas,
    )


@app.post("/api/tools/notify_caregiver", response_model=ToolResponse)
async def rest_notify_caregiver(body: NotifyCaregiverInput):
    return tools.notify_caregiver(body.user_id, body.message)


@app.post("/api/tools/get_adherence_today", response_model=ToolResponse)
async def rest_get_adherence_today(body: UserIdInput):
    return tools.get_adherence_today(body.user_id)


@app.post("/api/tools/get_home", response_model=ToolResponse)
async def rest_get_home(body: UserIdInput):
    return tools.get_home(body.user_id)


@app.post("/api/tools/configure_home", response_model=ToolResponse)
async def rest_configure_home(body: ConfigureHomeInput):
    return tools.configure_home(body.user_id, body.nombre, body.cuidador_telefono)


@app.post("/api/tools/remove_medication", response_model=ToolResponse)
async def rest_remove_medication(body: RemoveMedicationInput):
    return tools.remove_medication(body.user_id, body.med_id, body.nombre)


@app.get("/demo/household/{user_id}")
async def demo_household(user_id: str):
    user = store.get_or_create_user(user_id)
    meds = store.list_medications(user_id)
    bot = tools._bot_username()
    return {
        "ok": True,
        "user": user.model_dump(),
        "medications": [m.model_dump() for m in meds],
        "telegram_vinculado": bool(user.cuidador_telegram),
        "bot": f"https://t.me/{bot}",
    }


@app.post("/demo/setup-household")
async def demo_setup_household(body: SetupHouseholdInput):
    user = store.get_or_create_user(body.user_id, body.nombre)
    user.nombre = body.nombre.strip() or user.nombre
    raw_phone = body.cuidador_telefono.strip()
    phone = normalize_phone(raw_phone)
    if raw_phone and not phone:
        return {"ok": False, "message": "El celular necesita al menos 8 dígitos, con lada"}
    user.cuidador_telefono = phone
    user.cuidador_telegram = store.chat_for_phone(phone) if phone else user.cuidador_telegram
    if body.cuidador_telegram.strip():
        user.cuidador_telegram = body.cuidador_telegram.strip()
    user.timezone = body.timezone
    store.upsert_user(user)
    destino = user.cuidador_telefono or "sin celular"
    if phone and user.cuidador_telegram:
        detalle = f"Alertas por Telegram al {destino}"
    elif phone:
        detalle = (
            f"Celular {destino} guardado. El cuidador abre @{tools._bot_username()}, "
            "pulsa Iniciar y comparte su contacto."
        )
    else:
        detalle = "Falta el celular del cuidador"
    return {
        "ok": True,
        "message": f"Hogar configurado: {user.nombre}. {detalle}",
        "user": user.model_dump(),
        "telegram_vinculado": bool(user.cuidador_telegram),
        "bot": f"https://t.me/{tools._bot_username()}",
    }


@app.post("/demo/add-medication")
async def demo_add_medication(body: AddMedicationInput):
    store.get_or_create_user(body.user_id)
    result = tools.schedule_reminder(
        user_id=body.user_id,
        time=body.time,
        nombre=body.nombre,
        dosis=body.dosis,
        con_comida=body.con_comida,
        dias_tratamiento=body.dias_tratamiento,
        uso=body.uso or ("temporal" if body.dias_tratamiento else "cotidiano"),
        forma=body.forma,
        cantidad=body.cantidad,
        intervalo_horas=body.intervalo_horas,
    )
    return result.model_dump()


@app.post("/demo/remove-medication")
async def demo_remove_medication(body: RemoveMedicationInput):
    return tools.remove_medication(body.user_id, body.med_id, body.nombre).model_dump()


@app.post("/demo/sync-telegram")
async def demo_sync_telegram():
    return tools.sync_telegram_links().model_dump()


@app.post("/demo/confirm-taken")
async def demo_confirm_taken(user_id: str = "demo-user"):
    nxt = tools.get_next_dose(user_id)
    med_id = (nxt.data or {}).get("med_id") or "próximo"
    logged = tools.log_dose(user_id, med_id, "taken")
    adherence = tools.get_adherence_today(user_id)
    return {"ok": True, "log": logged.model_dump(), "adherence": adherence.model_dump()}


@app.post("/demo/run-full-flow")
async def demo_run_full_flow(user_id: str = "demo-user"):
    """Demo del problema completo: recordatorio → 2 sin respuesta → celular del cuidador → confirmación."""
    store.get_or_create_user(user_id)
    store.clear_doses_today(user_id)
    reminder = await demo_trigger_reminder(user_id)
    escalation = await demo_simulate_no_response(user_id)
    confirmation = await demo_confirm_taken(user_id)
    return {
        "ok": True,
        "steps": {
            "1_reminder": reminder,
            "2_escalation": escalation,
            "3_user_confirmed": confirmation,
        },
        "summary": (
            "Recordatorio generado, cuidador alertado en su celular tras 2 intentos, "
            "y dosis confirmada. Ese es el ciclo de valor de CuidaVoz."
        ),
    }


@app.get("/demo/alerts/{user_id}")
async def demo_alerts(user_id: str):
    return {"alerts": [a.model_dump() for a in store.list_alerts(user_id)]}


@app.get("/demo/adherence/{user_id}")
async def demo_adherence(user_id: str):
    return tools.get_adherence_today(user_id).model_dump()


@app.post("/demo/trigger-reminder")
async def demo_trigger_reminder(user_id: str = "demo-user"):
    nxt = tools.get_next_dose(user_id)
    if not nxt.data:
        return {"ok": False, "message": "Sin medicamentos", "triggered_at": now_iso()}

    med = nxt.data
    cuanto = med.get("cantidad") or ""
    dosis = med.get("dosis") or ""
    if cuanto and dosis:
        label = f"{cuanto} de {med['nombre']} de {dosis}"
    elif cuanto:
        label = f"{cuanto} de {med['nombre']}"
    else:
        label = f"{med['nombre']} {dosis}".strip()
    prompt = f"Es hora de tu {label}. ¿Ya lo tomaste?"
    pending = tools.log_dose(user_id, med["med_id"], "pending")
    return {
        "ok": True,
        "triggered_at": now_iso(),
        "alexa_prompt": prompt,
        "medication": med,
        "dose_log": pending.model_dump(),
        "note": "En producción esto lo dispara EventBridge hacia Alexa Proactive Events.",
    }


@app.post("/demo/simulate-no-response")
async def demo_simulate_no_response(user_id: str = "demo-user"):
    nxt = tools.get_next_dose(user_id)
    med = nxt.data or {"med_id": "med-losartan", "nombre": "Losartán", "dosis": "50 mg"}
    first = tools.log_dose(user_id, med["med_id"], "pending")
    second = tools.log_dose(user_id, med["med_id"], "pending")
    alerts = [a.model_dump() for a in store.list_alerts(user_id)]
    return {
        "ok": True,
        "attempts": [first.model_dump(), second.model_dump()],
        "caregiver_notified": bool((second.data or {}).get("caregiver_notified")),
        "alerts": alerts[-1:] if alerts else [],
        "alerta_preview": alerts[-1]["mensaje"] if alerts else None,
        "alerta_destino": alerts[-1].get("destino") if alerts else None,
        "alerta_canal": alerts[-1].get("canal") if alerts else None,
        "telegram_preview": alerts[-1]["mensaje"] if alerts else None,
    }


@app.get("/panel", response_class=HTMLResponse)
async def panel():
    index = STATIC_DIR / "panel.html"
    if index.exists():
        return FileResponse(index)
    return HTMLResponse("<h1>CuidaVoz</h1><p>Panel no encontrado.</p>", status_code=404)


app.mount("/mcp", mcp_http_app)


def run() -> None:
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port, reload=True)


if __name__ == "__main__":
    run()
