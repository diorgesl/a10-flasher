"""Relatório em PDF de equipamento gerado por LLM (DeepSeek V4 Flash).

Duas responsabilidades:
  1. `analyze_with_llm(record, llm_cfg)` — manda os dados brutos do
     equipamento para o LLM e devolve a análise estruturada (JSON).
     Usa urllib (o portal não depende de SDK/requests) com o formato
     OpenAI-compatível da DeepSeek.
  2. `build_pdf(analysis, record=None)` — transforma a análise num PDF
     em memória com ReportLab: platypus cuida da paginação (sem
     contagem manual de página), a fonte DejaVu empacotada em
     a10flash/fonts/ cobre o Unicode do pt-BR (incluindo o ✓) e o
     canvas desenha o layout (faixa, selo, LED com brilho real via
     transparência, ícones vetoriais em duas cores, gráficos).

O módulo não conhece o portal nem o DB: recebe o registro (dict do
DeviceStore) e a config da seção `llm` do config.yaml
(api_key, base_url, model, timeout).
"""

import html
import io
import json
import re
import time
import urllib.request
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame,
                                KeepTogether, PageTemplate, Paragraph,
                                Spacer, Table, TableStyle)

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
    '- "licencas" (string): situação das licenças de forma simples\n'
    '- "licencas_table" (array de objetos {"licenca", "expiracao"}): '
    "APENAS as licenças SEM data de expiração (permanentes), com "
    'expiracao "Sem expiração" — o relatório mostra só as licenças '
    "definitivas\n"
    '- "hardware" (string): saúde do hardware em linguagem simples, '
    "mencionando as ventoinhas (todas funcionando bem se estiverem OK), "
    "fontes e temperatura\n"
    '- "hardware_table" (array de objetos {"item", "status"}): tabela '
    'curta do hardware — cada ventoinha, fonte e a temperatura, com '
    'status "OK", "ATENÇÃO" ou "FALHA"\n'
    '- "burnin" (string): resultado do ÚLTIMO teste de carga TRex '
    "aprovado, de forma simples (veredito, carga aplicada, duração, "
    "tráfego medido) — cite apenas o teste informado, nunca testes "
    "anteriores ou abortados\n"
    '- "uptime" (string): maior uptime registrado no modo teste, citando '
    "o valor exato informado e explicando o que ele significa\n"
    '- "kpis" (array de 4 objetos {"rotulo", "valor"}): indicadores '
    'curtos para cards — ex.: Uptime registrado em teste, Modelo, '
    "Carga testada (duração do teste em horas), Licenças\n"
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


def _normaliza_kpis(kpis, record, runs):
    """Ajusta os KPIs do LLM para os valores mais informativos.

    - KPI de uptime vira "Uptime registrado em teste";
    - KPI de carga vira "Carga testada" com o tráfego do teste e a
      duração em horas (ex.: "Tráfego de 1.20 Gbps por 24 horas" —
      mais claro para quem lê do que o cps aplicado);
    - KPI de interfaces é trocado pelo modelo (as interfaces já são
      detalhadas na seção própria).
    """
    out = [dict(k) for k in (kpis or []) if isinstance(k, dict)][:4]
    modelo = (record or {}).get("model")
    duracao = runs[0].get("duration_h") if runs else None
    trafego = ((runs[0].get("traffic") or {}).get("tx_bps")
               if runs else None)
    for kpi in out:
        rotulo = (kpi.get("rotulo") or "").lower()
        if "uptime" in rotulo:
            kpi["rotulo"] = "Uptime registrado em teste"
        elif "carga" in rotulo:
            kpi["rotulo"] = "Carga testada"
            if duracao is not None and trafego is not None:
                horas = "hora" if int(duracao) == 1 else "horas"
                kpi["valor"] = (f"Tráfego de {_fmt_bps(trafego)} "
                                f"por {_fmt_num(duracao)} {horas}")
            elif duracao is not None:
                kpi["valor"] = f"{_fmt_num(duracao)} horas"
        elif "interfaces" in rotulo or "portas" in rotulo:
            kpi["rotulo"] = "Modelo"
            kpi["valor"] = _texto(modelo) or "—"
    return out


