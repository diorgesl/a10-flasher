"""Agente — ponte entre o EventBus local (PC do laboratório) e o portal.

O agente roda no MESMO PC dos A10, conecta no portal via WebSocket
(conexão de SAÍDA, sem abrir porta), envia os eventos do laboratório em
tempo real e recebe comandos do operador (abort/pause/resume/rerun) para
repassar ao monitor. Reconexão automática (intervalo fixo
`reconnect_delay` entre tentativas).
"""

import json
import queue
import threading
import time
import urllib.parse


class AgentClient:
    def __init__(self, url, token, bus, monitor, agent_id="lab",
                 notifier=None, reconnect_delay=3.0, verify_tls=True):
        self.url = url
        self.token = token
        self.bus = bus
        self.monitor = monitor
        self.agent_id = agent_id
        self.notifier = notifier
        self.reconnect_delay = reconnect_delay
        self.verify_tls = verify_tls
        self._stop = threading.Event()
        self._ws = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="agent-client")

    # ------------------------------------------------------------ API
    def start(self):
        self._thread.start()

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
        ok, message = False, "monitor indisponível"
        if self.monitor is not None:
            if command == "rerun":
                ok, message = self.monitor.request_run(key)
            else:
                ok, message = self.monitor.send_command(
                    key, command, msg.get("reason"))
        self.bus.publish({"type": "cmd_ack", "device": key,
                          "command": command, "ok": ok, "message": message})

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
