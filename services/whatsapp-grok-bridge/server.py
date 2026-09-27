#!/usr/bin/env python3
"""Ponte autenticada entre um cliente externo e um único WhatsApp.

A chave do WAHA fica neste servidor. O cliente só fala com esta ponte.
Ela aceita o histórico do CRM, o envio de mensagens e os recursos de
grupos, contatos, etiquetas e status da sessão configurada. Não escolhe
outra sessão e não executa logout, start, stop, restart, exclusão da
sessão nem troca de chave. Não há rotina de resposta automática.
"""
from __future__ import annotations

import json
import os
import re
import hmac
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
PHONE_RE = re.compile(r"^[1-9]\d{9,14}$")
MAX_BODY = 8 * 1024 * 1024
WINDOW_S = 60
MAX_PER_WINDOW = 120
RESOURCE_ROOTS = ("groups", "labels", "status", "chats", "contacts")
CONTACT_QUERY = {"all", "about", "check-exists", "profile-picture", "block", "unblock"}
SEND_POST = {
    "sendText", "sendImage", "sendFile", "sendVoice", "sendVideo",
    "sendLocation", "sendContactVcard", "sendPoll", "sendPollVote",
    "sendSeen", "sendButtons", "sendList", "sendLinkPreview",
    "forwardMessage", "reply", "startTyping", "stopTyping",
    "send/link-custom-preview", "send/buttons/reply",
}
SEND_PUT = {"reaction", "star"}
SEND_GET = {"checkNumberStatus", "messages", "new-message-id"}


class BridgeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def load_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _scrub(text: str) -> str:
    text = re.sub(r"\d{6,}", lambda match: match.group(0)[:2] + "****" + match.group(0)[-2:], text)
    return text[:180]


def mask_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) < 6:
        return "****"
    return digits[:4] + "****" + digits[-2:]


def _clean_path(path: str) -> str:
    path = urllib.parse.urlsplit(path).path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


def is_allowed(method: str, path: str) -> bool:
    path = _clean_path(path)
    if ".." in path or "\\" in path:
        return False
    if path == "/v1/session":
        return method == "GET"
    if path == "/v1/messages":
        return method in ("GET", "POST")
    if not path.startswith("/v1/"):
        return False
    rest = path[4:]
    head = rest.split("/", 1)[0]
    if head in ("sessions", "keys", "server", "profile", "apps", "screenshot"):
        return False
    if head in RESOURCE_ROOTS:
        return method in ("GET", "POST", "PUT", "DELETE")
    if rest in SEND_POST or head in SEND_POST:
        return method == "POST"
    if rest in SEND_PUT or head in SEND_PUT:
        return method == "PUT"
    if rest in SEND_GET or head in SEND_GET:
        return method == "GET"
    return False


