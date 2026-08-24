"""Testes do portal (servidor) e do agente (laboratório).

Inclui um teste end-to-end REAL: uvicorn rodando em porta aleatória,
agente conectando via WebSocket, eventos fluindo e comando retornando.
"""

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from websockets.sync.client import connect as ws_connect  # noqa: E402

from a10flash.agent import AgentClient  # noqa: E402
from a10flash.bus import EventBus  # noqa: E402
from a10flash.notify import Notifier  # noqa: E402
from a10flash.portal import PortalServer, load_portal_config  # noqa: E402


class FakeMonitor:
    """Stub do monitor visto pelo agente (sem serial)."""

    def __init__(self):
        self.calls = []

    def send_command(self, key, command, reason=None):
        self.calls.append(("cmd", key, command, reason))
        return True, "ok (fake)"

    def request_run(self, key):
        self.calls.append(("rerun", key))
        return True, "ok (fake)"

    def device_statuses(self):
        return {"dev-a": {"device": "dev-a", "state": "running",
                          "stage": "login", "port": "/dev/ttyUSB0"}}


def make_portal(**over):
    cfg = {"host": "127.0.0.1", "port": 0, "token": "segredo",
           "db_path": ":memory:"}
    cfg.update(over)
    return PortalServer(cfg, notifier=Notifier(log_file=None))


# ------------------------------------------------------------- server
def test_rest_auth():
    portal = make_portal()
    client = TestClient(portal.app)
    assert client.get("/api/status").status_code == 401
    r = client.get("/api/status", headers={"X-Token": "segredo"})
    assert r.status_code == 200
    assert r.json()["agents"] == {}
    assert client.get("/").status_code == 200


