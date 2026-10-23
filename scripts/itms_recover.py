#!/usr/bin/env python3
"""itms-recover: read the off-site backup copy without the ITMS server.

Standalone on purpose. It imports nothing from the backend, holds no agent
identity and cannot talk to a gateway. It needs only the off-site bucket, a
credential that can read it, and the recovery private key. Requirements:
`pip install cryptography boto3 blake3`.

    itms_recover.py keygen --out recovery.key
        Make a recovery keypair on an offline machine. Keep recovery.key safe
        (two copies, offline). Paste the printed public key into the console.

    itms_recover.py prove --key recovery.key --challenge <text from console>
        Print the answer the console asks for, proving you hold the key.

    itms_recover.py list --key recovery.key --bucket B [--endpoint URL] [--prefix P]
        List repositories and backup points in the off-site copy.

    itms_recover.py restore --key recovery.key --bucket B --repository R --point P \
        --dest DIR [--path /only/this ...]
        Restore a backup point (or some paths of it) into DIR.

The bucket credential comes from the usual AWS variables
(AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY) or profile.

Formats (must match backend/app/modules/backup/recovery.py and offsite.py):
key wrap X25519-HKDF-SHA256-AES-256-GCM v1; chunks and manifests are
AES-256-GCM `nonce(12) || ct` under the repository key; off-site chunk names
are `chunks/{n[:2]}/{n}`, n = HMAC-SHA256(HKDF(repo_key, info), chunk_id).

This is the Python reference of the `agent/cmd/itms-recover` Go tool the spec
describes (spec-server-backup.md §6.5); both read the same bucket layout.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

_WRAP_INFO = b"itms recovery key wrap v1"
_CHALLENGE_AAD = b"itms recovery challenge v1:"
_CHUNK_NAME_INFO = b"itms offsite chunk names v1"
_CATALOG_AAD = b"itms catalog point v1:"
_PRIVATE_PREFIX = "itms-recovery-private:"
_PUBLIC_PREFIX = "itms-recovery-public:"


class RecoverError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #


def keygen() -> tuple[str, str]:
    """(private key text, public key text)."""
    priv = X25519PrivateKey.generate()
    raw_priv = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    raw_pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return (
        _PRIVATE_PREFIX + base64.b64encode(raw_priv).decode(),
        _PUBLIC_PREFIX + base64.b64encode(raw_pub).decode(),
    )


def load_private_key(text: str) -> bytes:
    value = text.strip().removeprefix(_PRIVATE_PREFIX)
    raw = base64.b64decode(value, validate=True)
    if len(raw) != 32:
        raise RecoverError("not a recovery private key")
    return raw


def public_of(private_key: bytes) -> bytes:
    return (
        X25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    )


def fingerprint(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()[:32]


def unwrap(private_key: bytes, obj: dict[str, Any], aad: bytes) -> bytes:
    if obj.get("v") != 1 or obj.get("alg") != "X25519-HKDF-SHA256-AES-256-GCM":
        raise RecoverError("unsupported key object version")
    priv = X25519PrivateKey.from_private_bytes(private_key)
    recipient = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    epk = base64.b64decode(obj["epk"])
    shared = priv.exchange(X25519PublicKey.from_public_bytes(epk))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=epk + recipient, info=_WRAP_INFO).derive(
        shared
    )
    return AESGCM(key).decrypt(base64.b64decode(obj["nonce"]), base64.b64decode(obj["ct"]), aad)


def prove(private_key: bytes, challenge: str) -> str:
    blob = json.loads(base64.b64decode(challenge.strip(), validate=True))
    if blob.get("kind") != "itms-recovery-challenge":
        raise RecoverError("not a recovery-key check")
    secret = unwrap(private_key, blob, _CHALLENGE_AAD + blob["key_id"].encode())
    return base64.b64encode(secret).decode()


# --------------------------------------------------------------------------- #
# Reading the off-site copy
# --------------------------------------------------------------------------- #


def _open(repo_key: bytes, blob: bytes, aad: bytes | None = None) -> bytes:
    return AESGCM(repo_key).decrypt(blob[:12], blob[12:], aad)


def _chunk_name(repo_key: bytes, chunk_id: str) -> str:
    name_key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_CHUNK_NAME_INFO
    ).derive(repo_key)
    n = hmac.new(name_key, chunk_id.encode(), hashlib.sha256).hexdigest()
    return f"chunks/{n[:2]}/{n}"


class OffsiteReader:
    def __init__(self, client: Any, bucket: str, prefix: str = "") -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = f"{prefix.strip('/')}/" if prefix.strip("/") else ""

    def _get(self, key: str) -> bytes:
        body: bytes = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return body

    def repositories(self) -> list[str]:
        out: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix, Delimiter="/"):
            for common in page.get("CommonPrefixes", []):
                name = common["Prefix"][len(self.prefix) :].strip("/")
                if not name.startswith("."):
                    out.append(name)
        return out

    def repo_key(self, repository: str, private_key: bytes) -> bytes:
        fp = fingerprint(public_of(private_key))
        base = f"{self.prefix}{repository}/"
        try:
            obj = json.loads(self._get(f"{base}keys/repository-key.{fp}.json"))
        except Exception as exc:  # noqa: BLE001
            raise RecoverError(
                f"no repository key for recovery key {fp} in {repository}"
            ) from exc
        return unwrap(private_key, obj, repository.encode())

    def points(self, repository: str, repo_key: bytes) -> list[dict[str, Any]]:
        base = f"{self.prefix}{repository}/catalog/points/"
        out: list[dict[str, Any]] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=base):
            for item in page.get("Contents", []):
                point_id = item["Key"][len(base) :]
                doc = json.loads(_open(repo_key, self._get(item["Key"]),
                                       _CATALOG_AAD + point_id.encode()))
                out.append(doc)
        return sorted(out, key=lambda d: d.get("taken_at", ""))

    def manifest(self, repository: str, repo_key: bytes, point_id: str) -> dict[str, Any]:
        base = f"{self.prefix}{repository}/"
        doc = json.loads(_open(repo_key, self._get(f"{base}catalog/points/{point_id}"),
                               _CATALOG_AAD + point_id.encode()))
        blob = self._get(base + doc["manifest_object"])
        parsed: dict[str, Any] = json.loads(_open(repo_key, blob))
        return parsed

    def chunk(self, repository: str, repo_key: bytes, chunk_id: str) -> bytes:
        from blake3 import blake3

        blob = self._get(f"{self.prefix}{repository}/{_chunk_name(repo_key, chunk_id)}")
        plain = _open(repo_key, blob)
        if blake3(plain).hexdigest() != chunk_id:
            raise RecoverError(f"chunk {chunk_id[:12]} does not match its hash")
        return plain


def _relative(source_path: str) -> Path:
    """A manifest path as a safe path under the destination."""
    if ":" in source_path[:3] or "\\" in source_path:
        parts = PureWindowsPath(source_path).parts[1:]
    else:
        parts = PurePosixPath(source_path).parts
        parts = parts[1:] if parts and parts[0] == "/" else parts
    if not parts or any(p in ("..", "") for p in parts):
        raise RecoverError(f"refusing unsafe path {source_path!r}")
    return Path(*parts)


def _selected(path: str, only: list[str]) -> bool:
    return not only or any(path == o or path.startswith(o.rstrip("/\\") + "/") for o in only)


def _entries(
    reader: OffsiteReader, repository: str, repo_key: bytes, manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every entry with its full chunk list, for manifest schema 1 or 2. A v2
    manifest names pages (chunks of NDJSON entries); a long chunk list lives in
    id pages (`chunk_pages`, one id per line)."""
    version = manifest.get("schema_version")
    if version == 1:
        return [e for s in manifest.get("streams", []) for e in s.get("entries", [])]
    if version != 2:
        raise RecoverError(f"manifest schema_version {version!r} is not supported")
    out: list[dict[str, Any]] = []
    for stream in manifest.get("streams", []):
        for page_id in stream.get("pages", []):
            page = reader.chunk(repository, repo_key, page_id)
            for line in page.split(b"\n"):
                if not line:
                    continue
                entry = json.loads(line)
                ids = list(entry.get("chunk_ids") or [])
                for id_page in entry.get("chunk_pages") or []:
                    raw = reader.chunk(repository, repo_key, id_page).decode()
                    ids.extend(x for x in raw.split("\n") if x)
                entry["chunk_ids"] = ids
                out.append(entry)
    return out


