"""Interfaz web local para el agente con herramientas."""

import argparse
import hmac
import json
import os
import secrets
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow
from itsdangerous import BadSignature, URLSafeTimedSerializer
from oauthlib.oauth2.rfc6749.errors import OAuth2Error

from agente import (
    AgenteAnthropic,
    AgenteOllama,
    MAPA_FUNCIONES,
    crear_base_de_datos,
    seleccionar_modelo_ollama,
)


ROOT = Path(__file__).parent
MAX_REQUEST_SIZE = 16_384
MAX_MESSAGE_LENGTH = 4_000
SESSION_MAX_AGE = 7 * 24 * 60 * 60
OAUTH_STATE_MAX_AGE = 10 * 60
SESSION_COOKIE = "agente_session"
OAUTH_STATE_COOKIE = "google_oauth_state"
GOOGLE_SCOPES = ["openid", "email", "profile"]
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "").strip()
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "")
AUTH_ENABLED = os.getenv(
    "AUTH_ENABLED", "true" if os.getenv("RENDER") else "false"
).strip().lower() in {"1", "true", "yes"}
COOKIE_SECURE = os.getenv(
    "COOKIE_SECURE", "true" if AUTH_ENABLED else "false"
).strip().lower() in {"1", "true", "yes"}
SESSION_SERIALIZER = (
    URLSafeTimedSerializer(APP_SECRET_KEY, salt="agente-session")
    if APP_SECRET_KEY
    else None
)
OAUTH_STATE_SERIALIZER = (
    URLSafeTimedSerializer(APP_SECRET_KEY, salt="agente-google-state")
    if APP_SECRET_KEY
    else None
)


def _google_auth_configured():
    if not all(
        (
            GOOGLE_CLIENT_ID,
            GOOGLE_CLIENT_SECRET,
            GOOGLE_REDIRECT_URI,
            SESSION_SERIALIZER,
            OAUTH_STATE_SERIALIZER,
        )
    ) or len(APP_SECRET_KEY) < 32:
        return False

    try:
        redirect = urlsplit(GOOGLE_REDIRECT_URI)
    except ValueError:
        return False
    valid_callback = (
        redirect.path == "/auth/google/callback"
        and not redirect.query
        and not redirect.fragment
        and bool(redirect.netloc)
    )
    return valid_callback and redirect.scheme == "https"


def _create_google_flow(state=None, code_verifier=None):
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI],
        }
    }
    return Flow.from_client_config(
        client_config,
        scopes=GOOGLE_SCOPES,
        state=state,
        redirect_uri=GOOGLE_REDIRECT_URI,
        code_verifier=code_verifier,
        autogenerate_code_verifier=code_verifier is None,
    )


