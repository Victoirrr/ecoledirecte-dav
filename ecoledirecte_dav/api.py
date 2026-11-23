"""Client minimal pour l'API interne (non officielle) d'EcoleDirecte.

Références communautaires :
  - https://github.com/EduWireApps/ecoledirecte-api-docs
  - https://github.com/Scolup/docsdirecte
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from typing import Any, Callable, Iterator

import keyring
import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.ecoledirecte.com/v3/"
# Le token est lié au User-Agent qui l'a obtenu : il ne doit jamais changer.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
KEYRING_SERVICE = "ecoledirecte-dav"

# Codes « métier » renvoyés dans le JSON (le statut HTTP est toujours 200).
CODE_OK = 200
CODE_DOUBLE_AUTH = 250
CODE_BAD_CREDENTIALS = 505
CODE_BAD_VERSION = 517
CODES_TOKEN_EXPIRED = {520, 525}

# Callback de double authentification : (question, propositions) -> index choisi
DoubleAuthHandler = Callable[[str, list[str]], int]


class EcoleDirecteError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class AuthError(EcoleDirecteError):
    pass


class DoubleAuthRequired(AuthError):
    pass


class VersionError(EcoleDirecteError):
    pass


# ---------------------------------------------------------------------------
# Trousseau macOS
# ---------------------------------------------------------------------------
class Coffre:
    """Mot de passe et jeton de double authentification (cn/cv) dans le Trousseau."""

    def __init__(self, identifiant: str):
        self.identifiant = identifiant

    def _get(self, suffix: str) -> str | None:
        return keyring.get_password(KEYRING_SERVICE, f"{self.identifiant}:{suffix}")

    def _set(self, suffix: str, value: str) -> None:
        keyring.set_password(KEYRING_SERVICE, f"{self.identifiant}:{suffix}", value)

    def _del(self, suffix: str) -> None:
        try:
            keyring.delete_password(KEYRING_SERVICE, f"{self.identifiant}:{suffix}")
        except keyring.errors.PasswordDeleteError:
            pass

    @property
    def mot_de_passe(self) -> str | None:
        return self._get("password")

    @mot_de_passe.setter
    def mot_de_passe(self, value: str) -> None:
        self._set("password", value)

    @property
    def fa(self) -> dict | None:
        raw = self._get("fa")
        return json.loads(raw) if raw else None

    @fa.setter
    def fa(self, value: dict | None) -> None:
        if value is None:
            self._del("fa")
        else:
            self._set("fa", json.dumps(value))

    def effacer(self) -> None:
        self._del("password")
        self._del("fa")


def _b64decode(value: str) -> str:
    try:
        return base64.b64decode(value).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return value


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class Client:
    def __init__(
        self,
        identifiant: str,
        coffre: Coffre,
        api_version: str = "4.91.0",
        on_double_auth: DoubleAuthHandler | None = None,
        mot_de_passe: str | None = None,
    ):
        self.identifiant = identifiant
        self.coffre = coffre
        self.api_version = api_version
        self.on_double_auth = on_double_auth
        self._mot_de_passe = mot_de_passe
        self.token: str | None = None
        # Depuis fin 2025, l'API renvoie aussi un en-tête `2fa-token` qu'il faut
        # renvoyer (en-tête `2FA-Token`) avec chaque requête authentifiée.
        self.fa_token: str | None = None
        self.account: dict | None = None
        self._lock = threading.RLock()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.ecoledirecte.com",
                "Referer": "https://www.ecoledirecte.com/",
            }
        )

    # ---- bas niveau --------------------------------------------------
    @property
    def account_id(self) -> int:
        if not self.account:
            raise AuthError("Pas connecté")
        return int(self.account["id"])

    def _params(self, **extra: Any) -> dict:
        return {"v": self.api_version, **{k: v for k, v in extra.items() if v is not None}}

    def _raw_post(self, path: str, body: Any, params: dict, headers: dict | None = None) -> dict:
        resp = self.session.post(
            BASE_URL + path,
            params=params,
            data={"data": json.dumps(body)},
            headers=headers or {},
            timeout=30,
        )
        resp.raise_for_status()
        try:
            # L'API envoie de l'UTF-8 sans le déclarer : requests le décoderait en
            # Latin-1 (« 1Ã¨re » au lieu de « 1ère »). On décode nous-mêmes.
            payload = json.loads(resp.content.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise EcoleDirecteError(f"Réponse non JSON pour {path}") from exc
        new_token = resp.headers.get("X-Token") or payload.get("token")
        if new_token:
            self.token = new_token
        new_fa_token = resp.headers.get("2fa-token")
        if new_fa_token:
            self.fa_token = new_fa_token
        log.debug(
            "%s %s → code %s (x-token:%s, 2fa-token:%s)",
            path, params.get("verbe", ""), payload.get("code"),
            "oui" if new_token else "non", "oui" if new_fa_token else "non",
        )
        return payload

    @staticmethod
    def _check(payload: dict, path: str) -> dict:
        code = payload.get("code")
        if code == CODE_OK:
            return payload
        message = payload.get("message") or ""
        if code == CODE_BAD_VERSION:
            raise VersionError(
                f"Version d'API refusée ({message}). Change `api_version` dans la config.", code
            )
        if code in CODES_TOKEN_EXPIRED:
            raise AuthError(f"Token expiré ({code})", code)
        raise EcoleDirecteError(f"{path} : code {code} {message}".strip(), code)

    def _auth_headers(self) -> dict:
        headers = {}
        if self.token:
            headers["X-Token"] = self.token
        if self.fa_token:
            headers["2FA-Token"] = self.fa_token
        return headers

    # ---- connexion ---------------------------------------------------
    def _gtk(self) -> dict:
        resp = self.session.get(
            BASE_URL + "login.awp", params={"gtk": 1, "v": self.api_version}, timeout=30
        )
        resp.raise_for_status()
        gtk = self.session.cookies.get("GTK") or resp.cookies.get("GTK")
        return {"X-Gtk": gtk} if gtk else {}

    def login(self) -> dict:
        """Se connecte (avec double authentification si nécessaire). Renvoie le compte."""
        with self._lock:
            mdp = self._mot_de_passe or self.coffre.mot_de_passe
            if not mdp:
                raise AuthError("Aucun mot de passe enregistré : lance `ecoledirecte-dav login`.")
            self.token = self.fa_token = None
            self.account = None
            fa = self.coffre.fa
            payload = self._login_request(mdp, fa)

            if payload.get("code") == CODE_DOUBLE_AUTH:
                fa = self._double_auth()
                payload = self._login_request(mdp, fa)

            code = payload.get("code")
            if code == CODE_BAD_CREDENTIALS:
                raise AuthError("Identifiant ou mot de passe refusé.", code)
            if code == CODE_DOUBLE_AUTH:
                raise DoubleAuthRequired("Double authentification toujours demandée.", code)
            self._check(payload, "login.awp")

            accounts = (payload.get("data") or {}).get("accounts") or []
            eleves = [a for a in accounts if a.get("typeCompte") == "E"] or accounts
            if not eleves:
                raise AuthError("Aucun compte trouvé dans la réponse de connexion.")
            self.account = eleves[0]
            log.info("Connecté : %s %s", self.account.get("prenom"), self.account.get("nom"))
            return self.account

    def _login_request(self, mdp: str, fa: dict | None) -> dict:
        body: dict[str, Any] = {
            "identifiant": self.identifiant,
            "motdepasse": mdp,
            "isRelogin": False,
            "uuid": "",
        }
        if fa:
            body["cn"] = fa["cn"]
            body["cv"] = fa["cv"]
            body["fa"] = [{"cn": fa["cn"], "cv": fa["cv"], "uniq": "false"}]
        # Relogin après la question de sécurité : on renvoie les jetons de l'étape 250.
        headers = {**self._auth_headers(), **self._gtk()}
        return self._raw_post("login.awp", body, self._params(), headers)

    def _double_auth(self) -> dict:
        if self.on_double_auth is None:
            raise DoubleAuthRequired(
                "EcoleDirecte demande la question de sécurité : relance `ecoledirecte-dav login`.",
                CODE_DOUBLE_AUTH,
            )
        q = self._check(
            self._raw_post(
                "connexion/doubleauth.awp", {}, self._params(verbe="get"), self._auth_headers()
            ),
            "doubleauth get",
        )["data"]
        question = _b64decode(q["question"])
        propositions_b64: list[str] = q["propositions"]
        propositions = [_b64decode(p) for p in propositions_b64]
        index = self.on_double_auth(question, propositions)
        r = self._check(
            self._raw_post(
                "connexion/doubleauth.awp",
                {"choix": propositions_b64[index]},
                self._params(verbe="post"),
                self._auth_headers(),
            ),
            "doubleauth post",
        )["data"]
        fa = {"cn": r["cn"], "cv": r["cv"]}
        self.coffre.fa = fa
        return fa

    def ensure_login(self) -> None:
        if not self.token or not self.account:
            self.login()

    # ---- appels authentifiés (avec reconnexion automatique) -----------
    def call(self, path: str, body: Any = None, **params: Any) -> Any:
        self.ensure_login()
        for attempt in (1, 2):
            payload = self._raw_post(path, body or {}, self._params(**params), self._auth_headers())
            if payload.get("code") in CODES_TOKEN_EXPIRED and attempt == 1:
                log.info("Token expiré, reconnexion…")
                with self._lock:
                    self.token = None
                    self.login()
                continue
            return self._check(payload, path).get("data")
        raise AuthError("Reconnexion impossible")

    # ---- espaces de travail -----------------------------------------
    def espaces(self) -> list[dict]:
        self.ensure_login()
        data = self.call(f"E/{self.account_id}/espacestravail.awp", verbe="get")
        return data if isinstance(data, list) else []

    def cloud(self, espace_id: int, folder_id: str | None = None) -> list[dict]:
        data = self.call(f"cloud/W/{espace_id}.awp", verbe="get", idfolder=folder_id)
        if isinstance(data, dict):
            return [data]
        return data or []

    def download(self, espace_id: int, file_id: str) -> Iterator[bytes]:
        """Télécharge un fichier du cloud d'un espace (flux d'octets)."""
        self.ensure_login()
        for attempt in (1, 2):
            resp = self._download_post(espace_id, file_id)
            if not _looks_like_error(resp):
                return resp.iter_content(64 * 1024)
            resp = self._download_get(file_id)
            if not _looks_like_error(resp):
                return resp.iter_content(64 * 1024)
            if attempt == 1:
                with self._lock:
                    self.token = None
                    self.login()
        raise EcoleDirecteError(f"Téléchargement impossible : {file_id}")

    def _download_post(self, espace_id: int, file_id: str) -> requests.Response:
        # Variante Scolup : POST form, token dans le corps, pas de X-Token.
        resp = self.session.post(
            BASE_URL + "telechargement.awp",
            params=self._params(verbe="get"),
            data={
                "leTypeDeFichier": "CLOUD",
                "fichierId": file_id,
                "tabFichiersIds": "",
                "token": self.token or "",
                "data": json.dumps({"idEntity": espace_id, "typeEntity": "W"}),
            },
            headers={"2FA-Token": self.fa_token} if self.fa_token else {},
            stream=True,
            timeout=120,
        )
        resp.raise_for_status()
        return resp

    def _download_get(self, file_id: str) -> requests.Response:
        # Variante EduWireApps : GET avec paramètres + X-Token.
        resp = self.session.get(
            BASE_URL + "telechargement.awp",
            params=self._params(verbe="get", leTypeDeFichier="CLOUD", fichierId=file_id),
            headers=self._auth_headers(),
            stream=True,
            timeout=120,
        )
        resp.raise_for_status()
        return resp


def _looks_like_error(resp: requests.Response) -> bool:
    """L'API renvoie un JSON {code: …} au lieu du fichier en cas d'erreur."""
    ctype = resp.headers.get("Content-Type", "")
    if "json" not in ctype:
        return False
    try:
        payload = json.loads(resp.content[:65536] or b"{}")
    except ValueError:
        return False
    if isinstance(payload, dict) and "code" in payload and payload.get("code") != CODE_OK:
        log.debug("Réponse d'erreur au téléchargement : %s", payload.get("code"))
        return True
    return False
