"""Fournisseur WebDAV en lecture seule pour les clouds des espaces de travail.

Arborescence exposée :
    /                         racine : « Espaces de travail » + favoris
    /Espaces de travail/      un dossier par espace ayant un cloud
    /Espaces de travail/<espace>/…  contenu du cloud de l'espace
    /<favori>/…               raccourci vers le cloud d'un espace
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import requests
from wsgidav.dav_error import DAVError
from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider

from .api import EcoleDirecteError
from .cache import FileCache, TTLCache
from .config import Config, ConfigWatcher

log = logging.getLogger(__name__)

ESPACES_DIR = "Espaces de travail"
HTTP_SERVICE_UNAVAILABLE = 503

# Fichiers que Finder/Spotlight réclament sans arrêt : réponse 404 immédiate.
JUNK_NAMES = {
    ".DS_Store",
    ".localized",
    ".hidden",
    ".Spotlight-V100",
    ".Trashes",
    ".TemporaryItems",
    ".fseventsd",
    ".metadata_never_index",
    ".metadata_never_index_unless_rootfs",
    ".metadata_direct_scope_only",
    ".ql_disablethumbnails",
    ".ql_disablecache",
    ".VolumeIcon.icns",
    "Icon\r",
    "Backups.backupdb",
    "desktop.ini",
    "Thumbs.db",
}


def is_junk(name: str) -> bool:
    return name.startswith("._") or name in JUNK_NAMES


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def sanitize(name: str) -> str:
    name = nfc(str(name or "")).replace("/", "-").replace(":", "-").replace("\\", "-").strip()
    return name or "_"


def _with_suffix(name: str, suffix: str, is_file: bool) -> str:
    stem, ext = os.path.splitext(name) if is_file else (name, "")
    return f"{stem} ({suffix}){ext}"


def parse_date(value: str | None) -> float | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y à %H:%M", "%d/%m/%Y"):
        try:
            return time.mktime(datetime.strptime(value.strip(), fmt).timetuple())
        except ValueError:
            continue
    return None


def is_folder(node: dict) -> bool:
    return node.get("type") == "folder"


class Api(Protocol):
    def espaces(self) -> list[dict]: ...
    def cloud(self, espace_id: int, folder_id: str | None = None) -> list[dict]: ...
    def download(self, espace_id: int, file_id: str) -> Any: ...


# ---------------------------------------------------------------------------
# Logique métier (indépendante de WebDAV, testable)
# ---------------------------------------------------------------------------
class Backend:
    def __init__(self, api: Api, config: ConfigWatcher | Config, file_cache: FileCache):
        self.api = api
        self._config = config
        self.files = file_cache
        self.listings = TTLCache(lambda: self.config.cache_ttl)
        self.started = time.time()

    @property
    def config(self) -> Config:
        return self._config.get() if isinstance(self._config, ConfigWatcher) else self._config

    # ---- espaces -----------------------------------------------------
    def espaces(self) -> list[dict]:
        def fetch() -> list[dict]:
            return [
                e for e in self.api.espaces()
                if e.get("cloud") and e.get("estMembre", True) is not False
            ]

        return self.listings.get_or_compute("espaces", fetch)

    def espace_names(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for e in sorted(self.espaces(), key=lambda e: sanitize(e.get("titre")).casefold()):
            name = sanitize(e.get("titre"))
            if name in out:
                name = _with_suffix(name, str(e["id"]), False)
            out[name] = e
        return out

    def espace_by_id(self, espace_id: int) -> dict | None:
        return next((e for e in self.espaces() if int(e["id"]) == int(espace_id)), None)

    def favoris(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        reserved = {nfc(ESPACES_DIR)}
        for fav in self.config.favoris:
            espace = self.espace_by_id(fav.id)
            if espace is None:
                continue
            name = sanitize(fav.nom or espace.get("titre"))
            if name in out or name in reserved:
                name = _with_suffix(name, str(espace["id"]), False)
            out[name] = espace
        return out

    # ---- cloud -------------------------------------------------------
    def _root_nodes(self, espace_id: int) -> list[dict]:
        return self.listings.get_or_compute(("cloud", espace_id, None), lambda: self.api.cloud(espace_id))

    @staticmethod
    def _as_root(nodes: list[dict]) -> dict | None:
        if len(nodes) == 1 and is_folder(nodes[0]) and nodes[0].get("libelle") in ("/", "", None):
            return nodes[0]
        return None

    def _root_children(self, espace_id: int) -> list[dict]:
        nodes = self._root_nodes(espace_id)
        root = self._as_root(nodes)
        return (root.get("children") or []) if root else nodes

    def _relative_id(self, espace_id: int, folder_id: str) -> str:
        """L'API attend un chemin relatif à la racine de l'espace (« \\Dossier\\Sous-dossier »)
        et préfixe elle-même l'identifiant de l'espace : passer l'id complet le doublerait."""
        root = self._as_root(self._root_nodes(espace_id))
        root_id = (root or {}).get("id") or ""
        if root_id and folder_id.startswith(root_id):
            return folder_id[len(root_id):]
        return folder_id

    def _folder_children(self, espace_id: int, node: dict) -> list[dict]:
        if node.get("isLoaded") is not False and "children" in node:
            return node.get("children") or []
        folder_id = node["id"]
        rel_id = self._relative_id(espace_id, folder_id)
        nodes = self.listings.get_or_compute(
            ("cloud", espace_id, folder_id), lambda: self.api.cloud(espace_id, rel_id)
        )
        if len(nodes) == 1 and is_folder(nodes[0]) and (
            nodes[0].get("id") == folder_id or nodes[0].get("libelle") == node.get("libelle")
        ):
            children = nodes[0].get("children") or []
        else:
            children = nodes
        # Garde-fou : un dossier ne doit jamais se contenir lui-même (boucle dans Finder).
        return [c for c in children if c.get("id") != folder_id and c is not node]

    def children(self, espace_id: int, node: dict | None) -> dict[str, dict]:
        raw = self._root_children(espace_id) if node is None else self._folder_children(espace_id, node)
        out: dict[str, dict] = {}
        for child in raw:
            if child.get("isTrash") or child.get("hidden"):
                continue
            name = sanitize(child.get("libelle"))
            if is_junk(name):
                continue
            base, n = name, 2
            while name in out:
                name = _with_suffix(base, str(n), not is_folder(child))
                n += 1
            out[name] = child
        return out

    # ---- fichiers ----------------------------------------------------
    @staticmethod
    def _file_key(espace_id: int, node: dict) -> tuple:
        return (espace_id, node.get("id"), node.get("date"), node.get("taille"))

    def cached_file(self, espace_id: int, node: dict) -> Path | None:
        return self.files.cached(*self._file_key(espace_id, node))

    def open_file(self, espace_id: int, node: dict) -> Path:
        return self.files.fetch(
            self._file_key(espace_id, node), lambda: self.api.download(espace_id, node["id"])
        )


