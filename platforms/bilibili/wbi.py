"""Bilibili WBI request signing.

The web player signs private API calls with ``w_rid``/``wts`` derived from the
``wbi_img`` keys published by ``/x/web-interface/nav``. Without a valid
signature the player endpoints increasingly reject requests.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any
from urllib.parse import urlencode, urlparse

# Permutation published by Bilibili's player bundle.
_MIXIN_KEY_ENC_TAB: tuple[int, ...] = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
)

_FILTERED_CHARS = "!'()*"


def extract_key(url: str) -> str:
    """Pull the 32 character key out of a ``wbi_img`` URL."""

    return urlparse(url).path.rsplit("/", 1)[-1].split(".")[0]


def mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[index] for index in _MIXIN_KEY_ENC_TAB)[:32]


def sign(
    params: dict[str, Any],
    img_key: str,
    sub_key: str,
    *,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Return ``params`` with ``wts`` and ``w_rid`` added."""

    key = mixin_key(img_key, sub_key)
    signed = {str(name): str(value) for name, value in params.items()}
    signed["wts"] = str(timestamp if timestamp is not None else int(time.time()))
    signed = dict(sorted(signed.items()))
    signed = {
        name: "".join(ch for ch in value if ch not in _FILTERED_CHARS)
        for name, value in signed.items()
    }
    query = urlencode(signed)
    signed["w_rid"] = hashlib.md5(f"{query}{key}".encode(), usedforsecurity=False).hexdigest()
    return signed