class Bridge:
    def __init__(self, env: dict[str, str]):
        self.token = env["BRIDGE_TOKEN"]
        self.session = env["WAHA_SESSION"]
        self.waha_base = env.get("WAHA_BASE", "http://127.0.0.1:3010").rstrip("/")
        self.waha_key = open(env["WAHA_KEY_FILE"], encoding="utf-8").read().strip()
        crm = load_env_file(env.get("CRM_ENV_FILE", "/root/deskcommcrm/.env"))
        self.supabase = crm["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
        self.service_role = crm["SUPABASE_SERVICE_ROLE_KEY"]
        if not SESSION_RE.match(self.session):
            raise SystemExit("sessao invalida")
        if not self.token or not self.waha_key:
            raise SystemExit("credencial ausente")
        self._hits: list[float] = []
        self._channel: tuple[str, str] | None = None

    def authorize(self, header: str | None) -> None:
        if not header or not header.startswith("Bearer "):
            raise BridgeError(401, "Credencial ausente.")
        got = header[7:].strip()
        if not hmac.compare_digest(got, self.token):
            raise BridgeError(401, "Credencial recusada.")
        now = time.monotonic()
        self._hits = [t for t in self._hits if now - t < WINDOW_S]
        if len(self._hits) >= MAX_PER_WINDOW:
            raise BridgeError(429, "Muitas consultas. Espere um minuto.")
        self._hits.append(now)

    def route(self, method: str, target: str, body: bytes | None) -> object:
        path = _clean_path(target)
        if not is_allowed(method, path):
            raise BridgeError(404, "Caminho nao permitido.")
        if method == "GET" and path == "/v1/session":
            return self.session_status()
        if method == "GET" and path == "/v1/messages":
            return self.list_messages()
        if method == "POST" and path == "/v1/messages":
            return self.send_text(body or b"")
        return self.proxy(method, path, target, body or b"")

    def session_status(self) -> dict:
        code, payload = self._waha("GET", f"/api/sessions/{self.session}")
        if code != 200 or not isinstance(payload, dict):
            raise BridgeError(502, "Nao foi possivel ler o estado da sessao.")
        me = payload.get("me") if isinstance(payload.get("me"), dict) else {}
        return {
            "data": {
                "name": self.session,
                "status": payload.get("status"),
                "account": mask_phone(str(me.get("id") or "")),
                "push_name": me.get("pushName") or "",
            }
        }

    def list_messages(self) -> dict:
        channel_id, org_id = self._channel_ids()
        query = (
            "messages?select=id,sent_at,direction,sent_via,type,body,"
            "contacts(display_name,phone_number)"
            f"&channel_session_id=eq.{channel_id}"
            f"&organization_id=eq.{org_id}"
            "&order=sent_at.desc&limit=50"
        )
        rows = self._supabase(query)
        data = []
        for row in rows:
            contact = row.get("contacts")
            if isinstance(contact, list):
                contact = contact[0] if contact else {}
            if not isinstance(contact, dict):
                contact = {}
            data.append(
                {
                    "id": row.get("id"),
                    "sent_at": row.get("sent_at"),
                    "direction": row.get("direction"),
                    "sent_via": row.get("sent_via"),
                    "type": row.get("type"),
                    "body": row.get("body") or "",
                    "contact_name": contact.get("display_name") or "",
                    "contact_phone": contact.get("phone_number") or "",
                }
            )
        return {"data": data, "meta": {"count": len(data)}}

    def send_text(self, raw: bytes) -> dict:
        if len(raw) > MAX_BODY:
            raise BridgeError(413, "Pedido grande demais.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BridgeError(400, "JSON invalido.")
        if not isinstance(payload, dict):
            raise BridgeError(400, "JSON invalido.")
        phone = re.sub(r"\D", "", str(payload.get("to") or ""))
        text = str(payload.get("text") or "").strip()
        if not PHONE_RE.match(phone):
            raise BridgeError(400, "Informe o numero em to, com DDI e DDD, sem texto extra.")
        if not text or len(text) > 4000:
            raise BridgeError(400, "Informe text com ate 4000 caracteres.")
        code, result = self._waha(
            "POST",
            "/api/sendText",
            {"session": self.session, "chatId": f"{phone}@c.us", "text": text},
        )
        if code == 403:
            raise BridgeError(403, "Envio recusado pela chave da sessao.")
        if code not in (200, 201) or not isinstance(result, dict):
            raise BridgeError(502, "O WhatsApp nao aceitou o envio.")
        message_id = result.get("id")
        if isinstance(message_id, dict):
            message_id = message_id.get("id") or message_id.get("_serialized")
        return {"data": {"id": message_id, "to": mask_phone(phone)}}

    def proxy(self, method: str, path: str, target: str, raw: bytes) -> object:
        if len(raw) > MAX_BODY:
            raise BridgeError(413, "Pedido grande demais.")
        rest = path[4:]
        head, _, tail = rest.partition("/")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query, keep_blank_values=False)
        query.pop("session", None)
        if head == "contacts" and (not tail or tail.split("/", 1)[0] in CONTACT_QUERY):
            waha_path = "/api/contacts" + (("/" + tail) if tail else "")
            query["session"] = [self.session]
            payload = self._json_body(raw, inject_session=False)
            if isinstance(payload, dict) and "session" in payload:
                payload["session"] = self.session
        elif head in RESOURCE_ROOTS:
            waha_path = f"/api/{self.session}/{head}" + (("/" + tail) if tail else "")
            payload = self._json_body(raw, inject_session=False)
            if isinstance(payload, dict) and "session" in payload:
                payload["session"] = self.session
        elif rest == "new-message-id":
            waha_path = f"/api/{self.session}/new-message-id"
            payload = None
        else:
            waha_path = "/api/" + rest
            query["session"] = [self.session]
            payload = self._json_body(raw, inject_session=True)
        if payload is None and raw and head in RESOURCE_ROOTS:
            code, result = self._waha(method, waha_path, raw_body=raw, query=query)
        else:
            code, result = self._waha(method, waha_path, body=payload, query=query)
        if code == 403:
            raise BridgeError(403, "Acao recusada para esta sessao.")
        if code >= 400:
            message = "O WhatsApp recusou a consulta."
            if isinstance(result, dict) and result.get("message"):
                message = _scrub(str(result.get("message")))
            raise BridgeError(code if code < 500 else 502, message)
        return result

    def _json_body(self, raw: bytes, inject_session: bool) -> dict | None:
        if not raw:
            return {"session": self.session} if inject_session else None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            raise BridgeError(400, "JSON invalido.")
        if inject_session or "session" in payload:
            payload["session"] = self.session
        return payload

    def _channel_ids(self) -> tuple[str, str]:
        if self._channel:
            return self._channel
        rows = self._supabase(
            "channel_sessions?select=id,organization_id"
            f"&waha_session_name=eq.{urllib.parse.quote(self.session)}&limit=1"
        )
        if not rows:
            raise BridgeError(503, "Canal nao encontrado.")
        self._channel = (rows[0]["id"], rows[0]["organization_id"])
        return self._channel

    def _supabase(self, query: str) -> list:
        req = urllib.request.Request(self.supabase + "/rest/v1/" + query)
        req.add_header("apikey", self.service_role)
        req.add_header("Authorization", "Bearer " + self.service_role)
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise BridgeError(502, "Falha ao ler o historico.") from exc
        if not isinstance(payload, list):
            raise BridgeError(502, "Falha ao ler o historico.")
        return payload

    def _waha(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        raw_body: bytes | None = None,
        query: dict | None = None,
    ) -> tuple[int, object]:
        if query:
            path = path + "?" + urllib.parse.urlencode(query, doseq=True)
        data = raw_body if raw_body is not None else (None if body is None else json.dumps(body).encode("utf-8"))
        req = urllib.request.Request(self.waha_base + path, data=data, method=method)
        req.add_header("X-Api-Key", self.waha_key)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {}
            return exc.code, parsed
        except urllib.error.URLError:
            raise BridgeError(502, "WhatsApp indisponivel.")


def make_handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self._handle()

        def do_POST(self):  # noqa: N802
            self._handle()

        def do_PUT(self):  # noqa: N802
            self._handle()

        def do_DELETE(self):  # noqa: N802
            self._handle()

        def _handle(self):
            length = int(self.headers.get("Content-Length") or "0")
            if length > MAX_BODY:
                self._send(413, {"error": {"message": "Pedido grande demais."}})
                return
            raw = self.rfile.read(length) if length else b""
            try:
                bridge.authorize(self.headers.get("Authorization"))
                payload = bridge.route(self.command, self.path, raw)
                status = 200
                body = json.dumps(payload).encode("utf-8")
            except BridgeError as exc:
                status = exc.status
                body = json.dumps({"error": {"message": exc.message}}).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send(self, status: int, payload: dict):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args):
            return

    return Handler


def main() -> None:
    env = {
        "BRIDGE_TOKEN": os.environ["BRIDGE_TOKEN"],
        "WAHA_SESSION": os.environ["WAHA_SESSION"],
        "WAHA_BASE": os.environ.get("WAHA_BASE", "http://127.0.0.1:3010"),
        "WAHA_KEY_FILE": os.environ["WAHA_KEY_FILE"],
        "CRM_ENV_FILE": os.environ.get("CRM_ENV_FILE", "/root/deskcommcrm/.env"),
    }
    bridge = Bridge(env)
    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("BRIDGE_PORT", "3011"))
    server = ThreadingHTTPServer((host, port), make_handler(bridge))
    server.serve_forever()


if __name__ == "__main__":
    main()
