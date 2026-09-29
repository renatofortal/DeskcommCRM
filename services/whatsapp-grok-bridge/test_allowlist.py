import json
import unittest

from server import (
    BridgeError,
    OAuthDesk,
    PHONE_RE,
    _receipt_filter,
    _waha_message_id,
    achar_conta,
    contas_para_buscar,
    aplicar_marcacoes,
    confirmacao_de_grupo,
    id_bate,
    id_de_envio,
    quem_leu_no_grupo,
    referencia_de_grupo,
    rotulo_do_leitor,
    audio_blocks,
    chat_id_com_lid,
    confirmacao_de,
    Conta,
    id_publico,
    image_blocks,
    is_allowed,
    formato_de_audio,
    mask_phone,
    transcricao_pronta,
    montar_contas,
    mcp_message,
    mcp_tools,
    pkce_s256,
    summarize_group_messages,
    summarize_groups,
)


class AllowlistTest(unittest.TestCase):
    def test_so_leitura_e_envio(self):
        self.assertTrue(is_allowed("GET", "/v1/messages"))
        self.assertTrue(is_allowed("GET", "/v1/session"))
        self.assertTrue(is_allowed("POST", "/v1/messages"))
        self.assertTrue(is_allowed("GET", "/v1/groups"))
        self.assertTrue(is_allowed("POST", "/v1/groups/1/participants/add"))
        self.assertTrue(is_allowed("GET", "/v1/contacts/all"))
        self.assertTrue(is_allowed("POST", "/v1/labels"))
        self.assertTrue(is_allowed("POST", "/v1/status/text"))
        self.assertTrue(is_allowed("POST", "/v1/sendText"))
        self.assertFalse(is_allowed("POST", "/v1/sessions/x/logout"))
        self.assertFalse(is_allowed("DELETE", "/v1/sessions/x"))
        self.assertFalse(is_allowed("GET", "/v1/keys"))
        self.assertFalse(is_allowed("PUT", "/v1/profile"))
        self.assertFalse(is_allowed("POST", "/api/sendText"))

    def test_numero_direto(self):
        self.assertTrue(PHONE_RE.match("5511999999999"))
        self.assertFalse(PHONE_RE.match("0123"))
        self.assertFalse(PHONE_RE.match("5511999999999@g.us"))

    def test_mascara(self):
        self.assertEqual(mask_phone("5511999999999"), "5511****99")
        self.assertNotIn("999999", mask_phone("5511999999999"))

    def test_aviso_de_falha_nao_e_transcricao(self):
        self.assertFalse(transcricao_pronta(""))
        self.assertFalse(transcricao_pronta("[o cliente enviou uma mídia que não consegui interpretar]"))
        self.assertTrue(transcricao_pronta("pode me ligar amanha"))

    def test_formato_do_audio_do_whatsapp(self):
        self.assertEqual(formato_de_audio("audio/ogg; codecs=opus"), "ogg")
        self.assertEqual(formato_de_audio("audio/mpeg"), "mp3")


class McpTest(unittest.TestCase):
    def test_ferramentas_sem_sessao_nem_logout(self):
        names = [tool["name"] for tool in mcp_tools()]
        self.assertIn("whatsapp_grupos", names)
        self.assertIn("whatsapp_contatos", names)
        self.assertIn("whatsapp_etiquetas", names)
        self.assertIn("whatsapp_enviar_texto", names)
        self.assertNotIn("logout", " ".join(names))

    def test_initialize_e_lista(self):
        status, payload = mcp_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            lambda name, arguments: (_ for _ in ()).throw(AssertionError("nao devia chamar ferramenta")),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["serverInfo"]["name"], "whatsapp-assistente")
        status, listed = mcp_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, lambda name, arguments: "")
        self.assertEqual(len(listed["result"]["tools"]), 12)
        texto = json.dumps(listed["result"]["tools"], ensure_ascii=False)
        self.assertNotIn("Nao ha outra sessao", texto)
        self.assertNotIn("5585992001234", texto)
        for tool in listed["result"]["tools"]:
            self.assertIn("conta", tool["inputSchema"]["properties"])

    def test_aviso_sem_corpo(self):
        status, payload = mcp_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, lambda name, arguments: "")
        self.assertEqual(status, 202)
        self.assertIsNone(payload)

    def test_grupos_sem_telefones(self):
        brief = summarize_groups(
            {
                "120363@g.us": {
                    "id": "120363@g.us",
                    "subject": "Equipe",
                    "participants": [{"id": "5585992001234@c.us"}],
                }
            }
        )
        self.assertEqual(brief["meta"]["count"], 1)
        self.assertEqual(brief["data"][0]["nome"], "Equipe")
        self.assertNotIn("5585992001234", str(brief))

    def test_audio_leva_arquivo_e_transcricao(self):
        blocks = audio_blocks({"transcricao": "bom dia"}, b"abc", "audio/ogg")
        self.assertEqual(blocks[0]["type"], "text")
        self.assertIn("bom dia", blocks[0]["text"])
        self.assertEqual(blocks[1]["type"], "audio")
        self.assertEqual(blocks[1]["mimeType"], "audio/ogg")
        self.assertNotIn("abc", blocks[0]["text"])

    def test_imagem_leva_arquivo(self):
        blocks = image_blocks({"legenda": "print"}, b"jpg", "image/jpeg")
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["mimeType"], "image/jpeg")
        self.assertNotIn("jpg", blocks[0]["text"])


