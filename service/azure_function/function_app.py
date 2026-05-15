"""
Azure Function (Python v2 model) exposing a batched FPE/FF3 encryption endpoint.

Key material (KEY, TWEAK) is loaded once at cold start from app settings that
are configured as Key Vault references, so it never leaves the Function App.

Endpoint:
    POST /api/fpe
    Header: x-functions-key: <function key>     (auth_level=FUNCTION)
    Body:
        {
          "type": "numeric" | "alphanumeric" | "alphanumeric_extended"
                  | "phone" | "email" | "ascii_preserve_other",
          "values": ["...", "...", null, ...]
        }
    Response:
        { "values": ["...", "...", null, ...] }   # same length & order

The handler is intentionally CPU-bound (FF3 in pure Python). Scaling is achieved
by:
  * Batching from the Spark side (one HTTP call per Arrow micro-batch, per
    column, per executor task).
  * Horizontal scale-out of the Function App (Premium / Flex Consumption plan
    with high maxBurst and per-instance concurrency tuned for CPU work).
"""

from __future__ import annotations

import json
import logging
import os
import string
from typing import Callable, List, Optional

import azure.functions as func
from ff3 import FF3Cipher
from unidecode import unidecode

# ---------------------------------------------------------------------------
# Configuration (loaded once per worker process)
# ---------------------------------------------------------------------------
KEY = os.environ["FPE_KEY"]      # 32-hex-char AES-128 key (Key Vault reference)
TWEAK = os.environ["FPE_TWEAK"]  # 14-hex-char tweak       (Key Vault reference)

C_NUMERIC_MIN, C_NUMERIC_MAX, C_NUMERIC_RADIX, C_NUMERIC_MIN_PREFIX = 6, 56, 10, "0"
C_ALPHANUMERIC_MIN, C_ALPHANUMERIC_MAX, C_ALPHANUMERIC_RADIX, C_ALPHANUMERIC_MIN_PREFIX = 6, 32, 62, "0"
C_ALPHA_LOWER_MIN, C_ALPHA_LOWER_MAX, C_ALPHA_LOWER_MIN_PREFIX = 5, 40, "a"
C_ALPHA_LOWER_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
C_ALPHA_UPPER_MIN, C_ALPHA_UPPER_MAX, C_ALPHA_UPPER_MIN_PREFIX = 5, 40, "A"
C_ALPHA_UPPER_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
C_ALPHANUMERIC_EXTENDED_MIN, C_ALPHANUMERIC_EXTENDED_MAX, C_ALPHANUMERIC_EXTENDED_MIN_PREFIX = 4, 30, " "
C_ALPHANUMERIC_EXTENDED_ALPHABET = (
    string.digits + string.ascii_lowercase + string.ascii_uppercase
    + " !ç@#$%^&*()?'\\/-.,¡é"
)
C_EMAIL_MIN, C_EMAIL_MAX, C_EMAIL_MIN_PREFIX = 4, 30, " "
C_EMAIL_ALPHABET = (
    string.digits + string.ascii_lowercase + string.ascii_uppercase + "._%+-çé"
)

# ---------------------------------------------------------------------------
# Cipher cache: FF3Cipher construction is non-trivial -> reuse per worker.
# ---------------------------------------------------------------------------
_CIPHER_NUMERIC = FF3Cipher(KEY, TWEAK, radix=C_NUMERIC_RADIX)
_CIPHER_ALPHANUMERIC = FF3Cipher(KEY, TWEAK, radix=C_ALPHANUMERIC_RADIX)
_CIPHER_ALPHANUMERIC_EXT = FF3Cipher.withCustomAlphabet(KEY, TWEAK, alphabet=C_ALPHANUMERIC_EXTENDED_ALPHABET)
_CIPHER_EMAIL = FF3Cipher.withCustomAlphabet(KEY, TWEAK, alphabet=C_EMAIL_ALPHABET)
_CIPHER_ALPHA_LOWER = FF3Cipher.withCustomAlphabet(KEY, TWEAK, alphabet=C_ALPHA_LOWER_ALPHABET)
_CIPHER_ALPHA_UPPER = FF3Cipher.withCustomAlphabet(KEY, TWEAK, alphabet=C_ALPHA_UPPER_ALPHABET)


# ---------------------------------------------------------------------------
# Core FPE primitives (identical semantics to the original notebook)
# ---------------------------------------------------------------------------
def _fpe_ff3_base(col_val: Optional[str], c: FF3Cipher, c_min: int, c_max: int, c_min_prefix: str) -> Optional[str]:
    if col_val is None:
        return None
    ciphertext = ""
    if len(col_val) < c_min:
        col_val = str(col_val).rjust(c_min, c_min_prefix)
        ciphertext = c.encrypt(col_val)
    elif len(col_val) > c_max:
        current = c_max
        while current > 0:
            chunks = [col_val[i:i + current] for i in range(0, len(col_val), current)]
            if len(chunks[-1]) < c_min:
                current -= 1
            else:
                break
        for chunk in chunks:
            ciphertext += c.encrypt(chunk)
    else:
        ciphertext = c.encrypt(col_val)
    return ciphertext


