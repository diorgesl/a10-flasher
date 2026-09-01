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
# Sonda de saúde do ws quando o bus está quieto (ver _forward): sem ela
# o _forward preso em q.get só notava a morte do portal quando o bus
# emitia algo (modo teste pode ficar 1h em silêncio) e a reconexão
# nunca acontecia com o lab ocioso.
WS_PROBE_INTERVAL = 10.0
WS_PROBE_TIMEOUT = 5.0  # pong não veio neste tempo = conexão ruim
# Raiz do repositório (onde o código ESTÁ rodando) — o git roda AQUI,
# independente do cwd de quem iniciou o processo (fetch já falhou com
# exit 128 quando o agente subia de outro diretório: "not a git repo").
CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
        # identidade (serial/model/mgmt_ip) vista nos eventos do bus por
        # dispositivo — o monitor não a guarda, e sem este cache o hello
        # da reconexão voltaria ao portal SEM identidade (o card ficaria
        # sem modelo/serial/IP até o próximo status do worker)
        self._ident = {}
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
        out = {}
        for key, st in self.monitor.device_statuses().items():
            # o monitor só agrega estágio; a identidade vem do cache de
            # eventos do bus (serial/model/mgmt_ip sobrevivem à
            # reconexão com o portal)
            out[key] = {**self._ident.get(key, {}), **st}
        return out

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
            extra = {k: v for k, v in msg.items()
                     if k not in ("type", "device", "command", "reason")}
            if command == "burnin_stop":
                # broadcast: o agente não precisa saber QUAL worker tem o
                # burn-in ativo — manda para todos (os demais descartam)
                ok, message = self.monitor.send_command_all(
                    command, msg.get("reason"), **extra)
            elif command == "rerun":
                ok, message = self.monitor.request_run(key)
            else:
                ok, message = self.monitor.send_command(
                    key, command, msg.get("reason"), **extra)
        self.bus.publish({"type": "cmd_ack", "device": key,
                          "command": command, "ok": ok, "message": message})

    # -------------------------------------------------------- auto-update
    @staticmethod
    def _git_failure_detail(exc):
        """O MOTIVO real do git (stderr) para a mensagem — sem isso o
        operador só vê 'exit status 128' e não sabe o que aconteceu
        (ex.: remoto 'origin' não configurado)."""
        try:
            detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
        except Exception:
            detail = ""
        return detail or str(exc)

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
                           timeout=60, check=True, cwd=CODE_DIR)
            head = subprocess.run(["git", "rev-parse", "HEAD"],
                                  capture_output=True, timeout=10,
                                  check=True, cwd=CODE_DIR).stdout.strip()
            remote = subprocess.run(["git", "rev-parse", branch],
                                    capture_output=True, timeout=10,
                                    check=True, cwd=CODE_DIR).stdout.strip()
        except Exception as exc:
            msg = (f"falha ao checar atualização (git): "
                   f"{self._git_failure_detail(exc)}")
            if self.notifier:
                self.notifier.warn(None, msg)
            return {"status": "error", "message": msg}
        if head == remote:
            return {"status": "unchanged",
                    "message": "código já está atualizado — nada a fazer"}
        try:
            subprocess.run(["git", "reset", "--hard", branch],
                           capture_output=True, timeout=60, check=True,
                           cwd=CODE_DIR)
        except Exception as exc:
            msg = (f"falha ao aplicar atualização (git): "
                   f"{self._git_failure_detail(exc)}")
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
        last_probe = time.monotonic()
        try:
            while not self._stop.is_set():
                try:
                    event = q.get(timeout=1.0)
                except queue.Empty:
                    # lab quieto: sonda o ws (ping de protocolo — o
                    # servidor responde pong sem tocar no app). Falhou =
                    # portal caiu: sai para o loop de reconexão agir.
                    if time.monotonic() - last_probe >= WS_PROBE_INTERVAL:
                        try:
                            if not ws.ping().wait(WS_PROBE_TIMEOUT):
                                return
                        except Exception:
                            return
                        last_probe = time.monotonic()
                    continue
                if event.get("type") in ("status", "device_result"):
                    # guarda a identidade vista no bus para o hello da
                    # reconexão levar junto (ver _sync_devices)
                    ident = {k: event[k] for k in ("serial", "model",
                                                   "mgmt_ip")
                             if event.get(k)}
                    if ident:
                        self._ident.setdefault(
                            event["device"], {}).update(ident)
                try:
                    ws.send(json.dumps(event))
                except Exception:
                    return
        finally:
            self.bus.unsubscribe(sid)