# ------------------------------------------------------------ PDF
# Fonte Unicode empacotada no repo (a10flash/fonts/): o pt-BR do LLM
# (aspas, travessões) e o ✓ saem nativos, sem transliteração. Se faltar
# o arquivo, cai para Helvetica (latin-1) com a transliteração antiga.
_FONTES_DIR = Path(__file__).resolve().parent / "fonts"
_FONTE = "Helvetica"
_FONTE_B = "Helvetica-Bold"
try:
    pdfmetrics.registerFont(TTFont("DejaVu", str(_FONTES_DIR / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold",
                                   str(_FONTES_DIR / "DejaVuSans-Bold.ttf")))
    _FONTE, _FONTE_B = "DejaVu", "DejaVu-Bold"
except Exception:  # fonte ausente: Helvetica cobre o latin-1
    pass

# Font Awesome 6 Solid (a10flash/fonts/fa-solid-900.ttf): ícones reais
# nas faixas/tabelas/cards (o glifo é texto, embutido como subset pelo
# ReportLab). Sem o arquivo, cai para os ícones vetoriais desenhados.
_ICONE_FONTE = None
try:
    pdfmetrics.registerFont(
        TTFont("FontAwesome", str(_FONTES_DIR / "fa-solid-900.ttf")))
    _ICONE_FONTE = "FontAwesome"
except Exception:
    pass

# margem lateral do conteúdo (menor que o padrão do A4 para caber mais)
_MARGEM_X = 12 * mm
_LARG = A4[0] - 2 * _MARGEM_X

_REPLACEMENTS = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
    "•": "-", "→": "->", "✅": "✓",
})


def _latin1(text):
    text = (text or "").translate(_REPLACEMENTS)
    return "".join(
        c if c == "\n" or 32 <= ord(c) < 128 or 160 <= ord(c) < 256
        else "?" for c in text)


def _texto(value):
    """Texto seguro para o PDF: ✅ vira ✓ (DejaVu não tem emoji) e, no
    fallback Helvetica, translitera para latin-1."""
    text = value if isinstance(value, str) else str(value or "")
    text = text.replace("✅", "✓")
    if _FONTE == "Helvetica":
        return _latin1(text)
    return "".join(c for c in text if c in "\n\t" or ord(c) >= 32)


def _para(value):
    """Texto XML-escapado para Paragraph do ReportLab."""
    return html.escape(_texto(value))


def _checa_xml(cor):
    """Check colorido para Paragraph: glifo Font Awesome ou ✓ simples."""
    if _ICONE_FONTE:
        return f'<font name="FontAwesome" color="{cor}">&#xf058;</font> '
    return f'<font color="{cor}">✓ </font>'


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

_ICONES = {
    "resumo": "",          # file-lines
    "identificacao": "",   # id-card
    "firmware": "",        # microchip
    "interfaces": "",      # network-wired
    "licencas": "",        # key
    "hardware": "",        # fan
    "burnin": "",          # chart-column
    "uptime": "",          # clock
    "aprovacao": "",       # circle-check
}
_ICONES_VETOR = {  # fallback sem Font Awesome (ícones desenhados)
    "": "doc", "": "id", "": "chip", "": "ports",
    "": "key", "": "fan", "": "bars", "": "clock",
    "": "check",
}
_FA_CHECK = ""   # circle-check (✅)
_FA_X = ""       # circle-xmark (✖)

