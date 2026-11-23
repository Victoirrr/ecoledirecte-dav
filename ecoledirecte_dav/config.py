"""Configuration : ~/.config/ecoledirecte-dav/config.toml (aucun secret ici)."""

from __future__ import annotations

import os
import threading
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomli_w

CONFIG_DIR = Path(
    os.environ.get("ECOLEDIRECTE_DAV_CONFIG_DIR", Path.home() / ".config" / "ecoledirecte-dav")
)
CACHE_DIR = Path(
    os.environ.get("ECOLEDIRECTE_DAV_CACHE_DIR", Path.home() / "Library" / "Caches" / "ecoledirecte-dav")
)


LEGACY_DEFAULT_VERSIONS = {"4.75.0"}


def config_file() -> Path:
    return CONFIG_DIR / "config.toml"


@dataclass
class Favori:
    id: int
    nom: str  # nom affiché à la racine (par défaut : titre de l'espace)


@dataclass
class Config:
    identifiant: str = ""
    host: str = "127.0.0.1"
    port: int = 8080
    api_version: str = "4.91.0"
    cache_ttl: int = 60  # secondes, pour les listings
    cache_max_mo: int = 1024  # taille max du cache de fichiers (0 = sans limite)
    favoris: list[Favori] = field(default_factory=list)

    # ---- persistance -------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_file()
        if not path.exists():
            return cls()
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        favoris = [Favori(id=int(f["id"]), nom=str(f["nom"])) for f in raw.pop("favoris", [])]
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        if known.get("api_version") in LEGACY_DEFAULT_VERSIONS:
            known.pop("api_version")  # ancienne valeur par défaut : on suit la nouvelle
        return cls(favoris=favoris, **known)

    def save(self, path: Path | None = None) -> None:
        path = path or config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        tmp = path.with_suffix(".tmp")
        with tmp.open("wb") as fh:
            tomli_w.dump(data, fh)
        tmp.replace(path)

    @property
    def cache_max_bytes(self) -> int | None:
        return self.cache_max_mo * 1024 * 1024 if self.cache_max_mo > 0 else None

    # ---- favoris -----------------------------------------------------
    def favori(self, espace_id: int) -> Favori | None:
        return next((f for f in self.favoris if f.id == espace_id), None)


class ConfigWatcher:
    """Relit la config quand le fichier change (pour les favoris, sans redémarrage)."""

    def __init__(self, path: Path | None = None, initial: Config | None = None):
        self.path = path or config_file()
        self._lock = threading.Lock()
        self._mtime = self._stat()
        self._config = initial or Config.load(self.path)

    def _stat(self) -> float:
        try:
            return self.path.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    def get(self) -> Config:
        mtime = self._stat()
        if mtime != self._mtime:
            with self._lock:
                if mtime != self._mtime:
                    try:
                        self._config = Config.load(self.path)
                    except (OSError, tomllib.TOMLDecodeError):
                        pass  # fichier en cours d'écriture : on garde l'ancienne version
                    self._mtime = mtime
        return self._config