class ConfirmacaoTest(unittest.TestCase):
    def test_entregue_e_lida(self):
        self.assertEqual(confirmacao_de(2, "delivered", "2026-09-27T00:00:00Z", None)["confirmacao"], "entregue")
        self.assertEqual(confirmacao_de(3, "read", "2026-09-27T00:00:00Z", "2026-09-27T00:01:00Z")["confirmacao"], "lida")
        self.assertEqual(confirmacao_de(4, "read", None, None)["confirmacao"], "ouvida")
        self.assertEqual(confirmacao_de(1, "sent", None, None)["confirmacao"], "enviada")
        self.assertEqual(confirmacao_de(-1, "failed", None, None)["confirmacao"], "falhou")

    def test_id_do_envio_e_destino_com_lid(self):
        self.assertEqual(_waha_message_id({"key": {"id": "3EB0ABCDEF1234567890"}}), "3EB0ABCDEF1234567890")
        composto = "true_5500000000000@c.us_3EB0ABCDEF1234567890"
        self.assertEqual(id_publico(composto), "3EB0ABCDEF1234567890")
        self.assertNotIn("@", id_publico(composto) or "")
        self.assertIn("external_id.like.*_3EB0ABCDEF1234567890", _receipt_filter("3EB0ABCDEF1234567890"))
        destino = chat_id_com_lid(
            "5511999999999",
            [{"phone_number": "+5511999999999", "wa_lid": "123456789012345"}],
        )
        self.assertEqual(destino, "123456789012345@lid")

    def test_mensagem_de_grupo_nao_leva_telefone(self):
        bruto = {
            "key": {"id": "3EB0ABCDEF1234567890", "fromMe": False, "remoteJid": "120363000000000000@g.us", "participant": "5511999999999@c.us"},
            "messageTimestamp": 1700000000,
            "pushName": "Ana",
            "message": {"conversation": "bom dia"},
        }
        saida = summarize_group_messages([bruto])["data"][0]
        self.assertEqual(saida["de"], "Ana")
        self.assertEqual(saida["texto"], "bom dia")
        self.assertEqual(saida["id"], "3EB0ABCDEF1234567890")
        self.assertNotIn("5511", json.dumps(saida))

    def test_marcacao_vira_jid_e_grupo_guarda_o_id(self):
        texto, jids = aplicar_marcacoes("oi @5511999999999", ["123456789012345@lid"])
        self.assertEqual(jids, ["123456789012345@lid"])
        self.assertIn("@123456789012345", texto)
        self.assertNotIn("@5511999999999", texto)
        composto = "true_120363000000000000@g.us_3EB0ABCDEF1234567890"
        self.assertEqual(id_publico(composto), composto)
        chat, ids = referencia_de_grupo(composto)
        self.assertEqual(chat, "120363000000000000@g.us")
        self.assertEqual(ids[0], composto)
        curto = "3EB0ABCDEF1234567890"
        self.assertEqual(id_de_envio("120363000000000000@g.us", curto), composto)
        self.assertTrue(id_bate(curto, composto))
        recibo = confirmacao_de_grupo(
            {
                "ack": 0,
                "ackName": "PENDING",
                "_data": {
                    "userReceipt": [
                        {"userJid": "111111111111111@lid", "readTimestamp": 10},
                        {"userJid": "222222222222222@lid", "receiptTimestamp": 9},
                    ]
                },
            }
        )
        self.assertEqual(recibo["confirmacao"], "lida")
        self.assertEqual(recibo["lida_por"], 1)
        self.assertEqual(recibo["entregue_para"], 2)
        self.assertNotIn("111111111111111", json.dumps(recibo))
        rotulos = {
            "111111111111111@lid": rotulo_do_leitor(
                "111111111111111@lid",
                "5511999999999@s.whatsapp.net",
                {"111111111111111": "Ana"},
                {},
                {},
            ),
            "222222222222222@lid": rotulo_do_leitor(
                "222222222222222@lid",
                "5511888887777@s.whatsapp.net",
                {},
                {},
                {},
            ),
        }
        leitores = quem_leu_no_grupo(recibo and {
            "_data": {
                "userReceipt": [
                    {"userJid": "111111111111111@lid", "readTimestamp": 10},
                    {"userJid": "222222222222222@lid", "receiptTimestamp": 9},
                    {"userJid": "999999999999999@lid", "readTimestamp": 8},
                ]
            }
        }, rotulos, {"999999999999999@lid", "999999999999999"})
        self.assertEqual(leitores["quem_leu"], ["Ana"])
        self.assertEqual(leitores["quem_recebeu"], ["5511888887777"])
        self.assertNotIn("999999999999999", json.dumps(leitores))
        self.assertNotIn("@", json.dumps(leitores))


