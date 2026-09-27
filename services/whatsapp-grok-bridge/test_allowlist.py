import unittest

from server import PHONE_RE, is_allowed, mask_phone, mcp_message, mcp_tools, summarize_groups


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
        self.assertEqual(len(listed["result"]["tools"]), 8)

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


if __name__ == "__main__":
    unittest.main()
