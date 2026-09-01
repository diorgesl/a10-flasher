"""Relatório em PDF de equipamento gerado por LLM (DeepSeek V4 Flash).

Duas responsabilidades:
  1. `analyze_with_llm(record, llm_cfg)` — manda os dados brutos do
     equipamento para o LLM e devolve a análise estruturada (JSON).
     Usa urllib (o portal não depende de SDK/requests) com o formato
     OpenAI-compatível da DeepSeek.
  2. `build_pdf(analysis)` — transforma a análise num PDF em memória
     (fpdf2, core fonts latin-1 — cobre o pt-BR sem fontes extras).

O módulo não conhece o portal nem o DB: recebe o registro (dict do
DeviceStore) e a config da seção `llm` do config.yaml
(api_key, base_url, model, timeout).
"""

import json
import re
import time
import urllib.request

from fpdf import FPDF

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_TIMEOUT = 120

_SYSTEM_PROMPT = (
    "Você é um especialista em infraestrutura de rede e equipamentos "
    "A10 Thunder (ACOS). Analise os dados do equipamento abaixo e produza "
    "um relatório de APROVAÇÃO OPERACIONAL em português do Brasil, "
    "pensado para ser lido por qualquer pessoa da equipe: texto claro, "
    "didático e fluido, sem jargão técnico excessivo — explique em poucas "
    "palavras o que cada item significa. O objetivo é comprovar que o "
    "equipamento está operacional e aprovado para entrar em operação.\n"
    "Responda APENAS com um objeto JSON válido, sem texto fora dele, com "
    "estas chaves:\n"
    '- "titulo" (string): título do relatório com o número de série\n'
    '- "resumo" (string): resumo executivo de 2 a 4 frases em tom de '
    'conclusão (ex.: "o equipamento está pronto para operar")\n'
    '- "identificacao" (string): modelo, série e identificação\n'
    '- "firmware" (string): versão ACOS e situação de atualização\n'
    '- "interfaces" (string): estado das interfaces de forma simples '
    "(quantas estavam UP do total, sem enumerar todas)\n"
    '- "licencas" (string): situação das licenças\n'
    '- "hardware" (string): saúde do hardware em linguagem simples, '
    "mencionando as ventoinhas (todas funcionando bem se estiverem OK), "
    "fontes e temperatura\n"
    '- "hardware_table" (array de objetos {"item", "status"}): tabela '
    'curta do hardware — cada ventoinha, fonte e a temperatura, com '
    'status "OK", "ATENÇÃO" ou "FALHA"\n'
    '- "burnin" (string): resultado dos testes de carga TRex de forma '
    "simples (veredito, carga aplicada, duração, tráfego medido)\n"
    '- "uptime" (string): maior uptime registrado no modo teste, citando '
    "o valor exato informado e explicando o que ele significa\n"
    '- "kpis" (array de 4 objetos {"rotulo", "valor"}): indicadores '
    'curtos para cards — ex.: Uptime máximo, Interfaces UP, Carga TRex, '
    "Licenças\n"
    '- "aprovacao" (string): conclusão clara se o equipamento está '
    "OPERACIONAL e APROVADO para trabalhar (ou não, com o motivo)\n"
    '- "aprovado" (booleano): true se o equipamento está operacional e '
    "aprovado (testes passaram e interfaces estão UP), false caso "
    "contrário\n"
    "Se alguma saída não foi coletada, diga isso em vez de inventar dados."
)


