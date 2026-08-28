# Burn-in de estabilidade com TRex — Plano de Implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatizar o burn-in de 24h (config CGNAT/LSN + tráfego TRex + veredito de estabilidade) no worker do a10-flasher, com resultado e histórico no portal.

**Architecture:** O burn-in é uma fase do worker entre o ciclo e o modo teste: `a10flash/trex_client.py` (cliente TRex local) + `a10flash/burnin.py` (regra de portas, template LSN e loop de 24h) + integração em `worker.py` (comandos `burnin_start`/`burnin_stop` pela mailbox existente) + eventos/tabelas/endpoints novos no portal.

**Tech Stack:** Python (stdlib + pyserial já existentes; lib Python do TRex vem da instalação v3.08 no PC do lab, sem pip), SQLite, FastAPI, HTML/JS vanilla no dashboard.

**Spec:** `docs/superpowers/specs/2026-08-28-trex-burnin-design.md` — o plano argumenta a partir dela; ler as duas juntas.

## Global Constraints

- Código e comentários em **pt-BR** (convenção do projeto).
- **Nenhuma dependência nova** (a lib do TRex entra no `sys.path` em runtime, de `<trex.path>/automation/trex_control_plane/interactive/`).
- `cfg.get("trex", {}).get("enabled", False)` — **default False** quando a seção não existe (não quebra labs/testes atuais).
- Duração default 24h, CPS default 1000, amostra default 60s.
- Suíte existente **verde a cada task** (rodar os testes da task + `pytest tests/test_worker.py tests/test_portal.py -q` ao final de cada task que tocar nesses arquivos).
- Commits no final de cada task, mensagem em pt-BR com trailer `Co-Authored-By: Claude <noreply@anthropic.com>`.
- `graphify update .` após modificar código (regra do projeto) — feito na Task 8.

---

### Task 1: `TRexClient` (daemon + ASTF + stats)

**Files:**
- Create: `a10flash/trex_client.py`
- Create: `tests/fake_trex.py`
- Create: `tests/test_trex_client.py`

**Interfaces:**
- Consumes: nada (primeira task).
- Produces (usado nas Tasks 4/5):
  - `TRexError(Exception)` — falha de infraestrutura do TRex, nunca da caixa.
  - `class TRexClient` — `__init__(self, path, daemon_args=("-i", "--astf"), port=4501, astf_factory=None, popen=None, sleep=None)`; `start_daemon(timeout=60) -> None`; `stop_daemon() -> None`; `start_traffic(profile_path, cps, duration) -> None`; `stats() -> dict` com chaves `tx_bps, rx_bps, tx_pps, rx_pps, active_sessions, errors`; `stop_traffic() -> None`; `stop_all() -> None`.

- [ ] **Step 1: Escrever o teste falhando**

`tests/fake_trex.py`:

```python
"""Fake do TRexClient para testes de worker/controller (sem TRex real)."""


class FakeTRexClient:
    """Espelha a interface de a10flash.trex_client.TRexClient."""

    def __init__(self, path=None, daemon_args=("-i", "--astf"), port=4501,
                 astf_factory=None, popen=None, sleep=None):
        self.path = path
        self.daemon_args = daemon_args
        self.calls = []                  # ("start_daemon",), ("stats",), ...
        self.start_traffic_called = False
        self.cps_seen = None
        self.duration_seen = None
        self.profile_seen = None
        self.fail_stats = False          # stats() levanta TRexError
        self.daemon_fail = False         # start_daemon() levanta TRexError
        self.stats_dict = {"tx_bps": 2000, "rx_bps": 2000, "tx_pps": 100,
                           "rx_pps": 100, "active_sessions": 5,
                           "errors": 0}
        self.daemon_terminated = False
        self.started_daemon = False

    def start_daemon(self, timeout=60):
        self.calls.append(("start_daemon",))
        if self.daemon_fail:
            from a10flash.trex_client import TRexError
            raise TRexError("daemon não respondeu (fake)")
        self.started_daemon = True

    def stop_daemon(self):
        self.calls.append(("stop_daemon",))
        self.daemon_terminated = True

    def start_traffic(self, profile_path, cps, duration):
        self.calls.append(("start_traffic", profile_path, cps, duration))
        self.start_traffic_called = True
        self.cps_seen = cps
        self.duration_seen = duration
        self.profile_seen = profile_path

    def stats(self):
        self.calls.append(("stats",))
        if self.fail_stats:
            from a10flash.trex_client import TRexError
            raise TRexError("stats falhou (fake)")
        return dict(self.stats_dict)

    def stop_traffic(self):
        self.calls.append(("stop_traffic",))

    def stop_all(self):
        self.calls.append(("stop_all",))
        self.stop_traffic()
        self.stop_daemon()
```

`tests/test_trex_client.py`:

```python
"""Testes do TRexClient (sem TRex real — daemon e client injetados)."""

import subprocess

import pytest

import a10flash.trex_client as tc
from a10flash.trex_client import TRexClient, TRexError


class FakeAstfClient:
    """ASTFClient falso: registra chamadas e devolve stats programáveis."""

    def __init__(self):
        self.connected = False
        self.calls = []
        self.frames = []

    def connect(self):
        self.connected = True
        self.calls.append("connect")

    def reset(self):
        self.calls.append("reset")

    def disconnect(self):
        self.connected = False
        self.calls.append("disconnect")

    def load_profile(self, profile, tunables=None):
        self.calls.append(("load_profile", profile, tunables))
        return {"profile": profile, "tunables": tunables}

    def start(self, profile, duration=None):
        self.calls.append(("start", duration))

    def stop(self):
        self.calls.append("stop")

    def get_stats(self):
        self.calls.append("get_stats")
        return self.frames.pop(0)


def _frame(tx_bytes, rx_bytes, tx_pkts, rx_pkts, errs=0, active=0):
    return {"traffic": {"global": {"ports": [
        {"obytes": tx_bytes, "ibytes": 0, "opackets": tx_pkts,
         "ipackets": 0, "rx_drop": 0, "tx_drop": 0, "m_active_flows": active},
        {"obytes": 0, "ibytes": rx_bytes, "opackets": 0,
         "ipackets": rx_pkts, "rx_drop": errs, "tx_drop": 0,
         "m_active_flows": 0},
    ]}}}


class FakePopen:
    def __init__(self):
        self.spawned = []
        self.terminated = []

    def __call__(self, args, cwd=None, stdout=None, stderr=None):
        proc = FakeProc(args, cwd)
        self.spawned.append(proc)
        return proc


class FakeProc:
    def __init__(self, args, cwd):
        self.args = args
        self.cwd = cwd
        self.terminate_called = False
        self.kill_called = False

    def terminate(self):
        self.terminate_called = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.kill_called = True


def test_start_daemon_spawns_and_waits(monkeypatch):
    opened = []

    def fake_port_open(port, timeout=2.0):
        return len(opened) > 1   # 2ª chamada: porta abriu

    monkeypatch.setattr(tc, "_port_open", fake_port_open)
    popen = FakePopen()
    c = TRexClient("/opt/trex/v3.08", popen=popen)
    # primeira chamada de _port_open acontece antes do spawn
    opened.append(False)
    c.start_daemon(timeout=5)
    assert len(popen.spawned) == 1
    assert popen.spawned[0].args == ["/opt/trex/v3.08/t-rex-64", "-i",
                                     "--astf"]
    assert popen.spawned[0].cwd == "/opt/trex/v3.08"
    assert c._started_daemon is True


def test_start_daemon_existing_is_not_ours(monkeypatch):
    monkeypatch.setattr(tc, "_port_open", lambda port, timeout=2.0: True)
    popen = FakePopen()
    c = TRexClient("/opt/trex/v3.08", popen=popen)
    c.start_daemon()
    assert popen.spawned == []
    assert c._started_daemon is False
    # stop_daemon de um daemon que não subimos: não faz nada
    c.stop_daemon()
    assert popen.terminated == []


def test_start_daemon_timeout(monkeypatch):
    monkeypatch.setattr(tc, "_port_open", lambda port, timeout=2.0: False)
    popen = FakePopen()
    c = TRexClient("/opt/trex/v3.08", popen=popen, sleep=lambda s: None)
    with pytest.raises(TRexError):
        c.start_daemon(timeout=0.1)


def test_start_daemon_missing_binary(monkeypatch):
    monkeypatch.setattr(tc, "_port_open", lambda port, timeout=2.0: False)
    popen = FakePopen()
    c = TRexClient("/nao/existe", popen=popen, sleep=lambda s: None)
    with pytest.raises(TRexError):
        c.start_daemon(timeout=0.1)


def test_start_traffic_loads_profile_with_cps():
    fake = FakeAstfClient()
    c = TRexClient("/opt/trex/v3.08", astf_factory=lambda: fake)
    c.start_traffic("trex/astf/a10_astf.py", 1000, 87000)
    assert fake.calls[0] == "connect"
    assert fake.calls[1] == "reset"
    assert fake.calls[2] == ("load_profile", "trex/astf/a10_astf.py",
                             ["--cps", "1000"])
    assert fake.calls[3] == ("start", 87000)


def test_stats_first_call_returns_zeros(monkeypatch):
    fake = FakeAstfClient()
    fake.frames = [_frame(tx_bytes=1000, rx_bytes=500, tx_pkts=10,
                          rx_pkts=5, active=3)]
    c = TRexClient("/opt/trex/v3.08", astf_factory=lambda: fake)
    now = [1000.0]

    class Clock:
        @staticmethod
        def monotonic():
            return now[0]

    monkeypatch.setattr(tc.time, "monotonic", Clock.monotonic)
    st = c.stats()
    assert st == {"tx_bps": 0, "rx_bps": 0, "tx_pps": 0, "rx_pps": 0,
                  "active_sessions": 0, "errors": 0}


def test_stats_rates_from_delta(monkeypatch):
    fake = FakeAstfClient()
    # porta 0 envia, porta 1 recebe; 10s entre amostras, delta de
    # 1.000.000 bytes tx e 500.000 rx
    fake.frames = [
        _frame(tx_bytes=0, rx_bytes=0, tx_pkts=0, rx_pkts=0),
        _frame(tx_bytes=1_000_000, rx_bytes=0, tx_pkts=1000, rx_pkts=0,
               active=7),
        _frame(tx_bytes=1_000_000, rx_bytes=500_000, tx_pkts=1000,
               rx_pkts=800, errs=3, active=9),
    ]
    c = TRexClient("/opt/trex/v3.08", astf_factory=lambda: fake)
    now = [1000.0]

    class Clock:
        @staticmethod
        def monotonic():
            return now[0]

    monkeypatch.setattr(tc.time, "monotonic", Clock.monotonic)
    assert c.stats()["tx_bps"] == 0          # primeira chamada: baseline
    now[0] += 10.0
    st = c.stats()
    assert st["tx_bps"] == 800_000           # 1MB * 8 / 10s
    assert st["tx_pps"] == 100
    assert st["rx_bps"] == 400_000           # 500KB * 8 / 10s
    assert st["rx_pps"] == 80
    assert st["active_sessions"] == 9
    assert st["errors"] == 3


def test_stats_raises_trex_error():
    fake = FakeAstfClient()
    fake.frames = [{"bad": "shape"}]
    c = TRexClient("/opt/trex/v3.08", astf_factory=lambda: fake)
    with pytest.raises(TRexError):
        c.stats()


def test_stop_all_stops_ours_only():
    fake = FakeAstfClient()
    popen = FakePopen()
    c = TRexClient("/opt/trex/v3.08", astf_factory=lambda: fake,
                   popen=popen)
    c._client = fake
    c.stop_all()
    assert "stop" in fake.calls
    assert "disconnect" in fake.calls
    assert popen.terminated == []            # daemon não era nosso
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_trex_client.py -q`
Expected: FAIL com `ModuleNotFoundError: a10flash.trex_client`

