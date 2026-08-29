"""Interação com o console serial do A10 (ACOS CLI).

Comandos usados (todos padrão do ACOS):
  login: admin / a10 (padrão de fábrica)
  enable
  show version
  show bootimage
  show interfaces management      (descobre IP da porta de gerência)
  configure terminal / interface management / ip address / ip default-gateway
  write memory
  erase                           (factory reset: apaga startup-config)
  system-reset                    (reset de sistema, preserva licença)
  reboot
"""

import re
import time

from .serial_console import (
    CONFIRM_RE,
    LOGIN_RE,
    PASSWORD_FUZZY_RE,
    PASSWORD_RE,
    PROMPT_RE,
    ConsoleError,
    SerialConsole,
    confirm_answer,
)
from .version import (
    parse_acos_version,
    parse_bootimage,
    parse_model,
    parse_serial_number,
)

# IP da gerência — dois formatos:
#   antigo: "IP Address: 10.0.0.10 /24"
#   novo (ACOS 5.x): "Internet address is 10.10.1.20, Subnet mask is 255.255.255.0"
MGMT_IP_RE = re.compile(
    r"(?:IP\s+Address\s*[:=]\s*|Internet\s+address\s+is\s+)"
    r"(\d+\.\d+\.\d+\.\d+)"
    r"(?:\s*/\s*(\d+)|,\s*Subnet\s+mask\s+is\s+(\d+\.\d+\.\d+\.\d+))",
    re.IGNORECASE,
)
# Mensagem de reboot no console (upgrade hd com reboot automático,
# system-reset): a caixa avisa antes de derrubar a sessão.
REBOOTING_RE = re.compile(r"rebooting|restarting|resetting|will reboot",
                          re.IGNORECASE)

# Modo LOADING do ACOS (inicialização pós-boot/reset): o prompt vira
# 'ACOS(LOADING)#' e os comandos respondem 'System is not ready yet.'
# até o sistema terminar de iniciar.
LOADING_RE = re.compile(r"\(loading\)|not ready", re.IGNORECASE)


def _mask_to_prefix(mask):
    """255.255.255.0 -> 24."""
    try:
        return sum(bin(int(o)).count("1") for o in mask.split("."))
    except ValueError:
        return 24

# Baudrates comuns de console (o 9600 é o padrão dos Thunder via serial;
# alguns modelos usam 115200). O autodetect tenta na ordem até achar.
BAUD_DEFAULTS = [9600, 115200, 57600, 38400, 19200, 4800]


class A10Error(Exception):
    """Falha em um passo da automação no equipamento A10."""


