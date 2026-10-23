import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.locale_middleware import locale_middleware
from app.core.i18n import CATALOG_ES, locale_var, parse_accept_language, tr, use_locale


def test_english_is_the_template_formatted():
    assert tr("Run {run_id} is not paused.", run_id="r1") == "Run r1 is not paused."


def test_spanish_comes_from_the_catalogue(monkeypatch):
    monkeypatch.setitem(
        CATALOG_ES, "Run {run_id} is not paused.", "La ejecución {run_id} no está en pausa."
    )
    with use_locale("es"):
        assert tr("Run {run_id} is not paused.", run_id="r1") == "La ejecución r1 no está en pausa."


def test_missing_spanish_falls_back_to_english():
    with use_locale("es"):
        assert tr("No such message {x}.", x=1) == "No such message 1."


def test_accept_language_keeps_only_supported():
    assert parse_accept_language("es-CO,es;q=0.9,en;q=0.8") == "es"
    assert parse_accept_language("fr-FR") == "en"
    assert parse_accept_language(None) == "en"
    assert parse_accept_language("en-GB") == "en"


def test_middleware_sets_the_language_for_the_request(monkeypatch):
    monkeypatch.setitem(CATALOG_ES, "Nope.", "No.")
    app = FastAPI()
    app.middleware("http")(locale_middleware)

    @app.get("/x")
    async def x() -> None:
        raise HTTPException(409, tr("Nope."))

    client = TestClient(app)
    assert client.get("/x", headers={"Accept-Language": "es"}).json() == {"detail": "No."}
    assert client.get("/x").json() == {"detail": "Nope."}


def test_accept_language_honours_weights_and_refusals():
    assert parse_accept_language("en;q=0.5, es;q=0.8") == "es"
    assert parse_accept_language("es;q=0.2, en;q=0.9") == "en"
    # q=0 means "not acceptable": never pick a language the client refused.
    assert parse_accept_language("es;q=0") == "en"
    assert parse_accept_language("es;q=0, en;q=0.1") == "en"
    # Whitespace around the weight, upper case tags, and a wildcard.
    assert parse_accept_language("en;q=0.1, es ; q=0.5") == "es"
    assert parse_accept_language("ES-co") == "es"
    assert parse_accept_language("*") == "en"
    # Garbage never raises and never selects anything but English.
    assert parse_accept_language("es;q=abc") == "en"
    assert parse_accept_language("") == "en"
    assert parse_accept_language(",,;q=") == "en"
    # Equal weights: the client's own order wins.
    assert parse_accept_language("es, en") == "es"
    assert parse_accept_language("en, es") == "en"


def test_use_locale_ignores_an_unsupported_language_and_always_resets():
    with use_locale("fr"):
        assert locale_var.get() == "en"
    try:
        with use_locale("es"):
            assert locale_var.get() == "es"
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert locale_var.get() == "en"


def test_tr_formats_with_str_format_only(monkeypatch):
    # The catalogue is data: an attribute or index walk is plain str.format and
    # nothing in a template is ever evaluated as an expression.
    monkeypatch.setitem(CATALOG_ES, "Hello {name}.", "Hola {name}.")
    with use_locale("es"):
        assert (
            tr("Hello {name}.", name="__import__('os').getcwd()")
            == "Hola __import__('os').getcwd()."
        )
        assert tr("Braces {{literal}} {n}.", n=3) == "Braces {literal} 3."


def _app_with_everything() -> FastAPI:
    """Middleware registered the way create_app does, with an inner
    middleware, a dependency and an exception handler that all call tr()."""
    from fastapi import Depends, Request
    from fastapi.responses import JSONResponse

    class Refused(Exception):
        pass

    app = FastAPI()
    seen: dict[str, str] = {}

    async def inner(request: Request, call_next):  # type: ignore[no-untyped-def]
        seen["inner_before"] = locale_var.get()
        response = await call_next(request)
        seen["inner_after"] = locale_var.get()
        return response

    app.middleware("http")(inner)
    app.middleware("http")(locale_middleware)

    async def dep() -> str:
        seen["dependency"] = locale_var.get()
        return tr("Dependency says {x}.", x="hi")

    @app.exception_handler(Refused)
    async def handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": tr("Handler says {x}.", x="hi")}, status_code=409)

    @app.get("/dep")
    async def route_dep(msg: str = Depends(dep)) -> dict[str, str]:
        return {"msg": msg}

    @app.get("/handled")
    async def route_handled() -> None:
        raise Refused

    @app.get("/boom")
    async def route_boom() -> None:
        raise RuntimeError("boom")

    app.state.seen = seen
    return app


def test_the_language_is_set_before_inner_layers_and_handlers_and_reset_after(monkeypatch):
    monkeypatch.setitem(CATALOG_ES, "Dependency says {x}.", "La dependencia dice {x}.")
    monkeypatch.setitem(CATALOG_ES, "Handler says {x}.", "El manejador dice {x}.")
    app = _app_with_everything()
    client = TestClient(app, raise_server_exceptions=False)
    es = {"Accept-Language": "es"}

    r = client.get("/dep", headers=es)
    assert r.json() == {"msg": "La dependencia dice hi."}
    assert app.state.seen["inner_before"] == "es"
    assert app.state.seen["dependency"] == "es"
    assert app.state.seen["inner_after"] == "es"

    # The exception handler that builds the error response is covered too.
    r = client.get("/handled", headers=es)
    assert (r.status_code, r.json()) == (409, {"detail": "El manejador dice hi."})
    assert client.get("/handled").json() == {"detail": "Handler says hi."}

    assert client.get("/dep").json() == {"msg": "Dependency says hi."}