_AZUL = colors.HexColor("#102A5A")
_AZUL_CLARO = colors.HexColor("#7C9CCD")
_VERDE = colors.HexColor("#146E3C")
_VERDE_CLARO = colors.HexColor("#34A853")
_VERMELHO = colors.HexColor("#C83C32")
_CINZA = colors.HexColor("#6E7887")
_FUNDO_CARD = colors.HexColor("#F6F8FC")
_BORDA_CARD = colors.HexColor("#DADEEB")
_HALO_VERDE = colors.HexColor("#A5E4B9")
_HALO_VERMELHO = colors.HexColor("#FAC3B9")
_SELO_VERDE_BG = colors.HexColor("#E1F3E8")
_SELO_VERMELHO_BG = colors.HexColor("#FAE0DC")
_CORPO = colors.HexColor("#282828")

_ESTILO_CORPO = ParagraphStyle(
    "corpo", fontName=_FONTE, fontSize=10, leading=13,
    alignment=TA_JUSTIFY, spaceBefore=2.5, spaceAfter=4,
    textColor=_CORPO)
_ESTILO_CELULA = ParagraphStyle(
    "celula", fontName=_FONTE, fontSize=9, leading=11, textColor=_CORPO)
_ESTILO_CELULA_C = ParagraphStyle(
    "celula-c", parent=_ESTILO_CELULA, alignment=1)


def _desenha_icone(c, nome, x, y, tam, cor, cor2):
    """Ícone de seção: glifo Font Awesome (cor `cor`) ou, sem a fonte,
    vetor desenhado em duas cores (corpo `cor`, detalhe `cor2`).

    Os glifos da FA têm o centro da tinta em +0.38em da linha de base —
    para centralizar no quadrado, a base vai em y + 0.12*tam.
    """
    c.saveState()
    if _ICONE_FONTE:
        c.setFillColor(cor)
        c.setFont(_ICONE_FONTE, tam)
        c.drawCentredString(x + tam / 2, y + 0.12 * tam, nome)
        c.restoreState()
        return
    nome = _ICONES_VETOR.get(nome, "doc")
    c.setLineWidth(0.4)
    cx, cy = x + tam / 2, y + tam / 2
    if nome == "doc":
        c.setFillColor(cor)
        c.roundRect(x, y, tam * 0.78, tam, 0.6, stroke=0, fill=1)
        c.setStrokeColor(cor2)
        for i in range(3):
            c.line(x + tam * 0.16, y + tam * (0.28 + 0.25 * i),
                   x + tam * 0.62, y + tam * (0.28 + 0.25 * i))
    elif nome == "id":
        c.setFillColor(cor)
        c.roundRect(x, y, tam * 0.95, tam * 0.72, 0.6, stroke=0, fill=1)
        c.setFillColor(cor2)
        c.circle(x + tam * 0.48, y + tam * 0.60, tam * 0.14, stroke=0, fill=1)
        c.setFillColor(cor)
        c.rect(x + tam * 0.2, y + tam * 0.22, tam * 0.55, tam * 0.09,
               stroke=0, fill=1)
    elif nome == "chip":
        c.setFillColor(cor)
        c.roundRect(x + tam * 0.15, y + tam * 0.15, tam * 0.7, tam * 0.7,
                    0.6, stroke=0, fill=1)
        c.setFillColor(cor2)
        c.rect(x + tam * 0.3, y + tam * 0.3, tam * 0.4, tam * 0.4,
               stroke=0, fill=1)
        c.setStrokeColor(cor2)
        for dx in (0.15, 0.85):
            c.line(x + tam * dx, y + tam * 0.65, x + tam * dx, y + tam * 0.9)
            c.line(x + tam * dx, y + tam * 0.1, x + tam * dx, y + tam * 0.35)
        for dy in (0.15, 0.85):
            c.line(x + tam * 0.1, y + tam * dy, x + tam * 0.35, y + tam * dy)
            c.line(x + tam * 0.65, y + tam * dy, x + tam * 0.9, y + tam * dy)
    elif nome == "ports":
        c.setFillColor(cor)
        c.roundRect(x, y, tam * 0.42, tam * 0.6, 0.5, stroke=0, fill=1)
        c.roundRect(x + tam * 0.58, y, tam * 0.42, tam * 0.6, 0.5,
                    stroke=0, fill=1)
        c.setStrokeColor(cor2)
        c.line(x, y + tam * 0.3, x + tam * 0.42, y + tam * 0.3)
        c.line(x + tam * 0.58, y + tam * 0.3, x + tam, y + tam * 0.3)
    elif nome == "key":
        c.setFillColor(cor)
        c.circle(x + tam * 0.26, y + tam * 0.54, tam * 0.26, stroke=0, fill=1)
        c.roundRect(x + tam * 0.48, y + tam * 0.45, tam * 0.5, tam * 0.12,
                    0.4, stroke=0, fill=1)
        c.setFillColor(cor2)
        c.rect(x + tam * 0.52, y + tam * 0.2, tam * 0.08, tam * 0.1,
               stroke=0, fill=1)
        c.rect(x + tam * 0.72, y + tam * 0.3, tam * 0.08, tam * 0.1,
               stroke=0, fill=1)
    elif nome == "fan":
        c.setStrokeColor(cor)
        for inicio in (30, 150, 270):
            c.arc(cx - tam * 0.36, cy - tam * 0.36,
                  cx + tam * 0.36, cy + tam * 0.36, inicio, 95)
        c.setFillColor(cor2)
        c.circle(cx, cy, tam * 0.07, stroke=0, fill=1)
    elif nome == "bars":
        c.setFillColor(cor)
        c.rect(x, y, tam * 0.24, tam * 0.5, stroke=0, fill=1)
        c.rect(x + tam * 0.38, y, tam * 0.24, tam * 0.76, stroke=0, fill=1)
        c.setFillColor(cor2)
        c.rect(x + tam * 0.76, y, tam * 0.24, tam, stroke=0, fill=1)
    elif nome == "clock":
        c.setFillColor(cor)
        c.circle(cx, cy, tam * 0.5, stroke=0, fill=1)
        c.setFillColor(cor2)
        c.rect(cx - tam * 0.05, cy - tam * 0.05, tam * 0.1, tam * 0.26,
               stroke=0, fill=1)
        c.rect(cx - tam * 0.05, cy - tam * 0.05, tam * 0.26, tam * 0.1,
               stroke=0, fill=1)
    elif nome == "check":
        c.setFillColor(cor)
        c.circle(cx, cy, tam * 0.5, stroke=0, fill=1)
        c.setStrokeColor(cor2)
        c.setLineWidth(0.7)
        c.line(x + tam * 0.28, cy, x + tam * 0.45, y + tam * 0.62)
        c.line(x + tam * 0.45, y + tam * 0.62, x + tam * 0.74, y + tam * 0.32)
    c.restoreState()


