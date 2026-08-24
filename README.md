# a10-flasher

Automação de **upgrade de firmware + factory reset** de equipamentos **A10 Networks (Thunder / ACOS)** via console serial, com **portal web em tempo real**.

Você só pluga o equipamento na porta serial do PC do laboratório e liga a energia
— o script detecta o hotplug (`/dev/ttyUSB*`), faz login, verifica a versão ACOS,
atualiza o firmware (via SFTP na porta de gerência), aplica factory reset e mostra
tudo no portal (além de Telegram opcional).

## Arquitetura (duas partes, máquinas diferentes)

```
🏢 SALA DO SERVIDOR                              🧪 LABORATÓRIO (PC dos A10)
┌──────────────────────────┐                     ┌──────────────────────────────┐
│ python -m a10flash.portal│    WebSocket        │ python -m a10flash.monitor_cli│
│ • dashboard web (8080)   │ ◄─────────────────► │ • agente (WS client)         │
│ • hub de agentes         │  eventos + comandos │ • monitor + workers seriais  │
│ • REST API               │                     │ • reconexão automática       │
└──────────────────────────┘                     └──────────────────────────────┘
```

- O **agente** conecta no portal (conexão de SAÍDA — não precisa abrir porta
  no PC do laboratório) e reconecta sozinho (intervalo fixo entre tentativas).
- O portal atende **vários laboratórios** ao mesmo tempo (agentes distintos).
- Se o portal cair, o laboratório continua trabalhando normalmente (só perde
  o acompanhamento ao vivo; status é re-sincronizado na reconexão).

## Como funciona

```
porta serial aparece  →  login (admin/a10)  →  show version  →  show bootimage
     │
     ├─ versão < alvo?  ── SIM ──►  AXAPI (HTTPS na gerência):
     │                              upgrade/hd { file-url: sftp://... , use-mgmt-port: 1 }
     │                              (o equipamento PUXA o firmware via SFTP)
     │                              → set bootimage → write memory → reboot
     │                              → aguarda voltar → confere versão
     │
     └─ factory reset: erase → reboot (ou system-reset) → confere versão final
          │
          └─ ✅ sucesso  |  ❌ falha: avisa / cicla energia (tomada Tasmota)
               tudo publicado no portal em tempo real (WebSocket)
```

Do portal você pode **comandar os workers**: `abort`, `pause`, `resume` e
`rerun` (repetir ciclo). Os comandos são consumidos nas fronteiras de estágio —
nunca interrompem um upgrade no meio.

Comandos usados no equipamento (padrão ACOS, validados contra documentação oficial):

| Ação | Comando |
|---|---|
| Login | `admin` / `a10` (padrão de fábrica) + `enable` |
| Versão | `show version`, `show bootimage` |
| IP de gerência | `show interfaces management` |
| IP estático (se não tiver) | `configure terminal` → `interface management` → `ip address X /24` → `ip default-gateway G` |
| Factory reset | `erase` + `reboot` (ou `system-reset`) |
| Salvar | `write memory` |

O upgrade em si usa a **AXAPI REST** (`https://<ip-gerencia>/axapi/v3/`), conforme o
fluxo oficial da A10 (projeto de referência: `ACOS-Upgrade`, da própria A10).
O `file-url` aponta para um servidor SCP/SFTP **alcançável pela rede de gerência** — o
equipamento baixa o arquivo sozinho (SCP/SFTP via porta de gerência, `use-mgmt-port: 1`).

No método `cli` (serial), o ACOS pergunta se quer reiniciar após instalar — com
`reboot_after_upgrade: true` (default) o worker responde "y": a caixa reinicia
**sozinha**, e o worker aguarda voltar ao login e confirma a versão antes do
factory reset (sem depender do prompt voltar no console). No método `axapi` a
mesma opção envia a flag oficial **`reboot-after-upgrade: 1`** no payload do
`POST /upgrade/hd` (doc A10: "reboot system after upgrade is done") — idem:
instala, reinicia sozinha, o worker confirma. Ambas exigem
`upgrade_slot: booted` (bancada).

## Instalação

```bash
# Linux, com Python 3.10+ (testado com 3.13) — faça nas DUAS máquinas
cd /opt/a10-flasher
uv venv .venv                                  # ou: python3 -m venv .venv
uv pip install --python .venv/bin/python -r requirements.txt

# permissão de acesso às portas seriais (somente no PC do laboratório):
sudo usermod -aG dialout $USER    # faça logout/login depois
```

## Configuração

Edite `config.yaml`. **Itens obrigatórios antes de usar de verdade:**