def test_the_response_declares_its_language():
    client = TestClient(_app_with_everything())
    assert (
        client.get("/dep", headers={"Accept-Language": "es-MX"}).headers["content-language"] == "es"
    )
    assert client.get("/dep", headers={"Accept-Language": "fr"}).headers["content-language"] == "en"
    assert client.get("/dep").headers["content-language"] == "en"


def test_an_unhandled_error_does_not_make_the_next_request_spanish():
    client = TestClient(_app_with_everything(), raise_server_exceptions=False)
    assert client.get("/boom", headers={"Accept-Language": "es"}).status_code == 500
    assert client.get("/dep").json() == {"msg": "Dependency says hi."}


def test_create_app_registers_the_locale_layer_outermost():
    """Starlette runs the LAST-registered http middleware outermost. If
    something is registered after the locale layer, that something runs
    before the language is set."""
    from tests.tool_surface import build_router_only_app

    app = build_router_only_app()
    assert app.user_middleware[0].kwargs["dispatch"] is locale_middleware


def _request(accept_language: str | None):
    from starlette.requests import Request

    headers = [(b"accept-language", accept_language.encode())] if accept_language else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


async def test_the_middleware_sets_the_language_inside_and_resets_it_after():
    """Called directly in THIS task, so `locale_var` here is the very variable the
    middleware sets: a leak would be visible, unlike through TestClient, whose
    server side runs in another thread and context."""
    from starlette.responses import Response

    inside: list[str] = []

    async def call_next(_request):  # type: ignore[no-untyped-def]
        inside.append(locale_var.get())
        return Response()

    assert locale_var.get() == "en"
    response = await locale_middleware(_request("es-CO,es;q=0.9"), call_next)
    assert inside == ["es"]
    assert locale_var.get() == "en"
    assert response.headers["content-language"] == "es"


async def test_the_middleware_resets_the_language_when_the_app_raises():
    async def call_next(_request):  # type: ignore[no-untyped-def]
        assert locale_var.get() == "es"
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await locale_middleware(_request("es"), call_next)
    assert locale_var.get() == "en"


def _vary_tokens(response) -> list[str]:  # type: ignore[no-untyped-def]
    return [t.strip() for v in response.headers.get_list("vary") for t in v.split(",")]


def test_vary_accept_language_is_on_every_response():
    client = TestClient(_app_with_everything())
    assert _vary_tokens(client.get("/dep")) == ["Accept-Language"]
    assert _vary_tokens(client.get("/dep", headers={"Accept-Language": "es"})) == [
        "Accept-Language"
    ]
    assert _vary_tokens(client.get("/nowhere")) == ["Accept-Language"]  # a 404 varies too


def test_vary_keeps_a_value_the_route_already_set():
    from fastapi import Response

    app = FastAPI()
    app.middleware("http")(locale_middleware)

    @app.get("/one")
    async def one() -> Response:
        return Response("x", headers={"Vary": "Sec-CH-UA-Arch, User-Agent"})

    @app.get("/two-lines")
    async def two() -> Response:
        r = Response("x")
        r.headers.append("Vary", "Origin")
        r.headers.append("Vary", "User-Agent")
        return r

    @app.get("/star")
    async def star() -> Response:
        return Response("x", headers={"Vary": "*"})

    @app.get("/already")
    async def already() -> Response:
        return Response("x", headers={"Vary": "origin, accept-language"})

    client = TestClient(app)
    assert _vary_tokens(client.get("/one")) == ["Sec-CH-UA-Arch", "User-Agent", "Accept-Language"]
    assert _vary_tokens(client.get("/two-lines")) == ["Origin", "User-Agent", "Accept-Language"]
    assert _vary_tokens(client.get("/star")) == ["*"]  # already varies on everything
    assert _vary_tokens(client.get("/already")) == ["origin", "accept-language"]  # not doubled


def test_accept_language_input_is_capped():
    assert parse_accept_language("x," * 400 + "es") == "en"  # `es` sits beyond 512 chars
    assert parse_accept_language("es," + "x" * 5000) == "es"
    assert parse_accept_language("x" * 5000 + ",es") == "en"


def test_a_broken_spanish_template_falls_back_to_english_and_warns(monkeypatch, caplog):
    import logging

    monkeypatch.setitem(CATALOG_ES, "Run {run_id} stopped.", "La ejecución {run} se detuvo.")
    monkeypatch.setitem(CATALOG_ES, "Item {n} done.", "Elemento {0} listo.")
    monkeypatch.setitem(CATALOG_ES, "Obj {o} done.", "Objeto {o.nope} listo.")
    monkeypatch.setitem(CATALOG_ES, "Brace {n}.", "Llave } {n}.")
    with use_locale("es"), caplog.at_level(logging.WARNING, logger="app.core.i18n"):
        assert tr("Run {run_id} stopped.", run_id="r1") == "Run r1 stopped."
        assert tr("Item {n} done.", n=3) == "Item 3 done."
        assert tr("Obj {o} done.", o=1) == "Obj 1 done."
        assert tr("Brace {n}.", n=1) == "Brace 1."
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 4
    assert all("r1" not in r.getMessage() for r in caplog.records)  # params are not logged


def test_english_formatting_still_raises_on_a_bad_template():
    with pytest.raises(KeyError):
        tr("Run {run_id} stopped.", other=1)
    with pytest.raises(KeyError):
        tr("Run {run_id} stopped.")  # a forgotten parameter is loud, never a literal brace


def test_tr_always_formats_so_escaped_braces_agree_with_or_without_params(monkeypatch):
    monkeypatch.setitem(CATALOG_ES, "Use {{braces}}.", "Usa {{llaves}}.")
    assert tr("Use {{braces}}.") == "Use {braces}."
    with use_locale("es"):
        assert tr("Use {{braces}}.") == "Usa {llaves}."