class _Selo(Flowable):
    """Selo de aprovação (verde/vermelho) com halo de brilho."""

    def __init__(self, aprovado):
        super().__init__()
        self.aprovado = aprovado

    def wrap(self, aw, ah):
        self.width = aw
        self.height = 13 * mm
        return self.width, self.height

    def draw(self):
        c = self.canv
        texto = "APROVADO PARA OPERAÇÃO" if self.aprovado else "NÃO APROVADO"
        c.setFont(_FONTE_B, 13)
        larg = c.stringWidth(texto, _FONTE_B, 13) + 14 * mm
        # halo (cantos mais largos atrás do selo)
        c.setFillColor(_SELO_VERDE_BG if self.aprovado else _SELO_VERMELHO_BG)
        c.roundRect(-1.2 * mm, 1 * mm - 1.2 * mm, larg + 2.4 * mm,
                    10 * mm + 2.4 * mm, 3 * mm, stroke=0, fill=1)
        c.setFillColor(_VERDE if self.aprovado else _VERMELHO)
        c.roundRect(0, 1 * mm, larg, 10 * mm, 2 * mm, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.drawCentredString(larg / 2, 3.6 * mm, texto)


class _CardKpi(Flowable):
    """Card de indicador: fundo claro, borda, LED com brilho e textos."""

    def __init__(self, rotulo, valor, ok=True):
        super().__init__()
        self.rotulo, self.valor, self.ok = rotulo, valor, ok

    def wrap(self, aw, ah):
        self.width = aw
        self.height = 16 * mm
        return self.width, self.height

    def draw(self):
        c = self.canv
        c.setFillColor(_FUNDO_CARD)
        c.setStrokeColor(_BORDA_CARD)
        c.setLineWidth(0.4)
        c.roundRect(0, 0, self.width, self.height, 2 * mm, stroke=1, fill=1)
        # check (✅) colorido no canto, com brilho real ao redor
        lx, ly = self.width - 5 * mm, self.height - 5 * mm
        c.setFillColor(_HALO_VERDE if self.ok else _HALO_VERMELHO)
        c.setFillAlpha(0.35)
        c.circle(lx, ly, 3.2 * mm, stroke=0, fill=1)
        c.setFillAlpha(1)
        c.setFillColor(_VERDE_CLARO if self.ok else _VERMELHO)
        if _ICONE_FONTE:
            c.setFont(_ICONE_FONTE, 5 * mm)
            c.drawCentredString(lx, ly - 0.38 * 5 * mm,
                                _FA_CHECK if self.ok else _FA_X)
        else:  # fallback: LED desenhado
            c.circle(lx, ly, 1.4 * mm, stroke=0, fill=1)
            c.setFillColor(colors.white)
            c.circle(lx - 0.4 * mm, ly + 0.5 * mm, 0.55 * mm, stroke=0, fill=1)
        c.setFillColor(_CINZA)
        c.setFont(_FONTE, 8)
        c.drawString(4 * mm, self.height - 5.6 * mm,
                     _texto(self.rotulo)[:34])
        c.setFillColor(_AZUL)
        valor = _texto(self.valor)
        # a frase inteira precisa caber (o card tem ~227pt úteis com os
        # paddings da tabela) — encolhe a fonte até o mínimo antes de
        # truncar com "…"
        larg_max = self.width - 7 * mm  # insets + espaço do check
        tam = 12
        while tam > 9 and c.stringWidth(valor, _FONTE_B, tam) > larg_max:
            tam -= 0.5
        if c.stringWidth(valor, _FONTE_B, tam) > larg_max:
            while valor and c.stringWidth(valor + "…", _FONTE_B, tam) > larg_max:
                valor = valor[:-1]
            valor += "…"
        c.setFont(_FONTE_B, tam)
        c.drawString(4 * mm, 2.6 * mm, valor)


class _FaixaSecao(Flowable):
    """Faixa de seção em largura total: fundo colorido + ícone + título."""

    def __init__(self, rotulo, icone, cor, cor2):
        super().__init__()
        self.rotulo, self.icone, self.cor, self.cor2 = rotulo, icone, cor, cor2

    def wrap(self, aw, ah):
        self.width = aw
        self.height = 8.5 * mm
        return self.width, self.height

    def draw(self):
        c = self.canv
        c.setFillColor(self.cor)
        c.roundRect(0, 0, self.width, self.height, 2 * mm, stroke=0, fill=1)
        _desenha_icone(c, self.icone, 1.5 * mm, 1.5 * mm, 5.5 * mm,
                       colors.white, self.cor2)
        c.setFillColor(colors.white)
        c.setFont(_FONTE_B, 11.5)
        c.drawString(9 * mm, self.height / 2 - 2.1 * mm, _texto(self.rotulo))


class _Chart(Flowable):
    """Gráfico de barras verticais (1 ou 2 séries) com máximo e legenda."""

    def __init__(self, pares, cores, rotulo, fmt_max):
        super().__init__()
        self.pares = pares
        self.cores = cores
        self.rotulo = rotulo
        self.fmt_max = fmt_max

    def wrap(self, aw, ah):
        self.width = aw
        self.height = 42 * mm
        return self.width, self.height

    def draw(self):
        c = self.canv
        n = len(self.pares)
        maximo = max(v for p in self.pares for v in p if v is not None) or 1
        altura = 24 * mm
        base = 12 * mm
        passo = self.width / n
        for i, (a, b) in enumerate(self.pares):
            cx = passo * i
            bw = max(0.6 * mm, passo * 0.30)
            if a is not None:
                ha = altura * a / maximo
                c.setFillColor(self.cores[0])
                c.rect(cx + passo * 0.08, base, bw, ha, stroke=0, fill=1)
            if b is not None:
                hb = altura * b / maximo
                c.setFillColor(self.cores[1])
                c.rect(cx + passo * 0.08 + bw + passo * 0.08, base, bw, hb,
                       stroke=0, fill=1)
        c.setStrokeColor(colors.HexColor("#C8CDD7"))
        c.setLineWidth(0.3)
        c.line(0, base, self.width, base)
        c.setFillColor(_CINZA)
        c.setFont(_FONTE, 8)
        c.drawRightString(self.width, base + altura + 3 * mm,
                          "máx: " + self.fmt_max(maximo))
        c.drawString(0, base - 4 * mm, _texto(self.rotulo))
        if any(b is not None for _, b in self.pares):
            y_leg = base - 8 * mm
            c.setFillColor(self.cores[0])
            c.rect(0, y_leg, 3 * mm, 2.2 * mm, stroke=0, fill=1)
            c.setFillColor(self.cores[1])
            c.rect(14 * mm, y_leg, 3 * mm, 2.2 * mm, stroke=0, fill=1)
            c.setFillColor(_CINZA)
            c.setFont(_FONTE, 8)
            c.drawString(4 * mm, y_leg, "TX")
            c.drawString(18 * mm, y_leg, "RX")


def _tabela_hardware(rows):
    """Tabela item/situação do hardware (listras alternadas, ✓ no OK)."""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return []
    dados = [["Item", "Situação"]]
    for r in rows:
        item = _para((r.get("item") or "—").strip())[:58]
        status = _texto((r.get("status") or "—").strip())
        ok = any(p in status.upper() for p in
                 ("OK", "ATIVO", "NORMAL", "FUNCIONANDO"))
        cor = _VERDE if ok else _VERMELHO
        checa = _checa_xml(cor) if ok else ""
        corpo = f'<font color="{cor}">{checa}{status}</font>'
        dados.append([Paragraph(item, _ESTILO_CELULA),
                      Paragraph(corpo, _ESTILO_CELULA_C)])
    tabela = Table(dados, colWidths=[None, 40 * mm])
    tabela.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _AZUL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), _FONTE_B),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTNAME", (0, 1), (-1, -1), _FONTE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _FUNDO_CARD]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, _AZUL),
    ]))
    return [KeepTogether([tabela, Spacer(1, 2 * mm)])]


