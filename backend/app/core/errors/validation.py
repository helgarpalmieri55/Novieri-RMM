"""422 bodies without the submitted values.

FastAPI's default handler answers a RequestValidationError with
`{"detail": jsonable_encoder(exc.errors())}`, and every Pydantic error carries
`input`: the value that failed or, for a missing field, the WHOLE object it is
missing from. `POST /api/v1/auth/login` without `email` would hand the
password back in the response body -- to every proxy log, error report and
screenshot on the way. This answers the same shape with `input` removed from
each error. The console never read it either
(`frontend/src/components/form.tsx`).
"""

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


async def validation_error_without_input(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [
        {key: value for key, value in error.items() if key != "input"} for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})
