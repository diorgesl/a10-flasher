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

    def send_command(self, key, command, reason=None, **extra):
        self.calls.append(("cmd", key, command, reason, extra))
        return True, "ok (fake)"

    def send_command_all(self, command, reason=None, **extra):
        self.calls.append(("cmd_all", command, reason, extra))
        return True, "comando enviado a 1 worker(s)"

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
            # restaura o ambiente original (sem deixar PORTAL_PORT="" —
            # env vazia quebra int() nos testes seguintes)
            if old["PORTAL_PORT"] is None:
                os.environ.pop("PORTAL_PORT", None)
            else:
                os.environ["PORTAL_PORT"] = old["PORTAL_PORT"]


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
        #    (o PORTAL manda um ack de roteamento na hora; espera o ack
        #    de EXECUÇÃO do agente — message 'ok (fake)' — que só sai
        #    DEPOIS do monitor registrar o comando)
        browser.send(json.dumps({"type": "cmd", "device": "dev-a",
                                 "command": "abort", "reason": "teste"}))
        ack = _read_until(browser, lambda m: m.get("type") == "cmd_ack"
                          and m.get("command") == "abort"
                          and m.get("message") == "ok (fake)")
        assert ack["ok"] is True
        # o monitor fake registrou o comando
        assert ("cmd", "dev-a", "abort", "teste", {}) in monitor.calls

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


# ----------------------------------------------------- relatório PDF
def test_report_endpoint_gera_pdf():
    """GET /api/devices/{serial}/report -> PDF do equipamento (via LLM).

    A chamada ao LLM é mockada (sem rede); o PDF é gerado de verdade.
    """
    import a10flash.portal as portal_mod

    portal = make_portal(llm={"api_key": "sk-teste"})
    client = TestClient(portal.app)
    headers = {"X-Token": "segredo"}
    client.post("/api/devices", headers=headers,
                json={"serial": "A10TH-REP", "model": "TH5430S",
                      "version": "5.2.1-P14", "upgraded": True,
                      "environment": "Fan 1: OK"})

    analise = {
        "titulo": "Relatório do equipamento A10TH-REP",
        "resumo": "ok", "identificacao": "x", "firmware": "y",
        "licencas": "z", "hardware": "w", "recomendacoes": "r",
    }
    old = portal_mod.analyze_with_llm
    portal_mod.analyze_with_llm = lambda rec, cfg: analise
    try:
        r = client.get("/api/devices/A10TH-REP/report", headers=headers)
    finally:
        portal_mod.analyze_with_llm = old

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert "relatorio-A10TH-REP.pdf" in r.headers["content-disposition"]
    assert r.content[:5] == b"%PDF-"

    # sem token -> 401; serial inexistente -> 404
    assert client.get("/api/devices/A10TH-REP/report").status_code == 401
    assert client.get("/api/devices/NAO-EXISTE/report",
                      headers=headers).status_code == 404


def test_report_endpoint_sem_chave_llm_503():
    """Portal sem llm.api_key -> 503 com mensagem clara (não chama o LLM)."""
    portal = make_portal()  # sem seção llm
    client = TestClient(portal.app)
    headers = {"X-Token": "segredo"}
    client.post("/api/devices", headers=headers, json={"serial": "A10TH-NOKEY"})
    r = client.get("/api/devices/A10TH-NOKEY/report", headers=headers)
    assert r.status_code == 503
    assert "api" in r.json()["detail"].lower()