class ReportError(Exception):
    """Erro de relatório com status HTTP para o endpoint repassar."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


# ----------------------------------------------------------- LLM
def analyze_with_llm(record, llm_cfg):
    """Chama o LLM com os dados do equipamento e devolve a análise (dict).

    `llm_cfg`: seção `llm` do config (api_key obrigatório; base_url,
    model e timeout com defaults para a DeepSeek).
    """
    llm_cfg = llm_cfg or {}
    api_key = (llm_cfg.get("api_key") or "").strip()
    if not api_key:
        raise ReportError(
            "chave da API do LLM não configurada "
            "(llm.api_key no config.yaml ou env DEEPSEEK_API_KEY)",
            status=503)
    base_url = (llm_cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    payload = {
        "model": llm_cfg.get("model") or DEFAULT_MODEL,
        "messages": _build_messages(record),
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(
                req, timeout=int(llm_cfg.get("timeout") or DEFAULT_TIMEOUT)) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except ReportError:
        raise
    except Exception as exc:  # rede, HTTP, JSON da resposta
        raise ReportError(f"falha ao chamar o LLM: {exc}", status=502)
    content = ((body.get("choices") or [{}])[0]
               .get("message", {}).get("content", ""))
    return _parse_analysis(content)


_VERDICT_LABELS = {
    "pass": "aprovada",
    "fail": "reprovada",
    "interrupted": "interrompida",
    "aborted": "abortada",
}


def _fmt_uptime(seconds):
    """Segundos -> '1d 2h 3m' (mesmo formato do `show version`)."""
    if seconds is None:
        return None
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d {h}h {m}m"


def _fmt_ts(epoch):
    """Epoch (float do SQLite) -> '2026-09-01 10:00' local; None -> '—'."""
    if not epoch:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def _fmt_num(value):
    """Número sem zeros decimais ('24' em vez de '24.0'); None -> '—'."""
    if value is None:
        return "—"
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def _fmt_bps(value):
    """bps -> '1.20 Gbps' / '800.0 Mbps' (mesmo padrão do dashboard)."""
    if value is None:
        return "—"
    return (f"{value / 1e9:.2f} Gbps" if value >= 1e9
            else f"{value / 1e6:.1f} Mbps")


def _build_messages(record):
    """Prompt: instruções (system) + dados brutos do equipamento (user)."""
    linhas = ["=== DADOS DO EQUIPAMENTO ==="]
    for rotulo, campo in (("Número de série", "serial"),
                          ("Modelo", "model"),
                          ("Versão ACOS", "version"),
                          ("Porta serial", "port"),
                          ("Agente", "agent")):
        linhas.append(f"{rotulo}: {record.get(campo) or '—'}")
    linhas.append("Atualizado no ciclo: "
                  + ("sim" if record.get("upgraded") else "não"))
    for titulo, campo in (("SHOW VERSION", "version_output"),
                          ("SHOW LICENSE-INFO", "license_info"),
                          ("SHOW INTERFACES BRIEF", "interfaces"),
                          ("SHOW ENVIRONMENT", "environment")):
        linhas.append(f"=== {titulo} ===\n"
                      + (record.get(campo) or "(saída não coletada)"))
    linhas.append("=== TESTES DE CARGA (TRex) ===")
    runs = record.get("burnin_runs") or []
    if runs:
        for i, run in enumerate(runs, 1):
            veredito = _VERDICT_LABELS.get(run.get("verdict"),
                                           run.get("verdict") or "em andamento")
            trafego = run.get("traffic") or {}
            linhas.append(
                f"Run {i}: início {_fmt_ts(run.get('started_ts'))}, "
                f"duração {_fmt_num(run.get('duration_h'))}h, "
                f"carga {_fmt_num(run.get('cps'))} cps, "
                f"veredito {veredito}, motivo: {run.get('reason') or '—'}, "
                f"pico TX {_fmt_bps(trafego.get('tx_bps'))}, "
                f"pico RX {_fmt_bps(trafego.get('rx_bps'))}, "
                f"pico de sessões {_fmt_num(trafego.get('active_sessions'))}, "
                f"erros {_fmt_num(trafego.get('errors'))}")
    else:
        linhas.append("(nenhum teste de carga TRex registrado)")
    linhas.append("Maior uptime registrado no modo teste (DB): "
                  + (_fmt_uptime(record.get("max_uptime_s")) or "—"))
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(linhas)},
    ]


def _parse_analysis(content):
    """Extrai o objeto JSON da resposta do LLM (tolera fences markdown)."""
    text = (content or "").strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        text = match.group(1)
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ReportError(f"resposta do LLM não é JSON válido: {exc}")
    if not isinstance(data, dict):
        raise ReportError("resposta do LLM não é um objeto JSON")
    return data


# ------------------------------------------------------------ PDF
# core fonts do fpdf2 usam latin-1; pt-BR cabe, mas LLMs soltam aspas
# curvas/travessões — translitera os comuns e troca o resto por "?".
_REPLACEMENTS = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
    "•": "-", "→": "->",
})

_SECTIONS = (
    ("resumo", "Resumo executivo"),
    ("identificacao", "Identificação"),
    ("firmware", "Firmware"),
    ("interfaces", "Interfaces"),
    ("licencas", "Licenças"),
    ("hardware", "Hardware"),
    ("burnin", "Teste de carga (TRex)"),
    ("uptime", "Maior uptime registrado"),
    ("aprovacao", "Aprovação"),
)

# ícone vetorial de cada seção (desenhado, sem fontes extras)
_ICONES = {
    "resumo": "doc",
    "identificacao": "id",
    "firmware": "chip",
    "interfaces": "ports",
    "licencas": "key",
    "hardware": "fan",
    "burnin": "bars",
    "uptime": "clock",
    "aprovacao": "check",
}


def _latin1(text):
    text = (text or "").translate(_REPLACEMENTS)
    return "".join(
        c if c == "\n" or 32 <= ord(c) < 128 or 160 <= ord(c) < 256
        else "?" for c in text)


class _RelatorioPDF(FPDF):
    """PDF do relatório com rodapé (número de página)."""

    def footer(self):
        self.set_y(-15)
        self.set_font("helvetica", "", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 10, f"Página {self.page_no()}", align="C")


_AZUL = (16, 42, 90)
_VERDE = (20, 110, 60)
_VERMELHO = (200, 60, 50)
_CINZA = (110, 120, 135)
_FUNDO_CHIP = (226, 233, 245)
_FUNDO_CARD = (246, 248, 252)


def _icone(pdf, nome, x, y, tam, cor):
    """Ícone vetorial simples (desenhado com primitivas, sem fontes)."""
    pdf.set_draw_color(*cor)
    pdf.set_fill_color(*cor)
    pdf.set_line_width(0.5)
    cx, cy = x + tam / 2, y + tam / 2
    if nome == "doc":
        pdf.rect(x, y, tam * 0.78, tam, style="D")
        for i in range(3):
            pdf.line(x + tam * 0.16, y + tam * (0.28 + 0.25 * i),
                     x + tam * 0.62, y + tam * (0.28 + 0.25 * i))
    elif nome == "id":
        pdf.rect(x, y, tam * 0.95, tam * 0.72, style="D")
        pdf.ellipse(x + tam * 0.33, y + tam * 0.13, tam * 0.3, tam * 0.3,
                    style="F")
        pdf.line(x + tam * 0.2, y + tam * 0.6, x + tam * 0.75, y + tam * 0.6)
    elif nome == "chip":
        pdf.rect(x + tam * 0.15, y + tam * 0.15, tam * 0.7, tam * 0.7,
                 style="D")
        pdf.rect(x + tam * 0.3, y + tam * 0.3, tam * 0.4, tam * 0.4,
                 style="D")
        for dx in (0.15, 0.85):
            pdf.line(x + tam * dx, y + tam * 0.35, x + tam * dx, y + tam * 0.1)
            pdf.line(x + tam * dx, y + tam * 0.65, x + tam * dx, y + tam * 0.9)
        for dy in (0.15, 0.85):
            pdf.line(x + tam * 0.35, y + tam * dy, x + tam * 0.1, y + tam * dy)
            pdf.line(x + tam * 0.65, y + tam * dy, x + tam * 0.9, y + tam * dy)
    elif nome == "ports":
        pdf.rect(x, y, tam * 0.42, tam * 0.6, style="D")
        pdf.rect(x + tam * 0.58, y, tam * 0.42, tam * 0.6, style="D")
        pdf.line(x, y + tam * 0.3, x + tam * 0.42, y + tam * 0.3)
        pdf.line(x + tam * 0.58, y + tam * 0.3, x + tam, y + tam * 0.3)
    elif nome == "key":
        pdf.ellipse(x, y + tam * 0.3, tam * 0.55, tam * 0.55, style="D")
        pdf.line(x + tam * 0.55, y + tam * 0.55, x + tam, y + tam * 0.95)
        pdf.line(x + tam * 0.8, y + tam * 0.75, x + tam * 0.95, y + tam * 0.6)
    elif nome == "fan":
        for inicio in (30, 150, 270):
            pdf.arc(cx, cy, tam * 0.38, inicio, inicio + 95)
        pdf.ellipse(cx - tam * 0.08, cy - tam * 0.08, tam * 0.16, tam * 0.16,
                    style="F")
    elif nome == "bars":
        pdf.rect(x, y + tam * 0.5, tam * 0.24, tam * 0.5, "F")
        pdf.rect(x + tam * 0.38, y + tam * 0.24, tam * 0.24, tam * 0.76, "F")
        pdf.rect(x + tam * 0.76, y, tam * 0.24, tam, "F")
    elif nome == "clock":
        pdf.ellipse(x, y, tam, tam, style="D")
        pdf.line(cx, cy, cx, y + tam * 0.22)
        pdf.line(cx, cy, x + tam * 0.75, cy)
    elif nome == "check":
        pdf.ellipse(x, y, tam, tam, style="D")
        pdf.line(x + tam * 0.3, cy, x + tam * 0.45, y + tam * 0.62)
        pdf.line(x + tam * 0.45, y + tam * 0.62, x + tam * 0.72, y + tam * 0.35)


def _led(pdf, x, y, r, cor, cor_halo):
    """LED com brilho: halo externo + corpo + reflexo claro.

    Sem transparência no fpdf2 instalado, o "glow" é simulado com
    círculos concêntricos em tons escalonados.
    """
    pdf.set_fill_color(*cor_halo)
    pdf.ellipse(x - r, y - r, r * 2, r * 2, style="F")
    pdf.set_fill_color(*cor)
    pdf.ellipse(x - r * 0.62, y - r * 0.62, r * 1.24, r * 1.24, style="F")
    pdf.set_fill_color(255, 255, 255)
    pdf.ellipse(x - r * 0.22, y - r * 0.34, r * 0.44, r * 0.44, style="F")


_LED_VERDE = ((52, 168, 83), (165, 228, 185))
_LED_VERMELHO = ((220, 60, 50), (250, 195, 185))
_LED_AMARELO = ((238, 168, 42), (252, 234, 190))


def _quebra_se(pdf, altura):
    """Quebra a página antes se não couber `altura` mm a partir daqui.

    Desenhar (rect/line) além da margem não quebra a página — só as
    células quebram, uma por vez, espalhando o conteúdo por páginas.
    """
    if pdf.get_y() + altura > pdf.h - pdf.b_margin:
        pdf.add_page()


def _kpis(pdf, kpis):
    """Cards de indicadores (2x2) logo abaixo do selo de aprovação."""
    kpis = [k for k in (kpis or []) if isinstance(k, dict)][:4]
    if not kpis:
        return
    _quebra_se(pdf, 42)
    largura = (pdf.w - pdf.l_margin - pdf.r_margin - 6) / 2
    y0 = pdf.get_y() + 2
    for i, kpi in enumerate(kpis):
        col, linha = i % 2, i // 2
        x = pdf.l_margin + col * (largura + 6)
        y = y0 + linha * 18
        pdf.set_fill_color(*_FUNDO_CARD)
        pdf.set_draw_color(218, 224, 236)
        pdf.rect(x, y, largura, 16, style="DF",
                 round_corners=True, corner_radius=2)
        _led(pdf, x + largura - 5, y + 4, 1.2, *_LED_VERDE)
        pdf.set_font("helvetica", "", 8)
        pdf.set_text_color(*_CINZA)
        pdf.set_xy(x + 4, y + 2.5)
        pdf.cell(largura - 14, 4, _latin1(kpi.get("rotulo") or ""))
        pdf.set_font("helvetica", "B", 12)
        pdf.set_text_color(*_AZUL)
        pdf.set_xy(x + 4, y + 7.5)
        pdf.cell(largura - 14, 6, _latin1(kpi.get("valor") or "—")[:28])
    pdf.set_y(y0 + 36 + 4)
    pdf.set_text_color(40, 40, 40)


def _tabela_hardware(pdf, rows):
    """Tabela item/situação do hardware (listras alternadas)."""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return
    _quebra_se(pdf, 16)  # cabeçalho + 1ª linha juntos
    col_item = pdf.w - pdf.l_margin - pdf.r_margin - 40
    alt = 6.5
    pdf.ln(1)
    pdf.set_font("helvetica", "B", 9)
    pdf.set_fill_color(*_AZUL)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(col_item, alt, " Item", fill=True)
    pdf.cell(40, alt, "Situação", align="C", fill=True)
    pdf.ln()
    pdf.set_font("helvetica", "", 9)
    for i, r in enumerate(rows):
        item = _latin1((r.get("item") or "—").strip())[:58]
        status = _latin1((r.get("status") or "—").strip())
        ok = any(p in status.upper() for p in
                 ("OK", "ATIVO", "NORMAL", "FUNCIONANDO"))
        cor_status = _VERDE if ok else _VERMELHO
        if i % 2 == 0:
            pdf.set_fill_color(*_FUNDO_CARD)
            pdf.set_text_color(40, 40, 40)
            pdf.cell(col_item, alt, " " + item, fill=True)
            pdf.set_text_color(*cor_status)
            pdf.cell(40, alt, status, align="C", fill=True)
        else:
            pdf.set_text_color(40, 40, 40)
            pdf.cell(col_item, alt, " " + item)
            pdf.set_text_color(*cor_status)
            pdf.cell(40, alt, status, align="C")
        pdf.ln()
    pdf.set_text_color(40, 40, 40)
    pdf.ln(2)


def _amostrar(rows, n=24):
    """Reduz uma série a no máximo n pontos igualmente espaçados."""
    if len(rows) <= n:
        return rows
    passo = (len(rows) - 1) / max(1, n - 1)
    return [rows[int(i * passo)] for i in range(n)]


def _chart_barras(pdf, pares, cores, rotulo, fmt_max):
    """Gráfico de barras verticais: pares = [(a, b), ...] (b opcional).

    Duas séries lado a lado por ponto (TX/RX ou valor/None), linha de
    base, valor máximo e legenda.
    """
    pares = [p for p in pares if any(v is not None for v in p)]
    if not pares:
        return
    _quebra_se(pdf, 42)  # bloco inteiro do gráfico na mesma página
    n = len(pares)
    maximo = max(v for p in pares for v in p if v is not None) or 1
    x = pdf.l_margin
    largura = pdf.w - pdf.l_margin - pdf.r_margin
    altura = 24
    y = pdf.get_y() + 3
    base = y + altura
    passo = largura / n
    pdf.set_line_width(0.3)
    for i, (a, b) in enumerate(pares):
        cx = x + passo * i
        bw = max(0.6, passo * 0.30)
        if a is not None:
            ha = altura * a / maximo
            pdf.set_fill_color(*cores[0])
            pdf.rect(cx + passo * 0.08, base - ha, bw, ha, "F")
        if b is not None:
            hb = altura * b / maximo
            pdf.set_fill_color(*cores[1])
            pdf.rect(cx + passo * 0.08 + bw + passo * 0.08, base - hb, bw, hb,
                     "F")
    pdf.set_draw_color(200, 205, 215)
    pdf.line(x, base, x + largura, base)
    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(*_CINZA)
    pdf.set_xy(x, y - 4)
    pdf.cell(largura, 3.5, "máx: " + fmt_max(maximo), align="R")
    pdf.set_xy(x, base + 1.5)
    pdf.cell(largura, 3.5, _latin1(rotulo))
    if any(b is not None for _, b in pares):
        pdf.set_fill_color(*cores[0])
        pdf.rect(x, base + 6.5, 3, 2.2, "F")
        pdf.set_fill_color(*cores[1])
        pdf.rect(x + 14, base + 6.5, 3, 2.2, "F")
        pdf.set_xy(x + 4, base + 6.3)
        pdf.cell(9, 2.5, "TX")
        pdf.set_xy(x + 18, base + 6.3)
        pdf.cell(9, 2.5, "RX")
        pdf.set_y(base + 10)
    else:
        pdf.set_y(base + 6)
    pdf.set_text_color(40, 40, 40)


def build_pdf(analysis, record=None):
    """Gera o PDF do relatório em memória e devolve os bytes.

    Layout: faixa de cabeçalho com o título, selo de aprovação
    (verde/vermelho conforme `aprovado`), cards de indicadores (kpis),
    seções com ícone + cabeçalho colorido, tabela do hardware e
    gráficos de barras das séries reais (uptime e tráfego TRex do
    registro), com rodapé de página.

    `record`: registro enriquecido do portal (uptime_series e
    burnin_runs[].samples alimentam os gráficos) — opcional.
    """
    analysis = analysis or {}
    pdf = _RelatorioPDF(format="A4")
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()
    # new_x/new_y: multi_cell(0, ...) deixa o cursor na margem direita —
    # sem voltar para a margem esquerda a próxima célula explode com
    # "Not enough horizontal space".
    cell = dict(new_x="LMARGIN", new_y="NEXT")
    # faixa de cabeçalho: gradiente azul (degradê em faixas) + título
    # em branco + data de geração + fileira de LEDs de status
    for i in range(10):
        t = i / 9
        cor = tuple(round(a + (b - a) * t) for a, b in
                    zip((10, 28, 58), (46, 90, 148)))
        pdf.set_fill_color(*cor)
        pdf.rect(0, i * 3.4, pdf.w, 3.5, "F")
    aprovado = analysis.get("aprovado") is True
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(pdf.l_margin, 7)
    pdf.set_font("helvetica", "B", 15)
    pdf.multi_cell(0, 8, _latin1(analysis.get("titulo")
                                 or "Relatório do equipamento"), **cell)
    pdf.set_xy(pdf.l_margin, 26)
    pdf.set_font("helvetica", "", 9)
    pdf.multi_cell(0, 5, _latin1(
        "Relatório de aprovação operacional — gerado em "
        + time.strftime("%Y-%m-%d %H:%M")), **cell)
    # fileira de LEDs de status no rodapé da faixa (verde = aprovado)
    cor_led = _LED_VERDE if aprovado else _LED_VERMELHO
    n_led = 12
    espaco = (pdf.w - pdf.l_margin - pdf.r_margin) / (n_led - 1)
    for i in range(n_led):
        _led(pdf, pdf.l_margin + espaco * i, 30.5, 1.1, *cor_led)
    pdf.set_y(38)
    # selo de aprovação (verde/vermelho) com halo de brilho
    selo = "APROVADO PARA OPERAÇÃO" if aprovado else "NÃO APROVADO"
    pdf.set_font("helvetica", "B", 13)
    largura = pdf.get_string_width(selo) + 14
    y_selo = pdf.get_y()
    pdf.set_fill_color(*(225, 243, 232) if aprovado else (250, 224, 220))
    pdf.rect(pdf.l_margin - 1.2, y_selo - 1.2, largura + 2.4, 12.4,
             style="F", round_corners=True, corner_radius=3)
    pdf.set_fill_color(*(_VERDE if aprovado else _VERMELHO))
    pdf.rect(pdf.l_margin, y_selo, largura, 10,
             style="F", round_corners=True, corner_radius=2)
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(pdf.l_margin, y_selo)
    pdf.cell(largura, 10, selo, align="C")
    pdf.set_y(y_selo + 13)
    pdf.set_text_color(40, 40, 40)
    # cards de indicadores (uptime, interfaces, carga, licenças)
    _kpis(pdf, analysis.get("kpis"))
    runs = (record or {}).get("burnin_runs") or []
    for chave, rotulo in _SECTIONS:
        conteudo = analysis.get(chave)
        if not conteudo:
            continue  # seção sem conteúdo não ocupa espaço
        _quebra_se(pdf, 9)  # cabeçalho da seção não fica órfão no rodapé
        # ícone + chip de fundo claro + título da seção
        pdf.set_line_width(0.5)
        cor_icone = (_VERDE if aprovado else _VERMELHO) \
            if chave == "aprovacao" else _AZUL
        _icone(pdf, _ICONES.get(chave, "doc"),
               pdf.l_margin, pdf.get_y() + 1, 5.5, cor_icone)
        pdf.set_font("helvetica", "B", 12)
        x_titulo = pdf.l_margin + 8.5
        largura = pdf.get_string_width(rotulo) + 8
        y = pdf.get_y()
        pdf.set_fill_color(*_FUNDO_CHIP)
        pdf.rect(x_titulo, y, largura, 7,
                 style="F", round_corners=True, corner_radius=1.5)
        pdf.set_text_color(*_AZUL)
        pdf.set_xy(x_titulo + 4, y)
        pdf.cell(largura - 8, 7, rotulo)
        pdf.set_y(y + 8)
        # fio fino abaixo do cabeçalho separa a seção da anterior
        pdf.set_line_width(0.2)
        pdf.set_draw_color(200, 205, 215)
        pdf.line(pdf.l_margin, pdf.get_y(),
                 pdf.w - pdf.r_margin, pdf.get_y())
        pdf.set_y(pdf.get_y() + 1.5)
        pdf.set_font("helvetica", "", 10)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(0, 5, _latin1(conteudo), align="J", **cell)
        # extras por seção: tabela do hardware e gráficos das séries
        if chave == "hardware":
            _tabela_hardware(pdf, analysis.get("hardware_table"))
        elif chave == "burnin" and runs and runs[0].get("samples"):
            amostras = _amostrar(runs[0]["samples"])
            _chart_barras(
                pdf,
                [(s.get("tx_bps"), s.get("rx_bps")) for s in amostras],
                cores=(_AZUL, (124, 156, 205)),
                rotulo="Tráfego no teste TRex (por amostra)",
                fmt_max=_fmt_bps)
        elif chave == "uptime" and (record or {}).get("uptime_series"):
            serie = [(s.get("uptime_s"), None)
                     for s in (record["uptime_series"] or [])]
            if serie:
                _chart_barras(
                    pdf, list(reversed(serie)),  # mais antigo primeiro
                    cores=((124, 156, 205), _AZUL),
                    rotulo="Evolução do uptime no modo teste",
                    fmt_max=_fmt_uptime)
        pdf.ln(4)
    return bytes(pdf.output())


def pdf_filename(serial):
    """Nome de arquivo seguro para o download (serial pode ter '/')."""
    seguro = re.sub(r"[^A-Za-z0-9._-]+", "_", serial or "").strip("._")
    return f"relatorio-{seguro or 'equipamento'}.pdf"
