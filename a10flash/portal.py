"""Portal web (SERVIDOR) — hub WebSocket para agentes + dashboard.

Roda na máquina do servidor; os agentes (PCs do laboratório) conectam via
WS e mandam os eventos dos workers. O dashboard mostra tudo em tempo real
e envia comandos (abort/pause/resume/rerun) de volta aos agentes.

Rotas:
  GET  /                        -> dashboard (index.html)
  GET  /api/status              -> agentes + dispositivos (REST)
  GET  /api/events?limit=N      -> últimos eventos
  GET  /api/devices/{serial}/report -> relatório PDF via LLM
  POST /api/devices/{key}/cmd   -> {"command": "abort|pause|resume|rerun"}
  WS   /ws                      -> browser: snapshot + stream + comandos
  WS   /agent                   -> agentes: hello + eventos + recebe comandos

Rode com: python -m a10flash.portal --config config.yaml
"""

import argparse
import asyncio
import json
import os
import queue
import sys
import time

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .bus import EventBus
from .db import DeviceStore
from .notify import Notifier
from .report import ReportError, analyze_with_llm, build_pdf, pdf_filename

INDEX_HTML = os.path.join(os.path.dirname(__file__), "web", "index.html")
COMMANDS = {"abort", "pause", "resume", "rerun"}
# comandos de AGENTE (POST /api/agents/{id}/cmd) — não vão para workers
AGENT_COMMANDS = {"update"}
# tipos de mensagem aceitos dos agentes (WS /agent)
AGENT_TYPES = {"status", "stage", "log", "cmd_ack", "device", "device_result"}


