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
import urllib.request

from fpdf import FPDF

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_TIMEOUT = 120

_SYSTEM_PROMPT = (
    "Você é um especialista em infraestrutura de rede e equipamentos "
    "A10 Thunder (ACOS). Analise os dados do equipamento abaixo e produza "
    "um relatório técnico em português do Brasil.\n"
    "Responda APENAS com um objeto JSON válido, sem texto fora dele, com "
    "exatamente estas chaves (todas strings):\n"
    '- "titulo": título do relatório com o número de série\n'
    '- "resumo": resumo executivo de 2 a 4 frases\n'
    '- "identificacao": modelo, série e identificação do equipamento\n'
    '- "firmware": versão ACOS e situação de atualização\n'
    '- "licencas": análise das licenças (ativas, datas, observações)\n'
    '- "hardware": saúde do hardware (fans, fontes, temperatura) com base '
    "no show environment\n"
    '- "recomendacoes": recomendações práticas\n'
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
                          ("SHOW ENVIRONMENT", "environment")):
        linhas.append(f"=== {titulo} ===\n"
                      + (record.get(campo) or "(saída não coletada)"))
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
    ("licencas", "Licenças"),
    ("hardware", "Saúde do hardware"),
    ("recomendacoes", "Recomendações"),
)


def _latin1(text):
    text = (text or "").translate(_REPLACEMENTS)
    return "".join(
        c if c == "\n" or 32 <= ord(c) < 128 or 160 <= ord(c) < 256
        else "?" for c in text)


def build_pdf(analysis):
    """Gera o PDF do relatório em memória e devolve os bytes."""
    analysis = analysis or {}
    pdf = FPDF()
    pdf.add_page()
    # new_x/new_y: multi_cell(0, ...) deixa o cursor na margem direita —
    # sem voltar para a margem esquerda a próxima célula explode com
    # "Not enough horizontal space".
    cell = dict(new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "B", 16)
    pdf.multi_cell(0, 8, _latin1(analysis.get("titulo")
                                 or "Relatório do equipamento"), **cell)
    pdf.ln(4)
    for chave, rotulo in _SECTIONS:
        pdf.set_font("helvetica", "B", 11)
        pdf.multi_cell(0, 6, rotulo, **cell)
        pdf.set_font("helvetica", "", 10)
        pdf.multi_cell(0, 5, _latin1(analysis.get(chave) or "—"), **cell)
        pdf.ln(3)
    return bytes(pdf.output())


def pdf_filename(serial):
    """Nome de arquivo seguro para o download (serial pode ter '/')."""
    seguro = re.sub(r"[^A-Za-z0-9._-]+", "_", serial or "").strip("._")
    return f"relatorio-{seguro or 'equipamento'}.pdf"
