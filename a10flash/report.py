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
    "um relatório técnico de APROVAÇÃO OPERACIONAL em português do "
    "Brasil: o objetivo é comprovar que o equipamento está operacional e "
    "aprovado para entrar em operação, não apenas relatar a saúde.\n"
    "Responda APENAS com um objeto JSON válido, sem texto fora dele, com "
    "estas chaves (todas strings, exceto 'aprovado' que é booleano):\n"
    '- "titulo": título do relatório com o número de série\n'
    '- "resumo": resumo executivo de 2 a 4 frases destacando o resultado '
    "da operação\n"
    '- "identificacao": modelo, série e identificação do equipamento\n'
    '- "firmware": versão ACOS e situação de atualização\n'
    '- "interfaces": estado das interfaces (quais estavam UP, contagem '
    "UP/total) com base no show interfaces\n"
    '- "licencas": situação das licenças (ativas, datas, observações)\n'
    '- "burnin": resultado dos testes de carga TRex: veredito, carga '
    "aplicada (cps), duração e tráfego medido\n"
    '- "uptime": maior uptime registrado no modo teste, citando o valor '
    "exato informado\n"
    '- "aprovacao": conclusão clara e direta se o equipamento está '
    "OPERACIONAL e APROVADO para trabalhar (ou não, com o motivo)\n"
    '- "aprovado": true se o equipamento está operacional e aprovado '
    "(testes passaram e interfaces estão UP), false caso contrário\n"
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
    ("burnin", "Teste de carga (TRex)"),
    ("uptime", "Maior uptime registrado"),
    ("aprovacao", "Aprovação"),
)


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


def build_pdf(analysis):
    """Gera o PDF do relatório em memória e devolve os bytes.

    Layout: faixa de cabeçalho com o título, selo de aprovação
    (verde/vermelho conforme o campo booleano `aprovado`), seções com
    cabeçalho colorido e rodapé com número de página.
    """
    analysis = analysis or {}
    pdf = _RelatorioPDF(format="A4")
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()
    # new_x/new_y: multi_cell(0, ...) deixa o cursor na margem direita —
    # sem voltar para a margem esquerda a próxima célula explode com
    # "Not enough horizontal space".
    cell = dict(new_x="LMARGIN", new_y="NEXT")
    # faixa de cabeçalho: título em branco + data de geração
    pdf.set_fill_color(*_AZUL)
    pdf.rect(0, 0, pdf.w, 34, "F")
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
    pdf.set_y(38)
    # selo de aprovação (verde/vermelho conforme o LLM decidiu)
    aprovado = analysis.get("aprovado") is True
    selo = "APROVADO PARA OPERAÇÃO" if aprovado else "NÃO APROVADO"
    pdf.set_font("helvetica", "B", 13)
    largura = pdf.get_string_width(selo) + 14
    pdf.set_fill_color(*(_VERDE if aprovado else _VERMELHO))
    pdf.rect(pdf.l_margin, pdf.get_y(), largura, 10,
             style="F", round_corners=True, corner_radius=2)
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(pdf.l_margin, pdf.get_y())
    pdf.cell(largura, 10, selo, align="C")
    pdf.set_y(pdf.get_y() + 13)
    pdf.set_text_color(40, 40, 40)
    for chave, rotulo in _SECTIONS:
        conteudo = analysis.get(chave)
        if not conteudo:
            continue  # seção sem conteúdo não ocupa espaço
        # cabeçalho da seção: chip de fundo claro + texto em azul
        pdf.set_font("helvetica", "B", 12)
        largura = pdf.get_string_width(rotulo) + 8
        y = pdf.get_y()
        pdf.set_fill_color(226, 233, 245)
        pdf.rect(pdf.l_margin, y, largura, 7,
                 style="F", round_corners=True, corner_radius=1.5)
        pdf.set_text_color(*_AZUL)
        pdf.set_xy(pdf.l_margin + 4, y)
        pdf.cell(largura - 8, 7, rotulo)
        pdf.set_y(y + 8)
        # fio fino abaixo do cabeçalho separa a seção da anterior
        pdf.set_draw_color(200, 205, 215)
        pdf.line(pdf.l_margin, pdf.get_y(),
                 pdf.w - pdf.r_margin, pdf.get_y())
        pdf.set_y(pdf.get_y() + 1.5)
        pdf.set_font("helvetica", "", 10)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(0, 5, _latin1(conteudo), **cell)
        pdf.ln(4)
    return bytes(pdf.output())


def pdf_filename(serial):
    """Nome de arquivo seguro para o download (serial pode ter '/')."""
    seguro = re.sub(r"[^A-Za-z0-9._-]+", "_", serial or "").strip("._")
    return f"relatorio-{seguro or 'equipamento'}.pdf"
