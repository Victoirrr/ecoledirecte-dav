import json
import unicodedata

import pytest

from ecoledirecte_dav.api import Client, EcoleDirecteError
from ecoledirecte_dav.cache import FileCache
from ecoledirecte_dav.cli import build_app
from ecoledirecte_dav.config import Config, ConfigWatcher, Favori
from ecoledirecte_dav.demo import DemoApi
from ecoledirecte_dav.provider import ESPACES_DIR, Backend, EcoleDirecteProvider


@pytest.fixture
def cfg_path(tmp_path):
    return tmp_path / "config.toml"


@pytest.fixture
def backend(tmp_path, cfg_path):
    Config(favoris=[Favori(101, "Maths 2nde B")]).save(cfg_path)
    return Backend(DemoApi(), ConfigWatcher(cfg_path), FileCache(tmp_path / "cache"))


def resolve(backend, path):
    provider = EcoleDirecteProvider(backend)
    return provider.get_resource_inst(path, {"REQUEST_METHOD": "PROPFIND", "wsgidav.provider": provider})


def test_root_has_espaces_dir_and_favoris(backend):
    root = resolve(backend, "/")
    assert root.get_member_names() == [ESPACES_DIR, "Maths 2nde B"]


def test_espaces_filtered_to_member_clouds(backend):
    names = resolve(backend, f"/{ESPACES_DIR}").get_member_names()
    assert names == ["Club Théâtre", "Maths 2nde B"]


def test_favori_is_same_content_as_espace(backend):
    a = resolve(backend, f"/{ESPACES_DIR}/Maths 2nde B").get_member_names()
    b = resolve(backend, "/Maths 2nde B").get_member_names()
    assert a == b
    assert "Corbeille" not in a  # isTrash filtré
    assert {"Notes.txt", "Notes (2).txt"} <= set(a)  # doublons renommés


def test_lazy_folder_is_fetched(backend):
    names = resolve(backend, "/Maths 2nde B/Exercices").get_member_names()
    assert names == ["Fiche 1 – Fonctions.txt", "Fiche 2 – Équations.txt"]
    # L'API attend le chemin relatif à la racine de l'espace, pas l'id complet.
    assert ("cloud", 101, r"\Exercices") in backend.api.calls


def test_lazy_folder_never_contains_itself(backend):
    """Si l'API renvoie le dossier lui-même sans enfants, on n'affiche pas de boucle."""
    real_cloud = backend.api.cloud
    # Simule l'ancien bug : l'id de l'espace se retrouve doublé, l'API renvoie
    # alors le dossier lui-même, non chargé et sans enfants.
    from ecoledirecte_dav.demo import ROOT_E1

    backend.api.cloud = lambda eid, fid=None: real_cloud(eid, ROOT_E1 + fid if fid else fid)
    assert resolve(backend, "/Maths 2nde B/Exercices").get_member_names() == []
    assert resolve(backend, "/Maths 2nde B/Exercices/Exercices") is None


def test_nfd_path_is_resolved(backend):
    nfd = unicodedata.normalize("NFD", "/Maths 2nde B/Exercices/Fiche 2 – Équations.txt")
    assert resolve(backend, nfd) is not None


def test_forbidden_chars_replaced(backend):
    assert resolve(backend, f"/{ESPACES_DIR}/Club Théâtre").get_member_names() == [
        "Planning- répétitions.txt"
    ]


def test_junk_names_short_circuit(backend):
    calls = len(backend.api.calls)
    assert resolve(backend, "/._Maths 2nde B") is None
    assert resolve(backend, "/Maths 2nde B/.DS_Store") is None
    assert len(backend.api.calls) == calls


def test_unknown_paths(backend):
    assert resolve(backend, "/Inconnu") is None
    assert resolve(backend, "/Maths 2nde B/Lisez-moi.txt/x") is None


def test_file_content_and_cache(backend):
    res = resolve(backend, "/Maths 2nde B/Lisez-moi.txt")
    with res.get_content() as fh:
        assert fh.read() == "Bienvenue dans l'espace Maths !\n".encode()
    res.get_content().close()
    downloads = [c for c in backend.api.calls if c[0] == "download"]
    assert len(downloads) == 1
    assert res.get_content_type() == "text/plain"


