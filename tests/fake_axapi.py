"""Servidor AXAPI fake (HTTP) para testar o upgrade sem hardware."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeAxapiServer:
    def __init__(self, sw_version="4.1.4", boot_from="HD_PRIMARY",
                 upgrade_delay=0.0, status_message="Downloading image...",
                 fail_status=None, on_upgrade_reboot=None):
        self.calls = []                 # ("GET"|"POST", path[, payload])
        self.sw_version = sw_version
        self.boot_from = boot_from
        self.upgrade_delay = upgrade_delay   # segundos "copiando" após o POST
        self.status_message = status_message
        self.fail_status = fail_status       # se setado, upgrade falha com esse status
        self.on_upgrade_reboot = on_upgrade_reboot  # flag reboot-after-upgrade
        self.upgrade_started_at = None  # time.time() do POST /upgrade/hd
        handler = self._make_handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def base_url(self):
        return f"http://127.0.0.1:{self.port}/axapi/v3/"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ------------------------------------------------------------ http
    def _make_handler(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                srv.calls.append(("GET", self.path))
                if self.path.endswith("/version/oper"):
                    self._send({"version": {"oper": {
                        "sw-version": srv.sw_version,
                        "boot-from": srv.boot_from}}})
                elif self.path.endswith("/bootimage/oper"):
                    self._send({"bootimage": {"oper": {
                        "hd-pri": "4.0.0", "hd-sec": "4.1.4",
                        "hd-default": ("hd-pri" if srv.boot_from
                                       == "HD_PRIMARY" else "hd-sec")}}})
                elif self.path.endswith("/upgrade-status/oper"):
                    started = srv.upgrade_started_at
                    done = (started is not None and
                            time.time() - started >= srv.upgrade_delay)
                    if done and srv.fail_status is not None:
                        status, message = srv.fail_status, "Upgrade failed"
                    else:
                        status, message = (10 if done else 5), (
                            "Success" if done else srv.status_message)
                    self._send({"upgrade-status": {"oper": {
                        "status": status, "message": message}}})
                else:
                    self._send({"error": "not found"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = b""
                if length:
                    body = self.rfile.read(length)
                payload = json.loads(body) if body else {}
                srv.calls.append(("POST", self.path, payload))
                if self.path.endswith("/auth"):
                    self._send({"authresponse": {"signature": "FAKETOKEN"}})
                elif self.path.endswith("/upgrade/hd"):
                    srv.upgrade_started_at = time.time()
                    # flag reboot-after-upgrade: a caixa reinicia sozinha
                    # após instalar (simula no equipamento fake)
                    if (srv.on_upgrade_reboot
                            and payload.get("hd", {}).get("reboot-after-upgrade")):
                        srv.on_upgrade_reboot()
                    self._send({}, 202)
                elif self.path.endswith("/bootimage"):
                    self._send({})
                elif self.path.endswith("/write/memory"):
                    self._send({})
                elif self.path.endswith("/reboot"):
                    self._send({})
                elif self.path.endswith("/logoff"):
                    self._send({})
                else:
                    self._send({"error": "not found"}, 404)

        return Handler
