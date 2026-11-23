"""Caches : listings en mémoire (TTL) et fichiers téléchargés sur disque."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable


class TTLCache:
    def __init__(self, ttl: Callable[[], float] | float):
        self._ttl = ttl if callable(ttl) else (lambda: ttl)
        self._data: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._key_locks: dict[Any, threading.Lock] = defaultdict(threading.Lock)

    def get_or_compute(self, key: Any, compute: Callable[[], Any]) -> Any:
        now = time.monotonic()
        hit = self._data.get(key)
        if hit and now - hit[0] < self._ttl():
            return hit[1]
        with self._lock:
            key_lock = self._key_locks[key]
        with key_lock:  # un seul appel API à la fois pour une même clé
            hit = self._data.get(key)
            if hit and time.monotonic() - hit[0] < self._ttl():
                return hit[1]
            value = compute()
            self._data[key] = (time.monotonic(), value)
            return value

    def clear(self) -> None:
        self._data.clear()


class FileCache:
    """Fichiers téléchargés, sur disque, avec une taille maximale.

    Quand la limite est dépassée, les fichiers ouverts le moins récemment sont
    supprimés (ils seront simplement retéléchargés si on les rouvre).
    `max_bytes` : nombre d'octets, fonction renvoyant ce nombre, ou None/0 = sans limite.
    """

    PART_MAX_AGE = 3600  # s : fichiers .part abandonnés (téléchargement interrompu)

    def __init__(self, root: Path, max_bytes: Callable[[], int | None] | int | None = None):
        self.root = Path(root)
        self._max = max_bytes if callable(max_bytes) else (lambda: max_bytes)
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._lock = threading.Lock()
        self._prune_lock = threading.Lock()

    def _path(self, *key_parts: Any) -> Path:
        digest = hashlib.sha256("\x00".join(map(str, key_parts)).encode()).hexdigest()
        return self.root / digest[:2] / digest

    def cached(self, *key_parts: Any) -> Path | None:
        p = self._path(*key_parts)
        return p if p.exists() else None

    def fetch(self, key_parts: tuple, download: Callable[[], Iterable[bytes]]) -> Path:
        p = self._path(*key_parts)
        if self._touch(p):
            return p
        with self._lock:
            lock = self._locks[p.name]
        with lock:
            if self._touch(p):
                return p
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.part")
            try:
                with tmp.open("wb") as fh:
                    for chunk in download():
                        if chunk:
                            fh.write(chunk)
                tmp.replace(p)
            finally:
                tmp.unlink(missing_ok=True)
        self.prune(keep=p)
        return p

    @staticmethod
    def _touch(p: Path) -> bool:
        """Marque le fichier comme utilisé (pour l'ordre de suppression)."""
        try:
            os.utime(p)
            return True
        except FileNotFoundError:
            return False

    # ---- taille / nettoyage -------------------------------------------
    def _entries(self) -> list[tuple[Path, os.stat_result]]:
        out = []
        if not self.root.exists():
            return out
        for p in self.root.glob("*/*"):
            try:
                out.append((p, p.stat()))
            except FileNotFoundError:
                pass
        return out

    def usage(self) -> tuple[int, int]:
        """(nombre de fichiers, octets) — hors téléchargements en cours."""
        files = [st for p, st in self._entries() if not p.name.endswith(".part")]
        return len(files), sum(st.st_size for st in files)

    def prune(self, keep: Path | None = None) -> int:
        """Supprime les plus anciens fichiers au-delà de la limite. Renvoie les octets libérés."""
        limit = self._max()
        now = time.time()
        freed = 0
        with self._prune_lock:
            files = []
            for p, st in self._entries():
                if p.name.endswith(".part"):
                    if now - st.st_mtime > self.PART_MAX_AGE:
                        p.unlink(missing_ok=True)
                    continue
                files.append((p, st))
            if not limit or limit <= 0:
                return 0
            total = sum(st.st_size for _, st in files)
            for p, st in sorted(files, key=lambda e: e[1].st_mtime):
                if total <= limit:
                    break
                if p == keep:
                    continue
                p.unlink(missing_ok=True)  # un fichier en cours de lecture reste lisible
                total -= st.st_size
                freed += st.st_size
        return freed

    def clear(self) -> int:
        """Vide le cache. Renvoie les octets libérés."""
        freed = 0
        with self._prune_lock:
            for p, st in self._entries():
                if p.name.endswith(".part"):
                    continue
                p.unlink(missing_ok=True)
                freed += st.st_size
        return freed