- [ ] **Step 3: Implementar `a10flash/trex_client.py`**

```python
"""Cliente TRex local do lab (daemon ASTF + profile de burn-in).

O daemon roda no MESMO PC do agente (localhost:4501). A lib Python do
TRex fica dentro da instalação (`automation/trex_control_plane/
interactive/`) e entra no `sys.path` na hora de conectar — não é
dependência de pip, e este módulo não importa nada do TRex no
import-time (o worker/código funciona mesmo sem TRex instalado).
"""

import os
import socket
import subprocess
import sys
import time


class TRexError(Exception):
    """Falha de infraestrutura do TRex (daemon/client) — nunca da caixa."""


def _port_open(port, timeout=2.0):
    """True se o daemon TRex responde na porta (STL async = 4501)."""
    with socket.socket() as s:
        s.settimeout(timeout)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


class TRexClient:
    def __init__(self, path, daemon_args=("-i", "--astf"), port=4501,
                 astf_factory=None, popen=None, sleep=None):
        self.path = path
        self.daemon_args = daemon_args
        self.port = port
        self.astf_factory = astf_factory  # testes: callable -> client falso
        self.popen = popen or subprocess.Popen
        self._sleep = sleep or time.sleep
        self._proc = None
        self._started_daemon = False
        self._client = None
        self._last = None  # (monotonic, raw) p/ calcular taxas

    # ------------------------------------------------------------ daemon
    def start_daemon(self, timeout=60):
        """Sobe o daemon (`t-rex-64 -i --astf`) e espera a porta 4501.

        Daemon já rodando (sessão manual do usuário) é usado e NÃO é
        marcado como nosso — stop_daemon não o mata.
        """
        if self._proc is not None:
            return
        if _port_open(self.port):
            return
        bin_path = os.path.join(self.path, "t-rex-64")
        if not os.path.exists(bin_path):
            raise TRexError(f"binário do TRex não encontrado: {bin_path}")
        self._proc = self.popen([bin_path, *self.daemon_args],
                                cwd=self.path,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self._started_daemon = True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _port_open(self.port):
                return
            self._sleep(1)
        raise TRexError(f"daemon TRex não respondeu na porta {self.port} "
                        f"após {timeout}s")

    def stop_daemon(self):
        """Termina o subprocess SOMENTE se fomos nós que subimos."""
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
        self._started_daemon = False

    # ------------------------------------------------------------ client
    def _connect(self):
        if self._client is not None:
            return self._client
        interactive = os.path.join(self.path, "automation",
                                   "trex_control_plane", "interactive")
        if os.path.isdir(interactive) and interactive not in sys.path:
            sys.path.insert(0, interactive)
        if self.astf_factory is not None:
            self._client = self.astf_factory()
        else:
            try:
                from trex.astf.api import ASTFClient  # noqa: F401
            except ImportError as exc:
                raise TRexError(
                    f"lib Python do TRex não encontrada em {interactive} "
                    f"({exc})")
            self._client = ASTFClient(server="127.0.0.1")
        try:
            self._client.connect()
        except Exception as exc:
            self._client = None
            raise TRexError(f"sem conexão com o daemon TRex: {exc}")
        self._client.reset()
        return self._client

    def _disconnect(self):
        if self._client is None:
            return
        try:
            self._client.disconnect()
        except Exception:
            pass
        self._client = None

    # ------------------------------------------------------------ tráfego
    def start_traffic(self, profile_path, cps, duration):
        """Daemon de pé + carrega o profile ASTF com `--cps` e dispara
        por `duration` segundos (idempotente no daemon: reusa se já
        estiver rodando)."""
        client = self._connect()
        profile = client.load_profile(profile_path,
                                      tunables=["--cps", str(cps)])
        client.start(profile, duration=duration)
        self._last = None  # primeira amostra de taxa é zero

    def _raw_stats(self):
        client = self._connect()
        try:
            stats = client.get_stats()
        except Exception as exc:
            raise TRexError(f"falha ao ler stats do TRex: {exc}")
        out = {}
        for i, name in ((0, "port0"), (1, "port1")):
            try:
                p = stats["traffic"]["global"]["ports"][i]
            except (KeyError, IndexError, TypeError):
                raise TRexError(f"stats do TRex sem porta {i}")
            out[name] = {
                "obytes": p.get("obytes", 0),
                "ibytes": p.get("ibytes", 0),
                "opackets": p.get("opackets", 0),
                "ipackets": p.get("ipackets", 0),
                "errors": p.get("rx_drop", 0) + p.get("tx_drop", 0),
                "active": p.get("m_active_flows", 0),
            }
        return out

    def stats(self):
        """Taxas desde a última chamada: tx/rx bps e pps, sessões ativas
        e erros acumulados no intervalo. Primeira chamada = zeros."""
        now = time.monotonic()
        raw = self._raw_stats()
        if self._last is None:
            self._last = (now, raw)
            return {"tx_bps": 0, "rx_bps": 0, "tx_pps": 0, "rx_pps": 0,
                    "active_sessions": 0, "errors": 0}
        t0, prev = self._last
        self._last = (now, raw)
        dt = max(now - t0, 1e-6)
        tx = (raw["port0"]["obytes"] + raw["port1"]["obytes"]
              - prev["port0"]["obytes"] - prev["port1"]["obytes"])
        rx = (raw["port0"]["ibytes"] + raw["port1"]["ibytes"]
              - prev["port0"]["ibytes"] - prev["port1"]["ibytes"])
        tx_p = (raw["port0"]["opackets"] + raw["port1"]["opackets"]
                - prev["port0"]["opackets"] - prev["port1"]["opackets"])
        rx_p = (raw["port0"]["ipackets"] + raw["port1"]["ipackets"]
                - prev["port0"]["ipackets"] - prev["port1"]["ipackets"])
        return {
            "tx_bps": tx * 8 / dt,
            "rx_bps": rx * 8 / dt,
            "tx_pps": tx_p / dt,
            "rx_pps": rx_p / dt,
            "active_sessions": (raw["port0"]["active"]
                                + raw["port1"]["active"]),
            "errors": raw["port0"]["errors"] + raw["port1"]["errors"],
        }

    def stop_traffic(self):
        if self._client is None:
            return
        try:
            self._client.stop()
        except Exception:
            pass

    def stop_all(self):
        """Para tráfego, desconecta e derruba o daemon (só o nosso)."""
        self.stop_traffic()
        self._disconnect()
        self.stop_daemon()
```

- [ ] **Step 4: Rodar os testes**

Run: `pytest tests/test_trex_client.py -v`
Expected: PASS (11 testes)

- [ ] **Step 5: Commit**

```bash
git add a10flash/trex_client.py tests/fake_trex.py tests/test_trex_client.py
git commit -m "adiciona TRexClient: daemon ASTF local, profile com tunables e stats com taxas

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Regra de portas + template LSN (helpers puros em `burnin.py`)

**Files:**
- Create: `a10flash/burnin.py` (só os helpers puros nesta task; o controller entra na Task 4)
- Create: `tests/test_burnin.py`

**Interfaces:**
- Consumes: nada.
- Produces (usados nas Tasks 4/5):
  - `pick_lsn_ports(model, brief, skip_map=None) -> tuple[str, str]` — `("15", "16")`; `ValueError` com mensagem clara quando não dá.
  - `render_lsn_template(template_text, inside, outside, extra_ports=()) -> list[str]` — linhas prontas para `configure terminal`.
  - `DEFAULT_SKIP_MAP` — lista `[{"pattern": "...", "skip": 4}]` (modelos "4xxx" pra cima têm 4 portas traseiras de 40G/100G).

- [ ] **Step 1: Escrever o teste falhando**

`tests/test_burnin.py` (parte 1 — helpers):

```python
"""Testes do burn-in: regra de portas e template LSN (helpers puros)."""

import pytest

from a10flash.burnin import (DEFAULT_SKIP_MAP, pick_lsn_ports,
                             render_lsn_template)


def _brief(count):
    lines = ["Port              Link  State    Speed    Duplex"]
    for i in range(1, count + 1):
        lines.append(f"ethernet {i}         Up    Forward  10Gbps   full")
    return "\r\n".join(lines)


def test_pick_lsn_ports_9_portas():
    assert pick_lsn_ports("TH930S", _brief(9)) == ("8", "9")


def test_pick_lsn_ports_10_portas():
    assert pick_lsn_ports("TH1040S", _brief(10)) == ("9", "10")


def test_pick_lsn_ports_4430_desconta_4():
    assert pick_lsn_ports("TH4430S", _brief(20)) == ("15", "16")


def test_pick_lsn_ports_48_portas_desconta_4():
    assert pick_lsn_ports("TH7650S", _brief(48)) == ("43", "44")


def test_pick_lsn_ports_modelo_sem_match_desconta_zero():
    assert pick_lsn_ports("TH930S", _brief(16)) == ("15", "16")


def test_pick_lsn_ports_skip_map_customizado():
    custom = [{"pattern": "TH9\\d+", "skip": 0}]
    assert pick_lsn_ports("TH930S", _brief(9), custom) == ("8", "9")


def test_pick_lsn_ports_sem_portas():
    with pytest.raises(ValueError, match="sem portas ethernet"):
        pick_lsn_ports("TH930S", "nada aqui")


def test_pick_lsn_ports_poucas_portas():
    with pytest.raises(ValueError, match="portas insuficientes"):
        pick_lsn_ports("TH4430S", _brief(4))


def test_pick_lsn_ports_primeiro_match_vence():
    custom = [{"pattern": "TH4430S", "skip": 4},
              {"pattern": "TH4", "skip": 0}]
    assert pick_lsn_ports("TH4430S", _brief(20), custom) == ("15", "16")


def test_render_template_substitui_e_limpa():
    tpl = (
        "interface management\n"
        "  ip address dhcp\n"
        "!\n"
        "interface ethernet {INSIDE_PORT}\n"
        "  ip nat inside\n"
        "!\n"
        "interface ethernet {OUTSIDE_PORT}\n"
        "  ip nat outside\n"
        "!\n"
        "end\n"
    )
    lines = render_lsn_template(tpl, "15", "16")
    assert lines == [
        "interface management",
        "  ip address dhcp",
        "interface ethernet 15",
        "  ip nat inside",
        "interface ethernet 16",
        "  ip nat outside",
    ]


