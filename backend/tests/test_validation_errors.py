"""A 422 never hands back what was submitted (P2 phase 1, Task 6 Step 9b).

Pydantic puts the failing value in each error's `input` -- for a missing
field, the whole object it is missing from, secrets included. Protects
`app/core/errors/validation.py::validation_error_without_input` and its
registration in `app/main.py`: remove either and these fail.
"""

from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.errors.validation import validation_error_without_input

MARKER = "Validation-Echo-Marker-7"


class _Login(BaseModel):
    email: str
    password: str


def _app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_error_without_input)

    @app.post("/login")
    def login(body: _Login) -> dict[str, str]:
        return {"email": body.email}

    return app


def test_a_missing_field_does_not_echo_the_rest_of_the_body() -> None:
    response = TestClient(_app()).post("/login", json={"password": MARKER})

    assert response.status_code == 422
    assert MARKER not in response.text
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "email"]
    assert error["msg"] == "Field required"
    assert "input" not in error


def test_a_wrong_type_does_not_echo_the_value() -> None:
    body = {"email": "a@example.com", "password": [MARKER]}
    response = TestClient(_app()).post("/login", json=body)

    assert response.status_code == 422
    assert MARKER not in response.text
    assert all("input" not in error for error in response.json()["detail"])


async def test_the_platform_answers_its_422s_with_this_handler(api_env: Any) -> None:
    """The registration in `create_app` -- without it the two tests above
    would still pass and every route would echo."""
    assert api_env.app.exception_handlers[RequestValidationError] is validation_error_without_input
