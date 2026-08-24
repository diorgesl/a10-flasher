"""Console serial com expect minimalista (baseado em pyserial).

Sem dependência de pexpect: fazemos um loop de leitura com regex contra o
buffer acumulado, o que é suficiente e previsível para consoles de rede.
"""

import os
import re
import select
import termios
import time
import tty

import serial


class ConsoleError(Exception):
    """Erro de comunicação com o console (timeout, porta, etc)."""


class SerialConsole:
    """Abre a porta serial e expõe send/expect no estilo de um terminal."""

    def __init__(self, port, baudrate=115200, read_timeout=0.3,
                 write_timeout=2.0):
        self.port = port
        self.rx_bytes = 0      # bytes recebidos desde a abertura (autodetect)
        try:
            self.ser = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=read_timeout,
                write_timeout=write_timeout,
                xonxoff=False,
                rtscts=False,
            )
        except serial.SerialException as exc:
            raise ConsoleError(f"não consegui abrir {port}: {exc}") from exc
        # RAW MODE no tty: sem ECHO, sem bufferização de linha, sem
        # tradução de caracteres — igual ao que o screen faz (cfmakeraw).
        # Sem isso o kernel ecoa o que enviamos e segura a leitura até um
        # \n, e o console parece "lixo" (baudrate certo lido como lixo).
        try:
            tty.setraw(self.ser.fileno())
        except (OSError, ValueError):
            pass  # fd sem termios — tolerado
        # RAW + modo "polling" (VMIN=0/VTIME=0): com o VMIN=1 do raw o
        # read bloqueia até chegar 1 byte — e o O_NONBLOCK do fd NÃO vale
        # em tty (o termios manda). Num select espúrio (pty no macOS,
        # adaptadores ruins) o read ficaria preso além do timeout do
        # expect — o wake do login travou ~195s por isso. VMIN=0/VTIME=0
        # faz o read voltar na hora (vazio se nada chegou); quem dá o
        # passo de polling é o _read_some (select+read com deadline).
        try:
            attr = termios.tcgetattr(self.ser.fileno())
            attr[6][termios.VMIN] = 0
            attr[6][termios.VTIME] = 0
            termios.tcsetattr(self.ser.fileno(), termios.TCSANOW, attr)
        except (OSError, ValueError, termios.error):
            pass  # fd sem termios — tolerado
        # Modo não-bloqueante no fd: seguro extra para fds que NÃO são
        # tty (onde O_NONBLOCK vale); em tty quem manda é o VMIN acima.
        try:
            os.set_blocking(self.ser.fileno(), False)
        except (OSError, ValueError):
            pass  # fd sem fcntl — tolerado
        # DTR ativo: muitos consoles (incl. A10) só enviam o banner de
        # login quando detectam o terminal (DTR subiu). O screen deixa
        # DTR ativo — precisamos do mesmo. Em pty (testes) não há
        # linhas de controle — falha é tolerada.
        try:
            self.ser.dtr = True
        except (OSError, ValueError):
            pass
        # NÃO resetar o buffer de entrada aqui! O banner "login:" chega
        # logo após a abertura (DTR ativo) e seria descartado — o A10 não
        # reimprime o prompt para ENTER vazio, então perder o banner
        # inicial = timeout no login.

    # ---------------------------------------------------------- básicos
    def send(self, text):
        try:
            # SEM flush(): o flush do pyserial é tcdrain — BLOQUEIA até o
            # outro lado LER a saída (console mudo/caixa dormindo = wake e
            # login travados por minutos, atravessando os timeouts). O
            # write já é limitado pelo write_timeout se o buffer encher;
            # a ordem dos bytes não depende de drain.
            self.ser.write(text.encode("utf-8", "replace"))
        except serial.SerialException as exc:
            raise ConsoleError(
                f"falha ao enviar para {self.port}: {exc}") from exc

    def sendline(self, text=""):
        self.send(text + "\r")

    def drain(self, seconds=0.2):
        """Consome lixo de entrada (banner de boot etc.) sem casar padrão."""
        end = time.time() + seconds
        while time.time() < end:
            self._read_some(0.02)

    def _read_some(self, timeout):
        """Lê o que estiver disponível sem bloquear além de `timeout`.

        select + fd não-bloqueante: imune a select espúrio (pty/macOS,
        adaptadores ruins) e nunca passa do prazo — o deadline do expect
        é respeitado de verdade. Retorna bytes (b'' se nada chegou).
        """
        try:
            ready, _, _ = select.select([self.ser.fileno()], [], [], timeout)
        except (OSError, ValueError):
            raise ConsoleError(f"falha ao ler de {self.port}: porta fechada")
        if not ready:
            return b""
        try:
            return os.read(self.ser.fileno(), 4096)
        except BlockingIOError:
            return b""
        except OSError as exc:
            raise ConsoleError(
                f"falha ao ler de {self.port}: {exc}") from exc

    def close(self):
        try:
            # pyserial's close() faz flush() -> tcdrain, que BLOQUEIA até
            # o par ler a saída (console mudo = trava). Fecha o fd direto
            # (o SO descarta o buffer sem travar); is_open=False faz o
            # close()/__del__ do pyserial virarem no-op (sem tcdrain e sem
            # os.close duplo no GC).
            os.close(self.ser.fileno())
        except Exception:
            pass
        try:
            self.ser.is_open = False
        except Exception:
            pass
        try:
            self.ser.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------- expect
    def expect(self, patterns, timeout=10.0):
        """Aguarda até que um dos regex case no buffer de entrada.

        patterns: lista de strings regex (search).
        Retorna (índice do padrão casado, texto acumulado desde a chamada).
        Levanta ConsoleError em timeout.
        """
        rxs = [re.compile(p) for p in patterns]
        buf = ""
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise ConsoleError(
                    f"timeout aguardando {patterns!r} em {self.port}; "
                    f"recebido: {buf[-300:]!r}"
                )
            # read com budget limitado ao que resta do deadline — nunca
            # bloqueia além dele (ver _read_some)
            chunk = self._read_some(min(0.3, remaining))
            if chunk:
                self.rx_bytes += len(chunk)
                buf += chunk.decode("utf-8", "replace")
                for i, rx in enumerate(rxs):
                    if rx.search(buf):
                        return i, buf
            else:
                time.sleep(0.05)