def test_load_portal_config_llm_env_docker():
    """Seção llm do config.yaml + env DEEPSEEK_API_KEY (padrão do deploy)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("llm:\n  api_key: do-arquivo\n  model: deepseek-v4-flash\n")
        old = os.environ.get("DEEPSEEK_API_KEY")
        try:
            os.environ["DEEPSEEK_API_KEY"] = "chave-do-container"
            cfg = load_portal_config(path)
            assert cfg["llm"]["api_key"] == "chave-do-container"
            assert cfg["llm"]["model"] == "deepseek-v4-flash"
        finally:
            if old is None:
                os.environ.pop("DEEPSEEK_API_KEY", None)
            else:
                os.environ["DEEPSEEK_API_KEY"] = old
        # sem env nem seção -> llm presente mas sem chave
        cfg = load_portal_config(os.path.join(tmp, "nao-existe.yaml"))
        assert cfg["llm"]["api_key"] == ""


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


def test_endpoint_agents_cmd_update_chega_no_agente():
    """POST /api/agents/{id}/cmd {'command': 'update'} -> o agente
    recebe o comando pelo WS (canal do agente, não do dispositivo)."""
    portal = make_portal()
    client = TestClient(portal.app)
    with client.websocket_connect("/agent?token=segredo&agent=lab-1") as ws:
        ws.send_json({"type": "hello", "agent": "lab-1"})
        assert ws.receive_json()["type"] == "welcome"
        r = client.post("/api/agents/lab-1/cmd",
                        headers={"X-Token": "segredo"},
                        json={"command": "update"})
        assert r.status_code == 200, r.text
        msg = ws.receive_json()
        assert msg["type"] == "cmd"
        assert msg["command"] == "update"
    # agente desconectado -> 404
    r = client.post("/api/agents/lab-1/cmd",
                    headers={"X-Token": "segredo"},
                    json={"command": "update"})
    assert r.status_code == 404


def test_uptime_sample_ws_salva_e_endpoint_lista():
    """Agente envia uptime_sample pelo WS /agent -> portal salva no DB;
    GET /api/devices/{serial}/uptime devolve o histórico (novo primeiro)."""
    portal = make_portal()
    client = TestClient(portal.app)
    with client.websocket_connect("/agent?token=segredo&agent=lab-1") as ws:
        ws.send_json({"type": "hello", "agent": "lab-1"})
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "uptime_sample", "device": "ttyUSB0",
                      "serial": "A10TH-UP-001", "ts": 123.0,
                      "uptime_s": 7380})
        ws.send_json({"type": "uptime_sample", "device": "ttyUSB0",
                      "serial": "A10TH-UP-001", "ts": 999.0,
                      "uptime_s": 9000})
        time.sleep(0.3)

    r = client.get("/api/devices/A10TH-UP-001/uptime",
                   headers={"X-Token": "segredo"})
    assert r.status_code == 200
    samples = r.json()["samples"]
    assert len(samples) == 2
    assert samples[0]["uptime_s"] == 9000   # mais recente primeiro
    # sem token -> 401
    assert client.get("/api/devices/A10TH-UP-001/uptime").status_code == 401


def test_uptime_sample_com_falha_no_db_nao_derruba_agente():
    """Erro ao salvar amostra de uptime NÃO pode derrubar a conexão do
    agente: o except externo do ws_agent engolia a exceção e o agente
    caía sem NENHUM erro visível ('modo teste nunca salva em lugar
    nenhum' do lab)."""
    portal = make_portal()
    seen = []

    def boom(*a, **k):
        raise RuntimeError("db cheio")

    portal.store.add_uptime_sample = boom
    portal.notifier.error = lambda dev, msg: seen.append(msg)
    client = TestClient(portal.app)
    with client.websocket_connect("/agent?token=segredo&agent=lab-1") as ws:
        ws.send_json({"type": "hello", "agent": "lab-1"})
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "uptime_sample", "device": "ttyUSB0",
                      "serial": "A10TH-UP-002", "ts": 1.0, "uptime_s": 10})
        time.sleep(0.3)
        # a conexão segue VIVA: um status seguinte ainda é processado
        ws.send_json({"type": "status", "device": "ttyUSB0",
                      "state": "running", "port": "/dev/ttyUSB0"})
        time.sleep(0.3)
        assert portal.agents["lab-1"]["online"] is True
    # e o erro foi REGISTRADO (não engolido em silêncio)
    assert any("uptime" in m for m in seen), seen


# ------------------------------------------------------------- burn-in
def test_rest_burnin_history_vazio():
    portal = make_portal()
    client = TestClient(portal.app)
    r = client.get("/api/devices/SER-1/burnin",
                   headers={"X-Token": "segredo"})
    assert r.status_code == 200
    assert r.json() == {"runs": [], "samples": {}}


