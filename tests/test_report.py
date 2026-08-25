"""Testes do relatório em PDF gerado por LLM (a10flash/report.py).

A chamada HTTP ao DeepSeek é a única fronteira mockada (sem rede nos
testes); o resto — montagem do prompt, parsing e PDF — é código real.
"""

import json
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import urllib.error  # noqa: E402

from a10flash import report  # noqa: E402


def _record():
    """Registro típico do DeviceStore (o que o portal manda para o LLM)."""
    return {
        "serial": "A10TH-ABC123",
        "device_key": "ttyUSB0",
        "port": "/dev/ttyUSB0",
        "model": "TH5430S",
        "version": "5.2.1-P14",
        "upgraded": True,
        "status": "success",
        "agent": "lab-1",
        "license_info": "License Type: STANDARD\nRenewal date: 2027-01-01",
        "environment": "Fan 1: OK\nPSU 1: OK\nTemp: 38C",
        "version_output": "ACOS version 5.2.1-P14",
    }


ANALISE = {
    "titulo": "Relatório do equipamento A10TH-ABC123",
    "resumo": "Equipamento em bom estado geral.",
    "identificacao": "TH5430S, serial A10TH-ABC123.",
    "firmware": "ACOS 5.2.1-P14, atualizado no ciclo.",
    "licencas": "License Type: STANDARD ativa, renovação 2027.",
    "hardware": "Fan 1: OK, fonte OK, temperatura 38°C.",
    "recomendacoes": "Manter o monitoramento de rotina.",
}


def _fake_resp(data):
    class Resp:
        def read(self):
            return data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return Resp()


def _llm_payload(analysis=None, content=None):
    """Resposta OpenAI-compatível do DeepSeek (choices[0].message.content)."""
    if content is None:
        content = json.dumps(analysis or ANALISE)
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")


def _patch_urlopen(fn):
    old = report.urllib.request.urlopen
    report.urllib.request.urlopen = fn
    return old


# ------------------------------------------------------------- PDF
def _pdf_texto(pdf_bytes):
    """Texto dos streams do PDF (descomprime flate — fpdf2 comprime)."""
    s = pdf_bytes.decode("latin-1")
    partes = []
    pos = 0
    while True:
        i = s.find("stream", pos)
        if i < 0:
            break
        ini = i + 6
        if s[ini] in "\r\n":
            ini += 1
            if s[ini] == "\n":
                ini += 1
        fim = s.find("endstream", ini)
        if fim < 0:
            break
        raw = s[ini:fim]
        try:
            partes.append(zlib.decompress(raw.encode("latin-1"))
                          .decode("latin-1"))
        except zlib.error:
            partes.append(raw)  # stream sem compressão
        pos = fim
    return "".join(partes)


def test_build_pdf_gera_pdf_valido_com_secoes_e_acentos():
    """build_pdf devolve bytes de PDF com as seções da análise (pt-BR)."""
    pdf = report.build_pdf(ANALISE)
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 500
    texto = _pdf_texto(pdf)
    assert "Relatório do equipamento A10TH-ABC123" in texto
    assert "temperatura 38" in texto
    assert "recomenda" in texto.lower()


def test_build_pdf_substitui_caracteres_fora_do_latin1():
    """Texto com aspas curvas vira latin-1 (core fonts não têm Unicode)."""
    analise = {**ANALISE, "resumo": "“Equipamento OK” — ver detalhes."}
    texto = _pdf_texto(report.build_pdf(analise))
    assert '"Equipamento OK"' in texto
    assert "- ver detalhes." in texto


# ------------------------------------------------------- prompt/LLM
def test_analyze_with_llm_envia_payload_correto_e_parseia_json():
    """Monta o prompt com os dados brutos, chama o DeepSeek e devolve a análise."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["headers"] = dict(req.headers)
        captured["timeout"] = timeout
        return _fake_resp(_llm_payload())

    old = _patch_urlopen(fake_urlopen)
    try:
        out = report.analyze_with_llm(
            _record(), {"api_key": "sk-teste", "base_url": "https://api.deepseek.com",
                        "model": "deepseek-v4-flash", "timeout": 45})
    finally:
        report.urllib.request.urlopen = old

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["timeout"] == 45
    payload = captured["payload"]
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["response_format"]["type"] == "json_object"
    assert captured["headers"].get("Authorization") == "Bearer sk-teste"
    # prompt: instruções em system + dados do equipamento em user
    assert payload["messages"][0]["role"] == "system"
    user = payload["messages"][-1]
    assert user["role"] == "user"
    for esperado in ("A10TH-ABC123", "TH5430S", "5.2.1-P14",
                     "Fan 1: OK", "License Type: STANDARD"):
        assert esperado in user["content"]
    # resposta vira o dict da análise
    assert out == ANALISE


def test_analyze_with_llm_aceita_fences_markdown_no_conteudo():
    """Conteúdo ```json ... ``` (LLMs às vezes envolvem) é tolerado."""
    content = "```json\n" + json.dumps(ANALISE) + "\n```"

    def fake_urlopen(req, timeout=None):
        return _fake_resp(_llm_payload(content=content))

    old = _patch_urlopen(fake_urlopen)
    try:
        assert report.analyze_with_llm(_record(), {"api_key": "sk"}) == ANALISE
    finally:
        report.urllib.request.urlopen = old


def test_analyze_with_llm_sem_chave_levanta_503():
    """Sem api_key (config ausente) -> ReportError com status 503."""
    try:
        report.analyze_with_llm(_record(), {})
        raise AssertionError("deveria ter levantado ReportError")
    except report.ReportError as exc:
        assert exc.status == 503
        assert "api" in str(exc).lower()


def test_analyze_with_llm_resposta_sem_json_valido_levanta_502():
    """LLM devolve texto fora do JSON -> ReportError com status 502."""
    def fake_urlopen(req, timeout=None):
        return _fake_resp(_llm_payload(content="desculpe, não consigo"))

    old = _patch_urlopen(fake_urlopen)
    try:
        report.analyze_with_llm(_record(), {"api_key": "sk"})
        raise AssertionError("deveria ter levantado ReportError")
    except report.ReportError as exc:
        assert exc.status == 502
    finally:
        report.urllib.request.urlopen = old


def test_analyze_with_llm_falha_de_rede_levanta_502():
    """Erro de rede/HTTP vira ReportError com status 502."""
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("sem conexão")

    old = _patch_urlopen(fake_urlopen)
    try:
        report.analyze_with_llm(_record(), {"api_key": "sk"})
        raise AssertionError("deveria ter levantado ReportError")
    except report.ReportError as exc:
        assert exc.status == 502
        assert "sem conex" in str(exc)
    finally:
        report.urllib.request.urlopen = old


# ------------------------------------------------------- nome do arquivo
def test_pdf_filename_sanitiza_serial():
    """Serial com caracteres inválidos em nome de arquivo vira nome seguro."""
    assert report.pdf_filename("A10TH-ABC123") == "relatorio-A10TH-ABC123.pdf"
    feio = report.pdf_filename("port:/dev/ttyUSB0")
    assert "/" not in feio and ":" not in feio
    assert feio.endswith(".pdf")
    assert report.pdf_filename("") == "relatorio-equipamento.pdf"