def _tabela_licencas(rows):
    """Tabela de licenças em 4 colunas (LICENÇA/EXPIRAÇÃO ×2 por linha).

    Só licenças sem expiração chegam aqui (o prompt pede apenas as
    permanentes); "Sem expiração" fica verde para destacar.
    """
    rows = [r for r in (rows or [])
            if isinstance(r, dict) and (r.get("licenca") or r.get("expiracao"))]
    if not rows:
        return []
    dados = [["Licença", "Expiração", "Licença", "Expiração"]]
    for linha in [rows[i:i + 2] for i in range(0, len(rows), 2)]:
        linha = linha + [None] * (2 - len(linha))
        cel = []
        for lic in linha:
            if lic is None:
                cel += ["", ""]
                continue
            nome = _para((lic.get("licenca") or "—").strip())[:20]
            exp = _texto((lic.get("expiracao") or "—").strip())
            sem = any(p in exp.upper() for p in
                      ("SEM", "PERMANENTE", "NUNCA", "ILIMITADA"))
            cor = _VERDE if sem else _CINZA
            cel.append(Paragraph(_checa_xml(_VERDE) + nome, _ESTILO_CELULA))
            cel.append(Paragraph(f'<font color="{cor}">{exp}</font>',
                                 _ESTILO_CELULA_C))
        dados.append(cel)
    col = _LARG / 4
    tabela = Table(dados, colWidths=[col] * 4)
    tabela.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _AZUL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), _FONTE_B),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTNAME", (0, 1), (-1, -1), _FONTE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _FUNDO_CARD]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, _AZUL),
    ]))
    return [KeepTogether([tabela, Spacer(1, 2 * mm)])]