def test_burnin_events_via_ws_agente_salvam_no_db():
    portal = make_portal()
    client = TestClient(portal.app)
    portal.store.start_burnin_run("run-9", "SER-9", "dev-9", 100, 1,
                                  1000.0)
    with client.websocket_connect("/agent?token=segredo") as ws:
        ws.send_json({"type": "hello", "agent": "lab-1"})
        ws.receive_json()   # welcome
        ws.send_json({"type": "burnin_sample", "run_id": "run-9",
                      "serial": "SER-9", "ts": 1010.0, "tx_bps": 1,
                      "rx_bps": 2, "tx_pps": 3, "rx_pps": 4,
                      "active_sessions": 5, "errors": 0,
                      "uptime_s": 60})
        time.sleep(0.2)
        ws.send_json({"type": "burnin_result", "run_id": "run-9",
                      "serial": "SER-9", "ts": 1020.0,
                      "verdict": "pass", "reason": "", "summary": "ok"})
        time.sleep(0.2)
    assert len(portal.store.list_burnin_samples("run-9")) == 1
    runs = portal.store.list_burnin_runs("SER-9")
    assert runs and runs[0]["verdict"] == "pass"
    assert portal.store.active_burnin("SER-9") is None


def test_rest_burnin_start_valida_estado():
    portal = make_portal()
    client = TestClient(portal.app)
    portal.store.upsert(serial="SER-1", device_key="dev-a")
    # caixa conectada em test_mode (via hello do agente)
    with client.websocket_connect("/agent?token=segredo") as ws:
        ws.send_json({"type": "hello", "agent": "lab-1",
                      "devices": {"dev-a": {"device": "dev-a",
                                            "state": "test_mode"}}})
        ws.receive_json()   # welcome
        # start válido -> 200
        r = client.post("/api/devices/SER-1/burnin/start",
                        json={"cps": 2000},
                        headers={"X-Token": "segredo"})
        assert r.status_code == 200, r.text
        # run já em andamento -> 409
        portal.store.start_burnin_run("run-1", "SER-1", "dev-a", 1000,
                                      24, time.time())
        r = client.post("/api/devices/SER-1/burnin/start",
                        json={}, headers={"X-Token": "segredo"})
        assert r.status_code == 409, r.text
    # sem run ativo, mas caixa fora de test_mode -> 409
    portal.store.finish_burnin_run("run-1", time.time(), "aborted", "",
                                   "[]", "")
    with client.websocket_connect("/agent?token=segredo") as ws:
        ws.send_json({"type": "hello", "agent": "lab-1",
                      "devices": {"dev-a": {"device": "dev-a",
                                            "state": "running"}}})
        ws.receive_json()   # welcome
    r = client.post("/api/devices/SER-1/burnin/start",
                    json={}, headers={"X-Token": "segredo"})
    assert r.status_code == 409, r.text


def test_rest_burnin_stop_sem_run_409():
    portal = make_portal()
    client = TestClient(portal.app)
    r = client.post("/api/devices/SER-X/burnin/stop",
                    json={}, headers={"X-Token": "segredo"})
    assert r.status_code == 409


def test_rest_burnin_force_stop_encerra_run_preso():
    """Escape hatch: run órfã (burnin_result perdido) é encerrada no DB
    e o portal volta a permitir start/stop."""
    portal = make_portal()
    client = TestClient(portal.app)
    portal.store.upsert(serial="SER-1", device_key="dev-a")
    portal.store.start_burnin_run("run-1", "SER-1", "dev-a", 1000,
                                  24, time.time())
    r = client.post("/api/devices/SER-1/burnin/force_stop",
                    headers={"X-Token": "segredo"})
    assert r.status_code == 200, r.text
    assert "1 run(s) encerrado(s) no portal" in r.json()["message"]
    assert portal.store.active_burnin("SER-1") is None
    runs = portal.store.list_burnin_runs("SER-1")
    assert runs[0]["verdict"] == "aborted"
    assert "portal" in runs[0]["reason"]


def test_rest_burnin_force_stop_idempotente_sem_run():
    portal = make_portal()
    client = TestClient(portal.app)
    portal.store.upsert(serial="SER-1", device_key="dev-a")
    r = client.post("/api/devices/SER-1/burnin/force_stop",
                    headers={"X-Token": "segredo"})
    assert r.status_code == 200, r.text
    assert "nenhum burn-in ativo" in r.json()["message"]


