"""Caixa de comandos de cada worker (thread-safe).

O portal envia comandos ("abort", "pause", "resume") pela mailbox; o worker
consome nas fronteiras de estágio — nunca no meio de uma operação crítica.
"""

import queue


class Mailbox:
    def __init__(self):
        self._q = queue.Queue()

    def send(self, command):
        """command: dict com pelo menos {"command": str}."""
        self._q.put(command)

    def drain(self):
        """Retorna e esvazia os comandos pendentes."""
        cmds = []
        while True:
            try:
                cmds.append(self._q.get_nowait())
            except queue.Empty:
                break
        return cmds
