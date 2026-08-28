# Burn-in de estabilidade com TRex (fase 1 — uma caixa por vez)

Data: 2026-08-28
Status: aprovado (design apresentado e aceito em conversa)

## 1. Contexto e objetivo

As caixas A10 processadas na bancada são de segunda mão. O objetivo do burn-in é
**garantir estabilidade**: forçar tráfego CGNAT/LSN através da caixa por 24 horas
e verificar que ela **não reinicia nem trava sob carga**. Reiniciar durante o
teste = caixa reprovada (é exatamente a falha que o screening quer pegar).

O TRex (Cisco, v3.08) roda **no mesmo PC do agente do lab** (`monitor_cli`),
com placa X520-DA2 (2 portas 10G): porta 0 = lado inside/clientes, porta 1 =
lado outside/servidores. Hoje o teste é feito manualmente (daemon + console +
profile). Este projeto automatiza o ciclo completo: config LSN → tráfego →
veredito → limpeza.

Fase 1: uma caixa por vez. Fase 2 (fora de escopo, ver seção 10): múltiplas
caixas simultâneas via VLANs.

## 2. Requisitos

### 2.1 Funcionais

- **R1 — Burn-in automático pós-ciclo**: quando o ciclo termina com sucesso e a
  caixa está **na versão alvo** (upgraded, ou já chegou na versão alvo — em
  ambos os casos está "na última versão"), o burn-in inicia automaticamente
  após o registro no portal. Caixas em skip (já processadas, re-plugadas) entram
  direto no modo teste, sem re-burn-in automático.
- **R2 — Burn-in manual pelo portal**: caixa em modo teste pode receber
  comando de iniciar burn-in a qualquer momento (com CPS/duração opcionais).
- **R3 — Config LSN automatizada**: o worker aplica a config CGNAT/LSN
  (template com portas renderizadas por caixa) via serial, verifica erros
  linha a linha e dá `write memory`.
- **R4 — Tráfego**: daemon TRex (`--astf`) + profile `trex/astf/a10_astf.py`
  com `--cps 1000` (default; ~2 Gbps no teste manual de referência).
- **R5 — Duração default 24h**, configurável.
- **R6 — Monitoramento**: amostras de tráfego a cada `sample_interval_s`
  (default 60s) + amostras de uptime na cadência normal do modo teste.
- **R7 — Vereditos**:
  - ✅ **pass** — 24h completas sem reiniciar;
  - ❌ **fail** — reiniciou/travou durante o burn-in (uptime zerou ou sessão
    caiu e, após relogin, o uptime voltou do zero);
  - ⚠️ **interrupted** — caixa desconectada da serial antes do fim (tráfego
    parado por limpeza, sem erase);
  - ⚠️ **aborted** — comando de parada do portal ou falha de infraestrutura do
    TRex irrecuperável (sem veredito sobre a caixa).
- **R8 — Limpeza**: ao fim do burn-in (pass, fail ou aborted), a caixa é
  apagada com o factory reset existente (`_factory_reset` + relogin +
  `_wait_real_reboot`) e volta ao **modo teste** padrão (uptime) até ser
  desconectada. `interrupted` não apaga (caixa já foi embora).
- **R9 — Resultados no portal**: veredito + histórico de amostras visíveis no
  dashboard, por caixa.

### 2.2 Não-funcionais

- Nada de porta aberta na rede do lab — tudo local no PC do lab (WS de saída
  continua sendo a única conexão com o servidor).
- Um burn-in não bloqueia outros equipamentos na bancada (cada porta serial tem
  seu fluxo; o loop de 24h ocupa apenas o thread da própria caixa, como o modo
  teste já faz).
- Falha de banco no portal ao salvar amostra **não derruba** a conexão do
  agente (mesmo padrão de `uptime_sample`).
- Código testável sem TRex real e sem A10 real (injeção de dependência,
  fakes).

## 3. Componentes

### 3.1 `a10flash/trex_client.py` (novo)

