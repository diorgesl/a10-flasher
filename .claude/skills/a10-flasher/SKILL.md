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
- **Atualização de código do lab**: comando `update` via portal
  (`POST /api/agents/{id}/cmd`) ou auto-update (`portal_agent.auto_update`
  no config) — agente faz git fetch/reset --hard e SAI; o systemd
  (`Restart=always`) sobe com o código novo. NUNCA atualiza com ciclo
  ativo (`monitor.has_active_cycle()`); bootstrap inicial (clone do repo
  + config.yaml) é manual, uma vez.
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
5. **MODO TESTE (sempre ativo após sucesso)**: a caixa fica CONECTADA na
   serial; o worker coleta o uptime (`show version` → `parse_uptime`) a
   cada `device.test_interval_h` (default 6) até a caixa ser desconectada
   (porta some) ou `abort` do portal → publica `uptime_sample` → portal
   salva em `uptime_samples` → histórico no dashboard
   (`GET /api/devices/{serial}/uptime`). Sessão caiu? Reloga sozinho.
   TESTES: o fake NÃO "despluga" de verdade (o node do pty persiste no
   macOS enquanto o worker segura o fd) — os helpers patcheiam
   `os.path.exists` para o caminho do fake no evento `test_mode`.

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
- **NUNCA `flush()`/tcdrain no serial**: o flush do pyserial é tcdrain
  e BLOQUEIA até o A10 LER a saída — console mudo/caixa dormindo =
  wake/login travados por minutos atravessando todos os timeouts.
  `send` sem flush; `close` fecha o fd direto (is_open=False antes);
  `VMIN=0/VTIME=0` no termios mantém os reads sem bloqueio.
  ⚠️ Pendência de bancada: revalidar `diag_login.py --auto` em hardware
  real na próxima oportunidade (sem tcdrain o script não espera mais o
  A10 consumir cada send — ordem preservada pelo buffer do kernel,
  mas confirmar no equipamento).
- `logout` no fim do ciclo continua (console limpo para o próximo).
- `SerialException ... multiple access on port` = outro processo (screen,
  serviço antigo) segurando a porta — matar antes.
- Diagnóstico: `tests/diag_login.py --auto|--scan|--login`
  (A10_USER/A10_PASS via env).

## Upgrade — decisões e regras

- `upgrade_method: axapi` (padrão; sem as perguntas do CLI) | `cli`
  (responde as perguntas: salvar config→y, reboot→n).
- **ACOS 2.x NÃO tem AXAPI** (HTTPS da gerência recusa conexão) — com
  versão atual 2.x o método vira `cli` automaticamente (serial
  `upgrade hd ... sftp://`), independente do config. Repro de bancada:
  caixa 2.7.2 com method axapi → connection refused → ciclo morria
  pedindo para religar o equipamento.
- **ACOS 2.x: bootimage só na forma CURTA** (`bootimage hd pri`/`sec`) —
  a longa (`primary`/`secondary`) é rejeitada em silêncio e o reboot
  volta na partição antiga. `boot_to`/`set_bootimage` tentam as curtas
  primeiro e VALIDAM o eco + `show bootimage` (o `cmd()` engole erros).
- **ACOS 2.x corta linhas > 80 col** — o `upgrade hd` com URL sftp longa
  era truncado no meio da URL e rejeitado com `^`. Se as duas partições
  forem 2.x, o caminho/arquivo no servidor sftp PRECISA ser curto
  (symlink); o erro agora avisa com a dica.
- **Comparação de versões: o PATCH (P<n>) decide, não o build final** —
  `5.2.1-P14.73` > `5.2.1-p5.114` (P14 > P5; o parser antigo comparava
  o 114 vs 73 e trocava o boot para a partição mais VELHA, oscilando a
  cada ciclo). `version_tuple` = core + P-level + resto (GR/SP/build).
- **Boot-switch pela VERSÃO DA CONFIG, não pela mais nova**: `_cycle`
  muda o boot só se a partição não-bootada casa com o target do config
  (target_version/firmware_map) e a bootada não. Comparação relativa
  entre slots subia em família fora da config (ex.: 6.0.0).
- **IP de gerência legado `172.31.31.31`** (estático da bancada antiga,
  sem rota pro servidor sftp): `_ensure_mgmt_ip` troca para DHCP
  (`conf`/`interface management`/`ip address dhcp`) e espera o IP novo —
  roda nos DOIS métodos (o `use-mgmt-port` do cli também puxa pela
  gerência; caixa sem IP também ganha DHCP). Agente autônomo, sem
  religar nada.
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
  `show environment`/`show interfaces`; **cada leitura tolerante a
  falha** (não quebra o ciclo). Coluna `interfaces` no SQLite com
  migração `ALTER TABLE` para DBs antigos.
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