def _fpe_numeric(v: Optional[str]) -> Optional[str]:
    return _fpe_ff3_base(v, _CIPHER_NUMERIC, C_NUMERIC_MIN, C_NUMERIC_MAX, C_NUMERIC_MIN_PREFIX)


def _fpe_alphanumeric(v: Optional[str]) -> Optional[str]:
    return _fpe_ff3_base(v, _CIPHER_ALPHANUMERIC, C_ALPHANUMERIC_MIN, C_ALPHANUMERIC_MAX, C_ALPHANUMERIC_MIN_PREFIX)


def _fpe_alphanumeric_extended(v: Optional[str]) -> Optional[str]:
    return _fpe_ff3_base(v, _CIPHER_ALPHANUMERIC_EXT, C_ALPHANUMERIC_EXTENDED_MIN, C_ALPHANUMERIC_EXTENDED_MAX, C_ALPHANUMERIC_EXTENDED_MIN_PREFIX)


def _fpe_phone(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    raw = "".join(filter(str.isdigit, v))
    raw_ct = _fpe_ff3_base(raw, _CIPHER_NUMERIC, C_NUMERIC_MIN, C_NUMERIC_MAX, C_NUMERIC_MIN_PREFIX)
    out = []
    n = 0
    for ch in v:
        if ch.isnumeric():
            out.append(raw_ct[n])
            n += 1
        else:
            out.append(ch)
    return "".join(out)


def _fpe_email(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    at = v.split("@")
    len_at = len(at[0])
    dot = at[1].split(".")
    len_dot = len_at + len(dot[0])
    raw = at[0] + dot[0]
    raw_ct = _fpe_ff3_base(raw, _CIPHER_EMAIL, C_EMAIL_MIN, C_EMAIL_MAX, C_EMAIL_MIN_PREFIX)
    return raw_ct[0:len_at] + "@" + raw_ct[len_at:len_dot] + "." + v[len_dot + 2:]


def _fpe_ascii_preserve_other(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = unidecode(v)
    raw_alpha = "".join(filter(str.isalpha, v))
    raw_lower = "".join(filter(str.islower, raw_alpha))
    raw_upper = "".join(filter(str.isupper, raw_alpha))
    raw_num = "".join(filter(str.isdigit, v))
    ct_lower = _fpe_ff3_base(raw_lower, _CIPHER_ALPHA_LOWER, C_ALPHA_LOWER_MIN, C_ALPHA_LOWER_MAX, C_ALPHA_LOWER_MIN_PREFIX)
    ct_upper = _fpe_ff3_base(raw_upper, _CIPHER_ALPHA_UPPER, C_ALPHA_UPPER_MIN, C_ALPHA_UPPER_MAX, C_ALPHA_UPPER_MIN_PREFIX)
    ct_num = _fpe_ff3_base(raw_num, _CIPHER_NUMERIC, C_NUMERIC_MIN, C_NUMERIC_MAX, C_NUMERIC_MIN_PREFIX)
    out = []
    li = ui = ni = 0
    for ch in v:
        if ch.isalpha():
            if ch.islower():
                out.append(ct_lower[li]); li += 1
            else:
                out.append(ct_upper[ui]); ui += 1
        elif ch.isnumeric():
            out.append(ct_num[ni]); ni += 1
        else:
            out.append(ch)
    return "".join(out)


_DISPATCH: dict[str, Callable[[Optional[str]], Optional[str]]] = {
    "numeric": _fpe_numeric,
    "alphanumeric": _fpe_alphanumeric,
    "alphanumeric_extended": _fpe_alphanumeric_extended,
    "phone": _fpe_phone,
    "email": _fpe_email,
    "ascii_preserve_other": _fpe_ascii_preserve_other,
}

# Hard cap to bound per-request CPU/memory and align with Spark Arrow batch sizes.
MAX_BATCH_SIZE = int(os.environ.get("FPE_MAX_BATCH_SIZE", "10000"))

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


@app.route(route="fpe", methods=["POST"])
def fpe(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("invalid json", status_code=400)

    fpe_type = body.get("type")
    values = body.get("values")
    if fpe_type not in _DISPATCH or not isinstance(values, list):
        return func.HttpResponse("bad request", status_code=400)
    if len(values) > MAX_BATCH_SIZE:
        return func.HttpResponse(
            f"batch too large (>{MAX_BATCH_SIZE})", status_code=413
        )

    fn = _DISPATCH[fpe_type]
    try:
        out: List[Optional[str]] = [fn(v) if v is not None else None for v in values]
    except Exception:
        logging.exception("fpe batch failed (type=%s, n=%d)", fpe_type, len(values))
        return func.HttpResponse("encryption error", status_code=500)

    return func.HttpResponse(
        json.dumps({"values": out}),
        status_code=200,
        mimetype="application/json",
    )