# ---------------------------------------------------------------------------
# Ressources WebDAV
# ---------------------------------------------------------------------------
def _unavailable(exc: Exception) -> DAVError:
    log.warning("EcoleDirecte indisponible : %s", exc)
    return DAVError(HTTP_SERVICE_UNAVAILABLE, context_info=str(exc))


class _VirtualDir(DAVCollection):
    def __init__(self, path: str, environ: dict, backend: Backend, names: list[str]):
        super().__init__(path, environ)
        self.backend = backend
        self._names = names

    def get_member_names(self) -> list[str]:
        return self._names

    def get_display_info(self) -> dict:
        return {"type": "Directory"}

    def get_creation_date(self) -> float:
        return self.backend.started

    def get_last_modified(self) -> float:
        return self.backend.started

    def get_etag(self):
        return None


class EspaceDir(DAVCollection):
    """Un dossier du cloud d'un espace (node=None : racine du cloud)."""

    def __init__(self, path: str, environ: dict, backend: Backend, espace: dict, node: dict | None):
        super().__init__(path, environ)
        self.backend = backend
        self.espace = espace
        self.node = node

    def _children(self) -> dict[str, dict]:
        try:
            return self.backend.children(int(self.espace["id"]), self.node)
        except (EcoleDirecteError, requests.RequestException) as exc:
            raise _unavailable(exc) from exc

    def get_member_names(self) -> list[str]:
        return sorted(self._children(), key=str.casefold)

    def get_member(self, name: str):
        child = self._children().get(nfc(name))
        if child is None:
            return None
        path = self.path.rstrip("/") + "/" + name
        return make_resource(path, self.environ, self.backend, self.espace, child)

    def get_display_info(self) -> dict:
        return {"type": "Directory"}

    def get_creation_date(self):
        return parse_date((self.node or {}).get("date")) or parse_date(self.espace.get("creeLe"))

    def get_last_modified(self):
        return self.get_creation_date() or self.backend.started

    def get_etag(self):
        return None