def test_render_template_extra_ports():
    tpl = "interface ethernet {INSIDE_PORT}\n"
    lines = render_lsn_template(tpl, "15", "16", extra_ports=[17, 18])
    assert lines == ["interface ethernet 15",
                     "interface ethernet 17", "enable",
                     "interface ethernet 18", "enable"]


def test_default_skip_map_cobre_4430_e_7650():
    import re
    pats = [e["pattern"] for e in DEFAULT_SKIP_MAP]
    assert any(re.search(p, "TH4430S") for p in pats)
    assert any(re.search(p, "TH7650S") for p in pats)
    assert all(e["skip"] == 4 for e in DEFAULT_SKIP_MAP)
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_burnin.py -q`
Expected: FAIL com `ModuleNotFoundError: a10flash.burnin`

- [ ] **Step 3: Implementar os helpers em `a10flash/burnin.py`**

```python
"""Burn-in de estabilidade: config CGNAT/LSN + tráfego TRex.

Fase entre o ciclo e o modo teste: aplica a config LSN (template com
portas renderizadas por caixa), força tráfego por `duration_h` (default
24h) e observa se a caixa reinicia — triagem de estabilidade para
equipamentos de segunda mão.

Vereditos: pass | fail | interrupted | aborted.

A REGRA DE PORTAS não usa a velocidade do `show interfaces brief`
(todos os nomes são "ethernet N" e a velocidade não é fonte confiável):
o modelo (`show version`) diz quantas portas traseiras de 40G/100G
descontar, e o brief só fornece a contagem.
"""

import re

DEFAULT_SKIP_MAP = [
    # modelos "4xxx" pra cima: 4 portas de 40G/100G no final, sempre
    {"pattern": ("4430|4440|5430|5440|5630|6430|6435|6440|5840|5845|"
                 "7440|7445|7650|7655|14045"), "skip": 4},
]


def pick_lsn_ports(model, brief, skip_map=None):
    """(inside, outside) = as duas últimas portas ethernet utilizáveis.

    `brief` é a saída bruta de `show interfaces brief`; `model` vem do
    `show version`. `skip_map` = lista de {"pattern", "skip"} (primeiro
    match vence; default DEFAULT_SKIP_MAP; sem match desconta 0).
    """
    ports = {int(m) for m in
             re.findall(r"ethernet\s+(\d+)", brief or "", re.IGNORECASE)}
    if not ports:
        raise ValueError("sem portas ethernet no show interfaces brief")
    skip = 0
    for entry in (skip_map or DEFAULT_SKIP_MAP):
        if entry.get("pattern") and re.search(entry["pattern"],
                                              model or ""):
            skip = int(entry.get("skip", 0))
            break
    total = max(ports)
    usable = total - skip
    if usable < 2:
        raise ValueError(
            f"portas insuficientes para o burn-in: {total} porta(s), "
            f"{skip} descontada(s) (modelo {model or '?'})")
    return str(usable - 1), str(usable)


def render_lsn_template(template_text, inside, outside, extra_ports=()):
    """Substitui {INSIDE_PORT}/{OUTSIDE_PORT} e acrescenta os blocos
    `interface ethernet N`/`enable` de `extra_ports`. Remove separadores
    `!` e o `end` final (o aplicador envia `end` por conta própria)."""
    text = (template_text or "").replace("{INSIDE_PORT}", inside)
    text = text.replace("{OUTSIDE_PORT}", outside)
    lines = []
    for ln in text.splitlines():
        stripped = ln.strip()
        if not stripped or stripped == "!" or stripped == "end":
            continue
        lines.append(ln)
    for port in extra_ports:
        lines += [f"interface ethernet {port}", "enable"]
    return lines
```

- [ ] **Step 4: Rodar os testes**

Run: `pytest tests/test_burnin.py -v`
Expected: PASS (12 testes)

- [ ] **Step 5: Commit**

```bash
git add a10flash/burnin.py tests/test_burnin.py
git commit -m "adiciona regra de portas LSN por modelo e template de config do burn-in

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: `apply_config_lines` no `a10_cli.py` + template real + suporte no FakeA10

**Files:**
- Modify: `a10flash/a10_cli.py` (novo método `apply_config_lines`)
- Modify: `trex/config_lsn.conf` (vira template com placeholders)
- Modify: `tests/fake_device.py` (brief de interfaces + linhas inválidas)
- Modify: `tests/test_burnin.py` (teste de `apply_config_lines` com `FakeA10`)

**Interfaces:**
- Consumes: `pick_lsn_ports`/`render_lsn_template` (Task 2).
- Produces (usados nas Tasks 4/5):
  - `SerialA10.apply_config_lines(lines, timeout=30) -> list[str]` — linhas rejeitadas (vazia = tudo ok); **não** dá `write memory` (o chamador decide).
  - `FakeA10.interfaces_count` (int, default 20) e `FakeA10.bad_config_lines` (set de substrings → responde `% Invalid input`).

- [ ] **Step 1: Escrever o teste falhando** (em `tests/test_burnin.py`)

```python
"""Aplicação de config via serial (precisa do FakeA10/pty)."""
from a10flash.a10_cli import SerialA10
from tests.fake_device import FakeA10


def test_apply_config_lines_ok_e_rejeitada():
    fake = FakeA10()
    fake.start()
    try:
        cli = SerialA10(port=fake.port)
        cli.open_and_login()
        assert cli.apply_config_lines(
            ["interface ethernet 15", "ip nat inside"]) == []
        # linha rejeitada volta na lista
        fake.bad_config_lines = {"ip nat outside"}
        assert cli.apply_config_lines(
            ["interface ethernet 16", "ip nat outside"]) == \
            ["ip nat outside"]
    finally:
        fake.close()


def test_fake_brief_lista_interfaces():
    fake = FakeA10()
    fake.interfaces_count = 9
    fake.start()
    try:
        cli = SerialA10(port=fake.port)
        cli.open_and_login()
        out = cli.cmd("show interfaces brief")
        assert "ethernet 9" in out
        assert "ethernet 10" not in out
    finally:
        fake.close()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_burnin.py::test_apply_config_lines_ok_e_rejeitada tests/test_burnin.py::test_fake_brief_lista_interfaces -q`
Expected: FAIL com `AttributeError: 'SerialA10' object has no attribute 'apply_config_lines'`

- [ ] **Step 3: Implementar `apply_config_lines` em `a10flash/a10_cli.py`**

Adicionar perto de `write_memory` (após a linha ~539), na seção "escrita":

```python
    _CONFIG_ERROR_MARKERS = (
        "% invalid input", "invalid input detected", "syntax error",
        "command rejected", "unrecognized command",
    )

    @classmethod
    def config_line_failed(cls, line, output):
        """A saída de um comando de config contém erro do ACOS para a
        linha? (marcadores de erro no eco — `%`/`^` do ACOS)"""
        low = (output or "").lower()
        return any(marker in low for marker in cls._CONFIG_ERROR_MARKERS)

    def apply_config_lines(self, lines, timeout=30):
        """Aplica linhas de config via `configure terminal`, uma a uma,
        verificando erro no eco de cada uma. Retorna a lista de linhas
        rejeitadas (vazia = tudo aplicado). NÃO dá write memory — o
        chamador decide (só grava se nada falhou)."""
        self.cmd("configure terminal", timeout=timeout)
        rejected = []
        for line in lines:
            out = self.cmd(line, timeout=timeout)
            if self.config_line_failed(line, out):
                rejected.append(line)
        self.cmd("end", timeout=timeout)
        return rejected
```

- [ ] **Step 4: Transformar `trex/config_lsn.conf` no template**

Editar `trex/config_lsn.conf`:
- `interface ethernet 15` → `interface ethernet {INSIDE_PORT}`
- `interface ethernet 16` → `interface ethernet {OUTSIDE_PORT}`
- Remover o bloco `interface ethernet 17` … `interface ethernet 20`/`enable` (vira `trex.extra_enable_ports` no config, default vazia)
- Manter o resto idêntico (rotas, class-list, cgnv6, sflow, `end`).

Resultado esperado (trecho): `interface ethernet {INSIDE_PORT}` com `enable`/`ip address 10.255.0.1 255.255.255.252`/`ip nat inside`; `interface ethernet {OUTSIDE_PORT}` com `enable`/`ip address 10.255.0.5 255.255.255.252`/`ip nat outside`.

- [ ] **Step 5: Suporte no `tests/fake_device.py`**

No `FakeA10.__init__`, adicionar:

```python
        self.interfaces_count = 20      # portas ethernet 1..N no brief
        self.bad_config_lines = set()   # substrings -> '% Invalid input'
```

Trocar o handler de `show interfaces brief` (linha ~372: `self._send(self._mgmt_block() + self._prompt())`) por:

```python
        elif line == "show interfaces brief":
            self._send(self._mgmt_block() + self._interfaces_brief()
                       + self._prompt())
```

E adicionar os métodos:

```python
    def _interfaces_brief(self):
        lines = ["\r\nPort              Link  State    Speed    Duplex"]
        for i in range(1, self.interfaces_count + 1):
            lines.append(f"ethernet {i}         Up    Forward  10Gbps   full")
        return "\r\n".join(lines) + "\r\n"

    def _reject_bad_config(self, line):
        """No contexto de config, linhas 'ruins' recebem o erro do ACOS."""
        if any(bad in line for bad in self.bad_config_lines):
            self._send("\r\n% Invalid input detected at '^' marker.\r\n"
                       + self._prompt())
            return True
        return False
```

E no handler de comandos em contexto `config`/`if` (junto do `elif line == "interface management"` ~linha 379), ANTES dos comandos conhecidos:

```python
        elif self._ctx in ("config", "if") and self._reject_bad_config(line):
            pass
```

- [ ] **Step 6: Rodar os testes**

Run: `pytest tests/test_burnin.py -v`
Expected: PASS (14 testes)

- [ ] **Step 7: Commit**

```bash
git add a10flash/a10_cli.py trex/config_lsn.conf tests/fake_device.py tests/test_burnin.py
git commit -m "adiciona apply_config_lines com detecção de linhas rejeitadas e template LSN com portas dinâmicas

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: `BurninController` (loop de 24h, eventos, vereditos)

**Files:**
- Modify: `a10flash/burnin.py` (adicionar o controller e exceções)
- Modify: `tests/test_burnin.py` (testes unit do loop com clock falso)

**Interfaces:**
- Consumes: `pick_lsn_ports`, `render_lsn_template` (Task 2), `apply_config_lines` (Task 3), `TRexClient`/`FakeTRexClient` (Task 1), `parse_uptime` de `a10flash.version`.
- Produces (usado na Task 5):
  - `BurninStop(Exception)` — parada pelo operador (veredito aborted + erase).
  - `BurninAbort(Exception)` — abort do portal: publica `burnin_result` (aborted) e **re-levanta** (sem erase); o worker trata no `run()`.
  - `BurninConfigError(Exception)` — porta/config inválidas (veredito aborted, sem tráfego).
  - `class BurninController` — `__init__(self, cli, serial, device_info, trex, cfg, bus, notifier, device, port_path, mailbox=None, do_erase=None, cps_override=None, duration_override=None, clock=None)`; `run() -> dict` `{"verdict", "reason", "samples", "config_errors", "new_cli"}`.

- [ ] **Step 1: Escrever os testes do controller**

Em `tests/test_burnin.py`, acrescentar:

```python
"""BurninController: loop com clock falso, cli stub e trex fake."""
import os
import time