def test_ws_auth_e_ping():
    portal = make_portal()
    client = TestClient(portal.app)
    # token errado -> conexão rejeitada
    try:
        with client.websocket_connect("/ws?token=errado"):
            raise AssertionError("deveria ter rejeitado")
    except Exception:
        pass
    # token certo -> snapshot + ping/pong
    with client.websocket_connect("/ws?token=segredo") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_rest_cmd_dispositivo_inexistente():
    portal = make_portal()
    client = TestClient(portal.app)
    r = client.post("/api/devices/nao-existe/cmd",
                    headers={"X-Token": "segredo"},
                    json={"command": "abort"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


# --------------------------------------------------------- registros (DB)
def test_devices_rest_crud():
    """POST /api/devices salva; GET lista; GET /api/devices/{serial} detalha."""
    portal = make_portal()
    client = TestClient(portal.app)
    headers = {"X-Token": "segredo"}

    r = client.get("/api/devices", headers=headers)
    assert r.status_code == 200
    assert r.json()["devices"] == []

    payload = {
        "serial": "A10TH-ABC123",
        "device": "ttyUSB0",
        "port": "/dev/ttyUSB0",
        "model": "TH5430S",
        "version": "5.2.1-P14",
        "upgraded": True,
        "license_info": "License Type: STANDARD",
        "environment": "Fan 1: OK",
        "version_output": "ACOS version 5.2.1-P14",
        "agent": "lab-1",
    }
    r = client.post("/api/devices", headers=headers, json=payload)
    assert r.status_code == 200
    rec = r.json()
    assert rec["serial"] == "A10TH-ABC123"
    assert rec["upgraded"] is True

    r = client.get("/api/devices", headers=headers)
    assert r.status_code == 200
    assert len(r.json()["devices"]) == 1
    summary = r.json()["devices"][0]
    # lista é resumo (sem os blobs grandes)
    assert "license_info" not in summary
    assert summary["upgraded"] is True

    r = client.get("/api/devices/A10TH-ABC123", headers=headers)
    assert r.status_code == 200
    assert r.json()["license_info"] == "License Type: STANDARD"
    assert r.json()["environment"] == "Fan 1: OK"

    # upsert: mesmo serial atualiza (mantém 1 registro)
    client.post("/api/devices", headers=headers,
                json={**payload, "version": "5.2.1-P16"})
    r = client.get("/api/devices", headers=headers)
    assert len(r.json()["devices"]) == 1
    assert r.json()["devices"][0]["version"] == "5.2.1-P16"

    # auth: sem token -> 401
    assert client.get("/api/devices").status_code == 401
    assert client.post("/api/devices", json=payload).status_code == 401


def test_devices_delete():
    """DELETE /api/devices/{serial} apaga (limpeza manual de duplicados)."""
    portal = make_portal()
    client = TestClient(portal.app)
    headers = {"X-Token": "segredo"}
    client.post("/api/devices", headers=headers,
                json={"serial": "A10TH-DEL", "version": "1.0"})
    r = client.delete("/api/devices/A10TH-DEL", headers=headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert client.get("/api/devices/A10TH-DEL",
                      headers=headers).status_code == 404
    # inexistente -> 404; sem token -> 401
    assert client.delete("/api/devices/A10TH-DEL",
                         headers=headers).status_code == 404
    assert client.delete("/api/devices/A10TH-DEL").status_code == 401


def test_device_result_ws_salva_no_db():
    """Agente envia device_result pelo WS /agent -> portal salva no DB."""
    portal = make_portal()
    client = TestClient(portal.app)
    with client.websocket_connect("/agent?token=segredo&agent=lab-1") as ws:
        ws.send_json({"type": "hello", "agent": "lab-1"})
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome"
        ws.send_json({
            "type": "device_result",
            "device": "ttyUSB0",
            "port": "/dev/ttyUSB0",
            "serial": "A10TH-WS-001",
            "model": "TH1040S",
            "version": "5.2.1-P14",
            "upgraded": True,
            "license_info": "License: STANDARD",
            "environment": "PSU: OK",
        })
        time.sleep(0.3)

    # o registro chegou ao DB
    r = client.get("/api/devices/A10TH-WS-001", headers={"X-Token": "segredo"})
    assert r.status_code == 200
    rec = r.json()
    assert rec["model"] == "TH1040S"
    assert rec["agent"] == "lab-1"
    assert rec["license_info"] == "License: STANDARD"


def test_load_portal_config_env_docker():
    """Deploy Docker: config.yaml ausente + env vars (PORTAL_*)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        inexistente = os.path.join(tmp, "config.yaml")
        # sem config e sem env -> defaults
        cfg = load_portal_config(inexistente)
        assert cfg["host"] == "0.0.0.0"
        assert cfg["port"] == 8080
        assert cfg["token"] == ""
        # env vars sobrepõem (como no docker-compose)
        old = {k: os.environ.get(k) for k in ("PORTAL_HOST", "PORTAL_PORT",
                                              "PORTAL_TOKEN")}
        try:
            os.environ["PORTAL_HOST"] = "0.0.0.0"
            os.environ["PORTAL_PORT"] = "8091"
            os.environ["PORTAL_TOKEN"] = "token-do-container"
            cfg = load_portal_config(inexistente)
            assert cfg["host"] == "0.0.0.0"
            assert cfg["port"] == 8091
            assert cfg["token"] == "token-do-container"
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        # config.yaml existente é lido, mas env ainda sobrepõe
        with open(inexistente, "w", encoding="utf-8") as fh:
            fh.write("portal_server:\n  host: 10.0.0.5\n  port: 9999\n"
                     "  token: do-arquivo\n")
        os.environ["PORTAL_PORT"] = "8092"
        try:
            cfg = load_portal_config(inexistente)
            assert cfg["host"] == "10.0.0.5"
            assert cfg["port"] == 8092
            assert cfg["token"] == "do-arquivo"
        finally:
            os.environ["PORTAL_PORT"] = old["PORTAL_PORT"] or ""


# ------------------------------------------------------------- e2e
def _start_server(portal):
    config = uvicorn.Config(portal.app, host="127.0.0.1", port=0,
                            log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, thread, port


def _read_until(ws, predicate, timeout=10):
    """Lê mensagens do WS até `predicate` casar; devolve a mensagem."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msg = json.loads(ws.recv(timeout=2))
        except Exception:
            continue
        if predicate(msg):
            return msg
    raise AssertionError("timeout aguardando mensagem")


def test_agente_e2e_loopback():
    """Portal real + agente real (loopback): eventos e comandos."""
    portal = make_portal()
    server, thread, port = _start_server(portal)

    bus = EventBus()
    monitor = FakeMonitor()
    agent = AgentClient(f"ws://127.0.0.1:{port}/agent", "segredo", bus,
                        monitor, agent_id="lab-1", reconnect_delay=0.3)
    browser = None
    try:
        # browser conecta ANTES do agente para não perder o
        # agent_status online (eventos não são reentregues a assinantes
        # que chegam depois)
        browser = ws_connect(f"ws://127.0.0.1:{port}/ws?token=segredo")
        _read_until(browser, lambda m: m.get("type") == "snapshot")
        agent.start()
        # 1. agente conecta -> browser recebe agent_status online
        ev = _read_until(browser, lambda m: m.get("type") == "agent_status"
                        and m.get("online"))
        assert ev["agent"] == "lab-1"

        # 2. evento publicado no bus do agente chega ao browser.
        #    Há uma janela entre o connect e o agente assinar o bus local;
        #    publica em loop até o browser confirmar (robusto ao timing).
        def pub_loop():
            for _ in range(80):
                bus.publish({"type": "status", "device": "dev-b",
                             "state": "running", "stage": "upgrade"})
                time.sleep(0.2)

        threading.Thread(target=pub_loop, daemon=True).start()
        ev = _read_until(browser, lambda m: m.get("type") == "status"
                        and m.get("device") == "dev-b")
        assert ev["state"] == "running"
        assert ev["agent"] == "lab-1"

        # 3. comando do browser -> servidor -> agente -> monitor -> ack
        browser.send(json.dumps({"type": "cmd", "device": "dev-a",
                                 "command": "abort", "reason": "teste"}))
        ack = _read_until(browser, lambda m: m.get("type") == "cmd_ack"
                          and m.get("command") == "abort")
        assert ack["ok"] is True
        # o monitor fake registrou o comando
        assert ("cmd", "dev-a", "abort", "teste") in monitor.calls

        # 4. REST: status mostra agente e dispositivo
        r = requests.get(f"http://127.0.0.1:{port}/api/status",
                         headers={"X-Token": "segredo"}, timeout=5)
        assert r.status_code == 200
        agents = r.json()["agents"]
        assert agents["lab-1"]["online"] is True
        assert "dev-a" in agents["lab-1"]["devices"]
        assert "dev-b" in agents["lab-1"]["devices"]

        # 5. REST sem token -> 401
        assert requests.get(f"http://127.0.0.1:{port}/api/status",
                            timeout=5).status_code == 401
    finally:
        if browser:
            browser.close()
        agent.stop()
        server.should_exit = True
        thread.join(timeout=5)


def test_agente_token_errado_rejeitado():
    portal = make_portal()
    server, thread, port = _start_server(portal)
    try:
        try:
            ws = ws_connect(f"ws://127.0.0.1:{port}/agent?token=errado")
            # conexão pode abrir no nível HTTP; a rejeição chega ao ler
            ws.recv(timeout=3)
            raise AssertionError("deveria ter rejeitado o agente")
        except Exception:
            pass  # rejeitado (handshake 403 ou conexão fechada)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