class CloudFile(DAVNonCollection):
    def __init__(self, path: str, environ: dict, backend: Backend, espace: dict, node: dict):
        super().__init__(path, environ)
        self.backend = backend
        self.espace = espace
        self.node = node

    def _local(self, download: bool) -> Path | None:
        espace_id = int(self.espace["id"])
        if not download:
            return self.backend.cached_file(espace_id, self.node)
        try:
            return self.backend.open_file(espace_id, self.node)
        except (EcoleDirecteError, requests.RequestException) as exc:
            raise _unavailable(exc) from exc

    def get_content_length(self) -> int:
        # Pour un GET, on télécharge d'abord : la taille annoncée sera exacte.
        local = self._local(download=self.environ.get("REQUEST_METHOD") == "GET")
        if local is not None:
            return local.stat().st_size
        try:
            return int(self.node.get("taille") or 0)
        except (TypeError, ValueError):
            return 0

    def get_content_type(self) -> str:
        return mimetypes.guess_type(self.name)[0] or "application/octet-stream"

    def get_content(self):
        return self._local(download=True).open("rb")

    def get_creation_date(self):
        return parse_date(self.node.get("date"))

    def get_last_modified(self):
        return parse_date(self.node.get("date")) or self.backend.started

    def get_display_info(self) -> dict:
        return {"type": "File"}

    def get_etag(self) -> str:
        key = f"{self.node.get('id')}|{self.node.get('date')}|{self.node.get('taille')}"
        return hashlib.md5(key.encode()).hexdigest()

    def support_etag(self) -> bool:
        return True

    def support_ranges(self) -> bool:
        return True


def make_resource(path: str, environ: dict, backend: Backend, espace: dict, node: dict | None):
    if node is None or is_folder(node):
        return EspaceDir(path, environ, backend, espace, node)
    return CloudFile(path, environ, backend, espace, node)


class EcoleDirecteProvider(DAVProvider):
    def __init__(self, backend: Backend):
        super().__init__()
        self.backend = backend

    def is_readonly(self) -> bool:
        return True

    def get_resource_inst(self, path: str, environ: dict):
        self._count_get_resource_inst += 1
        try:
            return self._resolve(path, environ)
        except DAVError:
            raise
        except (EcoleDirecteError, requests.RequestException) as exc:
            raise _unavailable(exc) from exc

    def _resolve(self, path: str, environ: dict):
        parts = [nfc(p) for p in path.strip("/").split("/") if p]
        if any(is_junk(p) for p in parts):
            return None
        b = self.backend
        if not parts:
            return _VirtualDir("/", environ, b, [ESPACES_DIR, *b.favoris()])
        if parts[0] == ESPACES_DIR:
            names = b.espace_names()
            if len(parts) == 1:
                return _VirtualDir("/" + ESPACES_DIR, environ, b, list(names))
            espace, rest, prefix = names.get(parts[1]), parts[2:], parts[:2]
        else:
            espace, rest, prefix = b.favoris().get(parts[0]), parts[1:], parts[:1]
        if espace is None:
            return None

        node: dict | None = None
        for name in rest:
            if node is not None and not is_folder(node):
                return None
            node = b.children(int(espace["id"]), node).get(name)
            if node is None:
                return None
        return make_resource("/" + "/".join(prefix + rest), environ, b, espace, node)