Classe `TRexClient` — única interface entre o worker e o TRex. Injetada no
worker (mesmo padrão de `cli_cls`). Métodos:

- `start_daemon()` — subprocess `<trex.path>/t-rex-64` com `daemon_args`
  (default `["-i", "--astf"]`); espera a porta 4501 aceitar conexão (timeout
  60s, com log do progresso). Marca que **nós** subimos o daemon.
- `stop_daemon()` — termina o subprocess **somente se fomos nós que subimos**;
  daemon pré-existente (sessão manual do usuário) é usado e não é morto.
- `connect()` — ASTFClient (lib do TRex: `automation/trex_control_plane/
  interactive/` adicionado ao `sys.path` a partir de `trex.path`) conecta em
  `localhost:4501`. Se já houver daemon rodando, `start_daemon` é pulado.
- `start_profile(cps)` — carrega `trex/astf/a10_astf.py` com tunables
  `["--cps", str(cps)]` e inicia com `duration = duration_h*3600 + 300` (folga
  de 5 min; o burn-in para o tráfego explicitamente antes).
- `stats()` — retorna dict: `tx_bps, rx_bps, tx_pps, rx_pps, active_sessions,
  errors` (mapeado das stats do ASTFClient; sessões ativas = flows ativos).
- `stop()` — para o tráfego (idempotente) e desconecta o client.

Erros de conexão/RPC do TRex viram exceção `TRexError` (nova), tratada pelo
burn-in (seção 4).

### 3.2 `a10flash/burnin.py` (novo)

Classe `BurninController` — dona do loop de burn-in, para não inchar mais o
`worker.py`. Recebe: `cli` (console serial logado), `serial`, `device_info`
(com `interfaces` = saída bruta de `show interfaces brief`), `trex` (instância
`TRexClient`), `bus`, `cfg`, `notifier`, relógio via `time` (patchable em
teste). Método principal: `run() -> dict` (veredito + resumo).

### 3.3 `a10flash/worker.py` (alterado)

No caminho de sucesso do `_cycle` (após `_mark_processed`), no lugar de chamar
`self._test_mode(...)` direto:

```
if burn-in habilitado (trex.enabled) e versão final == alvo (regra da seção 5.2):
    resultado = BurninController(...).run()      # publica eventos; no fim faz erase
    cli = <sessão após o erase>                   # relogin pós factory reset
_modo teste padrão (como hoje)
```

O `_check_commands` do modo teste já trata `abort`; o burn-in tem seu próprio
`_check_commands` interno que trata `abort` (FlashAbort, mata tudo, como hoje)
e **`burnin_stop`** (exceção `BurninStop` → cleanup: para tráfego, erase,
retorna ao modo teste).

**Consumo de `burnin_start` (manual)**: os dois pontos que entram em modo
teste (caminho de sucesso e caminho de skip) passam por um wrapper
`_monitor_phase(cli, serial, device_info)`: chama `_test_mode`; se o modo
teste retornou porque o `_check_commands` viu `burnin_start` (sentinel, não
exceção — o modo teste não morre), roda o burn-in (com cps/duração do
payload do comando, se enviados) e **volta ao `_test_mode`** (loop). O
`abort` continua com a semântica atual em qualquer estado.

### 3.4 Config LSN — template e regra de portas

- `trex/config_lsn.conf` vira **template** com placeholders `{INSIDE_PORT}` e
  `{OUTSIDE_PORT}`. O bloco `interface ethernet 17-20 enable` do arquivo atual
  sai do template e vira opção de config **`trex.extra_enable_ports`** (lista,
  default vazia) — cada porta da lista gera `interface ethernet N` / `enable`.
- **Regra das portas** (função pura `pick_lsn_ports(brief) -> (inside,
  outside)` em `a10flash/burnin.py`): a detecção é **sempre dinâmica**, a
  partir do `show interfaces brief` da caixa — a bancada tem caixas de 9, 10
  e 48 portas (e outros tamanhos); nada de porta hardcoded.
  1. parseia `show interfaces brief` (dual-formato, como os parsers
     existentes: 4.x/5.x);
  2. ignora o **bloco traseiro de portas 40G+** (nos modelos grandes as
     últimas portas são 40G/100G e não servem para o TRex de 10G);
  3. seleciona as **duas últimas portas** restantes: inside = penúltima,
     outside = última;
  4. verifica que as duas são 10G — se não forem (hardware fora do padrão
     da bancada), erro claro e o burn-in não inicia (falha segura: nunca
     escolher portas erradas por dedução).