def restore(
    reader: OffsiteReader,
    private_key: bytes,
    repository: str,
    point_id: str,
    dest: Path,
    only: list[str] | None = None,
) -> int:
    """Restore files of one point into `dest`; returns files written. Each file
    is written to a temporary name and renamed once every chunk verified."""
    repo_key = reader.repo_key(repository, private_key)
    manifest = reader.manifest(repository, repo_key, point_id)
    written = 0
    for entry in _entries(reader, repository, repo_key, manifest):
        if not _selected(entry["path"], only or []):
            continue
        target = dest / _relative(entry["path"])
        if entry["kind"] == "dir":
            target.mkdir(parents=True, exist_ok=True)
            continue
        if entry["kind"] != "file":
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.itms-partial")
        with open(tmp, "wb") as out:
            for cid in entry.get("chunk_ids", []):
                out.write(reader.chunk(repository, repo_key, cid))
        if tmp.stat().st_size != entry["size"]:
            tmp.unlink()
            raise RecoverError(f"{entry['path']}: restored size does not match")
        os.replace(tmp, target)
        written += 1
    return written


def _client(args: argparse.Namespace) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=args.endpoint,
        region_name=args.region,
        config=Config(signature_version="s3v4", s3={"addressing_style": args.addressing}),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="itms-recover", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    g = sub.add_parser("keygen")
    g.add_argument("--out", required=True, type=Path)
    p = sub.add_parser("prove")
    p.add_argument("--key", required=True, type=Path)
    p.add_argument("--challenge", required=True)
    for name in ("list", "restore"):
        s = sub.add_parser(name)
        s.add_argument("--key", required=True, type=Path)
        s.add_argument("--bucket", required=True)
        s.add_argument("--endpoint", default=None)
        s.add_argument("--region", default="us-east-1")
        s.add_argument("--addressing", default="path", choices=("path", "virtual"))
        s.add_argument("--prefix", default="")
        if name == "restore":
            s.add_argument("--repository", required=True)
            s.add_argument("--point", required=True)
            s.add_argument("--dest", required=True, type=Path)
            s.add_argument("--path", action="append", default=[])
    args = parser.parse_args(argv)

    try:
        if args.command == "keygen":
            private, public = keygen()
            if args.out.exists():
                raise RecoverError(f"{args.out} exists; not overwriting a key")
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(private + "\n")
            raw_pub = base64.b64decode(public.removeprefix(_PUBLIC_PREFIX))
            print(public)
            print(f"fingerprint: {fingerprint(raw_pub)}", file=sys.stderr)
            return 0
        private_key = load_private_key(args.key.read_text())
        if args.command == "prove":
            print(prove(private_key, args.challenge))
            return 0
        reader = OffsiteReader(_client(args), args.bucket, args.prefix)
        if args.command == "list":
            for repo in reader.repositories():
                try:
                    key = reader.repo_key(repo, private_key)
                except RecoverError as exc:
                    print(f"{repo}\t({exc})")
                    continue
                for point in reader.points(repo, key):
                    print(f"{repo}\t{point['point_id']}\t{point.get('hostname')}"
                          f"\t{point.get('taken_at')}\t{point.get('size')}")
            return 0
        count = restore(reader, private_key, args.repository, args.point, args.dest, args.path)
        print(f"restored {count} files into {args.dest}", file=sys.stderr)
        return 0
    except RecoverError as exc:
        print(f"itms-recover: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
