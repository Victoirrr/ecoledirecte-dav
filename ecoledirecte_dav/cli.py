"""Ligne de commande : ecoledirecte-dav {login,logout,espaces,fav,cache,serve,install-agent,uninstall-agent}."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .api import Client, Coffre, EcoleDirecteError
from .cache import FileCache
from .config import CACHE_DIR, Config, ConfigWatcher, Favori, config_file
from .provider import Backend, EcoleDirecteProvider, sanitize

AGENT_LABEL = "com.local.ecoledirecte-dav"
AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
AGENT_LOG = Path.home() / "Library" / "Logs" / "ecoledirecte-dav.log"


# ---------------------------------------------------------------------------
# Aides
# ---------------------------------------------------------------------------
def ask_double_auth(question: str, propositions: list[str]) -> int:
    print("\nEcoleDirecte demande une vérification :")
    print(f"  {question}")
    for i, p in enumerate(propositions, 1):
        print(f"  {i}. {p}")
    while True:
        raw = input("Ton choix (numéro) : ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(propositions):
            return int(raw) - 1
        print("  Numéro invalide.")


def make_api(cfg: Config, demo: bool, interactive: bool):
    if demo:
        from .demo import DemoApi

        return DemoApi()
    if not cfg.identifiant:
        raise SystemExit("Aucun compte configuré : lance d'abord `ecoledirecte-dav login`.")
    return Client(
        cfg.identifiant,
        Coffre(cfg.identifiant),
        api_version=cfg.api_version,
        on_double_auth=ask_double_auth if interactive else None,
    )


def find_espace(espaces: list[dict], query: str) -> dict:
    if query.isdigit():
        match = [e for e in espaces if int(e["id"]) == int(query)]
    else:
        q = query.casefold()
        exact = [e for e in espaces if str(e.get("titre", "")).casefold() == q]
        match = exact or [e for e in espaces if q in str(e.get("titre", "")).casefold()]
    if len(match) == 1:
        return match[0]
    if not match:
        raise SystemExit(f"Aucun espace ne correspond à « {query} ». Voir `ecoledirecte-dav espaces`.")
    lines = "\n".join(f"  {e['id']:>8}  {e.get('titre')}" for e in match)
    raise SystemExit(f"Plusieurs espaces correspondent à « {query} », précise avec l'id :\n{lines}")


def _strip_private(obj):
    if isinstance(obj, dict):
        return {k: _strip_private(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_private(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------
def cmd_login(args) -> int:
    cfg = Config.load()
    default = cfg.identifiant
    ident = input(f"Identifiant EcoleDirecte{f' [{default}]' if default else ''} : ").strip() or default
    if not ident:
        print("Identifiant requis.")
        return 1
    mdp = getpass.getpass("Mot de passe (non affiché) : ")
    coffre = Coffre(ident)
    client = Client(ident, coffre, api_version=cfg.api_version,
                    on_double_auth=ask_double_auth, mot_de_passe=mdp)
    try:
        account = client.login()
    except EcoleDirecteError as exc:
        print(f"Échec de la connexion : {exc}")
        return 1
    coffre.mot_de_passe = mdp
    cfg.identifiant = ident
    cfg.save()
    print(f"\nConnecté : {account.get('prenom', '')} {account.get('nom', '')}"
          f" – {account.get('nomEtablissement', '')}")
    print("Mot de passe et jeton de vérification enregistrés dans le Trousseau macOS.")
    return 0


def cmd_logout(args) -> int:
    cfg = Config.load()
    if cfg.identifiant:
        Coffre(cfg.identifiant).effacer()
        print(f"Identifiants de {cfg.identifiant} supprimés du Trousseau.")
    return 0


def cmd_espaces(args) -> int:
    cfg = Config.load()
    api = make_api(cfg, args.demo, interactive=True)
    espaces = api.espaces()
    if args.debug:
        dump = {"espaces": espaces, "clouds": {}}
        for e in espaces:
            if e.get("cloud") and (args.debug_id is None or int(e["id"]) == args.debug_id):
                try:
                    dump["clouds"][str(e["id"])] = api.cloud(int(e["id"]))
                except Exception as exc:  # noqa: BLE001 - on veut voir l'erreur brute
                    dump["clouds"][str(e["id"])] = {"erreur": repr(exc)}
        print(json.dumps(_strip_private(dump), ensure_ascii=False, indent=2))
        return 0
    fav_ids = {f.id for f in cfg.favoris}
    print(f"{'id':>8}  {'cloud':5}  {'fav':3}  titre")
    for e in sorted(espaces, key=lambda e: str(e.get("titre", "")).casefold()):
        if not args.all and (not e.get("cloud") or e.get("estMembre") is False):
            continue
        print(f"{e['id']:>8}  {'oui' if e.get('cloud') else 'non':5}  "
              f"{'★' if int(e['id']) in fav_ids else '':3}  {e.get('titre')}")
    return 0


def cmd_fav(args) -> int:
    cfg = Config.load()
    if args.fav_cmd == "list":
        if not cfg.favoris:
            print("Aucun favori. Ajoute-en avec `ecoledirecte-dav fav add <id ou titre>`.")
        for f in cfg.favoris:
            print(f"{f.id:>8}  {f.nom}")
        return 0

    if args.fav_cmd == "remove" and args.espace.isdigit():
        before = len(cfg.favoris)
        cfg.favoris = [f for f in cfg.favoris if f.id != int(args.espace)]
    elif args.fav_cmd == "remove":
        q = args.espace.casefold()
        match = [f for f in cfg.favoris if q in f.nom.casefold()]
        if len(match) > 1:
            raise SystemExit("Plusieurs favoris correspondent, précise avec l'id (`fav list`).")
        before = len(cfg.favoris)
        cfg.favoris = [f for f in cfg.favoris if f not in match]
    else:  # add
        api = make_api(cfg, args.demo, interactive=True)
        espaces = [e for e in api.espaces() if e.get("cloud")]
        espace = find_espace(espaces, args.espace)
        nom = sanitize(args.nom or espace.get("titre"))
        existing = cfg.favori(int(espace["id"]))
        if existing:
            existing.nom = nom
        else:
            cfg.favoris.append(Favori(id=int(espace["id"]), nom=nom))
        cfg.save()
        print(f"★ « {nom} » ajouté à la racine.")
        return 0

    if len(cfg.favoris) == before:
        print("Aucun favori supprimé.")
        return 1
    cfg.save()
    print("Favori supprimé.")
    return 0


def build_app(backend: Backend, cfg: Config):
    from wsgidav.wsgidav_app import WsgiDAVApp

    return WsgiDAVApp({
        "host": cfg.host,
        "port": cfg.port,
        "provider_mapping": {"/": EcoleDirecteProvider(backend)},
        "simple_dc": {"user_mapping": {"*": True}},  # anonyme : écoute en local uniquement
        "http_authenticator": {"domain_controller": None, "accept_basic": True,
                               "accept_digest": False, "default_to_digest": False},
        "dir_browser": {"enable": True, "response_trailer": "EcoleDirecte – lecture seule"},
        "lock_storage": True,
        "property_manager": None,
        "verbose": 2,
        "logging": {"enable": False},
    })


def file_cache(watcher: ConfigWatcher, demo: bool = False) -> FileCache:
    return FileCache(CACHE_DIR / ("demo" if demo else "files"),
                     max_bytes=lambda: watcher.get().cache_max_bytes)


def _taille(n: int) -> str:
    for unit in ("o", "Ko", "Mo"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "o" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} Go"


def cmd_cache(args) -> int:
    watcher = ConfigWatcher()
    cfg = watcher.get()
    files = file_cache(watcher)
    if args.cache_cmd == "clear":
        print(f"Cache vidé : {_taille(files.clear())} libérés.")
        return 0
    if args.cache_cmd == "limit":
        cfg.cache_max_mo = args.mo
        cfg.save()
        freed = files.prune()
        print(f"Limite du cache : {'aucune' if args.mo <= 0 else f'{args.mo} Mo'}"
              + (f" ({_taille(freed)} libérés)." if freed else "."))
        return 0
    count, size = files.usage()
    limite = f"{cfg.cache_max_mo} Mo" if cfg.cache_max_mo > 0 else "aucune"
    print(f"Dossier : {files.root}")
    print(f"Contenu : {count} fichier(s), {_taille(size)}  (limite : {limite})")
    return 0


def cmd_serve(args) -> int:
    from cheroot import wsgi

    watcher = ConfigWatcher()
    cfg = watcher.get()
    if args.port:
        cfg.port = args.port
    api = make_api(cfg, args.demo, interactive=False)
    files = file_cache(watcher, demo=args.demo)
    files.prune()  # applique la limite dès le démarrage
    backend = Backend(api, watcher, files)

    if not args.demo:
        try:
            api.login()
        except EcoleDirecteError as exc:
            print(f"Connexion EcoleDirecte impossible : {exc}", file=sys.stderr)
            return 1

    app = build_app(backend, cfg)
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"⚠️  Le serveur écoute sur {cfg.host} : il est accessible depuis le réseau.",
              file=sys.stderr)
    server = wsgi.Server((cfg.host, cfg.port), app, server_name="ecoledirecte-dav", numthreads=8)
    print(f"Passerelle prête : http://localhost:{cfg.port}/  (Finder : ⌘K)"
          f"{'  [mode démo]' if args.demo else ''}")
    try:
        server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def cmd_install_agent(args) -> int:
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    AGENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [sys.executable, "-m", "ecoledirecte_dav", "serve"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "StandardOutPath": str(AGENT_LOG),
        "StandardErrorPath": str(AGENT_LOG),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    with AGENT_PLIST.open("wb") as fh:
        plistlib.dump(plist, fh)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(AGENT_PLIST)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(AGENT_PLIST)], check=True)
    print(f"Agent installé ({AGENT_PLIST}). Journal : {AGENT_LOG}")
    return 0


def cmd_uninstall_agent(args) -> int:
    if AGENT_PLIST.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(AGENT_PLIST)],
                       capture_output=True)
        AGENT_PLIST.unlink()
        print("Agent désinstallé.")
    else:
        print("Aucun agent installé.")
    return 0


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ecoledirecte-dav",
        description="Passerelle WebDAV locale (lecture seule) vers tes espaces de travail EcoleDirecte.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="journal détaillé")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="se connecter et enregistrer les identifiants dans le Trousseau")
    sub.add_parser("logout", help="supprimer les identifiants du Trousseau")

    p = sub.add_parser("espaces", help="lister les espaces de travail")
    p.add_argument("--all", action="store_true", help="inclure les espaces sans cloud")
    p.add_argument("--debug", action="store_true", help="afficher les réponses brutes de l'API")
    p.add_argument("--debug-id", type=int, help="avec --debug : un seul espace")
    p.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)

    p = sub.add_parser("fav", help="gérer les favoris affichés à la racine")
    fav = p.add_subparsers(dest="fav_cmd", required=True)
    fav.add_parser("list", help="lister les favoris")
    a = fav.add_parser("add", help="ajouter un favori")
    a.add_argument("espace", help="id ou (partie du) titre de l'espace")
    a.add_argument("--nom", help="nom affiché à la racine (par défaut : titre de l'espace)")
    a.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    r = fav.add_parser("remove", help="retirer un favori")
    r.add_argument("espace", help="id ou nom du favori")

    p = sub.add_parser("cache", help="voir, limiter ou vider le cache des fichiers")
    cache = p.add_subparsers(dest="cache_cmd")
    cache.add_parser("info", help="taille actuelle et limite (par défaut)")
    cache.add_parser("clear", help="supprimer tous les fichiers en cache")
    lim = cache.add_parser("limit", help="changer la taille maximale")
    lim.add_argument("mo", type=int, help="taille maximale en Mo (0 = sans limite)")

    p = sub.add_parser("serve", help="démarrer le serveur WebDAV")
    p.add_argument("--port", type=int, help=f"port (défaut : config, {Config().port})")
    p.add_argument("--demo", action="store_true", help="données factices, sans compte EcoleDirecte")

    sub.add_parser("install-agent", help="démarrer automatiquement à l'ouverture de session")
    sub.add_parser("uninstall-agent", help="retirer le démarrage automatique")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        logging.getLogger("wsgidav").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    handlers = {
        "login": cmd_login,
        "logout": cmd_logout,
        "espaces": cmd_espaces,
        "fav": cmd_fav,
        "cache": cmd_cache,
        "serve": cmd_serve,
        "install-agent": cmd_install_agent,
        "uninstall-agent": cmd_uninstall_agent,
    }
    try:
        return handlers[args.cmd](args)
    except EcoleDirecteError as exc:
        print(f"Erreur EcoleDirecte : {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


__all__ = ["main", "config_file"]
