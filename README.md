# CuidaVoz

Voice agent for medication adherence. Amazon Build, Ship, Shape 2026 (Alexa+ track).

## Track

- Primary: Alexa+
- MCP Streamable HTTP, spec 2025-11-25
- Live: https://cuidavoz-mcp.onrender.com

## Layout

```
├── mcp-server/   FastAPI MCP + demo panel
├── CuidaVoz/     Alexa Hosted Skill
├── render.yaml
└── LICENSE
```

## Run locally

```powershell
cd mcp-server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app.main
```

- http://127.0.0.1:8000/health
- http://127.0.0.1:8000/mcp/
- http://127.0.0.1:8000/panel

## Alexa

Invocation: `cuida voz`

## License

MIT