import pytest

from a10flash.burnin import (BurninAbort, BurninController, BurninStop,
                             pick_lsn_ports, render_lsn_template)
from a10flash.trex_client import TRexError
from tests.fake_trex import FakeTRexClient


class StubCli:
    """cli serial stub: cmd registra chamadas; uptime controlável."""

    def __init__(self):
        self.cmds = []
        self.uptime_s = 100
        self.reject = []
        self.written = []
        self.login_calls = 0

    def cmd(self, command, timeout=30):
        self.cmds.append(command)
        if command == "show interfaces brief":
            return "\r\n".join(
                ["Port  Link"] +
                [f"ethernet {i}         Up" for i in range(1, 21)])
        if command == "show version":
            return f"Up time is {self.uptime_s} seconds"
        if command == "configure terminal" or command == "end":
            return "ok"
        if self.reject and command in self.reject:
            return "% Invalid input detected at '^' marker."
        return "ok"

    def apply_config_lines(self, lines, timeout=30):
        self.cmds.append(("apply_config_lines", lines))
        return [ln for ln in lines if ln in self.reject]

    def write_memory(self, timeout=30):
        self.written.append("write memory")

    def open_and_login(self, login_timeout=20, baud_autodetect=True):
        self.login_calls += 1
        self.cmds.append("open_and_login")


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


class FakeBus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(dict(event))


class FakeNotifier:
    def __init__(self):
        self.log = []

    def info(self, device, msg):
        self.log.append(("info", msg))

    def warn(self, device, msg):
        self.log.append(("warn", msg))

    def ok(self, device, msg):
        self.log.append(("ok", msg))

    def error(self, device, msg):
        self.log.append(("error", msg))


def make_ctrl(clock=None, trex=None, cli=None, mailbox=None, do_erase=None,
              cps_override=None, duration_override=None, **cfg_over):
    cfg = {"device": {"test_interval_h": 1},
           "trex": {"path": "/opt/trex/v3.08", "cps": 1000,
                    "duration_h": 24, "sample_interval_s": 60,
                    "lsn_config": "trex/config_lsn.conf"}}
    cfg["trex"].update(cfg_over)
    return BurninController(
        cli=cli or StubCli(), serial="SER-1",
        device_info={"model": "TH930S"},
        trex=trex or FakeTRexClient(), cfg=cfg, bus=FakeBus(),
        notifier=FakeNotifier(), device="dev-a", port_path="/dev/ttyUSB0",
        mailbox=mailbox, do_erase=do_erase, cps_override=cps_override,
        duration_override=duration_override,
        clock=clock or FakeClock())


def _events(bus, etype):
    return [e for e in bus.events if e.get("type") == etype]


