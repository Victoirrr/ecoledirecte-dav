"""API factice (mêmes formes de réponses que l'API réelle) pour tester sans compte."""

from __future__ import annotations

ROOT_E1 = r"\0000000A\W\101"
ROOT_E2 = r"\0000000A\W\102"


def _file(parent: str, libelle: str, contenu: bytes, date: str = "2026-10-05 14:32:00") -> dict:
    return {
        "type": "file",
        "libelle": libelle,
        "id": f"{parent}\\{libelle}",
        "taille": len(contenu),
        "date": date,
        "readonly": True,
        "_contenu": contenu,
    }


def _folder(parent: str, libelle: str, children: list | None, loaded: bool = True) -> dict:
    node = {
        "type": "folder",
        "libelle": libelle,
        "id": f"{parent}\\{libelle}",
        "taille": 0,
        "date": "2026-09-01 08:00:00",
        "isLoaded": loaded,
        "readonly": True,
    }
    if loaded:
        node["children"] = children or []
    return node


class DemoApi:
    def __init__(self) -> None:
        cours = f"{ROOT_E1}\\Cours"
        lazy_id = f"{ROOT_E1}\\Exercices"
        self._lazy = {
            lazy_id: [
                _file(lazy_id, "Fiche 1 – Fonctions.txt", "Exercice 1 : f(x) = 2x + 3\n".encode()),
                _file(lazy_id, "Fiche 2 – Équations.txt", "Résoudre 3x - 7 = 11\n".encode()),
            ]
        }
        self._clouds = {
            101: [
                {
                    "type": "folder",
                    "libelle": "/",
                    "id": ROOT_E1,
                    "isLoaded": True,
                    "children": [
                        _folder(ROOT_E1, "Cours", [
                            _file(cours, "Chapitre 1.txt", "Les nombres réels.\n".encode()),
                            _file(cours, "Chapitre 2.txt", "Les fonctions affines.\n".encode()),
                        ]),
                        _folder(ROOT_E1, "Exercices", None, loaded=False),
                        _file(ROOT_E1, "Lisez-moi.txt", "Bienvenue dans l'espace Maths !\n".encode()),
                        _file(ROOT_E1, "Notes.txt", b"doublon 1\n"),
                        {**_file(ROOT_E1, "Notes.txt", b"doublon 2\n"), "id": ROOT_E1 + "\\Notes-2.txt"},
                        {"type": "folder", "libelle": "Corbeille", "id": f"{ROOT_E1}\\TRASH_#ED",
                         "isTrash": True, "children": []},
                    ],
                }
            ],
            102: [
                {
                    "type": "folder",
                    "libelle": "/",
                    "id": ROOT_E2,
                    "isLoaded": True,
                    "children": [
                        _file(ROOT_E2, "Planning: répétitions.txt", "Mardi 17h, salle B\n".encode()),
                    ],
                }
            ],
        }
        self.calls: list[tuple] = []

    def espaces(self) -> list[dict]:
        self.calls.append(("espaces",))
        base = {"description": "", "discussion": False, "agenda": False, "public": False,
                "ouvert": True, "type": "LIBRE", "estAdmin": False, "creePar": "Démo"}
        return [
            {**base, "id": 101, "titre": "Maths 2nde B", "cloud": True, "estMembre": True},
            {**base, "id": 102, "titre": "Club Théâtre", "cloud": True, "estMembre": True},
            {**base, "id": 103, "titre": "Sans cloud", "cloud": False, "estMembre": True},
            {**base, "id": 104, "titre": "Pas membre", "cloud": True, "estMembre": False},
        ]

    def cloud(self, espace_id: int, folder_id: str | None = None) -> list[dict]:
        self.calls.append(("cloud", espace_id, folder_id))
        if folder_id is None:
            return self._clouds[espace_id]
        # Comme l'API réelle : `folder_id` est relatif à la racine de l'espace, et le
        # serveur y préfixe lui-même l'id de l'espace (un id complet serait doublé).
        full_id = self._clouds[espace_id][0]["id"] + folder_id
        found = full_id in self._lazy
        node = {"type": "folder", "id": full_id, "libelle": folder_id.rsplit("\\", 1)[-1],
                "isLoaded": found}
        if found:
            node["children"] = self._lazy[full_id]
        return [node]

    def _find(self, nodes: list[dict], file_id: str) -> dict | None:
        for n in nodes:
            if n.get("id") == file_id and n.get("type") == "file":
                return n
            found = self._find(n.get("children") or [], file_id)
            if found:
                return found
        return None

    def download(self, espace_id: int, file_id: str):
        self.calls.append(("download", espace_id, file_id))
        pool = self._clouds.get(espace_id, []) + [
            {"children": v} for v in self._lazy.values()
        ]
        node = self._find(pool, file_id)
        if node is None:
            raise FileNotFoundError(file_id)
        return iter([node["_contenu"]])