class OAuthTest(unittest.TestCase):
    def _query(self, challenge: str, redirect: str = "https://grok.com/connectors-oauth-exchange-code/") -> dict:
        return {
            "response_type": "code",
            "client_id": "grok",
            "redirect_uri": redirect,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "abc",
        }

    def test_aceita_retorno_do_aplicativo(self):
        desk = OAuthDesk("token-de-teste")
        status, page = desk.page(self._query(pkce_s256("verificador-valido-123456"), "https://www.cursor.com/agents/mcp/oauth/callback"))
        self.assertEqual(status, 200)
        self.assertIn("Conectar", page)
        status, page = desk.page(self._query(pkce_s256("verificador-valido-123456"), "http://127.0.0.1:8787/callback"))
        self.assertEqual(status, 200)

    def test_recusa_retorno_fora_do_grok(self):
        desk = OAuthDesk("token-de-teste")
        status, page = desk.page(self._query(pkce_s256("verificador-valido-123456"), "https://example.com/callback"))
        self.assertEqual(status, 400)
        self.assertIn("Grok", page)

    def test_troca_codigo_por_acesso(self):
        desk = OAuthDesk("token-de-teste")
        verifier = "verificador-valido-1234567890"
        form = self._query(pkce_s256(verifier))
        form["senha"] = "token-de-teste"
        status, location, _page = desk.approve(form)
        self.assertEqual(status, 302)
        code = dict(pair.split("=", 1) for pair in location.split("?", 1)[1].split("&"))["code"]
        status, body = desk.exchange(
            {
                "grant_type": "authorization_code",
                "client_id": "grok",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": form["redirect_uri"],
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["token_type"], "Bearer")
        self.assertEqual(body["access_token"], "token-de-teste")
        again, _ = desk.exchange(
            {
                "grant_type": "authorization_code",
                "client_id": "grok",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": form["redirect_uri"],
            }
        )
        self.assertEqual(again, 400)


class ContasTest(unittest.TestCase):
    def test_padrao_e_segunda_pelo_ddd(self):
        chaves = {"/k85": "chave-85", "/k11": "chave-11"}
        padrao, contas = montar_contas(
            {
                "BRIDGE_TOKEN": "t",
                "WAHA_SESSION": "org_um",
                "WAHA_KEY_FILE": "/k85",
                "WAHA_CONTA": "85",
                "WAHA_CONTAS": "11|org_dois|/k11",
            },
            ler=chaves.get,
        )
        self.assertEqual(padrao.rotulo, "85")
        self.assertEqual(achar_conta(contas, None, padrao).session, "org_um")
        self.assertEqual(achar_conta(contas, "11", padrao).session, "org_dois")
        self.assertEqual(achar_conta(contas, "5511", padrao).rotulo, "11")
        self.assertEqual(achar_conta(contas, "org_dois", padrao).key, "chave-11")
        with self.assertRaises(BridgeError):
            achar_conta(contas, "21", padrao)

    def test_sem_extra_continua_uma_conta(self):
        padrao, contas = montar_contas(
            {"BRIDGE_TOKEN": "t", "WAHA_SESSION": "org_um", "WAHA_KEY_FILE": "/k"},
            ler=lambda _path: "chave",
        )
        self.assertEqual(len(contas), 1)
        self.assertEqual(padrao.rotulo, "principal")

    def test_busca_a_conta_atual_e_depois_a_outra(self):
        contas = [Conta("85", "org_um", "a"), Conta("11", "org_dois", "b")]
        ordem = contas_para_buscar(contas, "org_dois")
        self.assertEqual([conta.rotulo for conta in ordem], ["11", "85"])
        self.assertEqual([conta.rotulo for conta in contas_para_buscar(contas, "org_um")], ["85", "11"])


if __name__ == "__main__":
    unittest.main()