- **Aplicação**: no início do burn-in, novo `show interfaces brief` (sessão
  aberta), renderiza o template e envia linha a linha via `configure terminal`
  (padrão do `a10_cli`), **verificando o eco de cada linha** — marcadores de
  erro do ACOS (`% Invalid`, `^`, `syntax error`) → linha rejeitada. Qualquer
  rejeição: burn-in **não inicia**, evento `burnin_result` com
  `verdict: aborted` e `config_errors` = lista das linhas rejeitadas (portal
  mostra; o usuário sabe que aquele modelo precisa de config própria).
- Após aplicar com sucesso: `write memory` (config sobrevive a crash-reboot —
  útil para inspeção pós-falha).
- Sintaxe do template é ACOS 4.x/5.x. O burn-in automático só roda nessas
  versões (ver 5.2), então não há adaptação por versão na fase 1. Burn-in
  manual em versão incompatível: as linhas rejeitadas aparecem no relatório
  (comportamento aceito e documentado).

### 3.5 Portal

- **Eventos novos** (caminho bus → agente WS → portal, como os atuais;
  adicionar a `AGENT_TYPES`/handlers):
  - `burnin_started` `{type, device, port, serial, run_id, cps, duration_h,
    started_ts}` — o portal marca o run como em andamento;
  - `burnin_sample` `{type, device, port, serial, run_id, ts, tx_bps, rx_bps,
    tx_pps, rx_pps, active_sessions, errors, uptime_s}` — salvo no DB com
    try/except (nunca derruba a conexão do agente);
  - `burnin_result` `{type, device, port, serial, run_id, started_ts,
    ended_ts, duration_h, cps, verdict: pass|fail|interrupted|aborted,
    reason, config_errors, summary}` — salva o run e publica no bus do portal
    (dashboard atualiza).
  - Durante o burn-in, as amostras de **uptime continuam sendo publicadas**
    como `uptime_sample` (reuso do `_collect_uptime` intacto — o histórico de
    uptime do modo teste não ganha buraco).
- **DB** (`db.py`, `CREATE TABLE IF NOT EXISTS` — tabelas novas, sem migração):
  - `burnin_runs` `(run_id PK, serial, device, started_ts, ended_ts,
    duration_h, cps, verdict, reason, config_errors TEXT, summary TEXT)`;
  - `burnin_samples` `(id PK AUTOINCREMENT, run_id, ts, tx_bps, rx_bps,
    tx_pps, rx_pps, active_sessions, errors, uptime_s)`.
- **Endpoints** (auth `X-Token` como os atuais):
  - `GET /api/devices/{serial}/burnin` → `{runs: [...], samples: {...}}`
    (histórico de runs + amostras por run, espelho do `/uptime`);
  - `POST /api/devices/{serial}/burnin/start` — body opcional
    `{cps, duration_h}` (overrides válidos só para este run; o worker usa
    config quando ausentes; o payload viaja junto do comando no bus); valida
    que o device está online e em `test_mode` (senão 409); roteia comando
    `burnin_start` pelo caminho existente
    (`_route_command` → bus → agente → worker);
  - `POST /api/devices/{serial}/burnin/stop` — roteia `burnin_stop`; 409 se
    não houver burn-in ativo na caixa.
- **Comandos**: `COMMANDS` ganha `burnin_start` e `burnin_stop` (mesmo fluxo
  de `abort`/`pause`/`resume`; o worker os lê no loop do burn-in).