def test_favoris_reload_and_collisions(backend, cfg_path):
    cfg = Config.load(cfg_path)
    cfg.favoris.append(Favori(102, ESPACES_DIR))  # nom réservé
    cfg.favoris.append(Favori(999, "Disparu"))  # espace inexistant : ignoré
    cfg.save(cfg_path)
    import os, time

    os.utime(cfg_path, (time.time() + 5, time.time() + 5))
    names = resolve(backend, "/").get_member_names()
    assert names == [ESPACES_DIR, "Maths 2nde B", f"{ESPACES_DIR} (102)"]


# ---- WSGI de bout en bout ----------------------------------------------------
def wsgi_call(app, method, path, headers=None):
    import io

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "SCRIPT_NAME": "",
        "QUERY_STRING": "",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "8080",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(b""),
        "wsgi.errors": io.StringIO(),
        "CONTENT_LENGTH": "0",
    }
    for k, v in (headers or {}).items():
        environ["HTTP_" + k.upper().replace("-", "_")] = v
    status = {}

    def start_response(s, h, exc_info=None):
        status["s"], status["h"] = s, dict(h)

    body = b"".join(app(environ, start_response))
    return int(status["s"].split()[0]), status["h"], body


@pytest.fixture
def app(backend):
    return build_app(backend, Config())


def test_wsgi_propfind_get_and_readonly(app):
    code, _, body = wsgi_call(app, "PROPFIND", "/Maths 2nde B/", {"Depth": "1"})
    assert code == 207 and b"Lisez-moi.txt" in body
    code, headers, body = wsgi_call(app, "GET", "/Maths 2nde B/Lisez-moi.txt")
    assert code == 200 and body.startswith(b"Bienvenue")
    assert int(headers["Content-Length"]) == len(body)
    for method in ("PUT", "DELETE", "MKCOL"):
        code, _, _ = wsgi_call(app, method, "/Maths 2nde B/nouveau.txt")
        assert code in (403, 405), method


# ---- client API (sans réseau) --------------------------------------------------
class FakeResp:
    def __init__(self, payload, headers=None):
        from requests.structures import CaseInsensitiveDict

        self._payload, self.cookies = payload, {}
        self.headers = CaseInsensitiveDict(headers or {})

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload

    @property
    def content(self):
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")


class FakeCoffre:
    mot_de_passe = "x"
    fa = None


def test_client_double_auth_and_relogin(monkeypatch):
    """Flux observé fin 2025 : en-têtes x-token + 2fa-token renvoyés à chaque étape."""
    import base64

    b64 = lambda s: base64.b64encode(s.encode()).decode()
    account = {"accounts": [{"id": 42, "typeCompte": "E"}]}
    login_answers = [
        FakeResp({"code": 250, "token": ""}, {"x-token": "tmp", "2fa-token": "fa1"}),
        FakeResp({"code": 200, "token": "", "data": account}, {"x-token": "tok1", "2fa-token": "fa4"}),
        FakeResp({"code": 200, "token": "", "data": account}, {"x-token": "tok2", "2fa-token": "fa5"}),
    ]
    sent = []
    coffre = FakeCoffre()

    def post(url, params=None, data=None, headers=None, timeout=None):
        body = json.loads(data["data"])
        sent.append((url.rsplit("/v3/", 1)[1], params, body, headers))
        if url.endswith("login.awp"):
            return login_answers.pop(0)
        if url.endswith("doubleauth.awp") and params["verbe"] == "get":
            assert headers["2FA-Token"] == "fa1"
            return FakeResp({"code": 200, "data": {"question": b64("Ton année de naissance ?"),
                                                   "propositions": [b64("2008"), b64("2009")]}},
                            {"2fa-token": "fa2"})
        if url.endswith("doubleauth.awp"):
            assert headers["2FA-Token"] == "fa2"
            assert body == {"choix": b64("2009")}
            return FakeResp({"code": 200, "data": {"cn": "CN", "cv": "CV"}}, {"2fa-token": "fa3"})
        if "espacestravail" in url:
            if not any("espacestravail" in s[0] for s in sent[:-1]):
                return FakeResp({"code": 520})
            return FakeResp({"code": 200, "data": [{"id": 1, "titre": "A", "cloud": True}]})
        raise AssertionError(url)

    client = Client("eleve", coffre, on_double_auth=lambda q, p: p.index("2009"))
    monkeypatch.setattr(client.session, "post", post)
    monkeypatch.setattr(client, "_gtk", lambda: {"X-Gtk": "G"})

    assert client.espaces() == [{"id": 1, "titre": "A", "cloud": True}]
    assert coffre.fa == {"cn": "CN", "cv": "CV"}

    logins = [s for s in sent if s[0] == "login.awp"]
    assert "X-Token" not in logins[0][3] and "2FA-Token" not in logins[0][3]
    relogin_body, relogin_headers = logins[1][2], logins[1][3]
    assert relogin_body["fa"] == [{"cn": "CN", "cv": "CV", "uniq": "false"}]
    assert (relogin_body["cn"], relogin_body["cv"]) == ("CN", "CV")
    assert relogin_headers["2FA-Token"] == "fa3" and relogin_headers["X-Gtk"] == "G"

    espaces_calls = [s for s in sent if "espacestravail" in s[0]]
    assert espaces_calls[0][3] == {"X-Token": "tok1", "2FA-Token": "fa4"}
    assert espaces_calls[-1][3] == {"X-Token": "tok2", "2FA-Token": "fa5"}