class PortalServer:
    def __init__(self, config=None, bus=None, notifier=None, store=None):
        self.cfg = config or {}
        self.host = self.cfg.get("host", "0.0.0.0")
        self.port = int(self.cfg.get("port", 8080))
        self.token = self.cfg.get("token", "")
        self.bus = bus or EventBus()
        self.notifier = notifier or Notifier(log_file=None)
        self.store = store or DeviceStore(
            self.cfg.get("db_path", os.environ.get("PORTAL_DB", "a10flash.db")))
        # agent_id -> {"ws", "online", "last_seen", "devices": {key: status}}
        self.agents = {}
        self.app = FastAPI(title="a10-flasher portal", version="0.2.0")
        self._routes()

    # ---------------------------------------------------------- rotas
    def _routes(self):
        app = self.app

        @app.get("/", response_class=HTMLResponse)
        async def index():
            with open(INDEX_HTML, "r", encoding="utf-8") as fh:
                return fh.read()

        @app.get("/api/status")
        async def api_status(request: Request):
            self._authorize(request)
            return JSONResponse(self._status_payload())

        @app.get("/api/events")
        async def api_events(request: Request, limit: int = 100):
            self._authorize(request)
            return JSONResponse(
                {"events": self.bus.history(max(1, min(limit, 1000)))})

        @app.get("/api/devices")
        async def api_devices(request: Request):
            self._authorize(request)
            return JSONResponse({"devices": self.store.list()})

        @app.get("/api/devices/{serial}")
        async def api_device(serial: str, request: Request):
            self._authorize(request)
            rec = self.store.get(serial)
            if rec is None:
                raise HTTPException(status_code=404, detail="equipamento não encontrado")
            return JSONResponse(rec)

        @app.get("/api/devices/{serial}/report")
        def api_device_report(serial: str, request: Request):
            """Relatório em PDF do equipamento gerado por LLM.

            Síncrono de propósito: a chamada ao LLM pode levar dezenas de
            segundos e o FastAPI roda handlers sync numa threadpool, sem
            congelar o event loop (WS de agentes/browsers).
            """
            self._authorize(request)
            rec = self.store.get(serial)
            if rec is None:
                raise HTTPException(status_code=404,
                                    detail="equipamento não encontrado")
            try:
                analysis = analyze_with_llm(rec, self.cfg.get("llm") or {})
                pdf = build_pdf(analysis)
            except ReportError as exc:
                raise HTTPException(status_code=exc.status, detail=str(exc))
            return Response(
                content=pdf, media_type="application/pdf",
                headers={"Content-Disposition":
                         f'attachment; filename="{pdf_filename(serial)}"'},
            )

        @app.delete("/api/devices/{serial}")
        async def api_device_delete(serial: str, request: Request):
            self._authorize(request)
            if not self.store.delete(serial):
                raise HTTPException(status_code=404, detail="equipamento não encontrado")
            self.bus.publish({"type": "device_deleted", "serial": serial})
            return JSONResponse({"ok": True, "serial": serial})

        @app.post("/api/devices")
        async def api_device_save(request: Request):
            self._authorize(request)
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="corpo JSON esperado")
            rec = self._save_device_record(body.get("agent"), body)
            self.bus.publish({"type": "device_saved", "agent": body.get("agent"),
                              **{k: v for k, v in rec.items()
                                 if k not in ("license_info", "environment",
                                              "version_output")}})
            return JSONResponse(rec)

        @app.post("/api/devices/{key}/cmd")
        async def api_cmd(key: str, request: Request):
            self._authorize(request)
            body = await request.json()
            command = (body or {}).get("command")
            if command not in COMMANDS:
                raise HTTPException(status_code=400,
                                    detail=f"comando inválido: {command!r}")
            ok, message = await self._route_command(
                key, command, (body or {}).get("reason"))
            return JSONResponse({"ok": ok, "message": message})

        @app.post("/api/agents/{agent_id}/cmd")
        async def api_agent_cmd(agent_id: str, request: Request):
            """Comando para o AGENTE (não para um dispositivo): update.

            `update` faz o agente do lab puxar o código do git e
            reiniciar o serviço — o comando vai direto no WS do agente.
            """
            self._authorize(request)
            body = await request.json()
            command = (body or {}).get("command")
            if command not in AGENT_COMMANDS:
                raise HTTPException(status_code=400,
                                    detail=f"comando inválido: {command!r}")
            rec = self.agents.get(agent_id)
            if rec is None or not rec.get("online"):
                raise HTTPException(status_code=404,
                                    detail=f"agente não conectado: {agent_id}")
            try:
                await rec["ws"].send_json({
                    "type": "cmd", "device": agent_id,
                    "command": command, "reason": (body or {}).get("reason")})
            except Exception:
                raise HTTPException(status_code=502,
                                    detail="falha ao enviar ao agente")
            return JSONResponse({"ok": True,
                                 "message": "comando enviado ao agente"})

        @app.websocket("/ws")
        async def ws_browser(websocket: WebSocket):
            if not self._authorized_ws(websocket):
                await websocket.close(code=4401, reason="token inválido")
                return
            await websocket.accept()
            sid, q = self.bus.subscribe()
            sender = asyncio.create_task(self._ws_sender(websocket, q))
            try:
                await websocket.send_json(self._snapshot())
                while True:
                    msg = await websocket.receive_json()
                    await self._handle_browser_msg(websocket, msg)
            except Exception:
                pass  # desconectado
            finally:
                sender.cancel()
                self.bus.unsubscribe(sid)

        @app.websocket("/agent")
        async def ws_agent(websocket: WebSocket):
            if not self._authorized_ws(websocket):
                await websocket.close(code=4401, reason="token inválido")
                return
            await websocket.accept()
            agent_id = websocket.query_params.get("agent") or "lab"
            self.agents[agent_id] = {"ws": websocket, "online": True,
                                     "last_seen": time.time(), "devices": {}}
            self.bus.publish({"type": "agent_status", "agent": agent_id,
                              "online": True})
            self.notifier.info(None, f"agente conectado: {agent_id}")
            try:
                while True:
                    raw = await websocket.receive_text()
                    msg = json.loads(raw)
                    if msg.get("type") == "hello":
                        new_id = msg.get("agent") or agent_id
                        if new_id != agent_id:
                            self.agents.pop(agent_id, None)
                            agent_id = new_id
                            self.agents[agent_id] = {"ws": websocket,
                                                     "online": True,
                                                     "last_seen": time.time(),
                                                     "devices": {}}
                            self.bus.publish({"type": "agent_status",
                                              "agent": agent_id,
                                              "online": True})
                        for key, st in (msg.get("devices") or {}).items():
                            self._track(agent_id, {"type": "status",
                                                   "device": key, **st})
                        await websocket.send_json(
                            {"type": "welcome",
                             "agents": self._agents_summary()})
                    elif msg.get("type") in AGENT_TYPES:
                        if msg.get("type") == "device_result":
                            rec = self._save_device_record(agent_id, msg)
                            msg = {**msg, "saved": True, "serial": rec["serial"]}
                            self.bus.publish({
                                "type": "device_saved",
                                "agent": agent_id,
                                **{k: v for k, v in rec.items()
                                   if k not in ("license_info", "environment",
                                                "version_output")}})
                            continue
                        self._track(agent_id, msg)
                        self.bus.publish({**msg, "agent": agent_id})
            except Exception:
                pass
            finally:
                rec = self.agents.get(agent_id)
                if rec is not None:
                    rec["online"] = False
                    rec["last_seen"] = time.time()
                self.bus.publish({"type": "agent_status", "agent": agent_id,
                                  "online": False})
                self.notifier.warn(None, f"agente desconectado: {agent_id}")

    # ------------------------------------------------------ helpers ws
    async def _ws_sender(self, websocket, q):
        while True:
            try:
                event = await asyncio.to_thread(q.get, timeout=1.0)
            except queue.Empty:
                continue
            try:
                await websocket.send_json(event)
            except Exception:
                return

    async def _handle_browser_msg(self, websocket, msg):
        if not isinstance(msg, dict):
            return
        mtype = msg.get("type")
        if mtype == "ping":
            await websocket.send_json({"type": "pong"})
        elif mtype == "cmd":
            key = msg.get("device")
            command = msg.get("command")
            if not key or command not in COMMANDS:
                await websocket.send_json(
                    {"type": "cmd_ack", "ok": False,
                     "message": "comando inválido"})
                return
            ok, message = await self._route_command(
                key, command, msg.get("reason"))
            await websocket.send_json(
                {"type": "cmd_ack", "device": key, "command": command,
                 "ok": ok, "message": message})

    # ---------------------------------------------------- autorização
    def _authorize(self, request):
        if self.token and request.headers.get("X-Token") != self.token:
            raise HTTPException(status_code=401, detail="token inválido")

    def _authorized_ws(self, websocket):
        if not self.token:
            return True
        return (websocket.query_params.get("token") == self.token
                or websocket.headers.get("x-token") == self.token)

    # -------------------------------------------------------- comandos
    async def _route_command(self, key, command, reason=None):
        """Encaminha comando para o agente dono do dispositivo."""
        for agent_id, rec in self.agents.items():
            if rec.get("online") and key in rec.get("devices", {}):
                try:
                    await rec["ws"].send_json({
                        "type": "cmd", "device": key, "command": command,
                        "reason": reason})
                except Exception:
                    return False, "falha ao enviar ao agente"
                return True, f"comando enviado ao agente {agent_id}"
        return False, "dispositivo não encontrado em nenhum agente online"

    # ------------------------------------------------------- tracking
    def _save_device_record(self, agent_id, msg):
        """Salva o registro do equipamento (device_result) no DB.

        Campos aceitos: serial, model, version, upgraded, license_info,
        environment, version_output, interfaces, device, port. Retorna o
        registro salvo (dict).
        """
        try:
            rec = self.store.upsert(
                serial=msg.get("serial") or "",
                device_key=msg.get("device"),
                port=msg.get("port"),
                model=msg.get("model"),
                version=msg.get("version"),
                upgraded=bool(msg.get("upgraded")),
                status=msg.get("status", "success"),
                agent=agent_id,
                license_info=msg.get("license_info", ""),
                environment=msg.get("environment", ""),
                version_output=msg.get("version_output", ""),
                interfaces=msg.get("interfaces", ""),
            )
        except Exception as exc:  # DB cheio, disco, etc. — não derruba o WS
            self.notifier.error(
                None, f"falha ao salvar registro de {msg.get('device')}: {exc}")
            return {"serial": msg.get("serial") or msg.get("device") or "?"}
        self.notifier.info(
            None, f"equipamento registrado: {rec['serial']} "
                  f"({rec.get('model') or '?'} / ACOS {rec.get('version') or '?'})")
        return rec

    def _track(self, agent_id, msg):
        rec = self.agents.get(agent_id)
        if rec is None:
            return
        rec["last_seen"] = time.time()
        key = msg.get("device")
        if not key:
            return
        cur = rec["devices"].setdefault(key, {"device": key})
        # eventos de status/stage/log atualizam o retrato do dispositivo
        for field in ("state", "stage", "version", "attempts", "message",
                      "port", "level", "detail", "event", "ok"):
            if field in msg:
                cur[field] = msg[field]
        if msg.get("type") == "device" and msg.get("event") == "removed":
            rec["devices"].pop(key, None)

    # ------------------------------------------------------ payloads
    def _agents_summary(self):
        return {aid: {"online": rec["online"], "last_seen": rec["last_seen"]}
                for aid, rec in self.agents.items()}

    def _status_payload(self):
        agents = {}
        for aid, rec in self.agents.items():
            agents[aid] = {"online": rec["online"],
                           "last_seen": rec["last_seen"],
                           "devices": rec["devices"]}
        return {"agents": agents, "ts": time.time()}

    def _snapshot(self):
        return {"type": "snapshot", "agents": self._status_payload()["agents"],
                "events": self.bus.history(200)}


