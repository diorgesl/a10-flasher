"""a10flash — automação de upgrade/factory-reset de A10 Thunder (ACOS).

Detecta equipamentos plugados na porta serial (/dev/ttyUSB*), faz login,
verifica a versão ACOS, atualiza o firmware (SCP via AXAPI na porta de
gerência) e aplica factory reset — tudo sem intervenção manual.
"""

__version__ = "0.1.0"