class SerialA10:
    """Wrapper de alto nível: sessão serial logada no ACOS."""

    def __init__(self, port, baudrate=115200, username="admin", password="a10",
                 enable_password=""):
        self.port = port
        self.baudrate = baudrate
        self.username = username
        self.password = password
        self.enable_password = enable_password
        self.console = None

    # ------------------------------------------------------------ setup
    def open_and_login(self, login_timeout=20, baud_autodetect=True,
                       wake_enters=3, wake_delay=0.4):
        """Abre a porta e garante uma sessão logada.

        1. Tenta o baudrate configurado e, se `baud_autodetect`, os demais
           comuns até o console responder (o 9600 é o padrão dos Thunder).
        2. Envia ENTERs de "wake" — consoles de alguns modelos ficam
           dormentes e só mostram o login depois de Enter(s).
        3. Garante a sessão: se o console JÁ está logado (sessão órfã ou
           acesso manual), usa a sessão existente; se está no meio de um
           login ('Password:' na tela), completa; senão faz o login
           completo (user/senha/enable).
        """
        self.close()
        bauds = [self.baudrate]
        if baud_autodetect:
            for b in BAUD_DEFAULTS:
                if b not in bauds:
                    bauds.append(b)

        # fase 1: descobre o baudrate (espera curta por resposta)
        detect_timeout = min(login_timeout, 15)
        found = None
        seen = None
        last = None
        for baud in bauds:
            try:
                self.console = SerialConsole(self.port, baudrate=baud)
                # 1) espera PASSIVA: com DTR ativo o console envia o banner
                # "login:" espontaneamente ao detectar o terminal (como no
                # screen). NÃO drenar nada antes — descartar o banner
                # inicial é o que causava timeout.
                # 2) só se nada vier, ENTERs de wake espaçados (console
                # dormente) — manda ENTER, olha, manda de novo.
                seen = self._wake_console(enters=wake_enters,
                                          delay=wake_delay,
                                          passive_timeout=3.0,
                                          per_timeout=min(login_timeout, 4))
                if seen is None:
                    # nenhum ENTER acordou o console: espera final (pode
                    # estar logado e só reimprimindo o prompt, ou lento,
                    # ou parado na tela de senha de um login pela metade)
                    seen = self.console.expect(
                        [LOGIN_RE, PROMPT_RE, PASSWORD_RE, PASSWORD_FUZZY_RE],
                        timeout=detect_timeout)
                found = baud
                break
            except ConsoleError as exc:
                last = exc
                # TRAVA DE BAUDRATE: se o console respondeu QUALQUER coisa
                # (mesmo ilegível — lixo binário = termios errado/cabo),
                # NÃO testar outros baudrates: cada teste errado envia
                # lixo pro equipamento, que tenta autenticar e registra
                # falhas (visto no log do A10). O baudrate configurado
                # está falando com o equipamento — o problema é outro.
                if self.console is not None and self.console.rx_bytes > 0:
                    self.close()
                    raise A10Error(
                        f"o console respondeu em {baud} baud, mas o texto "
                        f"não está legível (recebido: {exc}). Confira o "
                        f"modo do terminal (raw), o adaptador USB-serial e "
                        f"o cabo — NÃO testei outros baudrates para não "
                        f"corromper o equipamento."
                    ) from exc
                self.close()
        if found is None:
            raise A10Error(
                f"nenhum baudrate respondeu em {self.port} "
                f"(tentados: {bauds}). Confira o cabo serial e a energia: "
                f"{last}")
        if found != self.baudrate:
            self.baudrate = found  # lembra o que funcionou (para o log)

        # fase 2: login (ou reuso da sessão já logada). `seen` já contém o
        # prompt/login/senha lido na fase 1 — consoles dormentes não
        # reenviam o banner, então o que foi lido não pode ser descartado.
        self._login(login_timeout, initial=seen)

    def _wake_console(self, enters=3, delay=0.4, passive_timeout=3.0,
                      per_timeout=5.0):
        """Espera o console falar; se ficar mudo, acorda com ENTERs.

        1. Espera `passive_timeout` pelo prompt de login / prompt logado
           SEM enviar nada — com DTR ativo a maioria dos consoles envia o
           banner sozinha ao detectar o terminal.
        2. Se nada veio, envia ENTERs espaçados (consoles dormentes), um
           de cada vez, esperando a resposta entre eles — igual ao fluxo
           manual: manda ENTER, olha, manda de novo.

        NUNCA drena antes: descartar o banner inicial é o que causava
        timeout no login real. Retorna (idx, buf) do que viu, ou None.
        """
        con = self.console
        try:
            return con.expect([LOGIN_RE, PROMPT_RE, PASSWORD_RE,
                               PASSWORD_FUZZY_RE], timeout=passive_timeout)
        except ConsoleError:
            pass  # mudo: tenta acordar com ENTERs
        for _ in range(enters):
            con.sendline("")
            try:
                return con.expect([LOGIN_RE, PROMPT_RE, PASSWORD_RE,
                                   PASSWORD_FUZZY_RE], timeout=per_timeout)
            except ConsoleError:
                continue
        return None

    def _logout_existing(self, con, timeout=8):
        """Derruba uma sessão órfã no console (exit/quit) para o getty
        voltar a mostrar 'login:'. Tolerante a sessão que pede confirmação
        ('Are you sure? [y/n]') ou que simplesmente fecha."""
        for cmd in ("exit", "quit"):
            try:
                con.sendline(cmd)
                idx, buf = con.expect([LOGIN_RE, PROMPT_RE, CONFIRM_RE],
                                      timeout=min(timeout, 4))
                if idx == 2:
                    # "Are you sure you want to exit? [y/n]" -> responde
                    con.sendline(confirm_answer(buf, "y"))
                    con.expect([LOGIN_RE, PROMPT_RE],
                               timeout=min(timeout, 4))
                return
            except ConsoleError:
                continue
        # última tentativa: Ctrl-D (EOF padrão de terminal)
        try:
            con.send("\x04")
            con.expect([LOGIN_RE], timeout=min(timeout, 4))
        except ConsoleError:
            pass

    def logout(self, timeout=10):
        """Desloga da sessão, se ainda estiver viva (exit limpo).

        O getty do console só mostra 'login:' com o console livre — se o
        ciclo terminar sem deslogar, a sessão fica órfã e o próximo login
        falha (console mudo). Seguro após reboot: se o console já voltou
        ao login:, não envia nada. Pós-reset a caixa pode demorar a
        responder — o timeout é generoso.
        """
        con = self.console
        if con is None:
            return
        try:
            idx, buf = con.expect([LOGIN_RE, PROMPT_RE],
                                  timeout=min(timeout, 5))
        except ConsoleError:
            return
        if idx == 1 and (buf.rstrip().endswith("#")
                         or buf.rstrip().endswith(">")):
            self._logout_existing(con, timeout=timeout)

    def close(self):
        if self.console is not None:
            try:
                self.console.close()
            finally:
                self.console = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------- login
    def _login(self, timeout=20, initial=None):
        """Loga no console — ou APROVEITA uma sessão já logada.

        O estado é decidido pelo FIM do buffer (o que está na tela AGORA),
        não pelo índice do expect: 'login:' pode casar em texto antigo do
        banner, mas PROMPT_RE é ancorado no fim.
          - prompt no fim ..... já existe sessão (órfã ou acesso manual):
                                 USA a sessão — não derruba nem reloga
                                 (derruba+reloga deixava o console mudo
                                 e travava o ciclo pedindo para religar)
          - 'Password:' ....... login (ou enable) pela metade: completa
          - 'login:' .......... autentica do zero
        """
        con = self.console
        if initial is not None:
            idx, buf = initial
        else:
            idx, buf = con.expect(
                [LOGIN_RE, PROMPT_RE, PASSWORD_RE, PASSWORD_FUZZY_RE],
                timeout=timeout)
        if re.search(PROMPT_RE, buf):
            idx = 1
        elif re.search(PASSWORD_RE, buf) or re.search(PASSWORD_FUZZY_RE, buf):
            idx = 2
        elif re.search(LOGIN_RE, buf):
            idx = 0
        else:
            raise A10Error(
                f"estado do console não reconhecido em {self.port}: "
                f"{buf[-200:]!r}")
        if idx == 1:
            # sessão já logada: usa. Se ficou em modo de configuração,
            # volta ao exec privilegiado (senão os shows falham).
            # (nível usuário 'ACOS>' sobe com enable no fim da função)
            if "(config" in buf.lower():
                con.sendline("end")
                _, buf = con.expect([PROMPT_RE], timeout=timeout)
        elif idx == 0:
            # prompt de login: autentica. Espera o prompt terminar de
            # "assentar" antes de digitar — o equipamento (getty em
            # ttyS0) pode estar terminando de transmitir o banner;
            # digitar em cima disso mistura o eco.
            time.sleep(0.4)
            con.sendline(self.username)
            try:
                con.expect([PASSWORD_RE, PASSWORD_FUZZY_RE], timeout=timeout)
            except ConsoleError as exc:
                raise A10Error(
                    f"login falhou em {self.port} (não veio 'Password:' — "
                    f"recebido: {exc}. Se o texto veio corrompido, confira "
                    f"o adaptador USB-serial: chips PL2303 falsificados/"
                    f"CH340 corrompem bytes intermitentemente)"
                ) from exc
            con.sendline(self.password)
            try:
                _, buf = con.expect([PROMPT_RE], timeout=timeout)
            except ConsoleError as exc:
                raise A10Error(
                    f"login falhou em {self.port} (senha/usuário errados?)"
                ) from exc
        else:
            # 'Password:' já na tela — login (ou enable) pela metade,
            # deixado por um acesso anterior: completa com a senha.
            con.sendline(self.password)
            try:
                idx, buf = con.expect([PROMPT_RE, PASSWORD_FUZZY_RE],
                                      timeout=timeout)
            except ConsoleError as exc:
                raise A10Error(
                    f"login falhou em {self.port} (senha não aceita no "
                    f"'Password:' já aberto — recebido: {exc})") from exc
            if idx == 1:
                # outro 'Password:' — era o do ENABLE (sessão em ACOS>):
                # completa com a senha de enable
                con.sendline(self.enable_password)
                _, buf = con.expect([PROMPT_RE], timeout=timeout)
        # nível de usuário ("ACOS>") -> enable
        if not buf.rstrip().endswith("#"):
            con.sendline("enable")
            idx, buf = con.expect([PASSWORD_RE, PROMPT_RE], timeout=5)
            if idx == 0:
                con.sendline(self.enable_password)
                con.expect([PROMPT_RE], timeout=timeout)
        # desliga a paginação: sem `terminal length 0` o ACOS pára no
        # ---MORE--- esperando ENTER no meio de saídas longas (show
        # version etc.) e o script nunca vê o prompt. Tolerante: se o
        # comando não existir, o console reimprime o prompt e seguimos.
        con.sendline("terminal length 0")
        con.expect([PROMPT_RE], timeout=5)
        con.drain(0.3)

    # ------------------------------------------------------------ comandos
    def cmd(self, command, timeout=30):
        """Executa comando e devolve a saída (até o prompt)."""
        con = self._console()
        con.drain(0.1)
        con.sendline(command)
        _, buf = con.expect([PROMPT_RE], timeout=timeout)
        return buf

    def cmd_confirm(self, command, answer="y", timeout=30):
        """Executa comando que pode pedir confirmação [y/n] ou [yes/no].

        Se a confirmação aparecer, envia `answer` no formato certo para
        a pergunta (y/n ou yes/no). Tolerante a queda da sessão no
        final (caso do reboot).
        """
        con = self._console()
        con.drain(0.1)
        con.sendline(command)
        idx, buf = con.expect([PROMPT_RE, CONFIRM_RE], timeout=timeout)
        if idx == 1:
            con.sendline(confirm_answer(buf, answer))
            # pós-resposta: espera curta pelo prompt (reboot derruba a
            # sessão e o prompt nunca mais vem — não dá pra esperar muito)
            try:
                _, more = con.expect([PROMPT_RE], timeout=min(timeout, 5))
                buf += more
            except ConsoleError:
                pass  # sessão caiu (reboot) — ok
        return buf

    def _console(self):
        if self.console is None:
            raise A10Error("console não aberto (chame open_and_login)")
        return self.console

    # ------------------------------------------------------------ leituras
    def wait_ready(self, timeout=600, on_wait=None, on_loading=None):
        """Aguarda a caixa SAIR do modo LOADING (inicialização pós-reset).

        O ACOS pós-reset pode: mostrar 'ACOS(LOADING)#' (respondendo
        'System is not ready yet.'), ficar mudo, ou DERRUBAR a sessão
        serial (o console volta para 'login:' / 'Password:'). Loop
        estado-dirigido: lê a tela com o MESMO conjunto de padrões
        (nunca perde o estado) e age — reloga se a sessão caiu, sobe
        para o modo privilegiado (enable) quando o login cai em
        'ACOS>', acorda com ENTER se mudo, e só retorna quando o
        prompt normal (sem LOADING) aparece.

        `on_wait(elapsed)` é chamado a cada ~10s (para progresso).
        `on_loading(elapsed)` é chamado UMA vez quando o prompt de
        LOADING é observado (prova de boot real pós-reset).
        Retorna True quando pronta; False se o timeout estourar.
        """
        con = self._console()
        deadline = time.time() + timeout
        last_report = 0
        need_enable = False  # acabamos de logar no nível usuário (ACOS>)
        term_length_sent = False  # sessão sem paginação (--MORE--)
        loading_seen = False  # on_loading dispara uma única vez
        while time.time() < deadline:
            elapsed = int(time.time() - (deadline - timeout))
            # 1) o que está na tela AGORA? (dados pendentes + 2s) —
            #    SEMPRE os 3 padrões: nada é consumido sem casar
            try:
                _, buf = con.expect([LOGIN_RE, PASSWORD_RE, PROMPT_RE],
                                    timeout=2)
            except ConsoleError:
                # mudo: acorda com ENTER (pode reimprimir prompt/senha)
                con.sendline("")
                continue
            low = buf.lower()
            if "password:" in low:
                # prompt de senha — a do LOGIN ou a do ENABLE
                con.sendline(self.enable_password if need_enable
                             else self.password)
                continue
            if "login:" in low:
                need_enable = False
                term_length_sent = False  # sessão nova: reaplica
                con.sendline(self.username)
                continue
            # chegou um prompt — nível usuário sobe para privilegiado
            if buf.rstrip().endswith(">"):
                need_enable = True
                con.sendline("enable")
                continue
            need_enable = False
            if not LOADING_RE.search(buf):
                # prompt normal — ANTES de voltar, garante a sessão sem
                # paginação: o 'terminal length 0' só funciona fora do
                # LOADING (em LOADING o ACOS responde 'not ready' e a
                # sessão volta a pagar no --MORE-- das saídas longas)
                if not term_length_sent:
                    con.sendline("terminal length 0")
                    term_length_sent = True
                    continue  # volta ao topo: confirma o prompt
                return True
            term_length_sent = False  # caiu de volta no LOADING
            if on_loading is not None and not loading_seen:
                loading_seen = True
                on_loading(elapsed)
            if on_wait and elapsed - last_report >= 10:
                last_report = elapsed
                on_wait(elapsed)
            time.sleep(1)
        return False

    def get_version(self, timeout=30):
        out = self.cmd("show version", timeout=timeout)
        ver = parse_acos_version(out)
        if not ver:
            raise A10Error(
                f"não consegui identificar a versão ACOS; saída:\n{out[-500:]}"
            )
        return ver

    def get_serial(self, timeout=30):
        """Número de série da caixa (extraído do `show version`)."""
        out = self.cmd("show version", timeout=timeout)
        serial = parse_serial_number(out)
        if not serial:
            raise A10Error(
                "serial não encontrado no show version; "
                f"saída:\n{out[-500:]}"
            )
        return serial

    def get_license_info(self, timeout=30):
        """Saída bruta do `show license-info` (para registro no portal)."""
        return self.cmd("show license-info", timeout=timeout)

    def get_environment(self, timeout=30):
        """Saída bruta do `show environment` (para registro no portal)."""
        return self.cmd("show environment", timeout=timeout)

    def get_bootimage(self, timeout=30):
        out = self.cmd("show bootimage", timeout=timeout)
        info = parse_bootimage(out)
        return info

    def get_mgmt_ip(self, timeout=30):
        """Descobre o IP da porta de gerência. Retorna (ip, prefix) ou None."""
        for command in ("show interfaces management",
                        "show running-config interface management"):
            try:
                out = self.cmd(command, timeout=timeout)
            except ConsoleError:
                continue
            m = MGMT_IP_RE.search(out)
            if m:
                ip = m.group(1)
                if m.group(2):
                    return ip, int(m.group(2))
                if m.group(3):
                    return ip, _mask_to_prefix(m.group(3))
        return None

    def get_model(self, timeout=30):
        """Identifica o modelo da caixa (ex.: 'Thunder 4430(S)').

        Usado para escolher o firmware certo (cada família de hardware usa
        uma imagem ACOS diferente, mesmo com a mesma versão base).
        """
        for command in ("show version", "show inventory"):
            try:
                out = self.cmd(command, timeout=timeout)
            except ConsoleError:
                continue
            model = parse_model(out)
            if model:
                return model
        raise A10Error(
            "não consegui identificar o modelo da caixa "
            "(show version / show inventory sem 'Thunder ...')")

    # ------------------------------------------------------------ escrita
    def set_static_mgmt(self, ip, prefix=24, gateway=None, timeout=30):
        """Configura IP estático na porta de gerência (modo configuração)."""
        self.cmd("configure terminal", timeout=timeout)
        self.cmd("interface management", timeout=timeout)
        self.cmd(f"ip address {ip} /{prefix}", timeout=timeout)
        self.cmd("exit", timeout=timeout)
        if gateway:
            self.cmd(f"ip default-gateway {gateway}", timeout=timeout)
        self.cmd("end", timeout=timeout)
        self.write_memory(timeout=timeout)

    def set_mgmt_dhcp(self, timeout=30):
        """Configura a porta de gerência para pegar IP por DHCP.

        A gerência do lab pega IP via DHCP — o upgrade usa a porta de
        gerência como origem do download (`use-mgmt-port`) e o
        equipamento só alcança o servidor SFTP com IP na interface.
        """
        self.cmd("configure terminal", timeout=timeout)
        self.cmd("interface management", timeout=timeout)
        self.cmd("ip address dhcp", timeout=timeout)
        self.cmd("exit", timeout=timeout)
        self.cmd("end", timeout=timeout)
        self.write_memory(timeout=timeout)

    def write_memory(self, timeout=30):
        self.cmd("write memory", timeout=timeout)

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

    def upgrade_hd(self, url, slot="pri", use_mgmt_port=True, timeout=1800,
                   reboot_after_upgrade=False):
        """Upgrade via CLI serial — MESMO comando do fluxo manual:
        `upgrade hd <pri|sec> use-mgmt-port <url>`.

        O equipamento PUXA a imagem pelo servidor usando a porta de
        gerência (que pode pegar IP por DHCP) — NÃO precisa saber o IP
        da caixa. O ACOS faz algumas perguntas durante o upgrade
        (salvar config? reboot agora?) — respondemos automaticamente
        (salvar = y). Na pergunta de reboot:

        - `reboot_after_upgrade=True`: responde "y" — a caixa reinicia
          SOZINHA após instalar (recomendado: evita ficar preso
          esperando o prompt voltar); retorna "rebooting".
        - `False` (padrão): responde "n" — o script controla o reboot
          (set_bootimage + write memory + reboot); retorna "ok" quando
          o prompt volta.

        Retorna "ok" (prompt voltou, caixa de pé) ou "rebooting"
        (a caixa está reiniciando — não espere o prompt).
        """
        con = self._console()
        con.drain(0.3)
        cmd = f"upgrade hd {slot}"
        if use_mgmt_port:
            cmd += " use-mgmt-port"
        cmd += f" {url}"
        con.sendline(cmd)
        # o download/instalação pode demorar minutos; o console mostra
        # progresso e pode fazer perguntas [y/n] no meio. Loop até o
        # prompt voltar (ou a caixa reiniciar sozinha).
        while True:
            idx, buf = con.expect([PROMPT_RE, CONFIRM_RE], timeout=timeout)
            if idx == 0:
                break  # prompt voltou — upgrade terminou
            # confirmação: pergunta de reboot -> y/n conforme a política;
            # demais (salvar config etc.) -> y. A resposta respeita o
            # formato da pergunta ([y/n] ou [yes/no]).
            if "reboot" in buf.lower():
                con.sendline(confirm_answer(
                    buf, "y" if reboot_after_upgrade else "n"))
                if not reboot_after_upgrade:
                    continue
                # "y": a caixa instala e reinicia sozinha. Não esperamos
                # o prompt voltar (pode demorar e travar o ciclo) —
                # aguardamos a mensagem de reboot ou a queda da sessão.
                try:
                    idx2, buf2 = con.expect([PROMPT_RE, REBOOTING_RE],
                                            timeout=15)
                except ConsoleError:
                    return "rebooting"   # sessão caiu — reiniciando
                if idx2 == 0:
                    return "ok"          # prompt voltou — não reiniciou
                return "rebooting"       # "System is rebooting..." visto
            con.sendline(confirm_answer(buf, "y"))
        low = buf.lower()
        if "unknown command" in low or "invalid" in low:
            hint = ""
            if len(cmd) > 80:
                # repro de bancada: ACOS 2.x corta linhas longas — o
                # comando era truncado no meio da URL e rejeitado com '^'
                hint = (f" — o comando tem {len(cmd)} chars e o ACOS "
                        "corta linhas longas (~80 col); encurte o "
                        "caminho/arquivo no servidor sftp (ex.: symlink "
                        "curto)")
            raise A10Error(
                f"comando de upgrade não aceito pelo ACOS: "
                f"{buf[-300:]!r}{hint}"
            )
        return "ok"

    def boot_to(self, slot, timeout=30):
        """Muda a partição de boot (configure -> bootimage hd <pri|sec>
        -> write mem) e CONFIRMA no `show bootimage` que o default mudou.

        O ACOS 2.x só aceita a forma CURTA (`pri`/`sec`) — repro de
        bancada: `bootimage hd primary` era rejeitado em silêncio (o
        cmd() engole o 'Unrecognized') e o reboot voltava na partição
        antiga. Tenta a curta, depois as longas, validando o eco.
        """
        short = {"primary": "pri", "secondary": "sec"}.get(slot, slot)
        con = self._console()
        con.drain(0.1)
        con.sendline("configure terminal")
        con.expect([PROMPT_RE], timeout=timeout)
        accepted = False
        for form in (f"bootimage hd {short}", f"bootimage hd {slot}",
                     f"bootimage {slot}"):
            con.sendline(form)
            _, buf = con.expect([PROMPT_RE], timeout=timeout)
            if "unknown command" not in buf.lower() \
                    and "invalid" not in buf.lower():
                accepted = True
                break
        self.write_memory(timeout=timeout)
        con.sendline("end")
        con.expect([PROMPT_RE], timeout=timeout)
        info = self.get_bootimage(timeout=timeout)
        if not accepted or info.get("default") != slot:
            raise A10Error(
                f"não consegui mudar o boot para {slot} — o ACOS não "
                f"aceitou os comandos bootimage ou o default não mudou "
                f"(bootimage atual: {info})")
        return info

    def set_bootimage(self, slot, timeout=15):
        """Marca o slot (primary|secondary) para o próximo boot.

        Tenta as formas aceitas pelo ACOS até uma não retornar erro —
        CURTAS primeiro (`bootimage pri`/`bootimage hd pri`): o 2.x só
        aceita `pri`/`sec`, e o SO continua 2.x até o reboot seguinte
        (repro de bancada: forma longa rejeitada em silêncio).
        """
        con = self._console()
        names = {"pri": "primary", "sec": "secondary"}
        name = names.get(slot, slot)
        short = {"primary": "pri", "secondary": "sec"}.get(name, name)
        last = None
        for cmd in (f"bootimage {short}", f"bootimage hd {short}",
                    f"bootimage {name}", f"bootimage hd {name}",
                    f"bootimage disk {name}"):
            try:
                con.drain(0.1)
                con.sendline(cmd)
                _, buf = con.expect([PROMPT_RE], timeout=timeout)
                low = buf.lower()
                if "unknown command" in low or "invalid" in low:
                    last = buf[-200:]
                    continue
                return buf
            except ConsoleError as exc:
                last = exc
                continue
        raise A10Error(f"não consegui definir bootimage {slot}: {last}")

    def erase_config(self, timeout=60):
        """Factory reset via `erase`: apaga startup-config; volta ao padrão
        no próximo reboot."""
        self.cmd_confirm("erase", "y", timeout=timeout)

    def system_reset(self, timeout=60):
        """Factory reset via `system-reset` (preserva a licença).

        Se o equipamento não reiniciar sozinho, força um reboot.
        """
        con = self._console()
        con.drain(0.1)
        con.sendline("system-reset")
        idx, buf = con.expect([PROMPT_RE, CONFIRM_RE], timeout=timeout)
        if idx == 1:
            con.sendline(confirm_answer(buf, "y"))
            try:
                _, more = con.expect([PROMPT_RE], timeout=timeout)
                buf += more
                # continuou de pé -> reboot para aplicar o reset
                self.reboot(timeout=timeout)
            except ConsoleError:
                pass  # sessão caiu (reset aplicado + reboot automático)
        self.close()

    def reboot(self, timeout=60):
        """Reboota o equipamento. A sessão serial cai — não espere prompt."""
        self.cmd_confirm("reboot", "y", timeout=timeout)
        self.close()