def _amostrar(rows, n=24):
    """Reduz uma série a no máximo n pontos igualmente espaçados."""
    if len(rows) <= n:
        return rows
    passo = (len(rows) - 1) / max(1, n - 1)
    return [rows[int(i * passo)] for i in range(n)]


def _desenha_header(c, titulo):
    """Faixa do topo (largura total da página, só na primeira). O título
    quebra em até 2 linhas por palavra em vez de truncar."""
    c.saveState()
    c.setFillColor(_AZUL)
    c.rect(0, A4[1] - 34 * mm, A4[0], 34 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont(_FONTE_B, 15)
    linhas = _quebra_linhas(c, _texto(titulo) or "Relatório do equipamento",
                            _LARG, _FONTE_B, 15, max_linhas=2)
    for i, linha in enumerate(linhas):
        c.drawString(_MARGEM_X, A4[1] - 10 * mm - i * 6.5 * mm, linha)
    c.setFont(_FONTE, 9)
    c.drawString(_MARGEM_X, A4[1] - 26 * mm,
                 "Relatório de aprovação operacional — gerado em "
                 + time.strftime("%Y-%m-%d %H:%M"))
    c.restoreState()


def _quebra_linhas(c, texto, larg, fonte, tam, max_linhas=2):
    """Quebra o texto em linhas por palavra; a última linha que não
    couber é truncada em caractere (sem '…' — nunca vaza da faixa)."""
    linhas, atual = [], ""
    for pal in texto.split():
        if atual and c.stringWidth(atual + " " + pal, fonte, tam) <= larg:
            atual += " " + pal
        elif not atual:
            atual = pal
        else:
            linhas.append(atual)
            atual = pal
            if len(linhas) == max_linhas:
                break
    if atual and len(linhas) < max_linhas:
        linhas.append(atual)
    while linhas and c.stringWidth(linhas[-1], fonte, tam) > larg:
        linhas[-1] = linhas[-1][:-1]
        if not linhas[-1]:
            linhas.pop()
    return linhas or [""]


def _rodape(c, doc):
    """Número de página no rodapé."""
    c.saveState()
    c.setFont(_FONTE, 8)
    c.setFillColor(colors.HexColor("#787878"))
    c.drawCentredString(A4[0] / 2, 12 * mm, f"Página {doc.page}")
    c.restoreState()


def _monta_story(analysis, record):
    """Flowables do relatório (platypus resolve a paginação)."""
    aprovado = analysis.get("aprovado") is True
    runs = (record or {}).get("burnin_runs") or []
    story = [_Selo(aprovado), Spacer(1, 2 * mm)]
    # cards de indicadores (uptime, modelo, carga em horas, licenças)
    kpis = _normaliza_kpis(analysis.get("kpis"), record, runs)
    if kpis:
        cards = [_CardKpi(k.get("rotulo") or "", k.get("valor") or "—",
                          ok=aprovado) for k in kpis]
        grid = [cards[i:i + 2] for i in range(0, len(cards), 2)]
        if len(grid[-1]) == 1:
            grid[-1].append("")
        grade = Table(grid, colWidths=[_LARG / 2] * 2)
        grade.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
            ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
        ]))
        story.append(grade)
    story.append(Spacer(1, 1 * mm))
    for chave, rotulo in _SECTIONS:
        conteudo = analysis.get(chave)
        if not conteudo:
            continue  # seção sem conteúdo não ocupa espaço
        cor_faixa = (_VERDE if aprovado else _VERMELHO) \
            if chave == "aprovacao" else _AZUL
        cor2 = cor_faixa if chave == "aprovacao" else _VERDE
        story.append(_FaixaSecao(rotulo, _ICONES.get(chave, "doc"),
                                 cor_faixa, cor2))
        corpo = _para(conteudo)
        if chave == "firmware" and (record or {}).get("upgraded"):
            corpo = corpo.rstrip() + \
                f'<br/><font color="#146E3C"><b>Atualizado {_checa_xml(_VERDE)}</b></font>'
        elif chave == "burnin" and runs and runs[0].get("verdict") == "pass":
            corpo = corpo.rstrip() + \
                f'<br/><font color="#146E3C"><b>Aprovado {_checa_xml(_VERDE)}</b></font>'
        story.append(Paragraph(corpo, _ESTILO_CORPO))
        if chave == "hardware":
            story += _tabela_hardware(analysis.get("hardware_table"))
        elif chave == "licencas":
            story += _tabela_licencas(analysis.get("licencas_table"))
        elif chave == "burnin" and runs and runs[0].get("samples"):
            amostras = _amostrar(runs[0]["samples"], 40)
            pares = [(s.get("tx_bps"), s.get("rx_bps")) for s in amostras]
            pares = [p for p in pares if any(v is not None for v in p)]
            if pares:
                story.append(_Chart(pares, (_AZUL, _AZUL_CLARO),
                                    "Tráfego no teste TRex (por amostra)",
                                    _fmt_bps))
        elif chave == "uptime" and (record or {}).get("uptime_series"):
            serie = [(s.get("uptime_s"), None)
                     for s in (record["uptime_series"] or [])]
            serie = [p for p in serie if any(v is not None for v in p)]
            serie = _amostrar(list(reversed(serie)), 40)
            if serie:
                story.append(_Chart(serie, (_AZUL_CLARO, _AZUL),
                                    "Evolução do uptime no modo teste",
                                    _fmt_uptime))
        story.append(Spacer(1, 3 * mm))
    return story


