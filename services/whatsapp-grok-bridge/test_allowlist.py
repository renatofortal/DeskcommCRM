import unittest

from server import PHONE_RE, is_allowed, mask_phone


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


if __name__ == "__main__":
    unittest.main()
