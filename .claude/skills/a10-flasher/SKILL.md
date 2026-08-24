---
name: a10-flasher
description: "Use when working on a10-flasher (A10 ACOS flash automation)."
---

# a10-flasher

Automação de **upgrade de firmware + factory reset** de A10 Thunder/ACOS
**DE BANCADA** (nunca produção) via console serial, com portal web para
acompanhamento e registro dos equipamentos.

## Arquitetura (2 partes — decisão explícita do usuário)

- **Servidor**: `python -m a10flash.portal` — dashboard + hub WS (`/ws`
  browser, `/agent` lab) na :8080; Docker+Traefik em `a10.app.diorg.es`;
  SQLite de equipamentos (volume `a10flash-db` → `/data/a10flash.db`, env
  `PORTAL_DB`).
- **Lab** (PC ligado aos A10, conexão de SAÍDA — nunca abrir porta):
  `python -m a10flash.monitor_cli` — monitor serial + agente WS.
- Cópia do usuário: `~/a10-flasher` em ServerLIVE (`willian@ServerLIVE`),
  roda com `--config ../config.yaml` (config fora do projeto).
- Comunicação em pt-BR; código Python; token único no `.env` do docker e
  no `config.yaml` (`portal_agent.token`).

## Ciclo do worker (por equipamento)

1. hotplug serial → login (9600) → `show version`/`show bootimage` → decisão
2. upgrade: **AXAPI** (padrão) ou **CLI** → `set_bootimage` → `write memory` → reboot
3. espera voltar → **confirma versão** → factory reset (`erase`+reboot ou
   `system-reset`) → espera → confirma padrão de fábrica
4. coleta `device_info` (serial + shows brutos) → publica `device_result`
   no bus → agente repassa → portal salva no SQLite → dashboard atualiza

## Login serial — pitfalls críticos (todos descobertos em hardware real)

- **`tty.setraw()` na abertura + DTR ativo** (igual screen/cfmakeraw): sem
  ECHO/canônico; o A10 envia `ACOS login:` espontaneamente ao detectar DTR.
- **Nunca** `reset_input_buffer()`/`drain()` na abertura nem no wake — o
  banner é enviado uma única vez e é descartado.
- Wake adaptativo: ENTER → espera até ~4s → só manda próximo se console
  mudo; espera final ~15s.
- **Trava de baudrate**: se o console respondeu QUALQUER byte no baud
  configurado, não testar outros baudrates (lixo de baud errado corrompe o
  console — log do A10 com `x~~~x`/`xbbbbbb`).
- `PASSWORD_FUZZY_RE` (clock do A10 instável na senha) + `terminal length 0`
  pós-enable (sem isso o ACOS para no `---MORE---`).
- **Console já logado no 1º acesso (sessão órfã ou acesso manual): USA a
  sessão** — `_login` decide o estado pelo FIM do buffer (prompt no fim =
  sessão ativa → usa, sem derrubar+relogar; `Password:` na tela = login
  pela metade → completa). Derrubar com exit/quit deixava o console mudo
  (timeout 20s com `recebido: ''`) e o ciclo morria com "Falha
  irrecuperável: RELIGUE O EQUIPAMENTO NA TOMADA".
- **Login do 1º acesso com retry até o timeout**: `_cycle` usa
  `_wait_and_login` (deadline `boot_wait`) em vez de 3 tentativas e
  morrer — religar só é pedido depois do deadline esgotado.
- `logout` no fim do ciclo continua (console limpo para o próximo).
- `SerialException ... multiple access on port` = outro processo (screen,
  serviço antigo) segurando a porta — matar antes.
- Diagnóstico: `tests/diag_login.py --auto|--scan|--login`
  (A10_USER/A10_PASS via env).

## Upgrade — decisões e regras

- `upgrade_method: axapi` (padrão; sem as perguntas do CLI) | `cli`
  (responde as perguntas: salvar config→y, reboot→n).
- AXAPI: POST `/upgrade/hd` com `file-url` (sftp:// aceito direto) +
  `use-mgmt-port: 1`; o equipamento PUXA a imagem pela gerência. Detalhes
  do polling em `references/axapi-upgrade.md`.
- AXAPI precisa do IP da gerência (lido via `show interfaces management`,
  dual-formato; sem IP → `ip address dhcp` + poll 40s).
- `upgrade_slot: booted` (BANCADA: SEMPRE o slot bootado) | `auto`
  (não-bootado, fallback preservado).
- `version_policy: skip_newer` | `upgrade_newer` (mais nova da MESMA
  família; nunca rebaixar, nunca pular família).
- `firmware_map` por grupos (models_fta 4430|4440|5430...; models_ftav2
  3430|5330...; models_non_fta 930|1040...) com `versions: [{version, url}]`;
  firmware sftp: `ispanel@138.97.60.34`; sem match → erro claro (nunca
  imagem errada).
- ACOS 5.x: parsers dual-formato — modelo `'Thunder Series...TH5430S'`,
  mgmt `'Internet address is X, Subnet mask is Y'` (+`_mask_to_prefix`),
  bootimage `'Hard Disk X image (default) version V'`.

## Registro de equipamentos (DB do portal)

- Worker: `_collect_device_info(cli)` — serial (`get_serial` do show
  version) + saídas brutas de `show version`/`show license-info`/
  `show environment`; **cada leitura tolerante a falha** (não quebra o ciclo).
- `_publish_device_result` (só no sucesso) → bus → agente `_forward`
  (repassa TUDO do bus) → portal WS `/agent` (`AGENT_TYPES` inclui
  `device_result`) → `DeviceStore.upsert` → publica `device_saved` →
  dashboard (tabela + clique expande os shows).
- Endpoints: `GET/POST /api/devices`, `GET /api/devices/{serial}` (token
  X-Token).
- **Pitfall SQLite em FastAPI**: `sqlite3.connect(check_same_thread=False)`
  - `threading.Lock` em todas as operações (handlers rodam em threads
    diferentes); `bool(upgraded)` na leitura (SQLite devolve int).

## Testes (~50+, suíte completa 7–9 min → rodar em background)

- Worker E2E: pty real (`FakeA10`) + `FakeAxapiServer` (HTTP real);
  `fake_axapi` com `upgrade_delay`/`fail_status` para simular cópia/falha.
- `test_portal`: `make_portal` SEMPRE com `db_path=":memory:"`; teste de WS
  deve **enviar `hello` ANTES de `receive_json()`** (o portal só responde
  `welcome` após hello — sem hello o teste trava).
- Zip: regenerar com python `zipfile` excluindo `.venv`/`__pycache__`
  (sem `zip`/`unzip` instalados no sandbox).

## Referências

- `references/axapi-upgrade.md` — POST /upgrade/hd síncrono vs 202+polling,
  timeouts, on_progress, status codes.