def test_legacy_default_version_is_upgraded(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('identifiant = "e"\napi_version = "4.75.0"\n')
    assert Config.load(p).api_version == Config().api_version != "4.75.0"
    p.write_text('identifiant = "e"\napi_version = "4.99.9"\n')
    assert Config.load(p).api_version == "4.99.9"


def test_client_bad_version(monkeypatch):
    client = Client("eleve", FakeCoffre())
    monkeypatch.setattr(client, "_gtk", lambda: {})
    monkeypatch.setattr(client.session, "post", lambda *a, **k: FakeResp({"code": 517}))
    with pytest.raises(EcoleDirecteError, match="api_version"):
        client.login()


def test_utf8_without_charset_is_decoded(monkeypatch):
    client = Client("eleve", FakeCoffre())
    resp = FakeResp({"code": 200, "data": [{"titre": "1ère Français"}]})
    resp.json = lambda: (_ for _ in ()).throw(AssertionError("ne pas utiliser resp.json()"))
    monkeypatch.setattr(client.session, "post", lambda *a, **k: resp)
    assert client._raw_post("x.awp", {}, {})["data"][0]["titre"] == "1ère Français"


def test_file_cache_evicts_least_recently_used(tmp_path):
    import os
    import time

    limit = {"v": None}
    fc = FileCache(tmp_path / "c", max_bytes=lambda: limit["v"])
    paths = {}
    for i, name in enumerate("abc"):
        paths[name] = fc.fetch((name,), lambda: iter([b"x" * 100]))
        old = time.time() - 100 + i  # a le plus ancien, c le plus récent
        os.utime(paths[name], (old, old))
    limit["v"] = 250
    # "c" a été écrit en dernier mais on relit "a" : "b" puis "c" doivent partir
    assert fc.fetch(("a",), lambda: iter([b"jamais"])) == paths["a"]
    fc.fetch(("d",), lambda: iter([b"y" * 100]))
    assert fc.cached("a") and fc.cached("d")
    assert fc.cached("b") is None and fc.cached("c") is None
    count, size = fc.usage()
    assert count == 2 and size == 200


def test_file_cache_keeps_oversized_current_file_and_clear(tmp_path):
    fc = FileCache(tmp_path / "c", max_bytes=lambda: 50)
    fc.fetch(("small",), lambda: iter([b"s" * 10]))
    big = fc.fetch(("big",), lambda: iter([b"b" * 100]))
    assert big.exists() and fc.cached("small") is None  # le fichier demandé reste servi
    assert fc.clear() == 100 and fc.usage() == (0, 0)


def test_file_cache_unlimited(tmp_path):
    fc = FileCache(tmp_path / "c", max_bytes=None)
    for n in range(5):
        fc.fetch((n,), lambda: iter([b"z" * 1000]))
    assert fc.prune() == 0 and fc.usage() == (5, 5000)
