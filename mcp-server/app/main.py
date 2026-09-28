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
    LogDoseInput,
    NotifyCaregiverInput,
    ScheduleReminderInput,
    ToolResponse,
    UserIdInput,
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
) -> dict:
    """Crea o actualiza un recordatorio de medicamento."""
    return tools.schedule_reminder(
        user_id=user_id,
        time=time,
        med_id=med_id,
        nombre=nombre,
        dosis=dosis,
        con_comida=con_comida,
    ).model_dump()


@mcp.tool()
def notify_caregiver(user_id: str, message: str) -> dict:
    """Registra una alerta al cuidador (Telegram cuando TELEGRAM_BOT_TOKEN está configurado)."""
    return tools.notify_caregiver(user_id, message).model_dump()


@mcp.tool()
def get_adherence_today(user_id: str) -> dict:
    """Resumen de adherencia del día."""
    return tools.get_adherence_today(user_id).model_dump()


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
    )


@app.post("/api/tools/notify_caregiver", response_model=ToolResponse)
async def rest_notify_caregiver(body: NotifyCaregiverInput):
    return tools.notify_caregiver(body.user_id, body.message)


@app.post("/api/tools/get_adherence_today", response_model=ToolResponse)
async def rest_get_adherence_today(body: UserIdInput):
    return tools.get_adherence_today(body.user_id)


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
    prompt = f"Es hora de tu {med['nombre']} {med['dosis']}. ¿Ya lo tomaste?"
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
