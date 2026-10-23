"""S3/MinIO object storage wrapper (docs/04-data-model.md bucket layout).

Large artifacts live here, addressed by s3:// references from the database.
boto3 is synchronous, so calls run in the default thread executor.

# Presigned grants: the one place an endpoint is allowed to reach this bucket

`s3://` references are a SERVER-SIDE addressing scheme. An agent cannot
resolve one, and it must never be given credentials that would let it
(`CLAUDE.md` invariant #3 puts stored credentials in the vault, decrypted at
point of use — a bucket credential resident on every endpoint in the fleet is
that invariant inverted, and one compromised endpoint would reach every other
tenant's objects).

The presign helpers below are the whole of the sanctioned alternative: a
short-lived, narrowly scoped, signed URL minted by the control plane and
carried to the endpoint INSIDE the signed job envelope. Three properties are
structural rather than conventional, and every helper here preserves all
three:

1. **The key is derived, never accepted.** Every helper builds its object key
   from the same `_*_key` method the writer used, out of ids the caller
   already had to be authorized for. A caller cannot ask for "that key over
   there"; where a stored `s3://` ref is available it is checked AGAINST the
   derived key rather than used in place of it.
2. **The grant is bounded in time.** `expires_in` is mandatory and capped
   (`MAX_PRESIGNED_TTL_S`). Callers derive it from the lifetime of the job
   that carries it — a URL that outlives its job is a standing grant.
3. **The URL is a bearer credential for its lifetime.** Anyone holding it can
   perform exactly the one operation it was signed for. It is never logged,
   never audited, and never returned by an API route (see
   `JobType.params_redactor`).

Presigning is pure computation — SigV4 query signing touches no socket — so
these are ordinary sync methods rather than `_run`-wrapped coroutines.

# The two endpoints, and why signing uses the other one

Everything above is about WHAT is signed. `resolve_presign_endpoint` below is
about WHERE it points, which was wrong in a way none of the guards could
catch: the whole store — reads, writes and signatures alike — used
`ITS_S3_ENDPOINT`, which in the bundled stack is `http://minio:9000`. That is
a Docker-internal name. Every presigned URL this module minted named a host no
endpoint in the fleet can resolve, so the grant was structurally correct and
practically dead on arrival.

Internal traffic must keep using the internal name (bulk object I/O should not
leave the container network), so the two are separate settings and only the
signing client uses the public one. `ITS_S3_PUBLIC_ENDPOINT` unset in
production raises rather than falling back: the fallback dispatches a job that
looks fine here and fails on the endpoint with a DNS error.
"""

import asyncio
import re
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import boto3
from botocore.config import Config as BotoConfig

from app.core.config import Settings
from app.core.i18n import tr

# Hard ceiling on any presigned grant this platform issues, independent of what
# a caller asks for. Job TTLs are configuration (`ITS_JOB_DEFAULT_TTL_S`) and
# configuration drifts; this is the value that stops a mistyped TTL from
# minting a bearer URL that outlives the incident it was issued during.
MAX_PRESIGNED_TTL_S = 12 * 3600


def _safe_object_suffix(suffix: str) -> str:
    """Reject a relative object suffix that could address anything outside the
    prefix it is about to be appended to.

    S3 keys are opaque strings and the service does not normalise `..`, but
    every HTTP client, proxy and CDN between here and the bucket may, so a key
    containing one is a way to reach a sibling tenant's prefix on SOME path.
    Rejected rather than normalised: normalising means guessing which of the
    two readings a caller meant.
    """
    if not suffix or suffix.startswith("/") or suffix.endswith("/"):
        raise ValueError(tr("object suffix must be a non-empty relative path"))
    if "\\" in suffix or "//" in suffix:
        raise ValueError(tr("object suffix must not contain a backslash or an empty segment"))
    if any(segment in ("", ".", "..") for segment in suffix.split("/")):
        raise ValueError(tr("object suffix must not contain a '.' or '..' segment"))
    if any(ch < " " or ch == "\x7f" for ch in suffix):
        raise ValueError(tr("object suffix must not contain control characters"))
    return suffix


#: The OS/arch pairs `agent/packaging/build.sh` cross-compiles. Presigning a
#: pair outside this set mints a grant for an object nothing ever uploaded,
#: which surfaces as a 404 on the endpoint at install time rather than as an
#: error where an operator is looking.
AGENT_RELEASE_OS = frozenset({"linux", "windows", "darwin"})
AGENT_RELEASE_ARCH = frozenset({"amd64", "arm64"})

#: Everything this platform publishes for endpoints to download lives under one
#: prefix, so a grant for an artefact can never be a grant for a tenant's data.
AGENT_RELEASE_PREFIX = "agent-releases"

#: A git short SHA or a semver tag. See agent_release_suffix for why this
#: is an allowlist rather than a denylist.
_AGENT_VERSION_RE = re.compile(r"[A-Za-z0-9._-]+")


