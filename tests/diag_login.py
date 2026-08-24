"""Diagnóstico do console serial A10 — mostra EXATAMENTE o que o
equipamento envia, em tempo real (hex + ascii).

Uso:
    .venv/bin/python tests/diag_login.py /dev/ttyUSB0 [baudrate]
    .venv/bin/python tests/diag_login.py /dev/ttyUSB0 9600 --auto
    .venv/bin/python tests/diag_login.py /dev/ttyUSB0 --scan

--scan: testa TODOS os baudrates comuns (9600, 115200, 57600, 38400,
19200, 4800) e aponta qual fala texto legível — o baudrate real do
equipamento. Rode e mande a saída (leva ~1 min).

--auto: reproduz o fluxo automático do script (espera passiva + ENTERs),
imprimindo tudo que o equipamento responder. Rode, espere ~30s e mande
a saída.

Modo interativo (sem --auto):
    - Tudo que o equipamento ENVIA aparece com timestamp (repr + hex).
    - O que você digita no terminal vai para a serial (como o screen).
    - Ctrl+C (ou 'q') para sair.
"""

import os
import sys
import time
import threading
import tty

import serial


def hexdump(data):
    return " ".join(f"{b:02x}" for b in data)


def safe_read(ser):
    """Leitura tolerante: em SerialException (porta em uso por outro
    processo / driver fantasma) retorna b'' em vez de crashar."""
    try:
        return ser.read(4096)
    except serial.SerialException as exc:
        print(f"!! leitura falhou (porta em uso?): {exc}")
        time.sleep(0.5)
        return b""


def print_rx(data):
    now = time.strftime("%H:%M:%S")
    print(f"[{now}] RX {data!r}")
    print(f"[{now}]    hex: {hexdump(data)}")


def auto_mode(ser):
    """Fluxo igual ao script corrigido: espera passiva (DTR ativo) +
    ENTERs só se mudo. NÃO descarta nada."""
    print("== --auto: espera passiva 5s + (se mudo) 3 ENTERs + espera 25s ==")
    end = time.time() + 5
    while time.time() < end:
        data = safe_read(ser)
        if data:
            print_rx(data)
    for i in range(3):
        print(f"-- ENTER #{i + 1} --")
        ser.write(b"\r")
        ser.flush()
        end = time.time() + 4
        while time.time() < end:
            data = safe_read(ser)
            if data:
                print_rx(data)
    end = time.time() + 20
    while time.time() < end:
        data = safe_read(ser)
        if data:
            print_rx(data)
    print("== fim do modo --auto ==")


def score_text(data):
    """Proporção de bytes ASCII imprimíveis (0x20-0x7e, \r, \n) — quanto
    maior, mais parece um console de texto (baudrate certo)."""
    if not data:
        return 0.0
    ok = sum(1 for b in data if 0x20 <= b <= 0x7e or b in (0x0a, 0x0d, 0x09))
    return ok / len(data)