class WebHandler(BaseHTTPRequestHandler):
    agente_factory = None
    proveedor = ""
    modelo = ""
    auth_enabled = AUTH_ENABLED
    sesiones = {}
    sesiones_lock = threading.Lock()
    agentes = {}
    agente_locks = {}
    agentes_lock = threading.Lock()

    def do_GET(self):
        path = urlsplit(self.path).path

        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return

        if path == "/logo-uanvelon.svg":
            self._send_file("logo-uanvelon.svg", "image/svg+xml; charset=utf-8")
            return

        if path == "/logo-unach.png":
            self._send_file("logo-unach.png", "image/png")
            return

        if path == "/login":
            if not self.auth_enabled:
                self._redirect("/")
            elif self._get_session_user():
                self._redirect("/")
            else:
                self._send_file("login.html", "text/html; charset=utf-8")
            return

        if path in {"/", "/index.html"}:
            if self.auth_enabled and not self._get_session_user():
                self._redirect("/login", [self._cookie(SESSION_COOKIE, "", 0)])
                return
            self._send_file("index.html", "text/html; charset=utf-8")
            return

        if path == "/api/auth-status":
            self._send_json(
                200,
                {
                    "enabled": self.auth_enabled,
                    "configured": _google_auth_configured(),
                },
            )
            return

        if path == "/api/session":
            user = self._get_session_user()
            if user is None:
                self._send_json(401, {"error": "Inicie sesión para continuar."})
                return
            self._send_json(
                200,
                {
                    "authenticated": not user.get("local", False),
                    "local": user.get("local", False),
                    "name": user["name"],
                    "email": user.get("email", ""),
                },
            )
            return

        if path == "/api/status":
            if not self._require_session():
                return
            self._send_json(
                200,
                {
                    "provider": self.proveedor,
                    "model": self.modelo,
                    "tools": list(MAPA_FUNCIONES),
                },
            )
            return

        if path == "/auth/google/start":
            self._start_google_login()
            return

        if path == "/auth/google/callback":
            self._finish_google_login()
            return

        self._send_json(404, {"error": "No se encontró la ruta solicitada."})

    def do_POST(self):
        path = urlsplit(self.path).path

        if path == "/auth/logout":
            user = self._get_session_user()
            if user and not user.get("local"):
                with self.sesiones_lock:
                    self.sesiones.pop(user["sid"], None)
                with self.agentes_lock:
                    self.agentes.pop(user["sid"], None)
                    self.agente_locks.pop(user["sid"], None)
            self._redirect(
                "/login" if self.auth_enabled else "/",
                [
                    self._cookie(SESSION_COOKIE, "", 0),
                    self._cookie(OAUTH_STATE_COOKIE, "", 0),
                ],
            )
            return

        if path != "/api/chat":
            self._send_json(404, {"error": "No se encontró la ruta solicitada."})
            return

        user = self._require_session()
        if not user:
            return

        if self.headers.get_content_type() != "application/json":
            self._send_json(415, {"error": "El contenido debe enviarse como JSON."})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "La longitud de la solicitud no es válida."})
            return

        if content_length <= 0:
            self._send_json(400, {"error": "La solicitud no contiene un mensaje."})
            return
        if content_length > MAX_REQUEST_SIZE:
            self._send_json(413, {"error": "La solicitud excede el tamaño permitido."})
            return

        try:
            data = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "El cuerpo de la solicitud no contiene JSON válido."})
            return

        if not isinstance(data, dict):
            self._send_json(400, {"error": "La solicitud debe ser un objeto JSON."})
            return
        mensaje = data.get("message")
        if not isinstance(mensaje, str) or not mensaje.strip():
            self._send_json(400, {"error": "Escriba una consulta antes de enviarla."})
            return
        if len(mensaje) > MAX_MESSAGE_LENGTH:
            self._send_json(
                413,
                {"error": f"El mensaje no puede superar {MAX_MESSAGE_LENGTH} caracteres."},
            )
            return

        if self.proveedor == "anthropic" and not os.getenv("ANTHROPIC_API_KEY"):
            self._send_json(
                503,
                {"error": "El proveedor de IA no está configurado. Configure ANTHROPIC_API_KEY."},
            )
            return

        try:
            agente, agente_lock = self._get_agent(user["sid"])
            with agente_lock:
                respuesta = agente.preguntar(mensaje.strip())
        except Exception as error:
            self._send_json(502, {"error": f"No fue posible obtener una respuesta: {error}"})
            return

        self._send_json(200, {"answer": respuesta})

    def _send_file(self, filename, content_type):
        cuerpo = (ROOT / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _send_json(self, status, payload):
        cuerpo = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _redirect(self, location, cookies=(), status=303):
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    @staticmethod
    def _cookie(name, value, max_age):
        cookie = f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
        if COOKIE_SECURE:
            cookie += "; Secure"
        return cookie

    def _read_cookie(self, name):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        morsel = cookies.get(name)
        return morsel.value if morsel else None

    def _get_session_user(self):
        if not self.auth_enabled:
            return {"sid": "local", "name": "Usuario local", "email": "", "local": True}
        if SESSION_SERIALIZER is None:
            return None

        token = self._read_cookie(SESSION_COOKIE)
        if not token:
            return None
        try:
            session_id = SESSION_SERIALIZER.loads(token, max_age=SESSION_MAX_AGE)
        except BadSignature:
            return None
        if not isinstance(session_id, str):
            return None
        with self.sesiones_lock:
            return self.sesiones.get(session_id)

    def _require_session(self):
        user = self._get_session_user()
        if user is None:
            self._send_json(401, {"error": "Inicie sesión para continuar."})
            return None
        return user

    def _get_agent(self, session_id):
        with self.agentes_lock:
            if session_id not in self.agentes:
                self.agentes[session_id] = self.agente_factory()
                self.agente_locks[session_id] = threading.Lock()
            return self.agentes[session_id], self.agente_locks[session_id]

    def _start_google_login(self):
        if not self.auth_enabled:
            self._redirect("/")
            return
        if not _google_auth_configured():
            self._redirect("/login?error=not_configured")
            return

        try:
            nonce = secrets.token_urlsafe(32)
            flow = _create_google_flow()
            authorization_url, state = flow.authorization_url(
                prompt="select_account",
                access_type="online",
                nonce=nonce,
                code_challenge_method="S256",
            )
            if not flow.code_verifier:
                raise ValueError("No se pudo generar el verificador PKCE.")
            state_data = OAUTH_STATE_SERIALIZER.dumps(
                {"state": state, "nonce": nonce, "code_verifier": flow.code_verifier}
            )
        except (OAuth2Error, ValueError) as error:
            self.log_error("No se pudo iniciar el acceso con Google: %s", error)
            self._redirect("/login?error=oauth_failed")
            return

        self._redirect(
            authorization_url,
            [self._cookie(OAUTH_STATE_COOKIE, state_data, OAUTH_STATE_MAX_AGE)],
            status=302,
        )

    def _finish_google_login(self):
        if not self.auth_enabled or not _google_auth_configured():
            self._redirect("/login?error=not_configured")
            return

        query = parse_qs(urlsplit(self.path).query)
        if query.get("error"):
            self._redirect(
                "/login?error=cancelled",
                [self._cookie(OAUTH_STATE_COOKIE, "", 0)],
            )
            return

        state = query.get("state", [""])[0]
        code = query.get("code", [""])[0]
        state_cookie = self._read_cookie(OAUTH_STATE_COOKIE)
        if not state or not code or not state_cookie:
            self._redirect(
                "/login?error=invalid_state",
                [self._cookie(OAUTH_STATE_COOKIE, "", 0)],
            )
            return

        try:
            state_data = OAUTH_STATE_SERIALIZER.loads(
                state_cookie, max_age=OAUTH_STATE_MAX_AGE
            )
        except BadSignature:
            self._redirect(
                "/login?error=invalid_state",
                [self._cookie(OAUTH_STATE_COOKIE, "", 0)],
            )
            return

        if not hmac.compare_digest(state, state_data.get("state", "")):
            self._redirect(
                "/login?error=invalid_state",
                [self._cookie(OAUTH_STATE_COOKIE, "", 0)],
            )
            return

        try:
            flow = _create_google_flow(
                state=state,
                code_verifier=state_data["code_verifier"],
            )
            flow.fetch_token(code=code)
            token = flow.credentials.id_token
            if not token:
                raise ValueError("Google no devolvió un token de identidad.")
            claims = id_token.verify_oauth2_token(
                token, GoogleRequest(), GOOGLE_CLIENT_ID
            )
            nonce = claims.get("nonce", "")
            if not isinstance(nonce, str) or not hmac.compare_digest(
                nonce, state_data.get("nonce", "")
            ):
                raise ValueError("El nonce de identidad no coincide.")
            email = claims.get("email")
            if not isinstance(email, str) or claims.get("email_verified") is not True:
                raise ValueError("La cuenta de Google no tiene un correo verificado.")
        except (
            GoogleAuthError,
            OAuth2Error,
            requests.RequestException,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            self.log_error("No se pudo completar el acceso con Google: %s", error)
            self._redirect(
                "/login?error=oauth_failed",
                [self._cookie(OAUTH_STATE_COOKIE, "", 0)],
            )
            return

        session_id = secrets.token_urlsafe(32)
        user = {
            "sid": session_id,
            "name": claims.get("given_name") or claims.get("name") or email.split("@")[0],
            "email": email,
            "local": False,
        }
        with self.sesiones_lock:
            self.sesiones[session_id] = user

        session_cookie = SESSION_SERIALIZER.dumps(session_id)
        self._redirect(
            "/",
            [
                self._cookie(SESSION_COOKIE, session_cookie, SESSION_MAX_AGE),
                self._cookie(OAUTH_STATE_COOKIE, "", 0),
            ],
        )


def main():
    parser = argparse.ArgumentParser(description="Interfaz web local del agente.")
    parser.add_argument(
        "--proveedor",
        choices=["ollama", "anthropic"],
        default=os.getenv("AGENTE_PROVEEDOR", "ollama"),
    )
    parser.add_argument("--modelo", default=None, help="Nombre del modelo que desea utilizar.")
    parser.add_argument(
        "--host",
        default=os.getenv("HOST", "0.0.0.0" if AUTH_ENABLED else "127.0.0.1"),
        help="Dirección de escucha.",
    )
    parser.add_argument(
        "--puerto",
        type=int,
        default=int(os.getenv("PORT", "8000")),
        help="Puerto de escucha.",
    )
    args = parser.parse_args()
    if not 1 <= args.puerto <= 65535:
        parser.error("--puerto debe estar entre 1 y 65535.")

    crear_base_de_datos()
    modelo = (
        seleccionar_modelo_ollama(args.modelo)
        if args.proveedor == "ollama"
        else args.modelo or "claude-sonnet-4-6"
    )

    def crear_agente():
        if args.proveedor == "ollama":
            return AgenteOllama(modelo)
        return AgenteAnthropic(modelo)

    WebHandler.agente_factory = staticmethod(crear_agente)
    WebHandler.proveedor = args.proveedor
    WebHandler.modelo = modelo
    WebHandler.auth_enabled = AUTH_ENABLED

    try:
        servidor = ThreadingHTTPServer((args.host, args.puerto), WebHandler)
    except OSError as error:
        raise SystemExit(f"No se pudo iniciar el servidor local: {error}") from error

    url_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    print(f"Interfaz disponible en http://{url_host}:{args.puerto}")
    print(f"Proveedor: {args.proveedor} | Modelo: {modelo}")
    print(f"Acceso con Google: {'habilitado' if AUTH_ENABLED else 'modo local sin login'}")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrando la interfaz local.")
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