- **Dashboard**: no cartão da caixa — estado do burn-in com barra de progresso
  (elapsed/duração, calculada de `started_ts`), badge do veredito
  (✅/❌/⚠️), botões "iniciar burn-in" (visível quando caixa em modo teste,
  sem run ativo) e "parar burn-in" (quando ativo). Painel de histórico com as
  amostras do run (espelho do gráfico de uptime). O `_snapshot()` do WS inclui
  o run ativo por caixa (refresh de página mostra o progresso).

### 3.6 `config.yaml` (seção nova)

```yaml
trex:
  enabled: true              # burn-in automático pós-ciclo
  path: /opt/trex/v3.08      # instalação do TRex no PC do lab
  lsn_config: trex/config_lsn.conf   # relativo ao repo do lab
  cps: 1000
  duration_h: 24
  sample_interval_s: 60
  daemon_args: ["-i", "--astf"]
  extra_enable_ports: []     # ex.: [17, 18, 19, 20] se quiser o bloco antigo
```

A seção só existe no `config.yaml` do lab (no servidor é ignorada). Atualizar
`config.yaml.example` com a seção comentada.

## 4. Loop do burn-in (estados e erro)

```
run():
  run_id = uuid4(); started = time.time()
  publica burnin_started
  try:
    brief = cli.cmd("show interfaces brief")
    inside, outside = pick_lsn_ports(brief)          # erro -> abort (config)
    aplica template (linha a linha, checa eco)       # erro -> abort (config)
    cli.cmd("write memory")
    trex.start_daemon(); trex.connect(); trex.start_profile(cps)
    loop:
      _check_commands()          # burnin_stop -> BurninStop; abort -> FlashAbort
      porta serial sumiu?        # -> verdict interrupted, break (sem erase)
      a cada sample_interval_s:  # sample = trex.stats() + uptime
        publica burnin_sample
        mantém uptime_sample na cadência normal
        uptime novo < último?    # -> reboot -> verdict fail, break
      trex.stats() falha?        # TRexError: reconecta/redá com backoff até 5 min
                                 # irrecuperável -> verdict aborted (reason: infra), break
      elapsed >= duration_h*3600 # -> verdict pass, break
  except BurninStop:  verdict = aborted (reason: parada pelo operador)
  finally:
    trex.stop(); trex.stop_daemon()   # só o que subimos
    publica burnin_result             # ANTES do erase: o veredito chega ao
                                      # portal imediatamente, sem esperar o
                                      # reboot da limpeza
    if verdict in (pass, fail, aborted-com-parada):
        erase: _factory_reset + _wait_and_login + _wait_real_reboot
    return (verdict, resumo, nova sessão cli pós-erase)
```

- **Detecção de reboot**: comparação entre amostras consecutivas de uptime
  (`uptime_s` novo < último ⇒ reiniciou). Sessão caída sem reboot (ruído de
  console): relogin (padrão do `_collect_uptime`) e continua — só vira fail se
  o uptime pós-relogin zerou.
- **TRex falhando no meio**: não é veredito sobre a caixa. Tenta reconectar
  (e religar o daemon se fomos nós que subimos) com backoff; passados 5 min
  sem tráfego, aborted com `reason: falha de infraestrutura TRex` e erase.
- **Timeout de começo**: daemon que não sobe em 60s → aborted (reason:
  infra), erase, volta ao modo teste.
- `fail`: para o tráfego, erase, e a caixa fica **conectada no modo teste**
  para inspeção manual (dashboard vermelho).

## 5. Integração com o ciclo atual

### 5.1 Ponto de entrada

`_cycle` (worker.py), caminho de sucesso, após `_mark_processed`:

- Burn-in só entra **no caminho de sucesso** (status success). O caminho de
  skip (já processada) continua indo direto ao modo teste.
- A condição de versão alvo usa o mesmo critério do ciclo:
  `compare_versions(version_final, alvo) >= 0` e versão da mesma família do
  alvo (o `_decide_upgrade` já garante isso no caminho de sucesso). Na
  prática: sucesso ⇒ burn-in, a menos de `trex.enabled: false`.

### 5.2 Caixas 2.x