def build_pdf(analysis, record=None):
    """Gera o PDF do relatório em memória e devolve os bytes.

    Layout: faixa de cabeçalho (desenhada na página 1), selo de
    aprovação verde/vermelho, cards de indicadores (kpis), seções com
    faixa colorida + ícone, tabela do hardware com ✓, tabela das
    licenças (4 colunas) e gráficos de barras das séries reais (uptime
    e tráfego TRex do registro). A paginação é do platypus.

    `record`: registro enriquecido do portal (uptime_series e
    burnin_runs[].samples alimentam os gráficos) — opcional.
    """
    analysis = analysis or {}
    buffer = io.BytesIO()
    doc = BaseDocTemplate(
        buffer, pagesize=A4,
        leftMargin=_MARGEM_X, rightMargin=_MARGEM_X,
        topMargin=44 * mm, bottomMargin=20 * mm,
        title="Relatório do equipamento", author="A10 Flash",
    )

    def _pagina(c, d):
        if d.page == 1:
            _desenha_header(c, analysis.get("titulo")
                            or "Relatório do equipamento")
        _rodape(c, d)

    # paddings zerados: o conteúdo ocupa exatamente _LARG (o default de
    # 6pt por lado faria as tabelas com colWidths explícitos vazarem)
    frame = Frame(_MARGEM_X, 20 * mm, _LARG, A4[1] - 64 * mm, id="frame",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates(
        [PageTemplate(id="pagina", frames=[frame], onPage=_pagina)])
    doc.build(_monta_story(analysis, record))
    return buffer.getvalue()


def pdf_filename(serial):
    """Nome de arquivo seguro para o download (serial pode ter '/')."""
    seguro = re.sub(r"[^A-Za-z0-9._-]+", "_", serial or "").strip("._")
    return f"relatorio-{seguro or 'equipamento'}.pdf"