def test_rest_burnin_force_stop_sem_registro_404():
    portal = make_portal()
    client = TestClient(portal.app)
    r = client.post("/api/devices/SER-X/burnin/force_stop",
                    headers={"X-Token": "segredo"})
    assert r.status_code == 404


def test_rest_burnin_delete_apaga_historico():
    portal = make_portal()
    client = TestClient(portal.app)
    portal.store.upsert(serial="SER-1", device_key="dev-a")
    portal.store.start_burnin_run("run-1", "SER-1", "dev-a", 1000,
                                  24, time.time())
    portal.store.add_burnin_sample("run-1", "SER-1", 1000.0, 1, 2, 3, 4,
                                   5, 0, 60)
    r = client.delete("/api/devices/SER-1/burnin",
                      headers={"X-Token": "segredo"})
    assert r.status_code == 200, r.text
    assert "1 run(s)" in r.json()["message"]
    assert portal.store.list_burnin_runs("SER-1") == []
    assert portal.store.list_burnin_samples("run-1") == []


def test_rest_burnin_delete_sem_registro_404():
    portal = make_portal()
    client = TestClient(portal.app)
    r = client.delete("/api/devices/SER-X/burnin",
                      headers={"X-Token": "segredo"})
    assert r.status_code == 404


def test_start_burnin_run_encerra_run_anterior():
    """Self-heal: run ativa anterior da MESMA caixa (resultado perdido)
    não pode acumular — o novo run a encerra como aborted."""
    portal = make_portal()
    portal.store.start_burnin_run("run-1", "SER-1", "dev-a", 1000,
                                  24, time.time())
    portal.store.start_burnin_run("run-2", "SER-1", "dev-a", 2000,
                                  12, time.time() + 10)
    runs = {r["run_id"]: r for r in portal.store.list_burnin_runs("SER-1")}
    assert runs["run-1"]["verdict"] == "aborted"
    assert runs["run-1"]["ended_ts"] is not None
    assert runs["run-2"]["verdict"] is None
    assert portal.store.active_burnin("SER-1")["run_id"] == "run-2"


def test_agent_burnin_stop_vira_broadcast():
    """burnin_stop chega no agente sem chave confiável — o agente manda
    para TODOS os workers (o controller ativo reage, os demais descartam)."""
    from a10flash.agent import AgentClient
    from a10flash.bus import EventBus

    mon = FakeMonitor()
    agent = AgentClient(url="ws://127.0.0.1:1", token="x", bus=EventBus(),
                        monitor=mon, agent_id="lab-1")
    agent._handle_cmd({"type": "cmd", "device": "dev-a",
                       "command": "burnin_stop", "reason": None})
    assert ("cmd_all", "burnin_stop", None, {}) in mon.calls
    # comandos comuns continuam roteados por chave
    agent._handle_cmd({"type": "cmd", "device": "dev-a",
                       "command": "abort", "reason": "teste"})
    assert ("cmd", "dev-a", "abort", "teste", {}) in mon.calls


def test_status_ws_preserva_identidade_no_card():
    """Status periódico (burn-in etc.) não pode zerar o card do ciclo.

    O _track do portal repassa ao retrato do dispositivo apenas uma lista
    fixa de campos — serial/model/mgmt_ip precisam estar nela, senão o
    burn-in (status a cada ~60s) apagava modelo/serial/IP de gerência
    do card do dashboard.
    """
    portal = make_portal()
    client = TestClient(portal.app)
    with client.websocket_connect("/agent?token=segredo&agent=lab-1") as ws:
        ws.send_json({"type": "hello", "agent": "lab-1"})
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({
            "type": "status",
            "device": "ttyUSB1",
            "port": "/dev/ttyUSB1",
            "state": "running",
            "stage": "burnin",
            "serial": "TH30B23316450072",
            "model": "TH4430S",
            "mgmt_ip": "10.0.0.77",
        })
        time.sleep(0.3)

    r = client.get("/api/status", headers={"X-Token": "segredo"})
    assert r.status_code == 200
    card = r.json()["agents"]["lab-1"]["devices"]["ttyUSB1"]
    assert card["serial"] == "TH30B23316450072"
    assert card["model"] == "TH4430S"
    assert card["mgmt_ip"] == "10.0.0.77"