def test_burnin_pass_24h(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus,
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "pass"
    assert "24h" in res["reason"]
    started = _events(bus, "burnin_started")
    assert started and started[0]["cps"] == 1000
    result = _events(bus, "burnin_result")
    assert result and result[0]["verdict"] == "pass"
    samples = _events(bus, "burnin_sample")
    assert len(samples) == 24 * 3600 / 60   # 1440 amostras
    assert erased == ["erase"]
    assert "write memory" in cli.written


def test_burnin_reboot_midtest_fail(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    class RebootCli(StubCli):
        """uptime desce entre leituras (caixa reiniciou sob carga)."""

        def __init__(self):
            super().__init__()
            self._reads = 0

        def cmd(self, command, timeout=30):
            if command == "show version":
                self._reads += 1
                if self._reads >= 3:
                    return "Up time is 5 seconds"   # reiniciou
                return "Up time is 5000 seconds"
            return super().cmd(command, timeout=timeout)

    clock = FakeClock()
    cli = RebootCli()
    bus = FakeBus()
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus,
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "fail"
    assert "reiniciou" in res["reason"]
    assert erased == ["erase"]


def test_burnin_unplug_interrupted(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus,
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "interrupted"
    assert erased == []


def test_burnin_stop_aborted_com_erase(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    class Mailbox:
        def __init__(self):
            self.cmds = [{"command": "burnin_stop", "reason": "chega"}]

        def drain(self):
            out, self.cmds = self.cmds, []
            return out

    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus, mailbox=Mailbox(),
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "aborted"
    assert "parado" in res["reason"]
    assert erased == ["erase"]


def test_burnin_abort_levanta_sem_erase(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    class Mailbox:
        def drain(self):
            return [{"command": "abort", "reason": "chega"}]

    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus, mailbox=Mailbox(),
                     do_erase=lambda: erased.append("erase") or cli)
    with pytest.raises(BurninAbort):
        ctrl.run()
    result = _events(bus, "burnin_result")
    assert result and result[0]["verdict"] == "aborted"
    assert erased == []


def test_burnin_config_rejeitada_nao_inicia(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = StubCli()
    cli.reject = ["ip nat inside"]
    bus = FakeBus()
    trex = FakeTRexClient()
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus, trex=trex,
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "aborted"
    assert res["config_errors"] == ["ip nat inside"]
    assert trex.start_traffic_called is False
    assert "write memory" not in cli.written
    assert erased == ["erase"]


def test_burnin_portas_insuficientes(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = StubCli()
    cli.cmd = lambda command, timeout=30: "sem portas"
    bus = FakeBus()
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus)
    res = ctrl.run()
    assert res["verdict"] == "aborted"
    assert "regra de portas" in res["reason"]


def test_burnin_trex_infra_aborta_apos_backoff(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    trex = FakeTRexClient()
    trex.fail_stats = True
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus, trex=trex,
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "aborted"
    assert "TRex" in res["reason"]
    assert erased == ["erase"]


def test_burnin_publica_uptime_sample_horario(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus)
    ctrl.run()
    ups = _events(bus, "uptime_sample")
    assert 0 < len(ups) < 30    # ~24, um por hora (não um por amostra)


def test_burnin_overrides_do_comando(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    trex = FakeTRexClient()
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus, trex=trex,
                     cps_override=2000, duration_override=1)
    ctrl.run()
    assert trex.cps_seen == 2000
    started = _events(bus, "burnin_started")
    assert started[0]["duration_h"] == 1
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_burnin.py -q`
Expected: FAIL — `ImportError: cannot import name 'BurninController' from 'a10flash.burnin'`

- [ ] **Step 3: Implementar o controller em `a10flash/burnin.py`**

Acrescentar ao final de `a10flash/burnin.py`:

```python
"""...continuação: o loop do burn-in."""

import os
import time as _time
import uuid

from .trex_client import TRexError
from .version import parse_uptime

TREX_DOWN_LIMIT_S = 300  # 5 min sem tráfego = infraestrutura irrecuperável


class BurninStop(Exception):
    """Burn-in parado por comando do portal (veredito aborted + erase)."""


class BurninAbort(Exception):
    """Abort geral do portal — mata o fluxo inteiro (sem erase)."""


class BurninConfigError(Exception):
    """Portas/config LSN inválidas — burn-in não começa."""


class BurninController:
    """Loop do burn-in: config LSN + TRex + observação de reboot.

    Publica no bus: `burnin_started`, `burnin_sample` (+ `uptime_sample`
    na cadência normal do modo teste) e `burnin_result`. Devolve a nova
    sessão cli pós-erase (ou a mesma, em `interrupted`).
    """

    def __init__(self, cli, serial, device_info, trex, cfg, bus,
                 notifier, device, port_path, mailbox=None, do_erase=None,
                 cps_override=None, duration_override=None, clock=None):
        self.cli = cli
        self.serial = serial or ""
        self.device_info = device_info or {}
        self.trex = trex
        self.cfg = cfg or {}
        self.bus = bus
        self.notifier = notifier
        self.device = device
        self.port_path = port_path
        self.mailbox = mailbox
        self.do_erase = do_erase
        self.cps_override = cps_override
        self.duration_override = duration_override
        self.clock = clock or _time

    # ------------------------------------------------------- utilitários
    def _repo_file(self, rel_path):
        """Caminho relativo ao repo (o lab roda o código de um clone)."""
        if os.path.isabs(rel_path):
            return rel_path
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.normpath(os.path.join(repo, rel_path))

    def _read_file(self, rel_path):
        with open(self._repo_file(rel_path), "r", encoding="utf-8") as fh:
            return fh.read()

    def _publish(self, event):
        if self.bus:
            self.bus.publish(event)

    def _check_burnin_commands(self):
        """abort mata tudo (sem erase); burnin_stop encerra com erase.
        pause/resume não se aplicam durante o burn-in (consumidos)."""
        if self.mailbox is None:
            return
        for cmd in self.mailbox.drain():
            kind = cmd.get("command")
            if kind == "abort":
                raise BurninAbort(cmd.get("reason")
                                  or "abortado pelo operador")
            if kind == "burnin_stop":
                raise BurninStop(cmd.get("reason")
                                 or "parado pelo operador")

    def _uptime(self):
        """`show version` -> uptime_s (None se não conseguiu). Reloga se
        a sessão caiu — a caixa reiniciando derruba o console."""
        for _ in (1, 2):
            try:
                out = self.cli.cmd("show version", timeout=30)
            except Exception:
                try:
                    self.cli.open_and_login()
                except Exception:
                    self.clock.sleep(5)
                    continue
                continue
            return parse_uptime(out)
        return None

    def _publish_result(self, run_id, started, duration_h, cps, verdict,
                        reason, samples, config_errors):
        self._publish({
            "type": "burnin_result", "device": self.device,
            "port": self.port_path, "serial": self.serial,
            "run_id": run_id, "started_ts": started,
            "ended_ts": self.clock.time(), "duration_h": duration_h,
            "cps": cps, "verdict": verdict, "reason": reason,
            "config_errors": config_errors,
            "summary": f"{samples} amostra(s) coletada(s)",
        })

    # -------------------------------------------------------------- run
    def run(self):
        t = self.cfg.get("trex", {})
        cps = float(self.cps_override if self.cps_override is not None
                    else t.get("cps", 1000))
        duration_h = float(self.duration_override
                           if self.duration_override is not None
                           else t.get("duration_h", 24))
        duration_s = duration_h * 3600
        sample_interval = float(t.get("sample_interval_s", 60))
        uptime_interval = (float(self.cfg.get("device", {})
                                 .get("test_interval_h", 1)) * 3600)
        profile = self._repo_file(t.get("profile",
                                        "trex/astf/a10_astf.py"))
        run_id = uuid.uuid4().hex
        started = self.clock.time()
        verdict = "aborted"
        reason = ""
        samples = 0
        config_errors = []
        last_uptime = None
        next_sample = started
        next_uptime_pub = started
        trex_down_at = None
        self._publish({
            "type": "burnin_started", "device": self.device,
            "port": self.port_path, "serial": self.serial,
            "run_id": run_id, "cps": cps, "duration_h": duration_h,
            "started_ts": started,
        })
        self.notifier.info(
            self.device,
            f"Burn-in: {duration_h:g}h de tráfego a {cps:g} CPS "
            "(config LSN + TRex)...")
        try:
            # 1) portas + template + config LSN
            model = self.device_info.get("model") or ""
            brief = self.cli.cmd("show interfaces brief", timeout=60)
            try:
                inside, outside = pick_lsn_ports(
                    model, brief, t.get("trailing_highspeed_ports"))
            except ValueError as exc:
                raise BurninConfigError(f"regra de portas: {exc}")
            lines = render_lsn_template(
                self._read_file(t.get("lsn_config",
                                      "trex/config_lsn.conf")),
                inside, outside, t.get("extra_enable_ports", []))
            rejected = self.cli.apply_config_lines(lines)
            if rejected:
                config_errors = list(rejected)
                raise BurninConfigError(
                    f"{len(rejected)} linha(s) de config rejeitadas")
            self.cli.write_memory()

            # 2) tráfego
            self.trex.start_daemon()
            self.trex.start_traffic(profile, cps, duration_s + 300)

            # 3) loop de observação
            while True:
                self._check_burnin_commands()
                if not os.path.exists(self.port_path):
                    verdict, reason = ("interrupted",
                                       "caixa desconectada da serial")
                    break
                now = self.clock.time()
                if now - started >= duration_s:
                    verdict, reason = ("pass",
                                       f"{duration_h:g}h de tráfego "
                                       "sem reiniciar")
                    break
                if now < next_sample:
                    self.clock.sleep(1)
                    continue
                try:
                    st = self.trex.stats()
                    trex_down_at = None
                except TRexError as exc:
                    if trex_down_at is None:
                        trex_down_at = now
                    if now - trex_down_at >= TREX_DOWN_LIMIT_S:
                        verdict, reason = ("aborted",
                                           f"TRex irrecuperável: {exc}")
                        break
                    self.notifier.warn(
                        self.device,
                        f"TRex: {exc} — reconectando...")
                    try:
                        self.trex.stop_all()
                        self.trex.start_daemon()
                        self.trex.start_traffic(profile, cps,
                                                duration_s + 300)
                    except TRexError:
                        pass
                    next_sample = now + 5
                    continue
                up = self._uptime()
                if up is not None:
                    if last_uptime is not None and up < last_uptime:
                        verdict, reason = ("fail",
                                           "a caixa reiniciou durante "
                                           "o burn-in")
                        break
                    last_uptime = up
                if now >= next_uptime_pub:
                    self._publish({
                        "type": "uptime_sample", "device": self.device,
                        "port": self.port_path, "serial": self.serial,
                        "ts": now,
                        "uptime_s": up if up is not None
                        else (last_uptime or 0),
                    })
                    next_uptime_pub = now + uptime_interval
                self._publish({
                    "type": "burnin_sample", "device": self.device,
                    "port": self.port_path, "serial": self.serial,
                    "run_id": run_id, "ts": now,
                    "tx_bps": st["tx_bps"], "rx_bps": st["rx_bps"],
                    "tx_pps": st["tx_pps"], "rx_pps": st["rx_pps"],
                    "active_sessions": st["active_sessions"],
                    "errors": st["errors"],
                    "uptime_s": up if up is not None
                    else (last_uptime or 0),
                })
                samples += 1
                next_sample = now + sample_interval
        except BurninStop:
            verdict, reason = "aborted", "parado pelo operador"
        except BurninConfigError as exc:
            verdict, reason = "aborted", f"config LSN: {exc}"
        except BurninAbort:
            self._publish_result(run_id, started, duration_h, cps,
                                 "aborted", "abort do portal", samples,
                                 config_errors)
            raise
        finally:
            try:
                self.trex.stop_all()
            except Exception:
                pass
        self._publish_result(run_id, started, duration_h, cps, verdict,
                             reason, samples, config_errors)
        new_cli = self.cli
        if verdict != "interrupted" and self.do_erase is not None:
            try:
                new_cli = self.do_erase()
            except Exception as exc:
                self.notifier.warn(
                    self.device, f"erase pós-burn-in falhou: {exc}")
                new_cli = self.cli
        return {"verdict": verdict, "reason": reason, "samples": samples,
                "config_errors": config_errors, "new_cli": new_cli}
```

- [ ] **Step 4: Rodar os testes**

Run: `pytest tests/test_burnin.py -v`
Expected: PASS — o `test_burnin_pass_24h` roda 1440 iterações com clock falso (rápido). O teste de reboot usa o `RebootCli` (uptime cai entre leituras → `up < last_uptime` → fail) e sai do loop antes das 24h, então a contagem de amostras dele não é assertada.

- [ ] **Step 5: Commit**

```bash
git add a10flash/burnin.py tests/test_burnin.py
git commit -m "adiciona BurninController: loop de 24h com config LSN, TRex e vereditos

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Integração no worker (auto pós-ciclo + manual via mailbox)

**Files:**
- Modify: `a10flash/worker.py`
- Modify: `tests/test_worker.py` (E2E do burn-in + ajustes de retorno do `_test_mode`)

**Interfaces:**
- Consumes: `BurninController`, `BurninAbort`, `BurninStop` (Task 4), `TRexClient` (Task 1).
- Produces: `FlashWorker._monitor_phase(cli, serial, device_info, auto_burnin=False) -> dict`; `_test_mode` passa a retornar `{"samples": int, "burnin": cmd|None}`; `_check_commands` retorna a lista de comandos não tratados.

- [ ] **Step 1: Escrever os testes E2E falhando**

Em `tests/test_worker.py`, acrescentar — conferir/adicionar no bloco de imports do arquivo: `from a10flash.bus import EventBus`, `from a10flash.mailbox import Mailbox` e `from tests.fake_trex import FakeTRexClient` (o `FakeA10`, `FakeAxapiServer`, `make_cfg`, `Notifier`, `PowerController` e `AxapiSemConfirmacao` já são usados pelos testes existentes):

```python
# -------------------------------------------------- burn-in (E2E)
def test_ciclo_com_burnin_automatico_pass():
    """Ciclo completo + burn-in curto (duração minúscula no cfg) -> pass."""
    fake = FakeA10()
    axapi = FakeAxapiServer(sw_version="4.1.4")
    orig_exists = os.path.exists

    def _unplug_after_test_mode(dev, stage, detail):
        # o burn-in termina ANTES do evento test_mode (erase -> modo
        # teste) — o hook continua válido: despluga na entrada do modo
        if detail == "test_mode":
            os.path.exists = lambda p: (False if p == fake.port
                                        else orig_exists(p))

    try:
        cfg = make_cfg(device={"reboot_after_upgrade": True},
                       trex={"enabled": True, "duration_h": 0.001,
                             "sample_interval_s": 1, "cps": 10,
                             "path": "/opt/trex/v3.08"})
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        bus = EventBus()
        trex = FakeTRexClient()
        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_cls=AxapiSemConfirmacao,
                             axapi_base_override=axapi.base_url(),
                             trex_cls=lambda **k: trex, bus=bus,
                             on_event=_unplug_after_test_mode)
        result = worker.run()
        assert result["status"] == "success", result
        assert result["test_mode"] is True
        events = bus.history()
        started = [e for e in events if e.get("type") == "burnin_started"]
        finished = [e for e in events
                    if e.get("type") == "burnin_result"]
        assert len(started) == 1 and started[0]["cps"] == 10
        assert finished and finished[0]["verdict"] == "pass"
        assert trex.start_traffic_called is True
        assert trex.profile_seen.endswith("trex/astf/a10_astf.py")
    finally:
        os.path.exists = orig_exists
        axapi.stop()
        fake.close()


def test_ciclo_com_burnin_reboot_fail():
    """A caixa reinicia no meio do burn-in -> fail + erase + modo teste."""
    fake = FakeA10()
    axapi = FakeAxapiServer(sw_version="4.1.4")
    orig_exists = os.path.exists

    def _reboot_mid_burnin():
        time.sleep(2.0)
        fake._do_reboot()

    def _unplug_after_test_mode(dev, stage, detail):
        if detail == "test_mode":
            os.path.exists = lambda p: (False if p == fake.port
                                        else orig_exists(p))

    try:
        cfg = make_cfg(device={"reboot_after_upgrade": True},
                       trex={"enabled": True, "duration_h": 0.005,
                             "sample_interval_s": 1, "cps": 10,
                             "path": "/opt/trex/v3.08"})
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        bus = EventBus()
        trex = FakeTRexClient()
        t = threading.Thread(target=_reboot_mid_burnin, daemon=True)
        t.start()
        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_cls=AxapiSemConfirmacao,
                             axapi_base_override=axapi.base_url(),
                             trex_cls=lambda **k: trex, bus=bus,
                             on_event=_unplug_after_test_mode)
        result = worker.run()
        t.join(timeout=5)
        assert result["status"] == "success", result
        finished = [e for e in bus.history()
                    if e.get("type") == "burnin_result"]
        assert finished and finished[0]["verdict"] == "fail"
        assert "reiniciou" in finished[0]["reason"]
    finally:
        os.path.exists = orig_exists
        axapi.stop()
        fake.close()


def test_burnin_stop_via_mailbox_aborta_com_erase():
    fake = FakeA10()
    axapi = FakeAxapiServer(sw_version="4.1.4")
    orig_exists = os.path.exists

    def _unplug_after_test_mode(dev, stage, detail):
        if detail == "test_mode":
            os.path.exists = lambda p: (False if p == fake.port
                                        else orig_exists(p))

    mailbox = Mailbox()

    def _send_stop():
        time.sleep(2.0)
        mailbox.send({"command": "burnin_stop"})

    try:
        cfg = make_cfg(device={"reboot_after_upgrade": True},
                       trex={"enabled": True, "duration_h": 0.005,
                             "sample_interval_s": 1, "cps": 10,
                             "path": "/opt/trex/v3.08"})
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        bus = EventBus()
        trex = FakeTRexClient()
        t = threading.Thread(target=_send_stop, daemon=True)
        t.start()
        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_cls=AxapiSemConfirmacao,
                             axapi_base_override=axapi.base_url(),
                             trex_cls=lambda **k: trex, bus=bus,
                             mailbox=mailbox,
                             on_event=_unplug_after_test_mode)
        result = worker.run()
        t.join(timeout=5)
        assert result["status"] == "success", result
        finished = [e for e in bus.history()
                    if e.get("type") == "burnin_result"]
        assert finished and finished[0]["verdict"] == "aborted"
    finally:
        os.path.exists = orig_exists
        axapi.stop()
        fake.close()


def test_burnin_config_rejeitada_nao_roda_trafico():
    fake = FakeA10()
    fake.bad_config_lines = {"ip nat inside"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    orig_exists = os.path.exists

    def _unplug_after_test_mode(dev, stage, detail):
        if detail == "test_mode":
            os.path.exists = lambda p: (False if p == fake.port
                                        else orig_exists(p))

    try:
        cfg = make_cfg(device={"reboot_after_upgrade": True},
                       trex={"enabled": True, "duration_h": 0.001,
                             "sample_interval_s": 1, "cps": 10,
                             "path": "/opt/trex/v3.08"})
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        bus = EventBus()
        trex = FakeTRexClient()
        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_cls=AxapiSemConfirmacao,
                             axapi_base_override=axapi.base_url(),
                             trex_cls=lambda **k: trex, bus=bus,
                             on_event=_unplug_after_test_mode)
        result = worker.run()
        assert result["status"] == "success", result
        assert trex.start_traffic_called is False
        finished = [e for e in bus.history()
                    if e.get("type") == "burnin_result"]
        assert finished and finished[0]["verdict"] == "aborted"
        assert finished[0]["config_errors"] == ["ip nat inside"]
    finally:
        os.path.exists = orig_exists
        axapi.stop()
        fake.close()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_worker.py -q -k burnin`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'trex_cls'` (e erros de retorno do `_test_mode` nos testes existentes que rodarem)

- [ ] **Step 3: Alterar o `FlashWorker`**

**3a. Imports** (topo de `worker.py`, junto dos existentes):

```python
from .burnin import BurninAbort, BurninController
from .trex_client import TRexClient
```

**3b. Construtor** — adicionar o parâmetro `trex_cls=TRexClient` e o atributo:

```python
                 force_cycle=False, trex_cls=TRexClient):
        ...
        self.trex_cls = trex_cls
```

**3c. `run()`** — tratar o `BurninAbort` como o `FlashAbort`:

```python
            except (FlashAbort, BurninAbort) as exc:
                self._state = "aborted"
                self.notifier.warn(self.device, f"Ciclo abortado: {exc}")
                self._publish_status(result={"status": "aborted",
                                             "error": str(exc)})
                return {"status": "aborted", "error": str(exc)}
```

**3d. `_check_commands`** — retornar os comandos que ele não trata (para o modo teste ver `burnin_start`):

```python
    def _check_commands(self):
        """Consome comandos do portal nas fronteiras de estágio.

        Pausa bloqueia aqui até receber resume (ou abort). Nunca interrompe
        uma operação no meio (upgrade, reboot, etc). Retorna a lista de
        comandos que o chamador precisa ver (ex.: burnin_start no modo
        teste) — abort/pause/resume são consumidos aqui.
        """
        if self.mailbox is None:
            return []
        handled = []
        while True:
            for cmd in self.mailbox.drain():
                kind = cmd.get("command")
                reason = cmd.get("reason")
                if kind == "abort":
                    raise FlashAbort(reason or "abortado pelo operador")
                if kind == "pause":
                    self._paused.set()
                    self._state = "paused"
                    self.notifier.warn(
                        self.device, "Ciclo pausado pelo operador — "
                                     "aguardando retomar")
                    self._publish_status()
                if kind == "resume":
                    if self._paused.is_set():
                        self._paused.clear()
                        self._state = "running"
                        self.notifier.info(
                            self.device, "Ciclo retomado pelo operador")
                        self._publish_status()
                if kind not in ("abort", "pause", "resume"):
                    handled.append(cmd)
            if not self._paused.is_set():
                return handled
            time.sleep(0.3)
```

**3e. `_test_mode`** — retornar dict com o comando `burnin_start` pendente:

Trocar o corpo do loop e o retorno:

```python
        self._event("stage", "test_mode")
        while True:
            for cmd in self._check_commands():
                if cmd.get("command") == "burnin_start":
                    return {"samples": samples, "burnin": cmd}
            if not os.path.exists(self.port_path):
                ...
                break
            ...
        return {"samples": samples, "burnin": None}
```

(O restante do loop — coleta de uptime — fica igual.)

**3f. Novo `_monitor_phase`** (após `_test_mode`):

```python
    def _monitor_phase(self, cli, serial, device_info, auto_burnin=False):
        """Modo teste + burn-in (automático pós-ciclo e manual via portal).

        O burn-in roda enquanto a caixa está conectada; ao fim (qualquer
        veredito com erase), o modo teste continua coletando uptime até a
        desconexão.
        """
        total_samples = 0
        burnin = {} if auto_burnin else None
        while True:
            if burnin is not None:
                cli = self._run_burnin(
                    cli, serial, device_info,
                    burnin.get("cps"), burnin.get("duration_h"))
                burnin = None
            res = self._test_mode(cli, serial)
            total_samples += res["samples"]
            if res.get("burnin") is None:
                return {"test_mode": True,
                        "uptime_samples": total_samples}
            burnin = res["burnin"]

    def _run_burnin(self, cli, serial, device_info, cps_override=None,
                    duration_override=None):
        """Executa o burn-in (config LSN + TRex + loop) e devolve a nova
        sessão cli (pós-erase) ou a mesma (interrupted)."""
        trex_cfg = self.cfg.get("trex", {})

        def do_erase():
            self.notifier.info(self.device,
                               "Factory reset pós-burn-in...")
            t_reset = time.time()
            self._factory_reset(cli)
            new_cli = self._wait_and_login()
            return self._wait_real_reboot(new_cli, since=t_reset)

        trex = self.trex_cls(
            path=trex_cfg.get("path", "/opt/trex/v3.08"),
            daemon_args=tuple(trex_cfg.get("daemon_args",
                                           ["-i", "--astf"])))
        ctrl = BurninController(
            cli=cli, serial=serial, device_info=device_info, trex=trex,
            cfg=self.cfg, bus=self.bus, notifier=self.notifier,
            device=self.device, port_path=self.port_path,
            mailbox=self.mailbox, do_erase=do_erase,
            cps_override=cps_override, duration_override=duration_override)
        res = ctrl.run()
        self._state = f"burnin_{res['verdict']}"
        self._publish_status(result={
            "summary": f"burn-in: {res['verdict']} — {res['reason']}"})
        return res["new_cli"]
```

**3g. Caminho de sucesso do `_cycle`** — trocar a chamada de `_test_mode`:

```python
            # MODO TESTE + BURN-IN: a caixa atualizada fica conectada na
            # serial; com `trex.enabled`, o burn-in roda antes do modo
            # teste (config LSN + 24h de tráfego -> veredito -> erase)
            auto = bool(self.cfg.get("trex", {}).get("enabled", False))
            result.update(self._monitor_phase(
                cli, device_info.get("serial"), device_info,
                auto_burnin=auto))
            return result
```

(remover as linhas `samples = self._test_mode(...)` / `result["test_mode"] = True` / `result["uptime_samples"] = samples` do bloco)

**3h. Caminho de skip** — trocar `samples = self._test_mode(cli, serial)` e o return:

```python
            mon = self._monitor_phase(cli, serial, device_info)
            return {"status": "skipped", "version": version,
                    "upgraded": False, "serial": serial,
                    **mon,
                    "summary": f"caixa {serial} já processada — "
                               "nada a fazer"}
```

- [ ] **Step 4: Rodar os testes**

Run: `pytest tests/test_worker.py -q -k "burnin or test_mode or uptime"`
Expected: PASS — os testes novos e os existentes de modo teste/skip continuam verdes (o retorno de `_test_mode` mudou, mas nenhum teste existente chama `_test_mode` direto; os que assertam `result["test_mode"]`/`result["uptime_samples"]` seguem válidos).

- [ ] **Step 5: Rodar a suíte inteira do worker**

Run: `pytest tests/test_worker.py -q`
Expected: PASS (~2-3 min)

- [ ] **Step 6: Commit**

```bash
git add a10flash/worker.py tests/test_worker.py
git commit -m "integra burn-in ao worker: automático pós-ciclo e manual via comando do portal

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Portal + agente + monitor (eventos, DB, endpoints, comandos)

**Files:**
- Modify: `a10flash/db.py` (tabelas e métodos de burn-in)
- Modify: `a10flash/portal.py` (eventos, endpoints, payload de comando, snapshot)
- Modify: `a10flash/agent.py` (`_handle_cmd` com payload)
- Modify: `a10flash/monitor.py` (`send_command` com `**extra`)
- Modify: `tests/test_db.py` (métodos novos)
- Modify: `tests/test_portal.py` (endpoints + eventos + ajuste do FakeMonitor)

**Interfaces:**
- Consumes: eventos do controller (Task 4).
- Produces (usado na Task 7):
  - `DeviceStore.start_burnin_run(run_id, serial, device, cps, duration_h, started_ts) -> None`
  - `DeviceStore.finish_burnin_run(run_id, ended_ts, verdict, reason, config_errors_json, summary) -> None`
  - `DeviceStore.add_burnin_sample(run_id, serial, ts, tx_bps, rx_bps, tx_pps, rx_pps, active_sessions, errors, uptime_s) -> None`
  - `DeviceStore.list_burnin_runs(serial, limit=20) -> list[dict]`
  - `DeviceStore.list_burnin_samples(run_id, limit=2000) -> list[dict]`
  - `DeviceStore.active_burnin(serial) -> dict|None`
  - `DeviceStore.active_burnin_runs() -> list[dict]`
  - Endpoints: `GET /api/devices/{serial}/burnin`, `POST /api/devices/{serial}/burnin/start`, `POST /api/devices/{serial}/burnin/stop`.
  - `PortalServer._route_command(key, command, reason=None, payload=None)`.

- [ ] **Step 1: Escrever os testes falhando**

`tests/test_db.py`:

```python
def test_burnin_run_lifecycle():
    store = DeviceStore(":memory:")
    store.start_burnin_run("run-1", "SER-1", "dev-a", 1000, 24, 100.0)
    assert store.active_burnin("SER-1")["run_id"] == "run-1"
    assert store.active_burnin("OUTRA") is None
    store.add_burnin_sample("run-1", "SER-1", 101.0, 1000, 900, 50, 45,
                            10, 2, 3600)
    store.add_burnin_sample("run-1", "SER-1", 102.0, 1100, 950, 55, 48,
                            12, 1, 3660)
    store.finish_burnin_run("run-1", 200.0, "pass", "", "[]", "ok")
    assert store.active_burnin("SER-1") is None
    runs = store.list_burnin_runs("SER-1")
    assert len(runs) == 1 and runs[0]["verdict"] == "pass"
    samples = store.list_burnin_samples("run-1")
    assert len(samples) == 2
    assert samples[0]["uptime_s"] == 3600
```

`tests/test_portal.py` (ajustar o `FakeMonitor` primeiro — assinatura nova):

```python
class FakeMonitor:
    def send_command(self, key, command, reason=None, **extra):
        self.calls.append(("cmd", key, command, reason, extra))
        return True, "ok (fake)"
```

E acrescentar:

```python
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
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_db.py::test_burnin_run_lifecycle tests/test_portal.py -q -k burnin`
Expected: FAIL — `AttributeError: 'DeviceStore' object has no attribute 'start_burnin_run'` / 404/405 nos endpoints

- [ ] **Step 3: Implementar `db.py`**

Acrescentar o schema (junto de `_UPTIME_SCHEMA`):

```python
# Burn-in: um run por execução (24h), amostras de tráfego por run.
_BURNIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS burnin_runs (
    run_id        TEXT PRIMARY KEY,
    serial        TEXT NOT NULL,
    device        TEXT,
    started_ts    REAL NOT NULL,
    ended_ts      REAL,
    duration_h    REAL,
    cps           REAL,
    verdict       TEXT,
    reason        TEXT,
    config_errors TEXT,
    summary       TEXT
);
CREATE TABLE IF NOT EXISTS burnin_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    serial          TEXT NOT NULL,
    ts              REAL NOT NULL,
    tx_bps          REAL,
    rx_bps          REAL,
    tx_pps          REAL,
    rx_pps          REAL,
    active_sessions INTEGER,
    errors          INTEGER,
    uptime_s        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_burnin_run ON burnin_samples (run_id, ts);
"""
```

No `__init__`, após `executescript(_UPTIME_SCHEMA)`: `self._conn.executescript(_BURNIN_SCHEMA)`.

E os métodos (após `list_uptime`):

```python
    # ------------------------------------------------------------ burn-in
    def start_burnin_run(self, run_id, serial, device, cps, duration_h,
                         started_ts):
        if not run_id:
            return None
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO burnin_runs (run_id, serial, "
                "device, started_ts, duration_h, cps, verdict, reason, "
                "config_errors, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?)",
                (run_id, (serial or "").strip(), device, started_ts,
                 duration_h, cps, None, None, "[]", ""))
            self._conn.commit()
        return run_id

    def finish_burnin_run(self, run_id, ended_ts, verdict, reason,
                          config_errors, summary):
        if not run_id:
            return None
        with self._lock:
            self._conn.execute(
                "UPDATE burnin_runs SET ended_ts = ?, verdict = ?, "
                "reason = ?, config_errors = ?, summary = ? "
                "WHERE run_id = ?",
                (ended_ts, verdict, reason or "", config_errors or "[]",
                 summary or "", run_id))
            self._conn.commit()
        return run_id

    def add_burnin_sample(self, run_id, serial, ts, tx_bps, rx_bps,
                          tx_pps, rx_pps, active_sessions, errors,
                          uptime_s):
        if not run_id:
            return None
        with self._lock:
            self._conn.execute(
                "INSERT INTO burnin_samples (run_id, serial, ts, tx_bps, "
                "rx_bps, tx_pps, rx_pps, active_sessions, errors, "
                "uptime_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, (serial or "").strip(), ts, tx_bps, rx_bps,
                 tx_pps, rx_pps, int(active_sessions), int(errors),
                 int(uptime_s)))
            self._conn.commit()
        return ts

    def list_burnin_runs(self, serial, limit=20):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM burnin_runs WHERE serial = ? "
                "ORDER BY started_ts DESC LIMIT ?",
                ((serial or "").strip(), limit)).fetchall()
        return [dict(r) for r in rows]

    def list_burnin_samples(self, run_id, limit=2000):
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, tx_bps, rx_bps, tx_pps, rx_pps, "
                "active_sessions, errors, uptime_s FROM burnin_samples "
                "WHERE run_id = ? ORDER BY ts DESC LIMIT ?",
                (run_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def active_burnin(self, serial):
        """Run em andamento de um equipamento (ended_ts NULL) ou None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM burnin_runs WHERE serial = ? "
                "AND ended_ts IS NULL ORDER BY started_ts DESC LIMIT 1",
                ((serial or "").strip(),)).fetchone()
        return dict(row) if row else None

    def active_burnin_runs(self):
        """Todos os runs em andamento (snapshot do dashboard)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM burnin_runs WHERE ended_ts IS NULL "
                "ORDER BY started_ts DESC").fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Implementar `portal.py`**

**4a.** `AGENT_TYPES` ganha os três tipos:

```python
AGENT_TYPES = {"status", "stage", "log", "cmd_ack", "device", "device_result",
               "uptime_sample", "burnin_started", "burnin_sample",
               "burnin_result"}
```

**4b.** `COMMANDS` ganha os dois comandos:

```python
COMMANDS = {"abort", "pause", "resume", "rerun", "burnin_start",
            "burnin_stop"}
```

**4c.** No loop `ws_agent`, após o handler de `uptime_sample` (linha ~254), acrescentar:

```python
                    elif msg.get("type") == "burnin_started":
                        try:
                            self.store.start_burnin_run(
                                msg.get("run_id", ""),
                                msg.get("serial", ""), msg.get("device", ""),
                                msg.get("cps", 0), msg.get("duration_h", 0),
                                msg.get("started_ts") or time.time())
                        except Exception as exc:
                            self.notifier.error(
                                None, f"falha ao iniciar run de burn-in de "
                                      f"{msg.get('serial') or msg.get('device')}: {exc}")
                        self._track(agent_id, msg)
                        self.bus.publish({**msg, "agent": agent_id})
                        continue
                    elif msg.get("type") == "burnin_sample":
                        try:
                            self.store.add_burnin_sample(
                                msg.get("run_id", ""),
                                msg.get("serial", ""), msg.get("ts"),
                                msg.get("tx_bps", 0), msg.get("rx_bps", 0),
                                msg.get("tx_pps", 0), msg.get("rx_pps", 0),
                                msg.get("active_sessions", 0),
                                msg.get("errors", 0),
                                msg.get("uptime_s", 0))
                        except Exception as exc:
                            self.notifier.error(
                                None, f"falha ao salvar amostra de burn-in de "
                                      f"{msg.get('serial') or msg.get('device')}: {exc}")
                        continue
                    elif msg.get("type") == "burnin_result":
                        try:
                            self.store.finish_burnin_run(
                                msg.get("run_id", ""),
                                msg.get("ended_ts") or time.time(),
                                msg.get("verdict", "aborted"),
                                msg.get("reason", ""),
                                json.dumps(msg.get("config_errors") or []),
                                msg.get("summary", ""))
                        except Exception as exc:
                            self.notifier.error(
                                None, f"falha ao finalizar run de burn-in de "
                                      f"{msg.get('serial') or msg.get('device')}: {exc}")
                        self.bus.publish({**msg, "agent": agent_id})
                        continue
```

**4d.** Endpoints novos (após `api_uptime`, linha ~157):

```python
        @app.get("/api/devices/{serial}/burnin")
        async def api_burnin(serial: str, request: Request):
            """Histórico de burn-ins (runs + amostras) de um equipamento."""
            self._authorize(request)
            runs = self.store.list_burnin_runs(serial)
            samples = {r["run_id"]: self.store.list_burnin_samples(
                r["run_id"]) for r in runs}
            return JSONResponse({"runs": runs, "samples": samples})

        @app.post("/api/devices/{serial}/burnin/start")
        async def api_burnin_start(serial: str, request: Request):
            """Dispara burn-in manual na caixa (só em modo teste)."""
            self._authorize(request)
            body = await request.json()
            rec = self.store.get(serial)
            if rec is None:
                raise HTTPException(status_code=404,
                                    detail="equipamento não registrado")
            key = rec.get("device_key") or rec.get("port")
            online = None
            for aid, arec in self.agents.items():
                dev = arec.get("devices", {}).get(key)
                if arec.get("online") and dev is not None:
                    online = dev
                    break
            if online is None:
                raise HTTPException(status_code=409,
                                    detail="equipamento não está conectado "
                                           "a um agente")
            if online.get("state") != "test_mode":
                raise HTTPException(
                    status_code=409,
                    detail=f"equipamento não está em modo teste "
                           f"(estado: {online.get('state')})")
            if self.store.active_burnin(serial):
                raise HTTPException(status_code=409,
                                    detail="já existe burn-in em andamento")
            ok, message = await self._route_command(
                key, "burnin_start", None,
                payload={"cps": body.get("cps"),
                         "duration_h": body.get("duration_h")})
            if not ok:
                raise HTTPException(status_code=409, detail=message)
            return JSONResponse({"ok": True, "message": message})

        @app.post("/api/devices/{serial}/burnin/stop")
        async def api_burnin_stop(serial: str, request: Request):
            """Para o burn-in em andamento (erase + volta ao modo teste)."""
            self._authorize(request)
            rec = self.store.get(serial)
            if rec is None or not self.store.active_burnin(serial):
                raise HTTPException(status_code=409,
                                    detail="sem burn-in em andamento")
            key = rec.get("device_key") or rec.get("port")
            ok, message = await self._route_command(key, "burnin_stop")
            if not ok:
                raise HTTPException(status_code=409, detail=message)
            return JSONResponse({"ok": True, "message": message})
```

**4e.** `_route_command` com payload:

```python
    async def _route_command(self, key, command, reason=None, payload=None):
        """Encaminha comando para o agente dono do dispositivo."""
        extra = {}
        if payload:
            extra = {k: v for k, v in payload.items() if v is not None}
        for agent_id, rec in self.agents.items():
            if rec.get("online") and key in rec.get("devices", {}):
                try:
                    await rec["ws"].send_json({
                        "type": "cmd", "device": key, "command": command,
                        "reason": reason, **extra})
                except Exception:
                    return False, "falha ao enviar ao agente"
                return True, f"comando enviado ao agente {agent_id}"
        return False, "dispositivo não encontrado em nenhum agente online"
```

**4f.** `_handle_browser_msg` passa o payload do WS do browser:

```python
            payload = {k: v for k, v in msg.items()
                       if k in ("cps", "duration_h") and v is not None}
            ok, message = await self._route_command(
                key, command, msg.get("reason"), payload or None)
```

**4g.** `_snapshot` ganha o burn-in ativo — no final do método, antes do `return`:

```python
        payload["burnin"] = self.store.active_burnin_runs()
```

(ajustar o nome da variável local do retorno ao código real do `_snapshot`)

- [ ] **Step 5: Implementar `agent.py` e `monitor.py`**

**5a.** `agent.py` `_handle_cmd` — passar os campos extras do comando:

```python
        ok, message = False, "monitor indisponível"
        if self.monitor is not None:
            extra = {k: v for k, v in msg.items()
                     if k not in ("type", "device", "command", "reason")}
            if command == "rerun":
                ok, message = self.monitor.request_run(key)
            else:
                ok, message = self.monitor.send_command(
                    key, command, msg.get("reason"), **extra)
```

**5b.** `monitor.py` `send_command`:

```python
    def send_command(self, key, command, reason=None, **extra):
        """Envia comando (abort|pause|resume|burnin_*) para o worker da
        chave. `extra` viaja junto no comando (ex.: cps/duration_h)."""
        rec = self.known.get(key)
        if rec is None:
            return False, "dispositivo não encontrado"
        if not rec["thread"].is_alive():
            return False, "worker não está rodando"
        rec["mailbox"].send({"command": command, "reason": reason,
                             **extra})
        return True, "comando enviado"
```

- [ ] **Step 6: Ajustar o teste existente do comando**

Em `tests/test_portal.py`, o assert do E2E de comando (linha ~314) passa a ser:

```python
    assert ("cmd", "dev-a", "abort", "teste", {}) in monitor.calls
```

- [ ] **Step 7: Rodar os testes**

Run: `pytest tests/test_db.py tests/test_portal.py -q`
Expected: PASS (~1-2 min)

- [ ] **Step 8: Commit**

```bash
git add a10flash/db.py a10flash/portal.py a10flash/agent.py a10flash/monitor.py tests/test_db.py tests/test_portal.py
git commit -m "adiciona burn-in ao portal: eventos, tabelas, endpoints start/stop e payload de comando

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Dashboard (`index.html`)

**Files:**
- Modify: `a10flash/web/index.html`

**Interfaces:**
- Consumes: `GET /api/devices/{serial}/burnin`, `POST .../burnin/start`, `POST .../burnin/stop` (Task 6); eventos `burnin_started`/`burnin_result` no WS do browser.

Sem testes automatizados (página estática) — verificação manual.

- [ ] **Step 1: Estado do burn-in no cliente**

Após a declaração de `paused` (perto da linha ~140), adicionar:

```javascript
  const burnin = {};   // key -> {run_id, started_ts, duration_h, cps, verdict}
```

- [ ] **Step 2: Alimentar o estado pelos eventos do WS**

No handler de mensagens do WS do browser (onde os eventos `status`/`stage` atualizam e chamam `render()`), adicionar:

```javascript
    if (msg.type === "burnin_started" && msg.device) {
      burnin[msg.device] = { run_id: msg.run_id, started_ts: msg.started_ts,
        duration_h: msg.duration_h, cps: msg.cps, verdict: null };
      render();
    }
    if (msg.type === "burnin_result" && msg.device) {
      const b = burnin[msg.device] || {};
      burnin[msg.device] = { ...b, verdict: msg.verdict, reason: msg.reason };
      render();
    }
```

- [ ] **Step 3: Linha de burn-in no cartão**

Dentro de `renderCards`, após a linha da mensagem (`${st.message ? ... : ""}`), adicionar:

```javascript
        ${burnin[f.key] ? burninRow(f.key) : ""}
```

E definir a função (antes de `renderCards`):

```javascript
  function burninRow(key) {
    const b = burnin[key];
    const [label, cls] = (() => {
      if (b.verdict === "pass") return ["burn-in: aprovada ✓", "st-ok"];
      if (b.verdict === "fail") return ["burn-in: REPROVADA ✗", "st-error"];
      if (b.verdict === "aborted") return ["burn-in: abortada", "st-aborted"];
      const elapsed = Math.min((Date.now() / 1000 - b.started_ts)
        / (b.duration_h * 3600), 1);
      const pct = Math.round(elapsed * 100);
      const done = Math.round((Date.now() / 1000 - b.started_ts) / 3600 * 10) / 10;
      return [`burn-in: ${pct}% (${done}h/${b.duration_h}h, ${b.cps} CPS)`, "st-running"];
    })();
    const controls = b.verdict === null ? `<button class="danger btn-burnin-stop" data-key="${esc(key)}">Parar burn-in</button>` : "";
    return `<div class="row"><span>${label}</span><b>${controls}</b></div>`;
  }
```

E os handlers de clique (junto dos handlers de `.btn-abort`):

```javascript
    document.querySelectorAll(".btn-burnin-stop").forEach(b => {
      b.onclick = () => burninCmd(b.dataset.key, "stop");
    });
```

- [ ] **Step 4: Botão de iniciar + handlers**

No bloco de ações do cartão (após o botão "Repetir ciclo"), adicionar:

```javascript
          ${st.state === "test_mode" && !burnin[f.key]
            ? `<button class="ok btn-burnin-start" data-key="${esc(f.key)}">Iniciar burn-in</button>` : ""}
```

E os handlers:

```javascript
    document.querySelectorAll(".btn-burnin-start").forEach(b => {
      b.onclick = () => burninCmd(b.dataset.key, "start");
    });

  async function burninCmd(key, action) {
    const dev = devices.find(d => (d.device_key || d.port) === key);
    if (!dev) {
      log({ type: "log", level: "error", device: key,
        message: "equipamento sem serial registrado — não dá para iniciar burn-in" });
      return;
    }
    const body = {};
    if (action === "start") {
      const cps = prompt("CPS do burn-in (vazio = default 1000):");
      if (cps === null) return;
      if (cps.trim()) body.cps = Number(cps.trim());
      const dur = prompt("Duração em horas (vazio = default 24):");
      if (dur === null) return;
      if (dur.trim()) body.duration_h = Number(dur.trim());
    }
    try {
      const r = await api(`/api/devices/${encodeURIComponent(dev.serial)}/burnin/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      log({ type: "log", level: "info", device: key,
        message: `burn-in ${action}: ${r.message}` });
    } catch (e) {
      log({ type: "log", level: "error", device: key, message: e.message });
    }
  }
```

- [ ] **Step 5: Histórico no detalhe do equipamento**

No corpo de `showDetail(serial)` (painel que expande os shows do registro na tabela de equipamentos), após montar o conteúdo atual do painel, acrescentar um bloco de histórico (usar a variável local do painel onde o conteúdo é montado):

```javascript
    const hist = document.createElement("div");
    hist.innerHTML = '<h4>Burn-ins</h4><div class="empty">carregando…</div>';
    detailPanel.appendChild(hist);
    api(`/api/devices/${encodeURIComponent(serial)}/burnin`).then(d => {
      if (!d.runs.length) { hist.innerHTML = '<h4>Burn-ins</h4><div class="empty">nenhum burn-in registrado</div>'; return; }
      const [label, cls] = { pass: ["aprovada ✓", "st-ok"], fail: ["reprovada ✗", "st-error"],
        aborted: ["abortada", "st-aborted"], interrupted: ["interrompida", "st-waiting"] }[d.runs[0].verdict] || ["?", "st-waiting"];
      hist.innerHTML = `<h4>Burn-ins</h4><table class="devices-table"><tr>
        <th>Início</th><th>Duração</th><th>CPS</th><th>Veredito</th><th>Motivo</th></tr>` +
        d.runs.map(r => `<tr><td>${fmtDate(r.started_ts)}</td><td>${r.duration_h}h</td>
        <td>${r.cps}</td><td class="${cls}">${label}</td>
        <td>${esc(r.reason || "—")}</td></tr>`).join("") + `</table>`;
    }).catch(e => { hist.innerHTML = '<h4>Burn-ins</h4><div class="empty">' + e.message + '</div>'; });
```

(`detailPanel` = a variável local do painel de detalhe em `showDetail` — ajustar ao nome real do código)

- [ ] **Step 6: Verificação manual**

Run: `python -m a10flash.portal` (com `db_path` temporário) e abrir `http://127.0.0.1:8080`. Verificar: cartão sem burn-in não mostra linha; com `burnin` preenchido mostra progresso; botão "Iniciar burn-in" aparece só em `test_mode`; "Parar burn-in" só com run ativo; POST start em equipamento fora de test_mode retorna 409 e o log mostra a mensagem.

- [ ] **Step 7: Commit**

```bash
git add a10flash/web/index.html
git commit -m "adiciona painel de burn-in no dashboard: progresso, botões e histórico

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: Config de exemplo, docs e verificação final

**Files:**
- Modify: `config.yaml.example`
- Modify: `.claude/skills/a10-flasher/SKILL.md`
- (graphify + suíte completa — sem arquivo)

- [ ] **Step 1: Seção `trex:` no `config.yaml.example`**

Adicionar ao final do arquivo (com o mesmo estilo de comentários das seções existentes):

```yaml
# Burn-in de estabilidade (24h de tráfego CGNAT/LSN via TRex).
# Só existe no config do LAB (no servidor é ignorada).
trex:
  enabled: true               # burn-in automático pós-ciclo (caixa na versão alvo)
  path: /opt/trex/v3.08       # instalação do TRex no PC do lab
  lsn_config: trex/config_lsn.conf   # template com {INSIDE_PORT}/{OUTSIDE_PORT}
  cps: 1000                   # ~2 Gbps no teste manual de referência
  duration_h: 24
  sample_interval_s: 60
  daemon_args: ["-i", "--astf"]
  extra_enable_ports: []      # ex.: [17, 18, 19, 20] para habilitar portas extras
  trailing_highspeed_ports:   # portas finais de 40G/100G a descontar, por modelo
    - pattern: "4430|4440|5430|5440|5630|6430|6435|6440|5840|5845|7440|7445|7650|7655|14045"
      skip: 4
```

- [ ] **Step 2: Documentar no SKILL.md do projeto**

Em `.claude/skills/a10-flasher/SKILL.md`, na seção "Ciclo do worker", trocar o item do MODO TESTE por um item MODO TESTE + BURN-IN:

```markdown
5. **MODO TESTE (após sucesso OU caixa já processada/skip)**: ... (texto
   existente)... 
6. **BURN-IN (só caminho de sucesso, com `trex.enabled: true`)**: ANTES do
   modo teste — aplica config CGNAT/LSN (`trex/config_lsn.conf`, template
   com `{INSIDE_PORT}`/`{OUTSIDE_PORT}` renderizado por caixa), `write
   memory`, sobe o daemon TRex (`t-rex-64 -i --astf`, lib Python em
   `<trex.path>/automation/trex_control_plane/interactive/`), roda
   `trex/astf/a10_astf.py` a `trex.cps` (default 1000) por
   `trex.duration_h` (default 24h). Vereditos: `pass` (24h sem
   reiniciar), `fail` (uptime zerou = reiniciou sob carga — caixa fica
   conectada p/ inspeção), `interrupted` (desconectada), `aborted`
   (parada/erro de config/infra). Fim do burn-in: factory reset (erase)
   e volta ao modo teste. Manual: `POST /api/devices/{serial}/burnin/
   start` (só com caixa em test_mode) e `/stop`; comando via mailbox.
   REGRA DE PORTAS: modelo (`show version`) define quantas portas
   traseiras de 40G/100G descontar (`trex.trailing_highspeed_ports`,
   default 4 para os modelos "4430+" e 0 para os demais — o brief NÃO
   distingue velocidade, só conta as portas); inside = penúltima
   restante, outside = última. Linha de config rejeitada (`%
   Invalid`/`syntax error` no eco) → burn-in não inicia e o portal
   mostra as linhas. Eventos: `burnin_started`/`burnin_sample`/
   `burnin_result`; DB: tabelas `burnin_runs`/`burnin_samples`.
   `pause`/`resume` NÃO se aplicam durante o burn-in (consumidos sem
   efeito). TRex é infra: erro dele NUNCA vira `fail` da caixa (aborta
   por infra após 5 min de backoff).
```

- [ ] **Step 3: Rodar a suíte completa em background**

Run: `pytest tests/ -q`
Expected: PASS (~7-9 min — rodar em background e conferir o relatório ao final)

- [ ] **Step 4: `graphify update .`** (regra do projeto após modificar código)

Run: `graphify update .`
Expected: termina sem erro e o grafo reflete os módulos novos (`trex_client`, `burnin`).

- [ ] **Step 5: Commit final**

```bash
git add config.yaml.example .claude/skills/a10-flasher/SKILL.md
git commit -m "documenta burn-in no config de exemplo e no SKILL.md do projeto

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Notas de execução

- O `trex/config_lsn.conf` e o `trex/astf/a10_astf.py` ficam versionados (o lab puxa por git no auto-update).
- Nos E2E do worker, `make_cfg(trex={...})` usa duração minúscula (`0.001`–`0.005`h) para o burn-in terminar em segundos com relógio real; os testes de unidade do controller usam `FakeClock`.
- O teste `test_burnin_reboot_midtest_fail` depende do stub `RebootCli` (uptime cai entre leituras) — está descrito na Task 4.
- Suíte completa antes de cada merge: `pytest tests/ -q` (7-9 min, rodar em background).
