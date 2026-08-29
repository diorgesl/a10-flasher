"""Simulador de console serial A10 (ACOS) sobre um pty real.

Permite testar toda a camada serial e a máquina de estados sem hardware:
o worker abre o caminho do pty como se fosse /dev/ttyUSB0.
"""

import os
import pty
import threading
import time
import tty


class FakeA10:
    def __init__(self, version="4.1.4", secondary="4.0.0", booted="primary",
                 mgmt_ip="10.0.0.10", prefix=24,
                 login_user="admin", login_pass="a10",
                 reboot_delay=1.0, needs_enter=False,
                 model="Thunder 4430(S)", start_logged_in=False,
                 serial="A10TH-TEST-0001", ask_reboot=False,
                 confirm_style="yn", loading_seconds=0,
                 drop_session_once=False, drop_to="login",
                 start_at_password=False, uptime_s=7380,
                 uptime_format="up_time", reboot_pending_delay=0.0):
        master, slave = pty.openpty()
        self._master = master
        self._slave = slave   # guardado para simular desconexão (unplug)
        os.set_blocking(master, False)   # leitura não-bloqueante (polling)
        # raw mode no slave: sem echo, sem bufferização de linha.
        # Sem isso o pty ecoa o banner de volta pro master (o fake lê o
        # próprio eco) e o texto fica preso até o slave ser aberto.
        tty.setraw(slave)
        self.port = os.ttyname(slave)
        self.login_user = login_user
        self.login_pass = login_pass
        self.versions = {"primary": version, "secondary": secondary}
        self.booted = booted
        self.mgmt_ip = mgmt_ip
        self.prefix = prefix
        self.model = model
        self.serial = serial
        self.ask_reboot = ask_reboot          # upgrade hd pergunta reboot?
        self.upgrade_reboot_answered = None   # "y"|"n" na pergunta do upgrade
        self.confirm_style = confirm_style    # "yn" | "yesno" (como o ACOS real)
        self.loading_seconds = loading_seconds  # fica em LOADING após reboot
        self.drop_session_once = drop_session_once  # derruba a sessão 1x no LOADING
        self.drop_to = drop_to  # "login" | "password" (para onde cai)
        # a caixa "acabou de bootar" na criação — inicia em LOADING
        self._loading_until = (time.time() + loading_seconds
                               if loading_seconds > 0 else 0.0)
        self._drop_done = False
        self.next_versions = {}     # aplicado no próximo boot
        self.reboot_delay = reboot_delay
        self.needs_enter = needs_enter   # console "dormente" (só responde a ENTER)
        self.start_logged_in = start_logged_in  # sessão órfã já ativa
        # login PELA METADE: usuário digitado, tela parada em 'Password:'
        self.start_at_password = start_at_password
        # uptime reportado no show version (modo teste)
        self.uptime_s = uptime_s
        # formato da linha de uptime: "up_time" (padrão ACOS), "system_up"
        # (TH3030S: 'The system has been up ...') ou "none" (sem linha)
        self.uptime_format = uptime_format
        self._booted_at = time.time()   # uptime zera a cada reboot
        # reboot ATRASADO (visto em bancada: o erase demora dezenas de
        # segundos até derrubar o console) — durante a espera a sessão
        # antiga continua VIVA e respondendo, como o hardware real
        self.reboot_pending_delay = reboot_pending_delay
        self._reboot_at = None
        self.commands = []
        self.interfaces_count = 20      # portas ethernet 1..N no brief
        self.bad_config_lines = set()   # substrings -> '% Invalid input'
        self._ctx = "priv"          # priv | config | if
        if start_logged_in:
            self._state = "priv"
        elif start_at_password:
            self._state = "login_pass"
        else:
            self._state = "sleep" if needs_enter else "login_user"
        self._pending = b""
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ I/O
    def _send(self, text):
        try:
            os.write(self._master, text.encode())
        except OSError:
            pass

    def _readline(self, timeout=1.0):
        """Lê até um \r (comando completo).

        Retorna None se nada chegou; b'' se chegou uma linha vazia.
        Dados parciais ficam pendentes para a próxima chamada.
        """
        buf = self._pending
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop:
            try:
                data = os.read(self._master, 4096)
            except BlockingIOError:
                # master non-blocking sem dados: espera um pouco e continua
                # (sem isso o loop de "nada chegou" gira sem freio e o
                # banner de login é reenviado milhares de vezes/segundo,
                # enchendo o buffer do pty e engolindo respostas reais)
                time.sleep(0.05)
                continue
            except OSError:
                return None
            if not data:
                time.sleep(0.05)
                continue
            buf += data
            if b"\r" in buf:
                line, _, rest = buf.partition(b"\r")
                self._pending = rest
                return line
        self._pending = buf
        return None

    def unplug(self):
        """Simula desconectar a caixa: fecha o slave — o caminho da
        porta (ttyname) some, como no hotplug real."""
        try:
            os.close(self._slave)
        except OSError:
            pass

    def start(self):
        """Garante o thread do console rodando (no-op: o __init__ já
        inicia; a chamada explícita é por compatibilidade com os testes)."""
        if not self._thread.is_alive() and not self._stop:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def close(self):
        self._stop = True
        try:
            os.close(self._master)
        except OSError:
            pass
        self.unplug()
        self._thread.join(timeout=5)

    # ------------------------------------------------------- respostas
    def _prompt(self):
        if self._loading():
            return "ACOS(LOADING)# "   # inicialização pós-reset
        return {"priv": "ACOS# ", "config": "ACOS(config)# ",
                "if": "ACOS(config-if:management)# "}[self._ctx]

    def _conf(self):
        """Sufixo da pergunta de confirmação: [y/n] ou [yes/no]."""
        return "yes/no" if self.confirm_style == "yesno" else "y/n"

    @staticmethod
    def _is_yes(line):
        return line.strip().lower() in ("y", "yes")

    @staticmethod
    def _is_no(line):
        return line.strip().lower() in ("n", "no")

    def _uptime_line(self):
        s = self.uptime_s + int(time.time() - self._booted_at)
        d, s = divmod(s, 86400)
        h, s = divmod(s, 3600)
        m = s // 60
        if self.uptime_format == "system_up":
            # formato do TH3030S (sem 'Up Time:')
            return (f"The system has been up {d} day, {h} hours, "
                    f"{m} minutes\r\n")
        if self.uptime_format == "none":
            return ""   # caixa sem linha de uptime no show version
        return f"Up Time: {d}d {h}h {m}m (Active)\r\n"

    def _version_block(self):
        ver = self.versions[self.booted]
        return (f"\r\nACOS version {ver}\r\n"
                f"Copyright 2004-2021 A10 Networks, Inc.\r\n"
                f"Platform: {self.model}\r\n"
                f"Serial Number: {self.serial}\r\n"
                f"Current Time: TEST\r\n"
                f"{self._uptime_line()}\r\n")

    def _license_block(self):
        return ("\r\nLicense Information\r\n"
                "  Serial Number: " + self.serial + "\r\n"
                "  License Type: STANDARD\r\n"
                "  Licensed Bandwidth: 1 Gbps\r\n\r\n")

    def _environment_block(self):
        return ("\r\nEnvironment Status\r\n"
                "  Temperature: 35 C (OK)\r\n"
                "  Fan 1: OK\r\n"
                "  Fan 2: OK\r\n"
                "  Power Supply 1: OK\r\n\r\n")

    def _bootimage_block(self):
        pri = self.versions["primary"]
        sec = self.versions["secondary"]
        pstar = " (*)" if self.booted == "primary" else ""
        sstar = " (*)" if self.booted == "secondary" else ""
        return (f"\r\n                       (* = Default)\r\n"
                f"                           Version\r\n"
                f" -----------------------------------------------\r\n"
                f" Hard Disk primary         {pri}{pstar}\r\n"
                f" Hard Disk secondary       {sec}{sstar}\r\n\r\n")

    def _mgmt_block(self):
        return (f"\r\nInterface Management\r\n"
                f"  Description: Management port\r\n"
                f"  IP Address: {self.mgmt_ip} /{self.prefix}\r\n"
                f"  Link: up\r\n\r\n")

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

    def _do_reboot(self):
        self._send("\r\nSystem is rebooting...\r\n")
        time.sleep(self.reboot_delay)
        for slot, ver in (self.next_versions or {}).items():
            self.versions[slot] = ver
        self.uptime_s = 0               # uptime zera no boot (como o real)
        self._booted_at = time.time()
        self._ctx = "priv"
        # pós-reboot a caixa inicia em modo LOADING (sistema subindo)
        if self.loading_seconds > 0:
            self._loading_until = time.time() + self.loading_seconds
        self._drop_done = False  # pode derrubar a sessão de novo
        # após o reboot o console volta ao estado inicial (dormente, se o
        # hardware real dorme até receber ENTER)
        self._state = "sleep" if self.needs_enter else "login_user"
        if not self.needs_enter:
            self._send("\r\nACOS login: ")

    def _loading(self):
        return time.time() < self._loading_until

    def _start_reboot(self):
        """Confirmação de reboot: imediato, ou ATRASADO (reboot_pending_delay)
        — durante a espera a sessão antiga segue respondendo, como o ACOS
        real com o erase demorando a derrubar o console."""
        if self.reboot_pending_delay > 0:
            self._reboot_at = time.time() + self.reboot_pending_delay
            self._state = "priv"
            self._send("\r\n" + self._prompt())
        else:
            self._do_reboot()

    # ------------------------------------------------------- máquina
    def _run(self):
        time.sleep(0.2)
        if self.start_logged_in:
            # sessão órfã: o console já está logado (prompt ACOS#)
            self._send("\r\nACOS# ")
        elif self.start_at_password:
            # login pela metade: tela parada em 'Password:'
            self._send("\r\nPassword: ")
        elif not self.needs_enter:
            self._send("\r\nACOS login: ")
        while not self._stop:
            raw = self._readline()
            if raw is None:
                # banner de login pode ter sido perdido (disco de linha do
                # pty antes do leitor abrir) — re-envia até interagirmos;
                # consoles "dormentes" (needs_enter) só enviam após ENTER
                if self._state == "login_user" and not self.needs_enter:
                    self._send("\r\nACOS login: ")
                continue
            line = raw.decode(errors="replace").strip()
            state = self._state
            if not line:
                # linha vazia = Enter sem senha (enable sem senha é o
                # padrão de fábrica do A10); no prompt de sessão ativa,
                # reimprime o prompt (como o equipamento real)
                if state == "enable_pass":
                    # em LOADING o prompt é 'ACOS(LOADING)#' — como o
                    # real mostra após o login durante a inicialização
                    self._send("\r\n" + self._prompt())
                    self._ctx = "priv"
                    self._state = "priv"
                elif state == "sleep":
                    # primeiro ENTER acorda o console (como no hardware real)
                    self._state = "login_user"
                    self._send("\r\nACOS login: ")
                elif state == "login_pass":
                    # ENTER na tela de senha: reimprime 'Password:' (como
                    # o getty real) — é assim que o script detecta o
                    # login pela metade no 1º acesso
                    self._send("\r\nPassword: ")
                elif state == "priv":
                    if (self._loading() and self.drop_session_once
                            and not self._drop_done):
                        # o ACOS real DERRUBA a sessão serial durante o
                        # LOADING pós-reset (o console volta para o
                        # login ou a tela de senha) — simula 1x,
                        # disparado pelo ENTER do wait_ready
                        self._drop_done = True
                        if self.drop_to == "password":
                            self._state = "login_pass"
                            self._send("\r\nPassword: ")
                        else:
                            self._state = "login_user"
                            self._send("\r\nACOS login: ")
                        continue
                    self._send("\r\n" + self._prompt())
                continue
            self.commands.append(line)

            # reboot agendado (erase demorando): no momento do reboot a
            # linha em voo é engolida, como no hardware real
            if self._reboot_at and time.time() >= self._reboot_at:
                self._reboot_at = None
                self._do_reboot()
                continue

            if state == "login_user":
                if line == self.login_user:
                    self._send("Password: ")
                    self._state = "login_pass"
                else:
                    self._send("\r\nLogin incorrect\r\nACOS login: ")
            elif state == "login_pass":
                if line == self.login_pass:
                    self._send("\r\nACOS> ")
                    self._state = "user"
                else:
                    self._send("\r\nLogin incorrect\r\nACOS login: ")
                    self._state = "login_user"
            elif state == "user":
                if line == "enable":
                    self._send("Password: ")
                    self._state = "enable_pass"
                else:
                    self._send(self._prompt())
            elif state == "enable_pass":
                # em LOADING o prompt é 'ACOS(LOADING)#' (privilegiado
                # do modo LOADING) — como o real mostra após login
                self._send("\r\n" + self._prompt())
                self._ctx = "priv"
                self._state = "priv"
            elif state == "confirm_erase":
                if self._is_yes(line):
                    self._send("\r\nStartup-config erased.\r\n" + self._prompt())
                self._state = "priv"
            elif state == "confirm_reboot":
                if self._is_yes(line):
                    self._start_reboot()
            elif state == "confirm_upgrade_reboot":
                self.upgrade_reboot_answered = line
                if self._is_yes(line):
                    self._start_reboot()        # caixa reinicia sozinha
                else:
                    self._send("\r\n" + self._prompt())
                    self._state = "priv"
            elif state == "confirm_reset":
                if self._is_yes(line):
                    self._start_reboot()
            elif state == "priv":
                self._handle_priv(line)

    def _handle_priv(self, line):
        # reboot ATRASADO: durante a janela de espera o console responde
        # normalmente (sessão antiga viva), mas o show version recebe a
        # tela de boot como resposta — visto em bancada: o prompt nunca
        # volta e o cmd estoura no timeout com o banner de boot
        if self._reboot_at is not None and line.startswith("show version"):
            self._reboot_at = None
            self._do_reboot()
            return
        if self._loading():
            # sistema ainda subindo: comandos não funcionam (como o real)
            if line.startswith("show"):
                self._send("System is not ready yet.\r\n" + self._prompt())
            else:
                self._send("\r\n" + self._prompt())
        elif line in ("terminal length 0", "terminal pager 0"):
            # desliga a paginação (---MORE---) — sem isso o console fica
            # esperando ENTER no meio de saídas longas
            self._send("\r\n" + self._prompt())
        elif line == "show version":
            self._send(self._version_block() + self._prompt())
        elif line == "show license-info":
            self._send(self._license_block() + self._prompt())
        elif line == "show environment":
            self._send(self._environment_block() + self._prompt())
        elif line == "show bootimage":
            self._send(self._bootimage_block() + self._prompt())
        elif line in ("show interfaces management",
                      "show running-config interface management"):
            self._send(self._mgmt_block() + self._prompt())
        elif line == "show interfaces brief":
            self._send(self._mgmt_block() + self._interfaces_brief()
                       + self._prompt())
        elif line == "write memory":
            self._send("\r\nConfiguration saved.\r\n" + self._prompt())
        elif line == "configure terminal":
            self._ctx = "config"
            self._send("\r\n" + self._prompt())
        elif line == "interface management":
            self._ctx = "if"
            self._send("\r\n" + self._prompt())
        elif self._ctx in ("config", "if") and self._reject_bad_config(line):
            pass
        elif line.startswith("ip address "):
            parts = line.split()
            # ip address 10.1.2.3 /24
            if len(parts) >= 3 and parts[2].lower() == "dhcp":
                # DHCP: a gerência pega um IP (simula a renovação)
                self.mgmt_ip = "10.0.0.50"
            else:
                self.mgmt_ip = parts[2]
                if len(parts) >= 4:
                    self.prefix = int(parts[3].lstrip("/"))
            self._send("\r\n" + self._prompt())
        elif line == "ip default-gateway":
            self._send("\r\n" + self._prompt())
        elif line == "exit":
            if self._ctx == "priv":
                # exit no prompt principal = logout da sessão (como no ACOS)
                self._state = "login_user"
                self._send("\r\nACOS login: ")
            else:
                self._ctx = "config" if self._ctx == "if" else "priv"
                self._send("\r\n" + self._prompt())
        elif line == "end":
            self._ctx = "priv"
            self._send("\r\n" + self._prompt())
        elif line.startswith("upgrade hd"):
            # upgrade via CLI serial: simula download + instalação
            # (progresso no console). A versão nova vai para o slot que
            # RECEBEU o upgrade (não-bootado). Com `ask_reboot`, o ACOS
            # pergunta se quer reiniciar após instalar (como no real).
            # ACOS 2.x: corta linhas > 80 col (visto em bancada — o
            # comando era truncado no meio da URL e rejeitado com ^).
            if self.versions[self.booted].startswith("2.") and len(line) > 80:
                self._send("\r\nUnknown command\r\n" + self._prompt())
                return
            parts = line.split()
            slot = parts[2] if len(parts) > 2 else "pri"
            full = {"pri": "primary", "sec": "secondary"}.get(slot, slot)
            # versão nova só "roda" após o REBOOT (como no real) — o
            # _do_reboot aplica o next_versions; o SO continua o antigo
            # até lá (importa para o 2.x, que rejeita comandos novos)
            self._send("\r\nDownloading... 100%\r\n")
            self._send("Installing image...\r\n")
            if self.ask_reboot:
                self._send("Do you want to reboot after the upgrade? "
                           f"[{self._conf()}] ")
                self._state = "confirm_upgrade_reboot"
            else:
                self._send("\r\n" + self._prompt())
        elif line.startswith("bootimage"):
            # marca o slot para o próximo boot (bootimage primary / hd primary)
            parts = line.split()
            slot = parts[-1] if parts else ""
            short = {"pri": "primary", "sec": "secondary"}
            if slot in short:
                # forma curta (pri/sec) — a única que o ACOS 2.x aceita
                self.booted = short[slot]
                self._send("\r\n" + self._prompt())
            elif slot in ("primary", "secondary") \
                    and self.versions[self.booted].startswith("2."):
                # repro de bancada: 2.x rejeita a forma longa
                self._send("\r\nUnknown command\r\n" + self._prompt())
            elif slot in ("primary", "secondary"):
                self.booted = slot
                self._send("\r\n" + self._prompt())
            else:
                self._send("\r\nUnknown command\r\n" + self._prompt())
        elif line == "erase":
            self._send("Do you want to erase the startup-config? "
                       f"[{self._conf()}] ")
            self._state = "confirm_erase"
        elif line == "reboot":
            self._send(f"Do you want to reboot? [{self._conf()}] ")
            self._state = "confirm_reboot"
        elif line == "system-reset":
            self._send("System reset will restart the device. Continue? "
                       f"[{self._conf()}] ")
            self._state = "confirm_reset"
        elif line in ("quit", "exit session", "exit"):
            # logout da sessão: o getty volta a mostrar o login
            self._state = "login_user"
            self._ctx = "priv"
            self._send("\r\nACOS login: ")
        else:
            self._send("\r\nUnknown command\r\n" + self._prompt())
