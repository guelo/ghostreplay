"""Wire representations and browser revalidation for opening roots.

The opening-root registry is immutable for the life of a process and is large
enough that rebuilding and compressing its JSON on every request is wasteful.
This module keeps those transport concerns out of the domain registry and the
frontend loader: it owns compact serialization, bounded representation caching,
content negotiation, and conditional GET handling.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from starlette.responses import Response

from app.opening_roots import OpeningRoots

CACHE_CONTROL = "private, no-cache"
VARY = "Accept-Encoding"
GZIP_LEVEL = 6

# The production registry currently contains 645 families. All nonexistent
# filters normalize to the same empty selection, so 1024 entries bounds memory
# while retaining the full registry and every possible real family selection.
REPRESENTATION_CACHE_SIZE = 1024

ContentEncoding = Literal["gzip", "identity"]

_QVALUE_RE = re.compile(r"^(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)$")


@dataclass(frozen=True, slots=True)
class OpeningRootsRepresentation:
    body: bytes
    etag: str
    content_encoding: ContentEncoding


@dataclass(frozen=True, slots=True)
class OpeningRootsRepresentations:
    identity: OpeningRootsRepresentation
    gzip: OpeningRootsRepresentation


def _strong_etag(body: bytes) -> str:
    return f'"{hashlib.sha256(body).hexdigest()}"'


def _json_payload(roots: OpeningRoots, family_names: tuple[str, ...]) -> bytes:
    families = []
    total_roots = 0
    for name in family_names:
        items = [
            {
                "opening_key": root.opening_key,
                "opening_name": root.opening_name,
                "opening_family": root.opening_family,
                "eco": root.eco,
                "depth": root.depth,
            }
            for root in roots.get_family(name)
        ]
        families.append({"family_name": name, "roots": items})
        total_roots += len(items)

    payload = {
        "families": families,
        "total_roots": total_roots,
        "total_families": len(families),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


@lru_cache(maxsize=REPRESENTATION_CACHE_SIZE)
def _cached_representations(
    roots: OpeningRoots,
    family_names: tuple[str, ...],
) -> OpeningRootsRepresentations:
    identity_body = _json_payload(roots, family_names)
    gzip_body = gzip.compress(identity_body, compresslevel=GZIP_LEVEL, mtime=0)
    return OpeningRootsRepresentations(
        identity=OpeningRootsRepresentation(
            body=identity_body,
            etag=_strong_etag(identity_body),
            content_encoding="identity",
        ),
        gzip=OpeningRootsRepresentation(
            body=gzip_body,
            etag=_strong_etag(gzip_body),
            content_encoding="gzip",
        ),
    )


def opening_roots_representations(
    roots: OpeningRoots,
    family: str | None,
) -> OpeningRootsRepresentations:
    """Return cached identity and gzip bytes for one normalized selection."""
    if family is None:
        family_names = tuple(roots.get_families())
    elif roots.get_family(family):
        family_names = (family,)
    else:
        # Every miss shares one cache entry instead of allowing arbitrary query
        # strings to consume process memory.
        family_names = ()
    return _cached_representations(roots, family_names)


def _quality(raw: str) -> float:
    raw = raw.strip()
    if not _QVALUE_RE.fullmatch(raw):
        return 0.0
    return float(raw)


def _accepted_codings(header: str) -> dict[str, float]:
    accepted: dict[str, float] = {}
    for item in header.split(","):
        parts = item.split(";")
        coding = parts[0].strip().lower()
        if not coding:
            continue

        quality = 1.0
        for parameter in parts[1:]:
            name, separator, value = parameter.partition("=")
            if separator and name.strip().lower() == "q":
                quality = _quality(value)
                break
        accepted[coding] = max(quality, accepted.get(coding, 0.0))
    return accepted


def choose_content_encoding(accept_encoding: str | None) -> ContentEncoding | None:
    """Select gzip or identity according to RFC Accept-Encoding preferences.

    ``None`` means that neither representation is acceptable. An absent or
    empty field selects identity; modern browsers advertise gzip explicitly.
    """
    if not accept_encoding:
        return "identity"

    accepted = _accepted_codings(accept_encoding)
    wildcard_quality = accepted.get("*", 0.0)
    gzip_quality = accepted.get("gzip", wildcard_quality)
    identity_is_explicit = "identity" in accepted

    if identity_is_explicit:
        identity_quality = accepted["identity"]
    elif accepted.get("*") == 0.0 and "*" in accepted:
        identity_quality = 0.0
    else:
        identity_quality = 1.0

    if gzip_quality > 0.0:
        # An implicit identity representation is the fallback when none of the
        # advertised codings is available. It does not outrank an explicitly
        # acceptable gzip coding. An explicit identity qvalue does participate
        # in preference selection.
        if identity_is_explicit and identity_quality > gzip_quality:
            return "identity"
        return "gzip"
    if identity_quality > 0.0:
        return "identity"
    return None


def _if_none_match_matches(if_none_match: str | None, etag: str) -> bool:
    if if_none_match is None:
        return False
    for candidate in if_none_match.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:].lstrip()
        if candidate == etag:
            return True
    return False


def _representation_headers(
    representation: OpeningRootsRepresentation,
) -> dict[str, str]:
    headers = {
        "Cache-Control": CACHE_CONTROL,
        "Content-Length": str(len(representation.body)),
        "Content-Type": "application/json",
        "ETag": representation.etag,
        "Vary": VARY,
    }
    if representation.content_encoding == "gzip":
        headers["Content-Encoding"] = "gzip"
    return headers


def opening_roots_response(
    roots: OpeningRoots,
    *,
    family: str | None,
    accept_encoding: str | None,
    if_none_match: str | None,
) -> Response:
    """Build a negotiated 200/304/406 response for the roots registry."""
    content_encoding = choose_content_encoding(accept_encoding)
    if content_encoding is None:
        return Response(
            status_code=406,
            headers={
                "Cache-Control": CACHE_CONTROL,
                "Content-Length": "0",
                "Vary": VARY,
            },
        )

    representations = opening_roots_representations(roots, family)
    representation = getattr(representations, content_encoding)
    headers = _representation_headers(representation)

    if _if_none_match_matches(if_none_match, representation.etag):
        # A 304 repeats validator/cache-selection fields but not representation
        # metadata. Content-Length is permitted when it equals the selected
        # representation's encoded length, which is exactly what is cached here.
        return Response(
            status_code=304,
            headers={
                "Cache-Control": CACHE_CONTROL,
                "Content-Length": str(len(representation.body)),
                "ETag": representation.etag,
                "Vary": VARY,
            },
        )
    return Response(content=representation.body, headers=headers)
