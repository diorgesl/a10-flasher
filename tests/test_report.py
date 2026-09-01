"""Testes do relatório em PDF gerado por LLM (a10flash/report.py).

A chamada HTTP ao DeepSeek é a única fronteira mockada (sem rede nos
testes); o resto — montagem do prompt, parsing e PDF — é código real.
"""

import json
import os
import re
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
        "interfaces": "Port  Status\n1/1   UP\n1/2   UP",
    }


ANALISE = {
    "titulo": "Relatório do equipamento A10TH-ABC123",
    "resumo": "Equipamento operacional, testes concluídos com sucesso.",
    "identificacao": "TH5430S, serial A10TH-ABC123.",
    "firmware": "ACOS 5.2.1-P14, atualizado no ciclo.",
    "interfaces": "Todas as interfaces UP (2/2).",
    "licencas": "License Type: STANDARD ativa, renovação 2027.",
    "hardware": "Ventoinhas funcionando bem, fontes e temperatura normais.",
    "hardware_table": [
        {"item": "Ventoinha 1", "status": "OK"},
        {"item": "Ventoinha 2", "status": "OK"},
        {"item": "Fonte 1", "status": "OK"},
        {"item": "Temperatura", "status": "OK"},
    ],
    "burnin": "Teste de carga TRex aprovado: 24h a 1000 cps, pico de 1.20 Gbps.",
    "uptime": "1d 1h 1m",
    "kpis": [
        {"rotulo": "Uptime máximo", "valor": "1d 1h 1m"},
        {"rotulo": "Modelo", "valor": "TH5430S"},
        {"rotulo": "Carga máxima", "valor": "1.20 Gbps"},
        {"rotulo": "Licenças", "valor": "Ativa"},
    ],
    "aprovacao": "Equipamento OPERACIONAL e APROVADO para operação.",
    "aprovado": True,
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
    """Texto dos streams do PDF (descomprime flate — fpdf2 comprime).

    Usa o `/Length N` de cada stream (tamanho comprimido exato): a busca
    sequencial por "stream" quebra em PDFs multipágina, porque dados
    comprimidos podem conter a sequência "stream"/"endstream" por acaso.
    """
    s = pdf_bytes.decode("latin-1")
    partes = []
    for m in re.finditer(r"/Length\s+(\d+)\s+>>\s*stream\r?\n", s):
        n = int(m.group(1))
        raw = s[m.end():m.end() + n]
        try:
            partes.append(zlib.decompress(raw.encode("latin-1"))
                          .decode("latin-1"))
        except zlib.error:
            partes.append(raw)  # stream sem compressão
    return "".join(partes)


def test_build_pdf_gera_pdf_valido_com_secoes_e_acentos():
    """build_pdf devolve bytes de PDF com as seções e o selo (pt-BR)."""
    pdf = report.build_pdf(ANALISE)
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 500
    texto = _pdf_texto(pdf)
    assert "Relatório do equipamento A10TH-ABC123" in texto
    assert "1.20 Gbps" in texto
    assert "1d 1h 1m" in texto
    # seções novas do relatório de aprovação operacional
    # (parênteses escapados \( \) no stream do PDF — sem eles na busca)
    for rotulo in ("Interfaces", "Hardware", "Teste de carga",
                   "Maior uptime registrado", "Aprovação"):
        assert rotulo in texto
    # cards de indicadores + tabela do hardware
    assert "Uptime máximo" in texto
    assert "Modelo" in texto and "Carga máxima" in texto
    assert "Situação" in texto
    assert "Ventoinha 1" in texto
    # selo de aprovação (verde) quando o LLM aprovou
    assert "APROVADO PARA OPERAÇÃO" in texto


def test_build_pdf_graficos_usam_series_do_registro():
    """Gráficos desenhados das séries reais do registro (uptime/TRex)."""
    record = {
        "uptime_series": [
            {"ts": 1.0, "uptime_s": 3600},
            {"ts": 2.0, "uptime_s": 7200},
            {"ts": 3.0, "uptime_s": 10800},
        ],
        "burnin_runs": [{
            "run_id": "r1",
            "samples": [
                {"ts": 1.0, "tx_bps": 500000000, "rx_bps": 400000000},
                {"ts": 2.0, "tx_bps": 800000000, "rx_bps": 600000000},
            ],
        }],
    }
    texto = _pdf_texto(report.build_pdf(ANALISE, record))
    assert "Evolução do uptime no modo teste" in texto
    assert "máx: 0d 3h 0m" in texto
    assert "Tráfego no teste TRex" in texto
    assert "máx: 800.0 Mbps" in texto


def test_build_pdf_sem_series_nao_desenha_graficos():
    """Sem registro/séries o PDF sai sem os gráficos, sem quebrar."""
    texto = _pdf_texto(report.build_pdf(ANALISE))
    assert "Evolução do uptime" not in texto
    assert "Tráfego no teste TRex" not in texto


def test_build_pdf_selo_nao_aprovado_quando_aprovado_false():
    """'aprovado': false -> selo vermelho NÃO APROVADO no PDF."""
    analise = {**ANALISE, "aprovado": False,
               "aprovacao": "NÃO APROVADO: teste de carga falhou."}
    texto = _pdf_texto(report.build_pdf(analise))
    assert "NÃO APROVADO" in texto


def test_build_pdf_substitui_caracteres_fora_do_latin1():
    """Texto com aspas curvas vira latin-1 (core fonts não têm Unicode)."""
    analise = {**ANALISE, "resumo": "“Equipamento OK” — ver detalhes."}
    texto = _pdf_texto(report.build_pdf(analise))
    assert '"Equipamento OK"' in texto
    assert "- ver detalhes." in texto


# ------------------------------------------------------- prompt/LLM
def test_build_messages_inclui_interfaces_burnin_e_max_uptime():
    """Prompt traz o show interfaces, os runs de TRex (com carga) e o
    maior uptime do DB, formatados para o LLM."""
    rec = _record()
    rec["max_uptime_s"] = 90061
    rec["burnin_runs"] = [
        {"run_id": "r1", "started_ts": 1754000000.0, "duration_h": 24.0,
         "cps": 1000, "verdict": "pass", "reason": "24h sem reiniciar",
         "traffic": {"tx_bps": 1200000000, "rx_bps": 800000000,
                     "active_sessions": 50000, "errors": 0}},
        {"run_id": "r2", "started_ts": 1755000000.0, "duration_h": 24.0,
         "cps": 1000, "verdict": "fail", "reason": "reiniciou sob carga",
         "traffic": None},
    ]
    user = report._build_messages(rec)[-1]["content"]
    assert "SHOW INTERFACES BRIEF" in user
    assert "1/1   UP" in user
    assert "TESTES DE CARGA (TRex)" in user
    assert "aprovada" in user and "reprovada" in user
    assert "1000 cps" in user
    assert "1.20 Gbps" in user and "800.0 Mbps" in user
    assert "50000" in user and "erros 0" in user
    assert "Maior uptime registrado no modo teste (DB): 1d 1h 1m" in user


def test_build_messages_sem_burnin_avisa_ausencia():
    """Sem runs registrados o prompt diz que não há teste, sem quebrar."""
    rec = _record()
    user = report._build_messages(rec)[-1]["content"]
    assert "nenhum teste de carga TRex registrado" in user
    assert "Maior uptime registrado no modo teste (DB): —" in user


def test_fmt_uptime_formato_acos():
    """Segundos -> '1d 1h 1m' (mesmo formato do `show version`)."""
    assert report._fmt_uptime(90061) == "1d 1h 1m"
    assert report._fmt_uptime(0) == "0d 0h 0m"
    assert report._fmt_uptime(None) is None


def test_normaliza_kpis_carga_vira_gbps_e_interfaces_vira_modelo():
    """KPI de carga mostra o pico de tráfego em Gbps (não cps) e o de
    interfaces é trocado pelo modelo do equipamento."""
    kpis = [
        {"rotulo": "Carga TRex", "valor": "10000 cps"},
        {"rotulo": "Interfaces UP", "valor": "14/14"},
        {"rotulo": "Licenças", "valor": "Ativa"},
    ]
    record = {"model": "TH5430S"}
    runs = [{"traffic": {"tx_bps": 1200000000, "rx_bps": 800000000}}]
    out = report._normaliza_kpis(kpis, record, runs)
    assert out[0] == {"rotulo": "Carga máxima", "valor": "1.20 Gbps"}
    assert out[1] == {"rotulo": "Modelo", "valor": "TH5430S"}
    assert out[2]["rotulo"] == "Licenças"  # inalterado


def test_normaliza_kpis_sem_trafego_mantem_valor_do_llm():
    """Sem pico de tráfego (teste sem stats), o valor do LLM é mantido."""
    kpis = [{"rotulo": "Carga máxima", "valor": "1000 cps"}]
    out = report._normaliza_kpis(kpis, {}, [])
    assert out[0] == {"rotulo": "Carga máxima", "valor": "1000 cps"}


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
