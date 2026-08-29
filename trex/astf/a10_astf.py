#!/usr/bin/env python3
"""Mix ASTF bidirecional para teste de CGNAT/LSN em A10.

Porta lógica 0: inside/clientes  -> gateway 10.255.0.1
Porta lógica 1: outside/servidor -> gateway 10.255.0.5

Redes emuladas:
  clientes:   100.64.1.1-100.64.1.254
  servidores: 198.18.1.1-198.18.1.254

O CPS informado é dividido entre HTTP, TCP/443, DNS/UDP e UDP/443.
"""

import argparse

from trex.astf.api import *


class A10InternetMix:
    @staticmethod
    def tcp_template(ip_gen, port, cps, request, response, name):
        client = ASTFProgram(stream=True)
        client.send(request)
        client.recv(len(response))

        server = ASTFProgram(stream=True)
        server.recv(len(request))
        server.send(response)

        return ASTFTemplate(
            client_template=ASTFTCPClientTemplate(
                program=client,
                ip_gen=ip_gen,
                port=port,
                cps=cps,
            ),
            server_template=ASTFTCPServerTemplate(
                program=server,
                assoc=ASTFAssociationRule(port=port),
            ),
            tg_name=name,
        )

    @staticmethod
    def udp_template(ip_gen, port, cps, request, response, name):
        client = ASTFProgram(stream=False)
        client.send_msg(request)
        client.recv_msg(1)

        server = ASTFProgram(stream=False)
        server.recv_msg(1)
        server.send_msg(response)

        return ASTFTemplate(
            client_template=ASTFTCPClientTemplate(
                program=client,
                ip_gen=ip_gen,
                port=port,
                cps=cps,
            ),
            server_template=ASTFTCPServerTemplate(
                program=server,
                assoc=ASTFAssociationRule(port=port),
            ),
            tg_name=name,
        )

    def get_profile(self, tunables, **kwargs):
        parser = argparse.ArgumentParser(
            description="Mix HTTP/HTTPS/DNS/QUIC para CGNAT A10",
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )
        parser.add_argument("--cps", type=float, default=20.0,
                            help="Total aproximado de novas sessoes por segundo")
        parser.add_argument("--http_size", type=int, default=32768,
                            help="Corpo da resposta HTTP em bytes")
        parser.add_argument("--https_size", type=int, default=32768,
                            help="Resposta TCP/443 em bytes")
        parser.add_argument("--client_start", default="100.64.1.1")
        parser.add_argument("--client_end", default="100.64.1.254")
        parser.add_argument("--server_start", default="198.18.1.1")
        parser.add_argument("--server_end", default="198.18.1.254")
        args = parser.parse_args(tunables)

        if args.cps <= 0:
            parser.error("--cps deve ser maior que zero")
        if args.http_size < 1 or args.https_size < 1:
            parser.error("Os tamanhos das respostas devem ser maiores que zero")

        client_dist = ASTFIPGenDist(
            ip_range=[args.client_start, args.client_end],
            distribution="seq",
        )
        server_dist = ASTFIPGenDist(
            ip_range=[args.server_start, args.server_end],
            distribution="seq",
        )
        ip_gen = ASTFIPGen(
            glob=ASTFIPGenGlobal(ip_offset="1.0.0.0"),
            dist_client=client_dist,
            dist_server=server_dist,
        )

        http_request = (
            b"GET /index.html HTTP/1.1\r\n"
            b"Host: www.trex-test.invalid\r\n"
            b"User-Agent: Mozilla/5.0 TRex-CGNAT\r\n"
            b"Accept: text/html,*/*\r\n"
            b"Connection: close\r\n\r\n"
        )
        http_body = b"H" * args.http_size
        http_response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/html\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n\r\n" % len(http_body)
        ).encode("ascii") + http_body

        # Payload com aspecto de TLS. O objetivo é exercitar uma sessão TCP/443;
        # o TRex não precisa implementar criptografia para testar o estado do NAT.
        tls_request = b"\x16\x03\x01\x00\x2f" + (b"C" * 47)
        tls_record_size = min(args.https_size, 65535)
        tls_response = b"\x16\x03\x03" + tls_record_size.to_bytes(2, "big") + (b"S" * args.https_size)

        # Consulta/resposta DNS binária simples para A/IN.
        dns_request = (
            b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
            b"\x03www\x04test\x07invalid\x00\x00\x01\x00\x01"
        )
        dns_response = (
            b"\x12\x34\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"
            b"\x03www\x04test\x07invalid\x00\x00\x01\x00\x01"
            b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04"
            b"\xc6\x12\x01\x0a"
        )

        quic_request = b"Q" * 1200
        quic_response = b"R" * 4096

        templates = [
            self.tcp_template(ip_gen, 80, args.cps * 0.30,
                              http_request, http_response, "http-tcp80"),
            self.tcp_template(ip_gen, 443, args.cps * 0.45,
                              tls_request, tls_response, "https-like-tcp443"),
            self.udp_template(ip_gen, 53, args.cps * 0.15,
                              dns_request, dns_response, "dns-udp53"),
            self.udp_template(ip_gen, 4433, args.cps * 0.10,
                              quic_request, quic_response, "quic-like-udp4433"),
        ]

        return ASTFProfile(default_ip_gen=ip_gen, templates=templates)


def register():
    return A10InternetMix()