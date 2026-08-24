"""Agente — ponte entre o EventBus local (PC do laboratório) e o portal.

O agente roda no MESMO PC dos A10, conecta no portal via WebSocket
(conexão de SAÍDA, sem abrir porta), envia os eventos do laboratório em
tempo real e recebe comandos do operador (abort/pause/resume/rerun) para
repassar ao monitor. Reconexão automática (intervalo fixo
`reconnect_delay` entre tentativas).
"""

import json
import os
import queue
import subprocess
import threading
import time
import urllib.parse

# Branch que o lab puxa na atualização de código (auto-update / comando
# `update` do portal). O sistema roda a partir do main do repositório.
GIT_BRANCH = "origin/main"


class AgentClient:
    def __init__(self, url, token, bus, monitor, agent_id="lab",
                 notifier=None, reconnect_delay=3.0, verify_tls=True,
                 auto_update=False, auto_update_interval=600):
        self.url = url
        self.token = token
        self.bus = bus
        self.monitor = monitor
        self.agent_id = agent_id
        self.notifier = notifier
        self.reconnect_delay = reconnect_delay
        self.verify_tls = verify_tls
        self.auto_update = auto_update
        self.auto_update_interval = auto_update_interval
        self._stop = threading.Event()
        self._ws = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="agent-client")

    # ------------------------------------------------------------ API
    def start(self):
        self._thread.start()
        if self.auto_update:
            threading.Thread(target=self._auto_update_loop, daemon=True,
                             name="agent-autoupdate").start()

    def stop(self):
        self._stop.set()
        self._close_ws()

    @property
    def connected(self):
        with self._lock:
            return self._ws is not None

    # ---------------------------------------------------------- infra
    def _close_ws(self):
        with self._lock:
            ws = self._ws
            self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _set_ws(self, ws):
        with self._lock:
            self._ws = ws

    def _connect_url(self):
        # só o agent_id vai na URL; o token segue como header x-token —
        # query string fica nos logs do Traefik/uvicorn (o portal aceita
        # os dois formatos)
        sep = "&" if "?" in self.url else "?"
        a = urllib.parse.quote(self.agent_id, safe="")
        return f"{self.url}{sep}agent={a}"

    # ----------------------------------------------------------- loop
    def _run(self):
        import ssl
        import websockets.sync.client as wsc
        ssl_ctx = None
        if not self.verify_tls:
            # útil durante transição (Traefik ainda com cert self-signed);
            # com wss:// + cert válido deixe verify_tls=true (padrão)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ssl_ctx = ctx
            if self.notifier:
                self.notifier.warn(
                    None, "agente: VERIFICAÇÃO TLS DESABILITADA "
                          "(verify_tls=false) — conexão sujeita a MITM")
        while not self._stop.is_set():
            try:
                headers = ({"x-token": self.token} if self.token else None)
                with wsc.connect(self._connect_url(), open_timeout=10,
                                 ssl=ssl_ctx,
                                 additional_headers=headers) as ws:
                    self._set_ws(ws)
                    if self.notifier:
                        self.notifier.info(
                            None, f"agente '{self.agent_id}' conectado ao "
                                  f"portal {self.url}")
                    ws.send(json.dumps({
                        "type": "hello",
                        "agent": self.agent_id,
                        "devices": self._sync_devices(),
                    }))
                    threading.Thread(target=self._reader, args=(ws,),
                                     daemon=True).start()
                    self._forward(ws)   # bloqueia enquanto houver conexão
            except Exception as exc:
                if not self._stop.is_set() and self.notifier:
                    self.notifier.warn(
                        None, f"agente: falha de conexão ({exc}) — "
                              f"reconectando em {self.reconnect_delay}s")
            finally:
                self._set_ws(None)
            if self._stop.is_set():
                break
            time.sleep(self.reconnect_delay)

    def _sync_devices(self):
        if self.monitor is None:
            return {}
        return self.monitor.device_statuses()

    # ---------------------------------------------------------- leitura
    def _reader(self, ws):
        while not self._stop.is_set():
            try:
                raw = ws.recv()
            except Exception:
                return
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            mtype = msg.get("type")
            if mtype == "cmd":
                self._handle_cmd(msg)
            elif mtype == "ping":
                try:
                    ws.send(json.dumps({"type": "pong"}))
                except Exception:
                    return

    def _handle_cmd(self, msg):
        key = msg.get("device")
        command = msg.get("command")
        if command == "update":
            # comando do AGENTE (não do worker): atualiza o código do lab
            # via git e reinicia o serviço (systemd Restart=always sobe
            # com a versão nova). Nunca roda no meio de um ciclo.
            res = self._do_update()
            self.bus.publish({"type": "cmd_ack", "device": key,
                              "command": command,
                              "ok": res["status"] in ("updated", "unchanged"),
                              "message": res["message"]})
            if res["status"] == "updated":
                self._restart()
            return
        ok, message = False, "monitor indisponível"
        if self.monitor is not None:
            if command == "rerun":
                ok, message = self.monitor.request_run(key)
            else:
                ok, message = self.monitor.send_command(
                    key, command, msg.get("reason"))
        self.bus.publish({"type": "cmd_ack", "device": key,
                          "command": command, "ok": ok, "message": message})

    # -------------------------------------------------------- auto-update
    def _do_update(self, branch=GIT_BRANCH):
        """Puxa o código novo do git (fetch + reset --hard) se o lab
        estiver ocioso.

        Retorna {"status": "busy"|"updated"|"unchanged"|"error",
                 "message": str}. Só "updated" pede reinício do serviço.
        """
        if self.monitor is not None and self.monitor.has_active_cycle():
            return {"status": "busy",
                    "message": "ciclo em andamento — atualização recusada"}
        if self.notifier:
            self.notifier.info(None, "checando atualização do código (git)...")
        try:
            subprocess.run(["git", "fetch", "origin"], capture_output=True,
                           timeout=60, check=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"],
                                  capture_output=True, timeout=10,
                                  check=True).stdout.strip()
            remote = subprocess.run(["git", "rev-parse", branch],
                                    capture_output=True, timeout=10,
                                    check=True).stdout.strip()
        except Exception as exc:
            msg = f"falha ao checar atualização (git): {exc}"
            if self.notifier:
                self.notifier.warn(None, msg)
            return {"status": "error", "message": msg}
        if head == remote:
            return {"status": "unchanged",
                    "message": "código já está atualizado — nada a fazer"}
        try:
            subprocess.run(["git", "reset", "--hard", branch],
                           capture_output=True, timeout=60, check=True)
        except Exception as exc:
            msg = f"falha ao aplicar atualização (git): {exc}"
            if self.notifier:
                self.notifier.warn(None, msg)
            return {"status": "error", "message": msg}
        if self.notifier:
            self.notifier.info(
                None, "código atualizado — reiniciando serviço (systemd "
                      "Restart=always sobe com a versão nova)...")
        return {"status": "updated",
                "message": "código atualizado — reiniciando serviço"}

    def _restart(self, delay=1.0):
        """Sai do processo para o systemd reiniciar com o código novo.

        Pausa curta antes do exit para o ack/evento sair pelo WS (o
        portal mostra o resultado da atualização).
        """

        def _exit_later():
            time.sleep(delay)
            os._exit(0)

        threading.Thread(target=_exit_later, daemon=True).start()

    def _auto_check(self):
        """Uma rodada do auto-update: só age conectado ao portal e com o
        lab ocioso. Retorna o status do _do_update (ou "offline")."""
        if not self.connected:
            return {"status": "offline", "message": "desconectado do portal"}
        res = self._do_update()
        if res["status"] == "updated":
            self._restart()
        return res

    def _auto_update_loop(self):
        while not self._stop.is_set():
            time.sleep(self.auto_update_interval)
            self._auto_check()

    # ----------------------------------------------------------- envio
    def _forward(self, ws):
        sid, q = self.bus.subscribe()
        try:
            while not self._stop.is_set():
                try:
                    event = q.get(timeout=1.0)
                except queue.Empty:
                    continue
                try:
                    ws.send(json.dumps(event))
                except Exception:
                    return
        finally:
            self.bus.unsubscribe(sid)