# Prompt do ACOS: "ACOS>", "ACOS#", "ACOS(config)#", "ACOS(config-if:management)#"
# (com hostname se o equipamento foi renomeado). Ancorado no FIM do buffer.
PROMPT_RE = r"[\w.\-]+(?:\([^)]*\))?[>#][ \t]*(?:\r?\n)*\Z"
LOGIN_RE = r"[Ll]ogin\s*:"
PASSWORD_RE = r"[Pp]assword\s*:"
# Tolerante a corrupção intermitente do adaptador USB-serial: casa
# "Password:" mesmo com bytes-lixo (U+FFFD/�) entre as letras — ex.:
# "P���wor�:" de um adaptador com clock instável. Nunca casa falso
# positivo relevante: exige P + wor + : na ordem, com pouco lixo entre.
PASSWORD_FUZZY_RE = r"[Pp][^P]{0,4}wor[^:]{0,3}\s*:"
# Confirmação do ACOS: "[y/n]" OU "[yes/no]" (o ACOS usa yes/no em
# perguntas como "Proceed with reboot? [yes/no]:"). Ancorado no fim.
CONFIRM_RE = r"[\[]?(?:[Yy]/[Nn]|[Yy][Ee][Ss]/[Nn][Oo])[\])]?\s*[:?]?\s*\Z"


def confirm_answer(buf, answer):
    """Ajusta a resposta à pergunta de confirmação do ACOS.

    O ACOS pergunta tanto "[y/n]" quanto "[yes/no]" (ex.: reboot) —
    envia a resposta completa correspondente ("y"/"n" ou "yes"/"no")
    para não depender de o equipamento aceitar abreviação.
    """
    low = (buf or "").lower()
    yesno = "yes" in low and "no" in low
    if str(answer).strip().lower().startswith("n"):
        return "no" if yesno else "n"
    return "yes" if yesno else "y"
