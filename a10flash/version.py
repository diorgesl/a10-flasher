"""Parsing e comparação de versões ACOS.

Versões típicas: "4.1.4", "4.1.4-P2", "5.2.1-P3", "6.0.0",
"4.1.4-GR1-P14" (padrão de release A10: grupo de release + patch).
A comparação usa (major, minor, patch, build) — o build é o ÚLTIMO número
do sufixo ("4.1.4-GR1-P14" > "4.1.4-GR1-P2" > "4.1.4").

Também extrai o MODELO do equipamento ("Thunder 4430(S)", "vThunder"...)
para selecionar o firmware correto por família de hardware.
"""

import re

_ACOS_VERSION_RE = re.compile(
    r"ACOS\s+version\s+([0-9]+(?:\.[0-9]+){1,3}(?:[-.][A-Za-z0-9]+)*)", re.IGNORECASE
)
# fallback: "version 4.1.4" / "Version: 4.1.4-P2" em qualquer linha
_LOOSE_VERSION_RE = re.compile(
    r"(?im)(?:^|\n)[^\n]*version[^\n:]*[:]?\s*([0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9]+)*)"
)
_CORE_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

# Modelo — dois formatos:
#   antigo: "Thunder 4430(S)", "Thunder 930", "vThunder"
#   novo:   "Thunder Series Unified Application Service Gateway TH5430S"
#           (TH + número + sufixo; também "TH1040S", "TH4430"...)
_MODEL_RE = re.compile(
    r"(?i)\b(?:TH\d{2,5}[A-Za-z]*(?:\(\s?S\s?\))?"
    r"|Thunder\s+\d{2,4}[A-Za-z]*(?:\(\s?S\s?\))?"
    r"|vThunder\b)"
)

# Slots de boot — dois formatos:
#   antigo: "Hard Disk primary        4.1.4-GR1-P14 (*)"
#   novo:   "Hard Disk secondary image (default) version 5.2.1-P11, build 66"
# A marca de default pode vir antes (novo: "(default)") ou depois
# (antigo: "(*)") da versão — checamos a linha inteira do match.
_BOOTIMAGE_RE = re.compile(
    r"Hard\s+Disk\s+(primary|secondary)[^\n]*?"
    r"([0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9]+)*)[^\n]*",
    re.IGNORECASE,
)

# Serial number — formatos vistos no `show version` do ACOS:
#   "Serial Number: A10TH-XXXX" / "Serial No: ..." / "Serial# ..." / "SN: ..."
_SERIAL_RE = re.compile(
    r"(?im)\bserial\s*(?:number|no\.?|#)?\s*[:=]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9\-_]{3,})\b"
)


class VersionError(ValueError):
    pass


def version_tuple(version):
    """Converte '4.1.4-GR1-P14' -> (4, 1, 4, 14, 1). None se inválido.

    Ordem: core (x.y.z), NÍVEL DE PATCH ([Pp]<n>), e os demais números
    do sufixo em ordem (GR, SP, build). O patch decide entre releases —
    '5.2.1-P14.73' > '5.2.1-p5.114' (patch 14 > patch 5); o build final
    NÃO decide (repro de bancada: comparar o build trocava o boot para a
    partição MAIS VELHA e oscilava entre partições a cada ciclo).
    """
    if not version:
        return None
    m = _CORE_RE.search(version)
    if not m:
        return None
    core = tuple(int(x) for x in m.groups())
    rest = version[m.end():]
    pm = re.search(r"(?:^|[-.])[Pp](\d+)", rest)
    patch = int(pm.group(1)) if pm else 0
    if pm:
        # remove o P já contado para os números restantes ficarem em ordem
        rest = rest[:pm.start()] + " " + rest[pm.end():]
    tail = tuple(int(x) for x in re.findall(r"\d+", rest))
    return core + (patch,) + tail


def compare_versions(a, b):
    """-1 se a<b, 0 se igual, 1 se a>b. None se alguma inválida."""
    ta, tb = version_tuple(a), version_tuple(b)
    if ta is None or tb is None:
        return None
    return (ta > tb) - (ta < tb)


def version_major(version):
    """Major de uma versão ('5.2.1-P3' -> 5). None se inválida."""
    t = version_tuple(version)
    return t[0] if t else None


def parse_acos_version(text):
    """Extrai a versão ACOS de um `show version`. Retorna str ou None."""
    if not text:
        return None
    m = _ACOS_VERSION_RE.search(text)
    if m:
        return m.group(1)
    m = _LOOSE_VERSION_RE.search(text)
    if m:
        return m.group(1)
    return None


def parse_serial_number(text):
    """Extrai o número de série de um `show version`. Retorna str ou None."""
    if not text:
        return None
    m = _SERIAL_RE.search(text)
    return m.group(1) if m else None


def parse_bootimage(text):
    """Interpreta a saída de `show bootimage`.

    Retorna dict {primary, secondary, default} com as versões de cada slot
    e qual é o slot padrão ('primary'|'secondary'|None).
    """
    slots = {}
    default = None
    for m in _BOOTIMAGE_RE.finditer(text or ""):
        slot = m.group(1).lower()
        ver = m.group(2)
        slots[slot] = ver
        line = m.group(0)
        if "*" in line or "default" in line.lower():
            default = slot
    return {"primary": slots.get("primary"), "secondary": slots.get("secondary"),
            "default": default}


def parse_model(text):
    """Extrai o modelo do equipamento ('Thunder 4430(S)', 'vThunder'...).

    Retorna str normalizada ou None se não encontrar.
    """
    if not text:
        return None
    m = _MODEL_RE.search(text)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(0)).strip()