def agent_release_suffix(version: str, os_name: str, arch: str) -> str:
    """The object suffix for one agent artefact, or ValueError.

    THE UNTRUSTED PART IS `version`. Every other presign path in this module
    derives its prefix from an `org_id` this process is already authorized for,
    so a caller cannot name a location outside their tenant. `agent_releases`
    has no org_id at all (docs/modules/agent-distribution/spec.md §4.2), so the
    prefix is built from three strings an operator typed -- and a version of
    `../../backups/<someone>` would address a tenant's backup chunks from a
    route whose permission check knows nothing about tenants.

    `_safe_object_suffix` does the rejecting, reused rather than restated: an
    earlier hand-rolled equivalent in this codebase silently omitted its
    control-character check.

    `os_name` and `arch` are checked against the sets build.sh actually
    produces rather than merely sanitised, because an alias like `x86_64` is
    perfectly safe and still wrong -- it names an object that was never
    uploaded.
    """
    if os_name not in AGENT_RELEASE_OS:
        raise ValueError(
            tr(
                "unknown agent OS {os_name}; expected one of {agent_release_os}",
                os_name=repr(os_name),
                agent_release_os=sorted(AGENT_RELEASE_OS),
            )
        )
    if arch not in AGENT_RELEASE_ARCH:
        raise ValueError(
            tr(
                "unknown agent architecture {arch}; expected one of {agent_release_arch}",
                arch=repr(arch),
                agent_release_arch=sorted(AGENT_RELEASE_ARCH),
            )
        )
    # Validate as a suffix segment first, so a traversal is refused rather than
    # normalised, then narrow to an ALLOWLIST.
    #
    # An allowlist rather than a denylist, even though the traversal check above
    # already covers the dangerous cases for an S3 key: in practice a version is
    # a git short SHA or a semver tag, both of which fit `[A-Za-z0-9._-]`
    # comfortably. Characters like `;` or `%` are harmless in a key and are
    # refused anyway, because this string is also rendered into operator-facing
    # output and into the filename an endpoint downloads to -- and the next
    # place it gets interpolated is not necessarily as safe as this one. The
    # cost of being strict is an operator renaming a tag; the cost of being
    # permissive is discovered later, somewhere else.
    safe_version = _safe_object_suffix(version)
    if not _AGENT_VERSION_RE.fullmatch(safe_version):
        raise ValueError(
            tr(
                "agent release version must be a single segment of letters, digits, dot, "
                "underscore or hyphen; got {version}",
                version=repr(version),
            )
        )
    return f"{AGENT_RELEASE_PREFIX}/{safe_version}/its-agent-{os_name}-{arch}"


class PublicEndpointError(ValueError):
    """The public object-storage endpoint is missing or cannot be signed against.

    Raised at `ObjectStore` construction, not at presign time: a deployment
    that cannot mint a usable grant should fail where an operator is looking,
    not on the endpoint at 02:00 inside a maintenance window.
    """


def resolve_presign_endpoint(settings: Settings) -> str | None:
    """The endpoint URL a presigned grant is signed against, or a hard error.

    Returns `None` only in dev/test with nothing configured, which is boto3's
    "use the real AWS endpoint" and is what the moto-backed suite runs on.

    THREE REFUSALS, AND EACH ONE IS A URL THAT WOULD BE SIGNED SUCCESSFULLY AND
    THEN FAIL IN THE FIELD:

    1. **Unset, in production.** The only available fallback is the internal
       name (`http://minio:9000`), which resolves in the container network and
       nowhere else — so the grant would be well-formed, correctly scoped,
       correctly time-bounded, and unreachable. That is the defect this module
       was changed to fix; re-introducing it quietly is worse than not
       shipping the route at all, because a route that is missing is at least
       visible from here.
    2. **Not `https`, in production.** A presigned URL is a bearer credential
       for its lifetime (see the module docstring). Over plaintext it is a
       bearer credential readable by anything on the path between the endpoint
       and the control plane.
    3. **Carrying a path.** MEASURED, not assumed. SigV4 signs the host AND
       the full path, so a proxy prefix has exactly two fates and both are
       broken: strip it and the path the store verifies is not the path that
       was signed (`SignatureDoesNotMatch`); keep it and S3 path-style
       addressing reads the first segment as the BUCKET NAME, so
       `https://host/objects/it-suite/key` asks for bucket `objects`
       (`NoSuchBucket`). The supported public route puts the bucket itself at
       the root of the shared hostname — `https://host/<bucket>/<key>`, which
       IS S3 path style, so nothing is rewritten and the signature covers what
       the store receives. See `deploy/docker-compose.yml`'s `minio` router.
    """
    public = (settings.s3_public_endpoint or "").strip()
    if not public:
        if settings.environment == "production":
            raise PublicEndpointError(  # i18n-exempt: misconfiguration, logged; client gets a 500
                "ITS_S3_PUBLIC_ENDPOINT is not set. Presigned URLs would be signed "
                f"against ITS_S3_ENDPOINT ({settings.s3_endpoint!r}), which is the "
                "control plane's own route to object storage — an agent cannot "
                "resolve it, so every app.deploy artifact download and backup "
                "upload would fail in the field. Set it to the object-storage URL "
                "ENDPOINTS reach (deploy/README.md, 'Object storage on :443')."
            )
        return settings.s3_endpoint

    parsed = urlparse(public)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PublicEndpointError(  # i18n-exempt: misconfiguration, logged; client gets a 500
            f"ITS_S3_PUBLIC_ENDPOINT={public!r} is not an absolute http(s) URL. It "
            "must be the scheme+host (+port) an endpoint reaches object storage on, "
            "e.g. https://itms.toeshee.com"
        )
    if settings.environment == "production" and parsed.scheme != "https":
        raise PublicEndpointError(  # i18n-exempt: misconfiguration, logged; client gets a 500
            f"ITS_S3_PUBLIC_ENDPOINT={public!r} is plaintext http. A presigned URL "
            "is a bearer credential for the object it names, for its whole "
            "lifetime; it must not cross the network in the clear."
        )
    if parsed.path.strip("/"):
        raise PublicEndpointError(  # i18n-exempt: misconfiguration, logged; client gets a 500
            f"ITS_S3_PUBLIC_ENDPOINT={public!r} carries a path. SigV4 signs the host "
            "and the full path, so a proxy prefix cannot work: stripping it breaks "
            "every signature, and keeping it makes S3 path-style addressing read the "
            "prefix as the bucket name. Give the scheme and host only — the bucket "
            f"is already the first path segment ({settings.s3_bucket!r}), which is "
            "what the public route matches on."
        )
    if parsed.query or parsed.fragment:
        raise PublicEndpointError(  # i18n-exempt: misconfiguration, logged; client gets a 500
            f"ITS_S3_PUBLIC_ENDPOINT={public!r} carries a query or fragment"
        )
    return public.rstrip("/")