```yaml
device:
  target_version: "4.1.4"      # versão ACOS alvo

  # firmware POR FAMÍLIA DE HARDWARE (padrão A10: mesma versão base,
  # imagem diferente por família — 4.1.4-GR1-P14 variantes A/F/n)
  firmware_map:
    models_fta:
      match: "4430|4440"        # regex contra o modelo (show version)
      url: "sftp://user:pass@SRV/fw/ACOS_4.1.4-GR1-P14_A....upg"
    models_ftav2:
      match: "3430|5330"
      url: "sftp://user:pass@SRV/fw/ACOS_4.1.4-GR1-P14_F....upg"
    models_non_fta:
      match: "930|840|vThunder"
      url: "sftp://user:pass@SRV/fw/ACOS_4.1.4-GR1-P14_n....upg"

  firmware_url: "sftp://usuario:senha@IP_SERVIDOR/caminho/ACOS_4.1.4.upg"
  # ^ fallback quando não há firmware_map ou nenhum grupo casa

portal_server:                 # na máquina DO SERVIDOR
  host: 0.0.0.0
  port: 8080
  token: "defina-um-token"     # mesmo token nos agentes

portal_agent:                  # no PC DO LABORATÓRIO
  url: "ws://IP_DO_SERVIDOR:8080/agent"
  agent_id: "lab-1"
  token: "defina-um-token"     # mesmo token do portal_server
```

- **Serial**: `baudrate: 9600` (padrão dos Thunder; autodetect tenta os
  comuns), `wake_enters: 3` — alguns consoles dormem até receber Enter
  antes do login aparecer.

- O servidor SFTP precisa ter `sshd` rodando e ser alcançável **pela porta de
  gerência do equipamento** (mesma VLAN de gerência).
- `mgmt_ip: auto` lê o IP do equipamento via serial. Se o equipamento estiver sem
  IP (config de fábrica), preencha `mgmt_static` e o script aplica via CLI.
- O monitor procura **apenas** `/dev/ttyUSB*` e `/dev/ttyACM*` (os nomes by-id do
  `/dev/serial/by-id` duplicavam a mesma porta com outra chave, gerando dois
  workers na mesma caixa).
- **Anti-loop**: caixas que já passaram por um ciclo bem-sucedido ficam num
  cache local persistente (`processed_serials.json`, ao lado do config) — o
  próximo ciclo só loga e **pula** (sem reset). "Repetir ciclo" no portal força
  o re-processamento.
- `notify.telegram`: aviso por bot (opcional, independente do portal).
- `power.mode: tasmota` + host da tomada: se o equipamento travar, cicla a
  energia e tenta de novo. Em `manual`, só avisa.

## Uso

```bash
# MÁQUINA DO SERVIDOR — portal:
.venv/bin/python -m a10flash.portal --config config.yaml
# abra http://IP_DO_SERVIDOR:8080/ no navegador

# PC DO LABORATÓRIO — monitor + agente:
.venv/bin/python -m a10flash.monitor_cli --config config.yaml

# demo completa SEM hardware (A10 simulado, sem portal):
.venv/bin/python -m a10flash.monitor_cli --config config.yaml --simulate

# demo ao vivo com portal (2 terminais):
.venv/bin/python -m a10flash.portal --config config.yaml        # terminal 1
.venv/bin/python tests/demo_lab.py ws://127.0.0.1:8080/agent    # terminal 2

# ciclo numa porta específica — DEPOIS do ciclo o processo NÃO encerra:
# segue como daemon (loop eterno; Ctrl+C para parar). Use o botão
# "Repetir ciclo" no portal para re-processar, ou --exit-after-cycle
# para o comportamento antigo (encerrar após o ciclo, teste):
.venv/bin/python -m a10flash.monitor_cli --config config.yaml --once /dev/ttyUSB0
.venv/bin/python -m a10flash.monitor_cli --config config.yaml --once /dev/ttyUSB0 --exit-after-cycle
```

### Rodando como serviço (produção)

```bash
sudo cp deploy/a10-flasher.service /etc/systemd/system/      # no lab (monitor)
sudo cp deploy/a10-flasher-portal.service /etc/systemd/system/  # no servidor
sudo systemctl daemon-reload
sudo systemctl enable --now a10-flasher        # lab
sudo systemctl enable --now a10-flasher-portal # servidor
journalctl -u a10-flasher -f
```

### Deploy do portal com Docker + Traefik (domínio público)

