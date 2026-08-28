"""Burn-in de estabilidade: config CGNAT/LSN + tráfego TRex.

Fase entre o ciclo e o modo teste: aplica a config LSN (template com
portas renderizadas por caixa), força tráfego por `duration_h` (default
24h) e observa se a caixa reinicia — triagem de estabilidade para
equipamentos de segunda mão.

Vereditos: pass | fail | interrupted | aborted.

A REGRA DE PORTAS não usa a velocidade do `show interfaces brief`
(todos os nomes são "ethernet N" e a velocidade não é fonte confiável):
o modelo (`show version`) diz quantas portas traseiras de 40G/100G
descontar, e o brief só fornece a contagem.
"""

import re

DEFAULT_SKIP_MAP = [
    # modelos "4xxx" pra cima: 4 portas de 40G/100G no final, sempre
    {"pattern": ("4430|4440|5430|5440|5630|6430|6435|6440|5840|5845|"
                 "7440|7445|7650|7655|14045"), "skip": 4},
]


def pick_lsn_ports(model, brief, skip_map=None):
    """(inside, outside) = as duas últimas portas ethernet utilizáveis.

    `brief` é a saída bruta de `show interfaces brief`; `model` vem do
    `show version`. `skip_map` = lista de {"pattern", "skip"} (primeiro
    match vence; default DEFAULT_SKIP_MAP; sem match desconta 0).
    """
    ports = {int(m) for m in
             re.findall(r"ethernet\s+(\d+)", brief or "", re.IGNORECASE)}
    if not ports:
        raise ValueError("sem portas ethernet no show interfaces brief")
    skip = 0
    for entry in (skip_map or DEFAULT_SKIP_MAP):
        if entry.get("pattern") and re.search(entry["pattern"],
                                              model or ""):
            skip = int(entry.get("skip", 0))
            break
    total = max(ports)
    usable = total - skip
    if usable < 2:
        raise ValueError(
            f"portas insuficientes para o burn-in: {total} porta(s), "
            f"{skip} descontada(s) (modelo {model or '?'})")
    return str(usable - 1), str(usable)


def render_lsn_template(template_text, inside, outside, extra_ports=()):
    """Substitui {INSIDE_PORT}/{OUTSIDE_PORT} e acrescenta os blocos
    `interface ethernet N`/`enable` de `extra_ports`. Remove separadores
    `!` e o `end` final (o aplicador envia `end` por conta própria)."""
    text = (template_text or "").replace("{INSIDE_PORT}", inside)
    text = text.replace("{OUTSIDE_PORT}", outside)
    lines = []
    for ln in text.splitlines():
        stripped = ln.strip()
        if not stripped or stripped == "!" or stripped == "end":
            continue
        lines.append(ln)
    for port in extra_ports:
        lines += [f"interface ethernet {port}", "enable"]
    return lines
