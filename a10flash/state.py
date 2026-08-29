"""Estado local persistente do monitor (PC do laboratório).

Guarda os números de série dos equipamentos que já passaram por um
ciclo BEM-SUCEDIDO, para não reprocessar a mesma caixa em loop
(após reboot/hotplug). Persistente em JSON — sobrevive a reinícios do
daemon. O operador pode forçar re-processamento com 'Repetir ciclo'
no portal (o worker é criado com force_cycle=True e ignora o cache).
"""

import json
import os
import threading
import time


class ProcessedSerials:
    def __init__(self, path=None):
        self.path = path
        self._lock = threading.Lock()
        self._data = {"serials": {}}
        if path:
            self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)
            self._data.setdefault("serials", {})
        except (OSError, ValueError):
            self._data = {"serials": {}}

    def _save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False)
            os.replace(tmp, self.path)  # atômico
        except OSError:
            pass  # sem permissão/disco — cache é best-effort

    # ----------------------------------------------------------- API
    def contains(self, serial):
        """O serial já foi processado com sucesso?"""
        if not serial:
            return False
        with self._lock:
            return serial in self._data["serials"]

    def processed_by(self, serial):
        """Porta que processou o serial ("" se desconhecida/formato antigo).

        O cache antigo guardava só o timestamp; o novo guarda {port, at}
        para distinguir o próprio worker (mesma porta — retry/ciclo em
        andamento) de OUTRO worker na mesma caixa (2º adaptador/plugue).
        """
        if not serial:
            return ""
        with self._lock:
            v = self._data["serials"].get(serial)
        if isinstance(v, dict):
            return v.get("port", "")
        return ""  # formato antigo (timestamp puro) — compat

    def processed_by_owner(self, serial):
        """Instância do worker que processou o serial ("" se desconhecida).

        Metadata apenas: o skip NÃO consulta o owner — id(self) pode ser
        reusado por outro worker e o pid+id não identifica instância de
        forma estável (a marcação sai no FIM do ciclo, então não existe
        retry do mesmo worker depois da marcação).
        """
        if not serial:
            return ""
        with self._lock:
            v = self._data["serials"].get(serial)
        if isinstance(v, dict):
            return v.get("owner", "")
        return ""  # formato antigo — compat (bloqueia, correto)

    def mark(self, serial, port=None, owner=None):
        """Registra o serial como processado (porta + instância dona)."""
        if not serial:
            return
        with self._lock:
            self._data["serials"][serial] = {
                "port": port or "",
                "owner": owner or "",
                "at": time.time(),
            }
            self._save()

    def clear(self):
        with self._lock:
            self._data["serials"] = {}
            self._save()

    def count(self):
        with self._lock:
            return len(self._data["serials"])
