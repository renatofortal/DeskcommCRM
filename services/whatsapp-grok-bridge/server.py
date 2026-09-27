#!/usr/bin/env python3
"""Ponte autenticada entre um cliente externo e um único WhatsApp.

A chave do WAHA fica neste servidor. O cliente só fala com esta ponte.
Ela aceita o histórico do CRM, o envio de mensagens e os recursos de
grupos, contatos, etiquetas e status da sessão configurada. Não escolhe
outra sessão e não executa logout, start, stop, restart, exclusão da
sessão nem troca de chave. Não há rotina de resposta automática.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import hmac
import secrets
import threading
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


def is_mcp(path: str) -> bool:
    clean = _clean_path(path)
    return clean in ("/mcp", "/wa-assistente/mcp")


def mcp_tools() -> list[dict]:
    conta = "Somente o WhatsApp +5585992001234. Nao ha outra sessao."
    return [
        {
            "name": "whatsapp_sessao",
            "description": conta + " Le o estado da conexao. Nao envia mensagem.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "whatsapp_mensagens",
            "description": "Lista as 50 mensagens mais recentes. Audio usa whatsapp_audio com o id. Foto usa whatsapp_imagem com o id.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "whatsapp_audio",
            "description": "Ouve um audio do WhatsApp. Devolve a transcricao e o arquivo. O id e o da mensagem.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "whatsapp_imagem",
            "description": "Ve uma imagem do WhatsApp. Devolve o arquivo da foto ou do print. O id e o da mensagem.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "whatsapp_grupos",
            "description": "Lista os grupos deste WhatsApp: id, nome e quantidade de participantes.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "whatsapp_grupo",
            "description": "Detalha um grupo pelo id terminado em @g.us.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "whatsapp_contatos",
            "description": "Lista a agenda deste WhatsApp. Se a store do motor estiver desligada, devolve esse erro e para.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "whatsapp_etiquetas",
            "description": "Lista as etiquetas deste WhatsApp. Se a store do motor estiver desligada, devolve esse erro e para.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "whatsapp_enviar_texto",
            "description": (
                "Envia um texto por este WhatsApp somente quando a pessoa pediu nesta conversa. "
                "Use to com DDI e DDD para pessoa, ou chat_id terminado em @c.us ou @g.us."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "chat_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
        {
            "name": "whatsapp_status",
            "description": "Publica um status de texto neste WhatsApp somente quando a pessoa pediu nesta conversa. Sem texto, apenas reserva um id e nao publica.",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    ]


def _group_brief(group_id: str, group: object) -> dict:
    if not isinstance(group, dict):
        return {"id": group_id, "nome": "", "participantes": 0}
    participants = group.get("participants")
    size = len(participants) if isinstance(participants, list) else group.get("size") or 0
    return {
        "id": str(group.get("id") or group_id),
        "nome": str(group.get("subject") or group.get("name") or ""),
        "participantes": size,
    }


def summarize_groups(payload: object) -> dict:
    rows = []
    if isinstance(payload, dict):
        source = payload.items()
    elif isinstance(payload, list):
        source = ((str(item.get("id") if isinstance(item, dict) else ""), item) for item in payload)
    else:
        return {"data": [], "meta": {"count": 0}}
    for group_id, group in source:
        if group_id == "error" or not str(group_id).endswith("@g.us") and not isinstance(group, dict):
            continue
        brief = _group_brief(str(group_id), group)
        if brief["id"].endswith("@g.us") or brief["nome"]:
            rows.append(brief)
    return {"data": rows, "meta": {"count": len(rows)}}


CHAT_ID_RE = re.compile(r"^\d{5,30}@(c\.us|g\.us)$")
OAUTH_CLIENT_ID = "grok"
CODE_TTL_S = 120
TOKEN_TTL_S = 90 * 24 * 60 * 60


PUBLIC_BASE = "https://crm.verticesales.com.br"
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def oauth_kind(path: str) -> str | None:
    clean = _clean_path(path)
    for suffix, kind in (
        ("/oauth/authorize", "authorize"),
        ("/oauth/token", "token"),
        ("/oauth/register", "register"),
    ):
        if clean == suffix or clean.endswith(suffix):
            return kind
    return None


def discovery_kind(path: str) -> str | None:
    clean = _clean_path(path)
    if clean in (
        "/.well-known/oauth-authorization-server/wa-assistente",
        "/wa-assistente/.well-known/oauth-authorization-server",
    ):
        return "authorization-server"
    if clean in (
        "/.well-known/oauth-protected-resource/wa-assistente/mcp",
        "/wa-assistente/oauth-protected-resource",
    ):
        return "protected-resource"
    return None


def redirect_allowed(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme == "cursor" and host == "anysphere.cursor-mcp" and parts.path.startswith("/oauth/callback"):
        return True
    if parts.scheme == "http" and host in ("localhost", "127.0.0.1") and parts.path.rstrip("/") == "/callback":
        return True
    if parts.scheme != "https" or not host:
        return False
    return any(host == name or host.endswith("." + name) for name in ("grok.com", "x.ai", "cursor.com"))


def client_id_ok(value: str | None) -> bool:
    return not value or bool(CLIENT_ID_RE.match(value))


def pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OAuthDesk:
    def __init__(self, token: str):
        self.token = token
        self._codes: dict[str, tuple[float, str, str, str]] = {}
        self._lock = threading.Lock()

    def page(self, query: dict[str, str]) -> tuple[int, str]:
        error = self._request_error(query)
        if error:
            return 400, _page("Nao foi possivel abrir a conexao", error)
        return 200, _consent(query)

    def approve(self, form: dict[str, str]) -> tuple[int, str | None, str]:
        error = self._request_error(form)
        if error:
            return 400, None, _page("Nao foi possivel conectar", error)
        given = str(form.get("senha") or "")
        if not given or not hmac.compare_digest(given, self.token):
            return 401, None, _page("Token nao confere", "O token da ponte nao confere. Pegue de novo no terminal e tente outra vez.")
        code = secrets.token_urlsafe(32)
        with self._lock:
            self._drop_expired()
            self._codes[code] = (time.time() + CODE_TTL_S, form["code_challenge"], form["redirect_uri"], form.get("client_id") or "")
        target = urllib.parse.urlsplit(form["redirect_uri"])
        params = dict(urllib.parse.parse_qsl(target.query, keep_blank_values=True))
        params["code"] = code
        if form.get("state"):
            params["state"] = form["state"]
        location = urllib.parse.urlunsplit((target.scheme, target.netloc, target.path, urllib.parse.urlencode(params), ""))
        return 302, location, ""

    def exchange(self, form: dict[str, str]) -> tuple[int, dict]:
        grant = form.get("grant_type") or ""
        if not client_id_ok(form.get("client_id")):
            return 400, {"error": "invalid_client"}
        if grant == "refresh_token":
            if not _refresh_ok(self.token, form.get("refresh_token") or ""):
                return 400, {"error": "invalid_grant"}
            return 200, _token_body(self.token)
        if grant != "authorization_code":
            return 400, {"error": "unsupported_grant_type"}
        code = form.get("code") or ""
        verifier = form.get("code_verifier") or ""
        with self._lock:
            self._drop_expired()
            saved = self._codes.pop(code, None)
        if not saved:
            return 400, {"error": "invalid_grant"}
        _expires, challenge, redirect, saved_client = saved
        if saved_client and form.get("client_id") and form.get("client_id") != saved_client:
            return 400, {"error": "invalid_client"}
        if form.get("redirect_uri") and form.get("redirect_uri") != redirect:
            return 400, {"error": "invalid_grant"}
        try:
            got = pkce_s256(verifier)
        except UnicodeEncodeError:
            return 400, {"error": "invalid_grant"}
        if not hmac.compare_digest(got, challenge.rstrip("=")):
            return 400, {"error": "invalid_grant"}
        return 200, _token_body(self.token)

    def _request_error(self, query: dict[str, str]) -> str | None:
        if not client_id_ok(query.get("client_id")):
            return "O ID do cliente nao serve."
        if query.get("response_type") not in ("", "code"):
            return "O Grok precisa pedir o codigo de autorizacao."
        if query.get("code_challenge_method") not in ("", "S256"):
            return "Este conector so aceita PKCE S256."
        if not query.get("code_challenge"):
            return "Faltou o desafio PKCE."
        if not redirect_allowed(query.get("redirect_uri") or ""):
            return "O retorno precisa ser um endereco https do Grok."
        return None

    def register(self, body: dict) -> tuple[int, dict]:
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris:
            return 400, {"error": "invalid_redirect_uri"}
        if not all(isinstance(item, str) and redirect_allowed(item) for item in uris):
            return 400, {"error": "invalid_redirect_uri"}
        return 201, {
            "client_id": secrets.token_urlsafe(16),
            "redirect_uris": uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }

    def _drop_expired(self) -> None:
        now = time.time()
        self._codes = {key: value for key, value in self._codes.items() if value[0] > now}


def _refresh_ok(token: str, refresh: str) -> bool:
    exp, sep, sig = refresh.partition(".")
    if not sep or not exp.isdigit():
        return False
    if int(exp) < int(time.time()):
        return False
    expected = hmac.new(token.encode("utf-8"), exp.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _token_body(token: str) -> dict:
    exp = int(time.time()) + TOKEN_TTL_S
    sig = hmac.new(token.encode("utf-8"), str(exp).encode("ascii"), hashlib.sha256).hexdigest()
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL_S,
        "refresh_token": f"{exp}.{sig}",
    }


def discovery_document(kind: str) -> dict:
    issuer = PUBLIC_BASE + "/wa-assistente"
    if kind == "protected-resource":
        return {
            "resource": issuer + "/mcp",
            "authorization_servers": [issuer],
            "bearer_methods_supported": ["header"],
        }
    return {
        "issuer": issuer,
        "authorization_endpoint": issuer + "/oauth/authorize",
        "token_endpoint": issuer + "/oauth/token",
        "registration_endpoint": issuer + "/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


def _page(title: str, message: str) -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>"
        + html.escape(title)
        + "</title><body style=\"font-family:sans-serif;max-width:28rem;margin:2rem auto;padding:0 1rem\">"
        + "<h1 style=\"font-size:1.2rem\">"
        + html.escape(title)
        + "</h1><p>"
        + html.escape(message)
        + "</p></body>"
    )


def _consent(query: dict[str, str]) -> str:
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(query.get(key) or "")}">'
        for key in ("client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "response_type", "scope")
    )
    return (
        "<!doctype html><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">"
        "<title>Conectar o Grok</title>"
        "<body style=\"font-family:sans-serif;max-width:28rem;margin:2rem auto;padding:0 1rem\">"
        "<h1 style=\"font-size:1.25rem\">Conectar o Grok a este WhatsApp</h1>"
        "<p>A conta é somente +5585992001234. Cole o token da ponte. Ele não fica gravado nesta página.</p>"
        "<form method=post>"
        + hidden
        + '<label>Token<br><input name=senha type=password autocomplete=off required style="width:100%;padding:.6rem"></label>'
        '<p><button type=submit style="padding:.6rem 1rem">Conectar</button></p>'
        "</form></body>"
    )


def _form_dict(raw: bytes, content_type: str | None) -> dict[str, str]:
    text = raw.decode("utf-8", "replace")
    if content_type and "application/json" in content_type:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): "" if value is None else str(value) for key, value in payload.items()}
    return {key: value for key, value in urllib.parse.parse_qsl(text, keep_blank_values=True)}


def mcp_message(payload: object, invoke) -> tuple[int, dict | None]:
    if not isinstance(payload, dict):
        return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Pedido invalido."}}
    method = str(payload.get("method") or "")
    request_id = payload.get("id")
    if "id" not in payload:
        return 202, None
    if method == "initialize":
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        version = str(params.get("protocolVersion") or "2025-03-26")
        if version not in ("2025-03-26", "2025-06-18"):
            version = "2025-03-26"
        return 200, {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "whatsapp-assistente", "version": "1"},
            },
        }
    if method == "ping":
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": {"tools": mcp_tools()}}
    if method == "tools/call":
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        known = {tool["name"] for tool in mcp_tools()}
        if name not in known:
            return 200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Ferramenta desconhecida."}}
        try:
            content = _as_content(invoke(name, arguments))
        except BridgeError as exc:
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": exc.message}], "isError": True},
            }
        return 200, {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": content, "isError": False},
        }
    return 200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Metodo desconhecido."}}


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
AUDIO_TYPES = {"audio", "ptt", "voice"}
IMAGE_TYPES = {"image", "sticker"}
MAX_AUDIO = 3 * 1024 * 1024
MAX_IMAGE = 4 * 1024 * 1024


def _as_content(value: object) -> list:
    if isinstance(value, list):
        return value
    return [{"type": "text", "text": str(value)}]


def image_blocks(summary: dict, image: bytes | None, mime: str) -> list:
    blocks: list = [{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}]
    if image and mime.startswith("image/"):
        blocks.append({"type": "image", "data": base64.b64encode(image).decode("ascii"), "mimeType": mime.split(";")[0]})
    return blocks


def audio_blocks(summary: dict, audio: bytes | None, mime: str) -> list:
    blocks: list = [{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}]
    if audio and mime.startswith("audio/"):
        blocks.append({"type": "audio", "data": base64.b64encode(audio).decode("ascii"), "mimeType": mime.split(";")[0]})
    return blocks


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
        self.oauth = OAuthDesk(self.token)
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

    def mcp_invoke(self, name: str, arguments: dict) -> str:
        if name == "whatsapp_sessao":
            result = self.session_status()
        elif name == "whatsapp_mensagens":
            result = self.list_messages()
        elif name == "whatsapp_audio":
            return self.audio_message(str(arguments.get("id") or ""))
        elif name == "whatsapp_imagem":
            return self.image_message(str(arguments.get("id") or ""))
        elif name == "whatsapp_grupos":
            result = summarize_groups(self.proxy("GET", "/v1/groups", "/v1/groups", b""))
        elif name == "whatsapp_grupo":
            group_id = str(arguments.get("id") or "")
            if not CHAT_ID_RE.match(group_id) or not group_id.endswith("@g.us"):
                raise BridgeError(400, "Informe o id do grupo terminado em @g.us.")
            result = _group_brief(group_id, self.proxy("GET", "/v1/groups/" + group_id, "/v1/groups/" + group_id, b""))
        elif name == "whatsapp_contatos":
            result = self._mcp_proxy("GET", "/v1/contacts/all")
        elif name == "whatsapp_etiquetas":
            result = self._mcp_proxy("GET", "/v1/labels")
        elif name == "whatsapp_enviar_texto":
            text = str(arguments.get("text") or "").strip()
            chat_id = str(arguments.get("chat_id") or "")
            if chat_id:
                if not CHAT_ID_RE.match(chat_id):
                    raise BridgeError(400, "chat_id precisa terminar em @c.us ou @g.us.")
                if not text or len(text) > 4000:
                    raise BridgeError(400, "Informe text com ate 4000 caracteres.")
                code, sent = self._waha("POST", "/api/sendText", {"session": self.session, "chatId": chat_id, "text": text})
                if code == 403:
                    raise BridgeError(403, "Envio recusado pela chave da sessao.")
                if code not in (200, 201):
                    raise BridgeError(502, "O WhatsApp nao aceitou o envio.")
                result = {"data": {"chat_id": chat_id}}
            else:
                result = self.send_text(json.dumps({"to": arguments.get("to") or "", "text": text}).encode("utf-8"))
        elif name == "whatsapp_status":
            text = str(arguments.get("text") or "").strip()
            if not text:
                result = self._mcp_proxy("GET", "/v1/status/new-message-id")
            else:
                if len(text) > 700:
                    raise BridgeError(400, "O status aceita ate 700 caracteres.")
                result = self._mcp_proxy("POST", "/v1/status/text", json.dumps({"text": text}).encode("utf-8"))
        else:
            raise BridgeError(404, "Ferramenta desconhecida.")
        return json.dumps(result, ensure_ascii=False)

    def _mcp_proxy(self, method: str, path: str, raw: bytes = b"") -> object:
        try:
            return self.proxy(method, path, path, raw)
        except BridgeError as exc:
            if exc.status < 500:
                return {"error": {"message": exc.message}}
            raise

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
            "messages?select=id,sent_at,direction,sent_via,type,body,media_derived_text,"
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
                    "transcricao": row.get("media_derived_text") or "",
                    "contact_name": contact.get("display_name") or "",
                    "contact_phone": contact.get("phone_number") or "",
                }
            )
        return {"data": data, "meta": {"count": len(data)}}

    def audio_message(self, message_id: str) -> list:
        if not UUID_RE.match(message_id):
            raise BridgeError(400, "Informe o id da mensagem.")
        channel_id, org_id = self._channel_ids()
        query = (
            "messages?select=id,type,sent_at,media_mime,media_derived_text,media_derived_status,media_storage_path,"
            "contacts(display_name,phone_number)"
            f"&id=eq.{message_id}"
            f"&channel_session_id=eq.{channel_id}"
            f"&organization_id=eq.{org_id}"
            "&limit=1"
        )
        rows = self._supabase(query)
        if not rows:
            raise BridgeError(404, "Audio nao encontrado neste WhatsApp.")
        row = rows[0]
        kind = str(row.get("type") or "")
        mime = str(row.get("media_mime") or "audio/ogg")
        if kind not in AUDIO_TYPES and not mime.startswith("audio/"):
            raise BridgeError(400, "Essa mensagem nao e um audio.")
        contact = row.get("contacts")
        if isinstance(contact, list):
            contact = contact[0] if contact else {}
        if not isinstance(contact, dict):
            contact = {}
        summary = {
            "id": row.get("id"),
            "sent_at": row.get("sent_at"),
            "contato": contact.get("display_name") or "",
            "telefone": mask_phone(str(contact.get("phone_number") or "")),
            "transcricao": row.get("media_derived_text") or "",
            "transcricao_status": row.get("media_derived_status") or "",
        }
        audio = self._download_media(str(row.get("media_storage_path") or ""), MAX_AUDIO)
        if audio is None and not summary["transcricao"]:
            summary["aviso"] = "O arquivo de audio nao esta guardado."
        elif audio is None:
            summary["aviso"] = "A transcricao esta pronta. O arquivo passou do tamanho que o conector envia."
        return audio_blocks(summary, audio, mime if mime.startswith("audio/") else "audio/ogg")

    def image_message(self, message_id: str) -> list:
        if not UUID_RE.match(message_id):
            raise BridgeError(400, "Informe o id da mensagem.")
        channel_id, org_id = self._channel_ids()
        query = (
            "messages?select=id,type,sent_at,body,media_mime,media_derived_text,media_storage_path,"
            "contacts(display_name,phone_number)"
            f"&id=eq.{message_id}"
            f"&channel_session_id=eq.{channel_id}"
            f"&organization_id=eq.{org_id}"
            "&limit=1"
        )
        rows = self._supabase(query)
        if not rows:
            raise BridgeError(404, "Imagem nao encontrada neste WhatsApp.")
        row = rows[0]
        kind = str(row.get("type") or "")
        mime = str(row.get("media_mime") or "image/jpeg")
        if kind not in IMAGE_TYPES and not mime.startswith("image/"):
            raise BridgeError(400, "Essa mensagem nao e uma imagem.")
        contact = row.get("contacts")
        if isinstance(contact, list):
            contact = contact[0] if contact else {}
        if not isinstance(contact, dict):
            contact = {}
        summary = {
            "id": row.get("id"),
            "sent_at": row.get("sent_at"),
            "contato": contact.get("display_name") or "",
            "telefone": mask_phone(str(contact.get("phone_number") or "")),
            "legenda": row.get("body") or "",
            "descricao": row.get("media_derived_text") or "",
        }
        image = self._download_media(str(row.get("media_storage_path") or ""), MAX_IMAGE)
        if image is None:
            summary["aviso"] = "O arquivo da imagem nao esta guardado ou passou do tamanho que o conector envia."
        return image_blocks(summary, image, mime if mime.startswith("image/") else "image/jpeg")

    def _download_media(self, path: str, limit: int) -> bytes | None:
        if not path or ".." in path or path.startswith("/"):
            return None
        url = self.supabase + "/storage/v1/object/whatsapp-media/" + urllib.parse.quote(path, safe="/")
        req = urllib.request.Request(url)
        req.add_header("apikey", self.service_role)
        req.add_header("Authorization", "Bearer " + self.service_role)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                data = response.read(limit + 1)
        except urllib.error.HTTPError:
            return None
        except urllib.error.URLError:
            return None
        if len(data) > limit:
            return None
        return data

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
            found = discovery_kind(self.path)
            if found and self.command == "GET":
                self._send(200, discovery_document(found))
                return
            kind = oauth_kind(self.path)
            if kind == "register" and self.command == "POST":
                form = _form_dict(raw, self.headers.get("Content-Type"))
                if self.headers.get("Content-Type") and "application/json" in self.headers.get("Content-Type", ""):
                    try:
                        parsed = json.loads(raw.decode("utf-8") or "{}")
                    except json.JSONDecodeError:
                        parsed = {}
                else:
                    parsed = form
                status, payload = bridge.oauth.register(parsed if isinstance(parsed, dict) else {})
                self._send(status, payload)
                return
            if kind == "authorize" and self.command == "GET":
                query = {key: value for key, value in urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)}
                status, page = bridge.oauth.page(query)
                self._html(status, page)
                return
            if kind == "authorize" and self.command == "POST":
                form = _form_dict(raw, self.headers.get("Content-Type"))
                status, location, page = bridge.oauth.approve(form)
                if location:
                    self.send_response(status)
                    self.send_header("Location", location)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._html(status, page)
                return
            if kind == "token" and self.command == "POST":
                form = _form_dict(raw, self.headers.get("Content-Type"))
                status, payload = bridge.oauth.exchange(form)
                self._send(status, payload)
                return
            if is_mcp(self.path) and self.command == "GET":
                self._send(200, {"ok": True, "mcp": True})
                return
            if is_mcp(self.path) and self.command == "POST":
                self._mcp(raw)
                return
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

        def _mcp(self, raw: bytes):
            try:
                bridge.authorize(self.headers.get("Authorization"))
                try:
                    incoming = json.loads(raw.decode("utf-8")) if raw else None
                except (UnicodeDecodeError, json.JSONDecodeError):
                    incoming = None
                status, payload = mcp_message(incoming, bridge.mcp_invoke)
            except BridgeError as exc:
                status = exc.status
                payload = {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": exc.message}}
            if status == 202:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if status == 401:
                self.send_header(
                    "WWW-Authenticate",
                    'Bearer realm="whatsapp", resource_metadata="'
                    + PUBLIC_BASE
                    + '/.well-known/oauth-protected-resource/wa-assistente/mcp"',
                )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, status: int, page: str):
            body = page.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
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