O portal roda em container atrás do **Traefik** com TLS (Let's Encrypt) em
`https://a10.app.diorg.es`:

```bash
# 1. crie a rede externa do Traefik (uma vez):
docker network create traefik-public
#    (se a sua rede tem outro nome, ajuste em deploy/docker/docker-compose.yml)

# 2. defina o token no arquivo .env (junto do compose):
cat > deploy/docker/.env <<'EOF'
PORTAL_TOKEN=um-token-forte
EOF

# 3. o domínio a10.app.diorg.es deve apontar (DNS A) para o host do Traefik,
#    e o certresolver do Traefik deve se chamar `letsencrypt` (ajuste o label
#    se for diferente)

# 4. suba:
docker compose -f deploy/docker/docker-compose.yml up -d --build
```

O compose expõe o portal **apenas na rede interna do Traefik** (sem publicar
porta no host) e inclui: rota HTTPS com certificado, redirecionamento
HTTP→HTTPS, healthcheck e volume de logs. WebSocket (`/ws` e `/agent`)
funcionam normalmente através do Traefik.

Com o portal no ar, o **laboratório** conecta pelo domínio público:

```yaml
portal_agent:
  url: "wss://a10.app.diorg.es/agent"   # wss (TLS)
  agent_id: "lab-1"
  token: "um-token-forte"               # mesmo token do PORTAL_TOKEN
```

> O portal lê `PORTAL_TOKEN`, `PORTAL_HOST` e `PORTAL_PORT` de variáveis de
> ambiente — o container não precisa de config.yaml montado. Se quiser
> Telegram/logs configurados no portal, monte o config.yaml (linha comentada
> no compose).

### API do portal (para integrar em outros sistemas)

```
GET  /api/status                -> agentes + dispositivos + estados
GET  /api/events?limit=100      -> últimos eventos (log em tempo real)
GET  /api/devices               -> equipamentos registrados (resumo)
GET  /api/devices/{serial}      -> registro completo (shows salvos)
DELETE /api/devices/{serial}   -> apaga o registro (limpeza manual)
POST /api/devices               -> salva/atualiza um registro (upsert por serial)
POST /api/devices/{key}/cmd     -> {"command": "abort|pause|resume|rerun"}
WS   /ws?token=...              -> stream de eventos + envio de comandos
```

### Equipamentos registrados (DB)

Ao concluir um ciclo com sucesso, o worker coleta o **número de série** e as
saídas de `show version`, `show license-info` e `show environment`, e o agente
envia ao portal, que salva em **SQLite** (`portal_server.db_path`, no Docker:
volume `a10flash-db` → `/data/a10flash.db`) e marca o equipamento como
**atualizado**. O dashboard lista os registros (tabela "Equipamentos
registrados") e o clique em um serial expande os shows salvos. O upsert é por
serial — a mesma caixa re-flashada atualiza o registro existente.

## Testes

A suíte roda o **ciclo completo contra um A10 simulado** (pty real + AXAPI fake),
testa o controle por comandos (abort/pause/resume), e um teste **end-to-end
real** do portal + agente via WebSocket em loopback:

```bash
.venv/bin/python -m pytest tests/ -v
```

> `pytest` não está em `requirements.txt` (dependência apenas de
> desenvolvimento): instale antes com
> `uv pip install --python .venv/bin/python pytest`.

Cobre: upgrade (slot correto, use-mgmt-port), verificação de versão, factory
reset antes/depois, IP estático, falha de login pedindo intervenção, abort/pause
dos workers, autenticação do portal e fluxo agente ↔ portal.

## Observações importantes

- ⚠️ **Equipamento fora de produção**: o ciclo apaga a configuração (factory reset).
- A licença **não** é perdida no reset (`system-reset`/`erase` não afetam licença —
  confirmado no fórum oficial da A10).
- O upgrade grava no **slot bootado** por padrão (bancada — o equipamento volta
  ao padrão de fábrica no fim do ciclo). Para preservar o fallback (gravar no
  slot não-bootado): `upgrade_slot: auto`.
- Senha padrão de fábrica (`admin`/`a10`) — depois do reset, o equipamento volta
  ao padrão; lembre de definir uma senha nova na configuração inicial.
- Sem `token` no portal, qualquer um na rede pode ver e comandar — **defina o token**.

## Estrutura

```
a10flash/
  monitor_cli.py   # entry point do LAB: monitor + agente
  portal.py        # entry point do SERVIDOR: portal web (hub WS + REST)
  agent.py         # agente: ponte EventBus local <-> portal (WS client)
  bus.py           # event bus thread-safe (histórico + assinantes)
  mailbox.py       # fila de comandos por worker
  monitor.py       # detecção de hotplug e spawn de workers
  worker.py        # máquina de estados do ciclo (+ abort/pause/resume)
  a10_cli.py       # console serial ACOS (login, show, erase, reboot...)
  a10_axapi.py     # cliente AXAPI (upgrade via SFTP, bootimage, write...)
  serial_console.py# camada serial com expect
  version.py       # parsing/comparação de versões ACOS
  notify.py        # log + Telegram + eventos no bus
  power.py         # tomada Tasmota / modo manual
  web/index.html   # dashboard (single-page, sem CDN)
tests/             # A10 fake (pty) + AXAPI fake + testes + demo_lab.py
```