def scan_bauds(ser_factory, bauds, port):
    """Testa cada baudrate: espera passiva + ENTERs, mede legibilidade.
    Mostra o melhor candidato (texto legível = baudrate do equipamento)."""
    results = []
    for baud in bauds:
        print(f"\n=== testando {baud} ===")
        ser = ser_factory(baud)
        collected = b""
        end = time.time() + 4
        while time.time() < end:
            data = safe_read(ser)
            if data:
                collected += data
                print_rx(data)
        for i in range(2):
            ser.write(b"\r")
            ser.flush()
            end = time.time() + 3
            while time.time() < end:
                data = safe_read(ser)
                if data:
                    collected += data
                    print_rx(data)
        ser.close()
        score = score_text(collected)
        results.append((score, baud, collected))
        print(f"--- {baud}: {len(collected)} bytes, legibilidade {score:.0%}")
    results.sort(reverse=True)
    score, baud, data = results[0]
    print("\n" + "=" * 50)
    print(f"MELHOR CANDIDATO: {baud} (legibilidade {score:.0%})")
    if score >= 0.7:
        print("-> parece um console de texto: é o baudrate do equipamento")
    else:
        print("-> ainda parece lixo: confira cabo/adaptação ou outro baudrate")
    print("=" * 50)
    return baud


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    port = sys.argv[1]
    baud = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 9600
    auto = "--auto" in sys.argv
    scan = "--scan" in sys.argv

    def ser_factory(b):
        s = serial.Serial(
            port=port, baudrate=b, bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            timeout=0.2, write_timeout=2.0, xonxoff=False, rtscts=False,
        )
        try:
            tty.setraw(s.fileno())
        except (OSError, ValueError):
            pass
        try:
            s.dtr = True
        except (OSError, ValueError):
            pass
        return s

    if scan:
        print(f"== scan de baudrates em {port} ==")
        scan_bauds(ser_factory,
                   [9600, 19200, 38400, 57600, 115200, 4800, 2400,
                    14400, 28800, 230400],
                   port)
        return

    if "--login" in sys.argv:
        # Login real usando o MESMO código do a10-flasher (SerialA10).
        # Seguro: NÃO faz upgrade nem reset — só login + show version.
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from a10flash.a10_cli import SerialA10  # noqa: E402

        user = os.environ.get("A10_USER", "admin")
        pwd = os.environ.get("A10_PASS", "a10")
        print(f"== login em {port} ({baud}) user={user} ==")
        cli = SerialA10(port, baudrate=baud, username=user, password=pwd)
        try:
            # instrumenta o envio (mostra o que o script manda, p/ comparar)
            from a10flash.serial_console import SerialConsole

            _orig_send = SerialConsole.send

            def _logged_send(self, text):
                data = text.encode("utf-8", "replace")
                print(f"  TX {text!r} hex: "
                      + " ".join(f"{b:02x}" for b in data))
                return _orig_send(self, text)

            SerialConsole.send = _logged_send
            try:
                cli.open_and_login(login_timeout=25, baud_autodetect=True,
                                   wake_enters=3, wake_delay=0.4)
            finally:
                SerialConsole.send = _orig_send
            print("✅ LOGIN OK")
            print("versão:", cli.get_version())
            try:
                print("modelo:", cli.get_model())
            except Exception as exc:
                print("(modelo não identificado:", exc, ")")
            # mostra o show version CRU para diagnosticar o parse_model
            try:
                raw = cli.cmd("show version")
                print("--- show version cru ---")
                print(raw[-1200:])
                print("--- fim ---")
            except Exception as exc:
                print("(show version cru falhou:", exc, ")")
            print("bootimage:", cli.get_bootimage())
        except Exception as exc:
            print(f"❌ FALHOU: {exc}")
            sys.exit(1)
        finally:
            cli.close()
        return

    ser = ser_factory(baud)
    print(f"== porta {port} @ {baud} 8N1 ==")

    if auto:
        auto_mode(ser)
        ser.close()
        return

    print("== tecle para enviar (Enter envia '\\r'); Ctrl+C ou 'q' sai ==")

    stop = threading.Event()

    def reader():
        buf = b""
        while not stop.is_set():
            data = safe_read(ser)
            if not data:
                continue
            now = time.strftime("%H:%M:%S")
            buf += data
            while b"\n" in buf or b"\r" in buf:
                line, _, buf = buf.partition(b"\n")
                line = line.rstrip(b"\r")
                print(f"[{now}] RX {line!r}")
                print(f"[{now}]    hex: {hexdump(line)}")
            if len(buf) > 1024:
                print(f"[{now}] RX(parcial) {buf!r}")
                buf = b""

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    try:
        while True:
            ch = sys.stdin.read(1)
            if ch in ("q", "\x03"):
                break
            if ch == "\n":
                ser.write(b"\r")
                ser.flush()
            else:
                ser.write(ch.encode())
                ser.flush()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        ser.close()
        print("\n== fim ==")


if __name__ == "__main__":
    main()
