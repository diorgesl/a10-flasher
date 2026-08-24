"""Cliente AXAPI (REST HTTPS) do A10 — usado para o upgrade de firmware.

O upgrade em si é disparado pela AXAPI: o equipamento PUXA o arquivo do
servidor via SCP usando a porta de gerência (use-mgmt-port), conforme o
fluxo oficial documentado em `upgrade hd` (ACOS Docs).

Referência de implementação: ACOS-Upgrade (A10 Networks, 2017).
"""

import json
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class AxapiError(Exception):
    """Falha numa chamada AXAPI (HTTP, autenticação ou upgrade)."""


class A10Axapi:
    def __init__(self, host, username, password, port=443,
                 base_url=None, timeout=30, verify=False):
        self.host = host
        self.base = base_url or f"https://{host}:{port}/axapi/v3/"
        self.timeout = timeout
        self.verify = verify
        self.session = requests.Session()
        self.session.verify = verify
        self.token = None
        self._auth(username, password)

    # ------------------------------------------------------------ auth
    def _auth(self, username, password):
        r = self._call("auth", "POST",
                       {"credentials": {"username": username,
                                        "password": password}})
        if r.status_code != 200:
            raise AxapiError(
                f"autenticação AXAPI falhou em {self.host} "
                f"(HTTP {r.status_code}): {r.text[:300]}"
            )
        self.token = r.json()["authresponse"]["signature"]

    def _call(self, module, method="GET", payload=None, timeout=None):
        url = self.base + module
        headers = {"content-type": "application/json"}
        if self.token:
            headers["Authorization"] = f"A10 {self.token}"
        data = json.dumps(payload) if payload is not None else None
        timeout = self.timeout if timeout is None else timeout
        try:
            return self.session.request(method, url, headers=headers, data=data,
                                        timeout=timeout)
        except requests.RequestException as exc:
            raise AxapiError(f"erro de conexão AXAPI {self.host}: {exc}") from exc

    # ---------------------------------------------------------- leituras
    def show_version(self):
        r = self._call("version/oper")
        if r.status_code != 200:
            raise AxapiError(f"version/oper falhou: HTTP {r.status_code}")
        return r.json()["version"]["oper"]

    def show_bootimage(self):
        r = self._call("bootimage/oper")
        if r.status_code != 200:
            raise AxapiError(f"bootimage/oper falhou: HTTP {r.status_code}")
        return r.json()["bootimage"]["oper"]

    # ---------------------------------------------------------- upgrade
    def upgrade(self, file_url, image="sec", use_mgmt_port=True,
                timeout=None, on_progress=None, poll_every=5,
                reboot_after_upgrade=False):
        """Dispara o upgrade via SCP. `image`: 'pri' ou 'sec' (slot).

        Com `reboot_after_upgrade=True` o payload leva a flag oficial
        `reboot-after-upgrade: 1` — o ACOS reinicia SOZINHO assim que a
        imagem é instalada (doc: "reboot system after upgrade is done").
        O POST fica aberto enquanto a caixa copia a imagem (síncrono no
        ACOS) — usa o MESMO timeout do polling, não o default curto.
        Se a caixa responder 202 (assíncrono), faz polling de
        `upgrade-status/oper`, chamando `on_progress(status, message,
        elapsed)` a cada mudança de status/mensagem e como heartbeat.
        """
        timeout = timeout or self.timeout
        payload = {
            "hd": {
                "image": image,
                "use-mgmt-port": 1 if use_mgmt_port else 0,
                "file-url": file_url,
            }
        }
        if reboot_after_upgrade:
            payload["hd"]["reboot-after-upgrade"] = 1
        r = self._call("upgrade/hd", "POST", payload, timeout=timeout)
        if r.status_code == 202:
            return self._poll_upgrade(timeout=timeout, poll_every=poll_every,
                                      on_progress=on_progress,
                                      tolerate_connection_loss=bool(
                                          reboot_after_upgrade))
        if r.status_code != 200:
            raise AxapiError(
                f"upgrade/hd falhou (HTTP {r.status_code}): {r.text[:300]}"
            )
        return "OK"

    def _poll_upgrade(self, timeout=1800, poll_every=5, on_progress=None,
                      tolerate_connection_loss=False):
        deadline = time.time() + timeout
        last = ""
        last_emit = 0.0
        while time.time() < deadline:
            elapsed = int(time.time() - (deadline - timeout))
            try:
                # timeout LONGO no GET: durante a cópia/instalação a caixa
                # pode demorar minutos para responder o upgrade-status
                # (nunca usar o timeout curto de 30s aqui)
                r = self._call("upgrade-status/oper", timeout=timeout)
            except AxapiError as exc:
                # conexão caiu no meio do upgrade: com reboot-after-upgrade
                # a caixa pode estar REINICIANDO após instalar — não é
                # falha; continua tentando até o deadline
                if not tolerate_connection_loss:
                    raise
                if on_progress:
                    on_progress(-1, "caixa não responde (provável reboot "
                                    "em andamento)", elapsed)
                time.sleep(poll_every)
                continue
            if r.status_code == 200:
                try:
                    st = r.json()["upgrade-status"]["oper"]
                    status = int(st.get("status") or 0)
                    message = st.get("message", "")
                except (KeyError, TypeError, ValueError) as exc:
                    raise AxapiError(
                        "upgrade-status/oper em formato inesperado "
                        f"(HTTP {r.status_code}): {r.text[:300]}"
                    ) from exc
                changed = bool(message) and message != last
                if changed:
                    last = message
                if (on_progress and
                        (changed or elapsed - last_emit >= 30)):
                    on_progress(status, message, elapsed)
                    last_emit = elapsed
                if status == 10:
                    return f"upgrade concluído: {message}"
                if status > 7:
                    raise AxapiError(f"upgrade falhou (status {status}): {message}")
            time.sleep(poll_every)
        if tolerate_connection_loss:
            raise AxapiError(
                "upgrade não confirmado: caixa não respondeu até o timeout "
                "(provável reboot em andamento)")
        raise AxapiError("timeout aguardando o upgrade terminar")

    # ------------------------------------------------------------ escrita
    def set_bootimage(self, slot):
        """Define o slot ('pri'|'sec') para o próximo boot."""
        payload = {"bootimage": {"hd-cfg": {"hd": 1, slot: 1}}}
        r = self._call("bootimage", "POST", payload)
        if r.status_code not in (200, 202):
            raise AxapiError(f"bootimage falhou: HTTP {r.status_code}: {r.text[:300]}")

    def write_memory(self):
        r = self._call("write/memory", "POST")
        if r.status_code not in (200, 202):
            raise AxapiError(f"write/memory falhou: HTTP {r.status_code}")

    def reboot(self):
        r = self._call("reboot", "POST")
        if r.status_code not in (200, 202):
            raise AxapiError(f"reboot falhou: HTTP {r.status_code}")

    def logoff(self):
        try:
            self._call("logoff", "POST")
        except AxapiError:
            pass
