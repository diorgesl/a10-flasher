"""Event bus thread-safe.

Workers (threads) publicam eventos; clientes asyncio (WebSocket) assinam
via filas thread-safe e os consomem com polling bloqueante (to_thread).
Mantém também um histórico em anel para snapshots e endpoint REST.
"""

import itertools
import queue
import threading
import time


class EventBus:
    def __init__(self, history_size=500):
        self._subs = {}
        self._lock = threading.Lock()
        self._history = []
        self._history_size = history_size
        self._seq = itertools.count(1)

    def subscribe(self):
        """Registra um assinante. Retorna (id, queue.Queue)."""
        q = queue.Queue(maxsize=2000)
        with self._lock:
            sid = next(self._seq)
            self._subs[sid] = q
        return sid, q

    def unsubscribe(self, sid):
        with self._lock:
            self._subs.pop(sid, None)

    def history(self, limit=None):
        with self._lock:
            return list(self._history[-limit:]) if limit else list(self._history)

    def publish(self, event):
        """Publica um evento (dict). Pode ser chamado de qualquer thread."""
        event = dict(event)
        event.setdefault("ts", time.time())
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_size:
                del self._history[:len(self._history) - self._history_size]
            subs = list(self._subs.values())
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                # assinante lento: descarta o mais antigo para não travar
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass
