# AXAPI — POST /upgrade/hd (referência)

Comportamento implementado em `a10flash/a10_axapi.py` e coberto por
`tests/test_axapi.py`.

## Fluxo

1. Autenticação: `POST /axapi/v3/auth` com `{"credentials": {...}}`;
   o token volta em `authresponse.signature` e segue em
   `Authorization: A10 <signature>`.
2. Upgrade: `POST /axapi/v3/upgrade/hd` com payload:

   ```json
   {"hd": {"image": "pri|sec", "use-mgmt-port": 1, "file-url": "sftp://..."}}
   ```

   - `image`: slot que recebe a imagem (`pri`/`sec` — o worker calcula a
     partir do slot bootado / `upgrade_slot`).
   - `use-mgmt-port: 1`: o equipamento PUXA a imagem pela porta de
     gerência (o script não precisa saber o IP da caixa para transferir).
   - `file-url`: `sftp://` aceito direto (o ACOS também aceita
     scp/tftp/http).
   - `reboot-after-upgrade: 1` (opcional, enviado com
     `reboot_after_upgrade: true`): o ACOS reinicia SOZINHO assim que a
     imagem é instalada — o worker então aguarda o login e confirma a
     versão, sem set_bootimage/write memory/reboot manuais.

## Síncrono vs 202 + polling

- O POST fica aberto enquanto a caixa copia a imagem (síncrono no ACOS) —
  por isso usa o **timeout do upgrade** (`upgrade_timeout`, nunca o
  default curto de 30s do cliente).
- Resposta `202` = assíncrono: o cliente faz polling de
  `GET /axapi/v3/upgrade-status/oper` a cada `poll_every` (5s), com o
  timeout LONGO também no GET (durante a cópia/instalação a caixa pode
  demorar minutos para responder).

## upgrade-status — códigos

- `status 10` → concluído (sucesso).
- `status > 7` → falha (`AxapiError` com a mensagem).
- demais status → em andamento (ex.: `5` = copiando/instalando).

## on_progress

`on_progress(status, message, elapsed)` é chamado a cada MUDANÇA de
status/mensagem e como heartbeat a cada ~30s de espera.

## Perda de conexão no meio do upgrade

- Com `reboot-after-upgrade: 1`: tolerada — a caixa provavelmente está
  REINICIANDO após instalar; o polling continua até o deadline e, se
  ainda não respondeu, levanta `AxapiError("não confirmado")`. O worker
  trata essa mensagem como "provavelmente reiniciou" e segue para
  aguardar o login e confirmar a versão.
- Sem a flag: a falha de conexão é levantada imediatamente (sem mascarar
  o erro).

## Finalização (reboot controlado pelo script)

Quando `reboot_after_upgrade` está desligado (ou `upgrade_slot: auto`):
`set_bootimage` (POST `/bootimage`), `write memory` (POST `/write/memory`),
`reboot` (POST `/reboot`) e `logoff` (POST `/logoff`).