def _check_ttl(expires_in: int) -> None:
    if expires_in < 1 or expires_in > MAX_PRESIGNED_TTL_S:
        raise ValueError(  # i18n-exempt: internal argument guard, unhandled 500
            f"presigned grant lifetime {expires_in}s is outside 1..{MAX_PRESIGNED_TTL_S}s"
        )


# S3 refuses parts under 5 MiB (except the last). Recordings upload in 8 MiB
# parts, so memory per upload is one part.
S3_MIN_PART_BYTES = 5 * 1024 * 1024
REMOTE_RECORDING_PART_BYTES = 8 * 1024 * 1024


class ObjectStore:
    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.s3_bucket
        kwargs: dict[str, Any] = {}
        if settings.s3_endpoint:
            kwargs["endpoint_url"] = settings.s3_endpoint
        if settings.s3_access_key:
            kwargs["aws_access_key_id"] = settings.s3_access_key
            kwargs["aws_secret_access_key"] = settings.s3_secret_key
        # SigV4 EXPLICITLY. boto3 still defaults `us-east-1` to the legacy
        # HMAC-SHA1 query signing, whose presigned URLs carry `Expires` as an
        # absolute epoch and no `X-Amz-Date`. SigV4 is what MinIO and every
        # current S3 region speak, and its `X-Amz-Date` + `X-Amz-Expires` pair
        # is what makes a grant's lifetime readable back out of the URL —
        # which is how `tests/test_deploy_artifacts.py` asserts that a grant
        # expires with the job that carries it rather than trusting the
        # `ExpiresIn` it passed in.
        kwargs["config"] = BotoConfig(signature_version="s3v4")
        self._client = boto3.client("s3", region_name="us-east-1", **kwargs)

        # THE SIGNING CLIENT. Identical credentials and bucket, different
        # endpoint: this one is never used to move a byte, only to compute the
        # host, path and signature of a grant an ENDPOINT will present. Built
        # eagerly so a deployment configured to mint unreachable URLs fails
        # here rather than at dispatch (see `resolve_presign_endpoint`).
        presign_endpoint = resolve_presign_endpoint(settings)
        if presign_endpoint == settings.s3_endpoint:
            # Dev/test, where the two are the same address. Reusing the object
            # keeps `mock_aws`-backed tests to a single patched client.
            self._signing_client = self._client
        else:
            signing_kwargs = dict(kwargs)
            signing_kwargs["endpoint_url"] = presign_endpoint
            # PATH-STYLE, PINNED. botocore's `auto` already chooses path style
            # for a custom endpoint, but the public route DEPENDS on it: the
            # bucket has to be the first path segment for the Traefik rule to
            # match, and virtual-host style would instead produce
            # `https://<bucket>.itms.toeshee.com/<key>` — a hostname that needs
            # a DNS record and a certificate SAN the operator does not have.
            # A botocore default that drifts must not silently break the fleet.
            signing_kwargs["config"] = BotoConfig(
                signature_version="s3v4", s3={"addressing_style": "path"}
            )
            self._signing_client = boto3.client(
                "s3", region_name="us-east-1", **signing_kwargs
            )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception:  # noqa: BLE001 - missing bucket is the expected case
            self._client.create_bucket(Bucket=self._bucket)

    async def _run(self, fn: Any, /, **kwargs: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(None, partial(fn, **kwargs))

    def _presign(
        self,
        client_method: str,
        key: str,
        *,
        expires_in: int,
        download_filename: str | None = None,
    ) -> str:
        """Sign one operation on ONE key. Never a prefix, never the bucket.

        `client_method` is `get_object` or `head_object`; a presigned URL for
        `list_objects_v2` would be a directory listing of a tenant's storage
        and is deliberately not reachable through this helper.

        `download_filename`, when given, is signed into the URL as a
        `ResponseContentDisposition` so the browser saves the object under that
        name rather than under the storage KEY. The key has no file extension
        -- it is `agent_release_suffix`'s (version, os, arch) triple -- so
        without this a Windows artefact downloads as `its-agent-windows-amd64`
        with no `.exe` and will not run. The name is part of the signature, so
        it cannot be swapped after the URL is minted.
        """
        if client_method not in ("get_object", "head_object"):
            raise ValueError(  # i18n-exempt: internal argument guard, unhandled 500
                f"refusing to presign {client_method!r}"
            )
        _check_ttl(expires_in)
        params: dict[str, str] = {"Bucket": self._bucket, "Key": key}
        if download_filename is not None:
            params["ResponseContentDisposition"] = (
                f'attachment; filename="{download_filename}"'
            )
        url: str = self._signing_client.generate_presigned_url(
            client_method,
            Params=params,
            ExpiresIn=expires_in,
        )
        return url

    async def put_agent_release(self, key: str, data: bytes) -> str:
        """Store one agent artefact under a key `agent_release_suffix` derived.

        Takes a KEY rather than (version, os, arch) only because the caller has
        already derived and validated it for the row it is updating -- deriving
        it twice would let the stored ref and the written object disagree if
        the two calls ever drifted.
        """
        await self._run(self._client.put_object, Bucket=self._bucket, Key=key, Body=data)
        return key

    async def delete_agent_release(self, key: str) -> None:
        """Remove one agent artefact, by the key `agent_release_suffix` derived.

        Deleting a key that is not there succeeds (S3 semantics), so a row
        whose file was never uploaded unpublishes cleanly."""
        await self._run(self._client.delete_object, Bucket=self._bucket, Key=key)

    def presign_agent_release_get(
        self,
        version: str,
        os_name: str,
        arch: str,
        *,
        expires_in: int,
        download_filename: str | None = None,
    ) -> str:
        """A short-lived GET grant for one agent artefact.

        The caller passes the release's IDENTITY, never a key or a stored
        `storage_ref`. That is the same rule the backup grants follow, and it
        matters more here than there: a `storage_ref` read back from the
        database is not automatically trustworthy -- it was a request body
        once -- so the key is DERIVED at signing time by
        `agent_release_suffix`, which refuses a traversal. Signing a stored
        string would trust whatever validation was in force when it was
        written.

        `download_filename` replaces the default saved-as name. The install link
        passes `ITMS-Agent-Setup_<host>[@<port>]_<code>.exe` (spec 2026-09-28
        §1.2, plan D6a): the bytes are the same published artefact, and the
        name is signed into the URL like the default one.
        """
        # The saved-as name, WITH the platform's extension. The key carries
        # none, so a Windows binary would otherwise land as an extensionless
        # file the OS will not run. `.exe` for Windows; Linux and macOS agents
        # are extensionless executables and take no suffix.
        filename = download_filename or (
            f"its-agent-{os_name}-{arch}{'.exe' if os_name == 'windows' else ''}"
        )
        return self._presign(
            "get_object",
            agent_release_suffix(version, os_name, arch),
            expires_in=expires_in,
            download_filename=filename,
        )

    async def get_agent_release(self, version: str, os_name: str, arch: str) -> bytes:
        """One agent artefact's bytes, addressed by the release's IDENTITY.

        The key is derived here by `agent_release_suffix`, exactly as for a
        grant, never read from a stored `storage_ref`. The Group Policy MSI
        reads the published Windows binary this way and re-hashes it against
        `agent_releases.sha256` before it wraps it (GPO MSI spec §2.2).
        """
        key = agent_release_suffix(version, os_name, arch)
        # The READ goes to the executor too: `Body` is a stream, and a
        # multi-megabyte binary read on the event loop would stall every
        # request this process serves.
        body: bytes = await self._run(self._get_object_bytes, key=key)
        return body

    def _get_object_bytes(self, *, key: str) -> bytes:
        """`get_object` and its body, in one blocking call for `_run`."""
        body: bytes = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        return body

    def agent_release_key(self, version: str, os_name: str, arch: str) -> str:
        """The full object key, for the upload path and for storing on the row.

        Derived by the same function that signs, so the value recorded in
        `agent_releases.storage_ref` cannot drift from the value a later grant
        addresses.
        """
        return agent_release_suffix(version, os_name, arch)

    async def health(self) -> bool:
        try:
            await self._run(self._client.head_bucket, Bucket=self._bucket)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _job_artifact_key(self, org_id: UUID, job_id: UUID, name: str) -> str:
        return f"{org_id}/job-artifacts/{job_id}/{name}"

    async def put_job_artifact(
        self, org_id: UUID, job_id: UUID, name: str, data: bytes
    ) -> str:
        key = self._job_artifact_key(org_id, job_id, name)
        await self._run(self._client.put_object, Bucket=self._bucket, Key=key, Body=data)
        return f"s3://{self._bucket}/{key}"

    async def get_job_artifact(self, ref: str) -> bytes:
        prefix = f"s3://{self._bucket}/"
        if not ref.startswith(prefix):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"artifact ref outside configured bucket: {ref!r}"
            )
        key = ref.removeprefix(prefix)
        response = await self._run(self._client.get_object, Bucket=self._bucket, Key=key)
        body: bytes = response["Body"].read()
        return body

    def _package_artifact_key(self, org_id: UUID, package_id: UUID) -> str:
        return f"{org_id}/package-artifacts/{package_id}"

    async def put_package_artifact(self, org_id: UUID, package_id: UUID, data: bytes) -> str:
        key = self._package_artifact_key(org_id, package_id)
        await self._run(self._client.put_object, Bucket=self._bucket, Key=key, Body=data)
        return f"s3://{self._bucket}/{key}"

    async def get_package_artifact(self, ref: str) -> bytes:
        prefix = f"s3://{self._bucket}/"
        if not ref.startswith(prefix):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"artifact ref outside configured bucket: {ref!r}"
            )
        key = ref.removeprefix(prefix)
        response = await self._run(self._client.get_object, Bucket=self._bucket, Key=key)
        body: bytes = response["Body"].read()
        return body

    def presign_package_artifact(
        self, org_id: UUID, package_id: UUID, *, ref: str, expires_in: int
    ) -> str:
        """A short-lived READ grant over exactly one package artifact.

        This is what an `app.deploy` job's `artifact_ref` carries: the agent
        holds no object-store credential and has no `s3://` route, so the
        control plane hands it an `https://` URL that is good for one GET of
        one object for `expires_in` seconds and nothing else
        (`docs/modules/app-deploy/spec.md` §6.1).

        THE KEY IS DERIVED FROM `org_id`/`package_id`, NOT PARSED OUT OF
        `ref`. `ref` is the value stored on the `packages` row and is only
        CHECKED against the derived key — so a row whose `artifact_ref` was
        somehow made to point at another tenant's object, at a bucket-wide
        prefix, or at a legacy layout yields a ValueError and no URL, rather
        than a signed grant over whatever it names. `get_budget_attachment`'s
        defence-in-depth check is the same idea; this one is stronger because
        it never uses the stored value at all.
        """
        key = self._package_artifact_key(org_id, package_id)
        if ref != f"s3://{self._bucket}/{key}":
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                "package artifact ref does not name this org's artifact object; "
                "refusing to presign"
            )
        return self._presign("get_object", key, expires_in=expires_in)

    def _remote_recording_key(
        self, org_id: UUID, session_id: UUID, ext: str = "guac"
    ) -> str:
        return f"{org_id}/remote-recordings/{session_id}.{ext}"

    async def put_remote_recording(self, org_id: UUID, session_id: UUID, data: bytes) -> str:
        key = self._remote_recording_key(org_id, session_id)
        await self._run(self._client.put_object, Bucket=self._bucket, Key=key, Body=data)
        return f"s3://{self._bucket}/{key}"

    async def put_remote_recording_stream(
        self,
        org_id: UUID,
        session_id: UUID,
        path: Path,
        *,
        ext: str = "rsp",
        part_size: int = REMOTE_RECORDING_PART_BYTES,
    ) -> str:
        """Upload a spooled recording from disk, multipart, one part in memory
        at a time. A failed upload is aborted so no orphaned parts are billed."""
        key = self._remote_recording_key(org_id, session_id, ext)
        await self._run(self._upload_file_multipart, key=key, path=path, part_size=part_size)
        return f"s3://{self._bucket}/{key}"

    def _upload_file_multipart(self, *, key: str, path: Path, part_size: int) -> None:
        if part_size < S3_MIN_PART_BYTES:
            raise ValueError(  # i18n-exempt: internal argument guard, unhandled 500
                f"part_size below the S3 minimum of {S3_MIN_PART_BYTES}"
            )
        size = path.stat().st_size
        if size <= part_size:
            with path.open("rb") as fh:
                self._client.put_object(Bucket=self._bucket, Key=key, Body=fh.read())
            return
        upload_id = self._client.create_multipart_upload(Bucket=self._bucket, Key=key)[
            "UploadId"
        ]
        try:
            parts: list[dict[str, Any]] = []
            with path.open("rb") as fh:
                number = 1
                while chunk := fh.read(part_size):
                    etag = self._client.upload_part(
                        Bucket=self._bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=number,
                        Body=chunk,
                    )["ETag"]
                    parts.append({"PartNumber": number, "ETag": etag})
                    number += 1
            self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except BaseException:
            self._client.abort_multipart_upload(
                Bucket=self._bucket, Key=key, UploadId=upload_id
            )
            raise

    async def get_remote_recording(self, ref: str) -> bytes:
        prefix = f"s3://{self._bucket}/"
        if not ref.startswith(prefix):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"artifact ref outside configured bucket: {ref!r}"
            )
        key = ref.removeprefix(prefix)
        response = await self._run(self._client.get_object, Bucket=self._bucket, Key=key)
        body: bytes = response["Body"].read()
        return body

    async def delete_object(self, ref: str) -> None:
        prefix = f"s3://{self._bucket}/"
        if not ref.startswith(prefix):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"artifact ref outside configured bucket: {ref!r}"
            )
        key = ref.removeprefix(prefix)
        await self._run(self._client.delete_object, Bucket=self._bucket, Key=key)

    def _backup_key(self, org_id: UUID, repository_id: UUID, suffix: str) -> str:
        return f"{org_id}/backup/{repository_id}/{suffix}"

    def backup_object_ref(self, org_id: UUID, repository_id: UUID, suffix: str) -> str:
        """The full `s3://` reference for a repository-relative suffix.

        The read side of `put_backup_object`'s return value, so the two cannot
        drift: `get_backup_object` is the only consumer of either.
        """
        return f"s3://{self._bucket}/{self._backup_key(org_id, repository_id, suffix)}"

    def backup_prefix(self, org_id: UUID, repository_id: UUID) -> str:
        """The repository's key prefix, WITHOUT a trailing slash.

        Identical by construction to `BackupRepository.storage_ref`
        (`repository.py::create_repository` writes
        `f"{org_id}/backup/{repo.id}"`). Exposed so the grant issuer can
        derive the prefix from ids it is already authorized for and then
        CHECK the stored `storage_ref` against it, rather than trusting a
        column to say which tenant's objects a grant covers.
        """
        return self._backup_key(org_id, repository_id, "").rstrip("/")

    def presign_backup_object(
        self,
        org_id: UUID,
        repository_id: UUID,
        suffix: str,
        *,
        method: str = "get_object",
        expires_in: int,
    ) -> str:
        """A short-lived READ (or HEAD) grant over exactly one object inside
        one repository's prefix.

        `suffix` is relative to `backup_prefix(...)` and is validated by
        `_safe_object_suffix` before it is concatenated: an absolute path, a
        `..` segment or a backslash would let a caller address an object
        outside the repository — including another tenant's — and is rejected
        rather than normalised.
        """
        key = self._backup_key(org_id, repository_id, _safe_object_suffix(suffix))
        return self._presign(method, key, expires_in=expires_in)

    def presign_backup_read(
        self,
        org_id: UUID,
        repository_id: UUID,
        suffixes: list[str],
        *,
        expires_in: int,
    ) -> list[dict[str, str]]:
        """Short-lived GET grants for a batch of objects in one repository.

        The read counterpart of `presign_backup_write`, and deliberately NOT
        prefix-scoped: SigV4 query signing cannot express a prefix READ, which
        is why each object is signed individually and the agent asks in batches
        (`docs/modules/backup/spec.md` §6.1 item 1).

        The caller passes SUFFIXES, never keys. Each is joined onto the prefix
        DERIVED from `org_id`/`repository_id` -- ids this process is already
        authorized for -- so a caller cannot express a location outside the
        repository.

        VALIDATION HAPPENS BEFORE ANY SIGNING, not per item as it goes: one
        unsafe suffix must refuse the WHOLE batch, and a loop that signed as it
        validated would have already minted grants for everything ahead of the
        bad one. Signing part of a batch hands an agent that is probing a
        partial success to learn from.

        `_safe_object_suffix` is the validator, shared with
        `presign_backup_object` rather than restated here: an earlier
        hand-rolled equivalent elsewhere in this codebase silently omitted its
        control-character check.
        """
        if not suffixes:
            raise ValueError(  # i18n-exempt: internal argument guard, unhandled 500
                "a read grant batch must name at least one object"
            )
        _check_ttl(expires_in)
        # Validate everything first; `_safe_object_suffix` raises on the first
        # bad one and nothing has been signed yet.
        safe = [_safe_object_suffix(suffix) for suffix in suffixes]
        return [
            {
                "suffix": suffix,
                "url": self._presign(
                    "get_object",
                    self._backup_key(org_id, repository_id, safe_suffix),
                    expires_in=expires_in,
                ),
            }
            for suffix, safe_suffix in zip(suffixes, safe, strict=True)
        ]

    def presign_backup_write(
        self, org_id: UUID, repository_id: UUID, *, expires_in: int, max_bytes: int
    ) -> dict[str, Any]:
        """A short-lived WRITE grant over one repository's prefix, and only
        that prefix.

        This is the one grant in the platform that is scoped to a PREFIX
        rather than to a single object, and it exists because a backup run's
        object names are content-derived: the control plane cannot enumerate
        the chunks a run will produce, so it cannot presign them
        individually (see `docs/modules/backup/spec.md` §5.1, and the
        divergence note in `docs/kernel/wire-protocol/README.md`).

        It is an S3 POST policy, NOT a presigned PUT URL, because that is the
        only S3 signing form whose signed document can carry a
        `starts-with $key` condition. The returned `{url, fields}` are handed
        to the agent verbatim inside the signed job params. Two conditions are
        signed into the policy and neither is advisory — S3 rejects an upload
        that violates either:

        * `["starts-with", "$key", f"{prefix}/"]` — a write outside this
          repository's prefix (another repository, another ORG, the bucket
          root) is refused by the object store itself, not merely by
          convention on the endpoint. Stated EXPLICITLY even though boto3
          derives an identical condition from the `Key` template below: the
          two are independent, and `tests/test_storage_grants.py` confirmed
          that removing either alone still leaves the grant scoped while
          removing both yields a bucket-wide write.
        * `["content-length-range", 0, max_bytes]` — a compromised endpoint
          holding this grant cannot fill the tenant's storage with one object.

        There is deliberately no matching prefix-scoped READ grant: SigV4
        query signing cannot express one, and the alternative (STS session
        credentials) is a credential on the endpoint. See the spec note.

        WHAT THIS PROCESS CANNOT VERIFY. The conditions are enforced by
        S3/MinIO, not here. `moto` does not model that enforcement — it
        accepts an upload whose `key` violates the signed policy and answers
        204, which was measured rather than assumed — so no test in this
        repository proves the rejection. The tests assert the signed policy
        DOCUMENT, which is the part this codebase can get wrong; the
        enforcement is a named gap, not a covered one.
        """
        _check_ttl(expires_in)
        if max_bytes < 1:
            raise ValueError(  # i18n-exempt: internal argument guard, unhandled 500
                "a write grant must permit at least one byte"
            )
        prefix = self.backup_prefix(org_id, repository_id)
        post: dict[str, Any] = self._signing_client.generate_presigned_post(
            Bucket=self._bucket,
            # `${filename}` is the S3 POST-policy placeholder the uploader
            # fills in; the `starts-with` condition below is what actually
            # bounds it.
            Key=f"{prefix}/${{filename}}",
            Fields=None,
            Conditions=[
                ["starts-with", "$key", f"{prefix}/"],
                ["content-length-range", 0, max_bytes],
            ],
            ExpiresIn=expires_in,
        )
        return post

    async def put_backup_object(
        self, org_id: UUID, repository_id: UUID, suffix: str, data: bytes
    ) -> str:
        key = self._backup_key(org_id, repository_id, suffix)
        await self._run(self._client.put_object, Bucket=self._bucket, Key=key, Body=data)
        return f"s3://{self._bucket}/{key}"

    async def get_backup_object(self, ref: str) -> bytes:
        prefix = f"s3://{self._bucket}/"
        if not ref.startswith(prefix):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"artifact ref outside configured bucket: {ref!r}"
            )
        key = ref.removeprefix(prefix)
        response = await self._run(self._client.get_object, Bucket=self._bucket, Key=key)
        body: bytes = response["Body"].read()
        return body

    async def delete_backup_object(self, ref: str) -> None:
        prefix = f"s3://{self._bucket}/"
        if not ref.startswith(prefix):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"artifact ref outside configured bucket: {ref!r}"
            )
        key = ref.removeprefix(prefix)
        await self._run(self._client.delete_object, Bucket=self._bucket, Key=key)

    async def list_backup_chunks(self, org_id: UUID, repository_id: UUID) -> list[str]:
        prefix = self._backup_key(org_id, repository_id, "chunks/")
        chunk_ids: list[str] = []
        continuation_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            response = await self._run(self._client.list_objects_v2, **kwargs)
            for obj in response.get("Contents", []):
                key = obj["Key"]
                # key = f"{org_id}/backup/{repository_id}/chunks/{aa}/{chunk_id}"
                chunk_ids.append(key.rsplit("/", 1)[-1])
            if response.get("IsTruncated"):
                continuation_token = response.get("NextContinuationToken")
            else:
                break
        return chunk_ids

    def _backup_chunk_suffix(self, chunk_id: str) -> str:
        # Chunk key layout: chunks/{chunk_id[:2]}/{chunk_id} — the sharding
        # convention lives here next to `_backup_key`/`list_backup_chunks` so
        # callers (GC/verify) never hand-build chunk keys.
        return f"chunks/{chunk_id[:2]}/{chunk_id}"

    async def put_backup_chunk(
        self, org_id: UUID, repository_id: UUID, chunk_id: str, data: bytes
    ) -> str:
        return await self.put_backup_object(
            org_id, repository_id, self._backup_chunk_suffix(chunk_id), data
        )

    async def get_backup_chunk(
        self, org_id: UUID, repository_id: UUID, chunk_id: str
    ) -> bytes:
        key = self._backup_key(org_id, repository_id, self._backup_chunk_suffix(chunk_id))
        return await self.get_backup_object(f"s3://{self._bucket}/{key}")

    async def delete_backup_chunk(
        self, org_id: UUID, repository_id: UUID, chunk_id: str
    ) -> None:
        key = self._backup_key(org_id, repository_id, self._backup_chunk_suffix(chunk_id))
        await self.delete_backup_object(f"s3://{self._bucket}/{key}")

    def _budget_attachment_key(self, org_id: UUID, expense_id: UUID) -> str:
        # Matches deploy/README.md's "IT budget & expense" section:
        # {org_id}/budget/expenses/{expense_id}/attachment -- a fixed key with
        # NO client-supplied path segment, ever (P1(a) of the post-review
        # controller fix, .git/sdd/task-12-report.md). The client-supplied
        # filename is a sanitized DISPLAY name only (expense.invoice_filename,
        # migration 0021) -- it never reaches the object-storage key.
        return f"{org_id}/budget/expenses/{expense_id}/attachment"

    async def put_budget_attachment(
        self, org_id: UUID, expense_id: UUID, data: bytes
    ) -> str:
        key = self._budget_attachment_key(org_id, expense_id)
        await self._run(self._client.put_object, Bucket=self._bucket, Key=key, Body=data)
        return f"s3://{self._bucket}/{key}"

    async def get_budget_attachment(self, ref: str, org_id: UUID) -> bytes:
        prefix = f"s3://{self._bucket}/"
        if not ref.startswith(prefix):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"artifact ref outside configured bucket: {ref!r}"
            )
        key = ref.removeprefix(prefix)
        # Defence in depth (P1(d)): the key is always server-derived now, so
        # this should never trip in practice -- but a caller must never be
        # able to read another tenant's attachment through this method.
        if not key.startswith(f"{org_id}/budget/"):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"attachment ref outside caller's org: {ref!r}"
            )
        response = await self._run(self._client.get_object, Bucket=self._bucket, Key=key)
        body: bytes = response["Body"].read()
        return body

    async def delete_budget_attachment(self, org_id: UUID, ref: str) -> None:
        """Kept for a future delete-expense path. Carries the same
        `{org_id}/budget/` assertion `get_budget_attachment` does -- a
        defence-in-depth check is worth little if only one of the two
        directions has it."""
        prefix = f"s3://{self._bucket}/"
        if not ref.startswith(prefix):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"artifact ref outside configured bucket: {ref!r}"
            )
        key = ref.removeprefix(prefix)
        if not key.startswith(f"{org_id}/budget/"):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"attachment ref outside caller's org: {ref!r}"
            )
        await self._run(self._client.delete_object, Bucket=self._bucket, Key=key)

    def _ticket_attachment_key(self, org_id: UUID, ticket_id: UUID, attachment_id: UUID) -> str:
        # docs/modules/service-desk/spec.md §2: entirely server-derived --
        # {org_id}/service-desk/tickets/{ticket_id}/{attachment_id} -- with NO
        # client-supplied segment anywhere in it, mirroring
        # `_budget_attachment_key`'s P1(a) reasoning exactly. The
        # client-supplied filename is a sanitized DISPLAY name only
        # (`ticket_attachment.filename`) -- it never reaches this key, and
        # unlike the budget attachment (one fixed slot per expense), each
        # upload gets its OWN key because a ticket can carry many attachments.
        return f"{org_id}/service-desk/tickets/{ticket_id}/{attachment_id}"

    async def put_ticket_attachment(
        self, org_id: UUID, ticket_id: UUID, attachment_id: UUID, data: bytes
    ) -> str:
        key = self._ticket_attachment_key(org_id, ticket_id, attachment_id)
        await self._run(self._client.put_object, Bucket=self._bucket, Key=key, Body=data)
        return f"s3://{self._bucket}/{key}"

    async def get_ticket_attachment(self, ref: str, org_id: UUID) -> bytes:
        prefix = f"s3://{self._bucket}/"
        if not ref.startswith(prefix):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"artifact ref outside configured bucket: {ref!r}"
            )
        key = ref.removeprefix(prefix)
        # Defence in depth (mirrors `get_budget_attachment`'s P1(d)): the key
        # is always server-derived, so this should never trip in practice --
        # but a caller must never be able to read another tenant's attachment
        # through this method.
        if not key.startswith(f"{org_id}/service-desk/"):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"attachment ref outside caller's org: {ref!r}"
            )
        response = await self._run(self._client.get_object, Bucket=self._bucket, Key=key)
        body: bytes = response["Body"].read()
        return body

    async def delete_ticket_attachment(self, org_id: UUID, ref: str) -> None:
        """Kept for a future attachment-delete path. Carries the same
        `{org_id}/service-desk/` assertion `get_ticket_attachment` does -- a
        defence-in-depth check is worth little if only one of the two
        directions has it."""
        prefix = f"s3://{self._bucket}/"
        if not ref.startswith(prefix):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"artifact ref outside configured bucket: {ref!r}"
            )
        key = ref.removeprefix(prefix)
        if not key.startswith(f"{org_id}/service-desk/"):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"attachment ref outside caller's org: {ref!r}"
            )
        await self._run(self._client.delete_object, Bucket=self._bucket, Key=key)

    def _provision_key(self, org_id: UUID, suffix: str) -> str:
        return f"{org_id}/provision/{suffix}"

    async def put_provision_object(self, org_id: UUID, suffix: str, data: bytes) -> str:
        key = self._provision_key(org_id, suffix)
        await self._run(self._client.put_object, Bucket=self._bucket, Key=key, Body=data)
        return f"s3://{self._bucket}/{key}"

    async def get_provision_object(self, ref: str) -> bytes:
        prefix = f"s3://{self._bucket}/"
        if not ref.startswith(prefix):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"artifact ref outside configured bucket: {ref!r}"
            )
        key = ref.removeprefix(prefix)
        response = await self._run(self._client.get_object, Bucket=self._bucket, Key=key)
        body: bytes = response["Body"].read()
        return body

    async def delete_provision_object(self, ref: str) -> None:
        prefix = f"s3://{self._bucket}/"
        if not ref.startswith(prefix):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"artifact ref outside configured bucket: {ref!r}"
            )
        key = ref.removeprefix(prefix)
        await self._run(self._client.delete_object, Bucket=self._bucket, Key=key)

    # ----------------------------------------------------------------- #
    # Stored documents -- files
    # (docs/modules/documentation/spec-attachments.md §3.2).
    #
    # FOUR METHODS AND DELIBERATELY NO FIFTH. There is no
    # `presign_doc_document_get` and there must never be one (§0.3). A
    # presigned URL is a bearer credential -- no token, no RBAC check, no
    # audit event, so invariants 4 and 6 both fall -- and the presign
    # machinery exists to hand a grant to an ENDPOINT inside a signed job
    # envelope, which is not what a console user needs when they already
    # hold an authenticated channel to the API. In THIS deployment it is
    # worse than merely unnecessary: `deploy/docker-compose.yml` routes
    # `PathPrefix('/${ITS_S3_BUCKET}/')` to MinIO on the same hostname and
    # the same Traefik entrypoint that serves the console, so a presigned
    # GET of an uploaded HTML file would be an unauthenticated,
    # same-origin, script-executing URL for the entire tenant. That is the
    # stored XSS this module exists to prevent, delivered by the
    # mitigation. `tests/test_doc_document_storage.py` makes the absence an
    # assertion rather than a habit.
    # ----------------------------------------------------------------- #

    def _doc_document_key(self, org_id: UUID, document_id: UUID, version_id: UUID) -> str:
        # ENTIRELY SERVER-DERIVED, mirroring `_ticket_attachment_key`'s
        # reasoning exactly: every segment is an id the caller was already
        # authorized for, and NO client-supplied byte reaches it. The
        # client-supplied filename is a sanitized DISPLAY name in a column
        # (`doc_document_versions.filename`) and never appears here.
        #
        # The trailing `{version_id}` is what makes versions immutable
        # (§0.6): each upload writes its OWN object, so a new version can
        # never overwrite -- and therefore can never destroy -- the only
        # copy of the previous one. Like the ticket attachment and unlike
        # the budget attachment's one fixed slot per expense.
        return f"{org_id}/docs/documents/{document_id}/{version_id}"

    async def put_doc_document(
        self, org_id: UUID, document_id: UUID, version_id: UUID, data: bytes
    ) -> str:
        key = self._doc_document_key(org_id, document_id, version_id)
        await self._run(self._client.put_object, Bucket=self._bucket, Key=key, Body=data)
        return f"s3://{self._bucket}/{key}"

    async def get_doc_document(self, ref: str, org_id: UUID) -> bytes:
        prefix = f"s3://{self._bucket}/"
        if not ref.startswith(prefix):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"artifact ref outside configured bucket: {ref!r}"
            )
        key = ref.removeprefix(prefix)
        # Defence in depth, and `{org_id}/docs/` rather than merely
        # `{org_id}/`: the bucket is SHARED -- backup repositories, agent
        # binaries, job artefacts and session recordings all sit under this
        # same org's prefix -- so a docs route that would fetch
        # `{org_id}/backup/...` on request is a `docs.read` grant reading
        # backup bytes. The key is always server-derived, so this should
        # never trip; it is here because the object store has no RLS and
        # this assertion is part of what replaces it (§3.3).
        if not key.startswith(f"{org_id}/docs/"):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"document ref outside caller's org: {ref!r}"
            )
        response = await self._run(self._client.get_object, Bucket=self._bucket, Key=key)
        body: bytes = response["Body"].read()
        return body

    async def delete_doc_document(self, org_id: UUID, ref: str) -> None:
        """The purge path (§6.3). Carries the IDENTICAL assertions
        `get_doc_document` does, in the same order -- a defence-in-depth check
        present on only one direction is worth very little, and this direction
        is the one where being wrong destroys somebody else's bytes rather
        than merely disclosing them."""
        prefix = f"s3://{self._bucket}/"
        if not ref.startswith(prefix):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"artifact ref outside configured bucket: {ref!r}"
            )
        key = ref.removeprefix(prefix)
        if not key.startswith(f"{org_id}/docs/"):
            raise ValueError(  # i18n-exempt: tenancy guard on a stored ref, unhandled 500
                f"document ref outside caller's org: {ref!r}"
            )
        await self._run(self._client.delete_object, Bucket=self._bucket, Key=key)