# ------------------------------------------------------------ CLI
def load_portal_config(path):
    """Carrega config.yaml (se existir) e sobrepõe com variáveis de ambiente.

    Env vars: PORTAL_HOST, PORTAL_PORT, PORTAL_TOKEN — essenciais para o
    deploy em Docker (container roda sem config.yaml montado).
    """
    cfg = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    portal_cfg = dict(cfg.get("portal_server") or {})
    portal_cfg["host"] = os.environ.get("PORTAL_HOST", portal_cfg.get("host", "0.0.0.0"))
    portal_cfg["port"] = int(os.environ.get("PORTAL_PORT", portal_cfg.get("port", 8080)))
    portal_cfg["token"] = os.environ.get("PORTAL_TOKEN", portal_cfg.get("token", ""))
    portal_cfg["db_path"] = os.environ.get(
        "PORTAL_DB", portal_cfg.get("db_path", "a10flash.db"))
    # LLM do relatório em PDF (seção `llm` do config + env DEEPSEEK_API_KEY)
    llm_cfg = dict(cfg.get("llm") or {})
    llm_cfg["api_key"] = os.environ.get(
        "DEEPSEEK_API_KEY", llm_cfg.get("api_key", ""))
    portal_cfg["llm"] = llm_cfg
    return portal_cfg


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="a10-flasher portal (servidor)")
    parser.add_argument("--config", default="config.yaml",
                        help="caminho do config.yaml (seção portal_server); "
                             "opcional — env vars PORTAL_HOST/PORTAL_PORT/"
                             "PORTAL_TOKEN também valem")
    args = parser.parse_args(argv)

    portal_cfg = load_portal_config(args.config)
    # notificações do próprio portal (Telegram) — se config.yaml existir
    notify_cfg = {}
    if os.path.exists(args.config):
        with open(args.config, "r", encoding="utf-8") as fh:
            notify_cfg = (yaml.safe_load(fh) or {}).get("notify", {}) or {}
    tg = notify_cfg.get("telegram", {}) or {}
    bus = EventBus()
    notifier = Notifier(
        telegram_token=tg.get("token") if tg.get("enabled") else None,
        telegram_chat_id=tg.get("chat_id"),
        log_file=notify_cfg.get("log_file"), bus=bus,
    )
    portal = PortalServer(portal_cfg, bus=bus, notifier=notifier)
    print(f"Portal em http://{portal.host}:{portal.port}"
          + (" (token configurado)" if portal.token else " (SEM token)"))
    uvicorn.run(portal.app, host=portal.host, port=portal.port,
                log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