Com a política atual (`upgrade_newer`), uma caixa 2.x nunca termina o ciclo
na versão alvo de 4.x/5.x (o ciclo não pula família), então **não recebe
burn-in automático**. Se no futuro houver alvo 2.x no `firmware_map`, o
critério de 5.1 se aplica do mesmo jeito. Burn-in manual continua disponível
em qualquer versão.

### 5.3 Comandos

- `abort` (existente): semântica atual — mata ciclo/modo teste inteiro; no
  burn-in, para o tráfego no `finally` (limpeza) e **não** faz erase.
- `burnin_stop` (novo): para o burn-in com veredito aborted, faz erase e
  retorna ao modo teste.

## 6. Testes

- **Unit `pick_lsn_ports`**: briefs falsos (formato 4.x e 5.x) — caixa de
  9 portas 10G → eth8/9; caixa de 10 portas 10G → eth9/10; caixa de 16
  portas 10G → eth15/16; caixa de 48 portas com bloco de 40G no fim →
  últimas duas 10G antes do bloco; últimas portas não-10G → erro claro;
  casos de parse com nomes/velocidades variados.
- **Unit de template**: renderização com inside/outside; `extra_enable_ports`
  gerando blocos; linhas rejeitadas detectadas (eco com `% Invalid`/`^`).
- **E2E worker (`FakeA10` + `FakeTRexClient`)**: ciclo completo com burn-in
  (relógio acelerado — mesmo padrão de patch de `time`/`os.path.exists` dos
  testes de modo teste): pass; **reboot no meio → fail** + erase + volta ao
  modo teste; unplug → interrupted; `burnin_stop` → aborted + erase; config
  rejeitada → burn-in não inicia; TRex caindo → aborted (infra) após backoff.
- **Portal**: `burnin_started`/`burnin_sample`/`burnin_result` salvos no DB
  (`db_path=":memory:"`), endpoints start/stop (validação de estado → 409),
  roteamento de comando, snapshot com run ativo. WS: enviar `hello` antes de
  `receive_json()` (regra existente).
- **FakeTRexClient** implementa a interface completa com controles de teste
  (falhar stats, cps recebido, contagem de start/stop).

## 7. Sequência de implementação sugerida

1. `trex_client.py` + `FakeTRexClient` (unit — sem A10, sem portal)
2. `burnin.py`: template + `pick_lsn_ports` + loop (unit com fakes)
3. Integração no `_cycle` + comandos (E2E worker)
4. Portal: DB + endpoints + eventos (tests portal)
5. Dashboard (web) + `config.yaml.example`
6. Validação em bancada com TRex real (1 caixa, CPS baixo, duração curta →
   depois 24h)

## 8. Fora de escopo (fase 2 — multi-caixa via VLANs)

- VLAN tagging por stream no profile (802.1Q por caixa), portas lógicas do
  TRex multiplexadas entre caixas, e tabela de alocação porta/caixa.
- O desenho da fase 1 não fecha essa porta: `TRexClient` recebe par de portas
  por instância, o template é renderizado por caixa e os runs são identificados
  por `run_id`/serial — a fase 2 adiciona tags de VLAN por stream e a
  alocação, sem redesenhar o resto.

## 9. Riscos e pontos de atenção

- **Portas físicas**: a regra das duas últimas 10G depende do
  `show interfaces brief` ser fiel (velocidades reais). Em dúvida, o erro de
  config aplicada aparece no relatório e o burn-in não começa (falha segura).
- **24h de TRex**: o daemon fica 24h em subprocesso no lab; se o PC do lab
  reiniciar, o burn-in aborta por infra (veredito aborted, sem culpar a
  caixa) — a caixa continua no modo teste e o teste pode ser refeito pelo
  portal.
- **Erase ao fim de todo burn-in**: caixa passa por reboot extra (o padrão
  `_wait_real_reboot` cuida); o modo teste segue normal depois.
- **Profile/template versionados no repo**: mudanças neles exigem
  `graphify update .` (regra do projeto) e atualização do lab via comando
  `update` do portal.
