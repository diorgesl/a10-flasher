# Digi Connect IT 16 como servidor serial (SSH) — design

Data: 2026-10-05

## Objetivo

Trocar o USB serial direto no PC do lab pelo Digi Connect IT 16
(`10.10.1.155`): cada porta física N é acessada por SSH na porta TCP
`3000 + N`. O monitor precisa saber, sozinho, quando há uma caixa ligada em
cada porta, como hoje faz com o hotplug do `/dev/ttyUSB*`. O ciclo
(login, upgrade, reset, modo teste, burn-in, portal) não muda — só o
transporte dos bytes e a forma de detectar presença.

Decisões do usuário: detecção por **sonda ativa**; **só o Digi** (o USB
fica no código, selecionado por config, sem rodar junto); autenticação
por **usuário e senha** no `config.yaml`.

## Fatos medidos no aparelho (sonda de 2026-10-05)

- OpenSSH 10.3; métodos `publickey,keyboard-interactive` (sem `password`
  puro — a senha entra pelo fallback keyboard-interactive do paramiko).
- Portas 3001–3008 abertas; 3009–3016 não abrem TCP.
- Toda conexão recebe um banner do Digi:
  `Connecting to portN: / Type '~b.' to disconnect … / UART Mode: 9600 8N1`.
- Em seguida o Digi reenvia o **histórico** da porta (até ~4 KB, sempre,
  em toda conexão — inclui sessões antigas).
- Porta já em uso (acesso exclusivo ligado): `PORT already in use` +
  `Disconnected from portN` e a conexão fecha.
- Porta sem caixa: silêncio total após ENTER.
- Caixa ligada: ENTER → `ACOS login:` em < 1,5 s.
- Padrões do Digi relevantes (manual): idle timeout 15 min, history 4000
  bytes, escape `~b`.

## Config

```yaml
serial:
  transport: digi            # usb (padrão) | digi
  digi:
    host: 10.10.1.155
    username: admin
    password: "..."
    base_port: 3000          # porta N -> SSH base_port + N
    ports: [1, 2, 3, 4, 5, 6, 7, 8]   # padrão 1..16
    probe_interval: 10       # s entre sondas
    probe_timeout: 4         # s esperando resposta ao ENTER
    absent_after: 3          # sondas seguidas sem resposta = desconectada
```

Chave da porta: `digi-p01`…`digi-p16`. Path: `ssh://10.10.1.155:3001`.
`--once ssh://host:porta` também funciona.

Config recomendada no Digi (portas dos A10): idle timeout 0, history 0,
escape sequence vazia, acesso exclusivo ligado. O código não depende de
nenhuma delas.

## Componentes

### `a10flash/ssh_console.py` — `SshConsole`

Mesma interface da `SerialConsole` (`send`, `sendline`, `drain`,
`expect`, `close`, `rx_bytes`, `port`).

- Abertura: paramiko `Transport` → `auth_password` (fallback
  keyboard-interactive) → pty + `invoke_shell` → lê até a linha
  `UART Mode:` do banner → **descarta o histórico** (lê até 0,5 s de
  silêncio, teto de 3 s; o histórico chega em rajada na velocidade da
  rede, o console ao vivo a 9600 baud).
- Erros (todos `ConsoleError`):
  - `PortBusy` — `PORT already in use`;
  - `LinkDown` — TCP/SSH/autenticação falhou (Digi fora, VPN, senha);
  - `SessionClosed` — canal fechou depois de aberto (idle timeout,
    queda de rede, reboot do Digi).
- Host key aceita sem verificação (bancada, LAN privada).
- `probe_port(...)` → `"present" | "absent" | "busy" | "unreachable"`:
  abre (banner + descarte), manda ENTER, `present` se chegar algum
  caractere visível em `probe_timeout`, fecha.

### `SerialA10`

- Novo parâmetro `console_factory` (padrão `SerialConsole`).
- `PortBusy`/`LinkDown` propagam direto do `open_and_login` (não viram
  "nenhum baudrate respondeu").
- `ping(timeout)`: ENTER na sessão aberta → True se veio caractere
  visível, False se silêncio; consome a resposta inteira (sem sobra de
  prompt para o próximo `cmd`). Canal morto → `SessionClosed`.

### Worker

- Path `ssh://` → `cli_cls(..., console_factory=<SshConsole com as
  credenciais do config>)` e `baud_autodetect=False`.
- `_port_present()` substitui `os.path.exists(self.port_path)` no modo
  teste; o burn-in recebe o mesmo callable (`port_present`).
  - USB: `os.path.exists(self.port_path)` (inalterado).
  - Digi: `LinkPresence` (abaixo) com a checagem:
    `ping` → presente/silêncio; `SessionClosed` → reabre a sessão
    (`open_and_login`); reabriu = presente; caixa muda = falta;
    `PortBusy`/`LinkDown` = **desconhecido** (avisa no log, não conta
    falta — Digi fora do ar nunca vira "caixa desconectada").
- O ENTER periódico (a cada `probe_interval`) também impede o idle
  timeout do Digi de derrubar a sessão.

### `a10flash/presence.py` — `LinkPresence`

`present()` chamado a cada tick do loop; executa a checagem no máximo a
cada `interval`; só devolve False após `absent_after` faltas seguidas;
resultado desconhecido (None) não zera nem soma.

### Monitor — `DigiScanner` (em `a10flash/digi.py`)

- `snapshot(held)` → `{chave: path}` das portas presentes. Portas em
  `held` (worker vivo segurando a sessão) entram como presentes **sem
  sonda** (acesso exclusivo).
- Demais portas sondadas em paralelo (`ThreadPoolExecutor`).
- Histerese: porta vista presente só sai após `absent_after` sondas
  `absent` seguidas. `busy`/`unreachable` = estado desconhecido, mantém
  o anterior.
- `unreachable` e `busy`: uma linha de log na transição (sem spam);
  porta inalcançável é sondada a cada 60 s.
- `PortMonitor` usa o scanner quando `serial.transport: digi`; o loop
  usa `probe_interval` como período.

## Fora do escopo

- Rodar USB e Digi ao mesmo tempo.
- Detecção por DCD/DSR (cabo console não tem os pinos).
- Configurar o Digi pelo código.

## Testes

- `SshConsole`/`probe_port`: servidor SSH paramiko em processo que imita o
  Digi (banner, histórico, porta ocupada, responde ENTER ou fica mudo,
  derruba o canal).
- `SerialA10.ping` com console falso.
- `LinkPresence` com relógio falso.
- `DigiScanner` com sonda falsa (histerese, held, busy, unreachable).
- Worker: `_port_present` USB inalterado (suíte existente) + Digi com
  `LinkPresence`/stub.
- Validação em hardware: `--once ssh://10.10.1.155:3001` na bancada.
