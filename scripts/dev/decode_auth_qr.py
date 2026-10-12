"""Decode a Google Authenticator 'Export accounts' QR (or a plain otpauth:// QR)
from a screenshot. Prints each account's label and a MASKED secret; the full
secrets are written to a private temp file so they never scroll the terminal.

Usage: python decode_auth_qr.py <screenshot-path>
"""
import base64
import os
import re
import sys
import tempfile
import urllib.parse

import cv2


def read_varint(buf, i):
    result = 0
    shift = 0
    while i < len(buf):
        b = buf[i]
        result |= (b & 0x7F) << shift
        i += 1
        if not b & 0x80:
            return result, i
        shift += 7
    raise ValueError("truncated varint")


def parse_otp_parameters(buf):
    """Parse one OtpParameters submessage: secret(1,bytes) name(2,str) issuer(3,str)."""
    secret = name = issuer = None
    i = 0
    while i < len(buf):
        tag, i = read_varint(buf, i)
        field, wire = tag >> 3, tag & 7
        if wire == 2:
            ln, i = read_varint(buf, i)
            val = buf[i:i + ln]
            i += ln
            if field == 1:
                secret = val
            elif field == 2:
                name = val.decode("utf-8", "replace")
            elif field == 3:
                issuer = val.decode("utf-8", "replace")
        elif wire == 0:
            _, i = read_varint(buf, i)
        else:
            raise ValueError(f"unsupported wire type {wire}")
    return secret, name, issuer


def parse_migration_payload(raw):
    """Parse the MigrationPayload: repeated OtpParameters (field 1)."""
    accounts = []
    i = 0
    while i < len(raw):
        tag, i = read_varint(raw, i)
        field, wire = tag >> 3, tag & 7
        if field == 1 and wire == 2:
            ln, i = read_varint(raw, i)
            accounts.append(parse_otp_parameters(raw[i:i + ln]))
            i += ln
        elif wire == 2:
            ln, i = read_varint(raw, i)
            i += ln
        elif wire == 0:
            _, i = read_varint(raw, i)
        else:
            break
    return accounts


def mask(secret):
    s = secret.decode() if isinstance(secret, bytes) else secret
    return s[:4] + "..." + s[-2:] if len(s) > 8 else "***"


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: decode_auth_qr.py <screenshot-path>")
    img = cv2.imread(sys.argv[1])
    if img is None:
        sys.exit("could not read image")
    data, *_ = cv2.QRCodeDetector().detectAndDecode(img)
    if not data:
        sys.exit("no QR code found in image - make sure the QR fills the frame")
    print(f"QR payload type: {data[:40]}...")

    out = []
    if data.startswith("otpauth-migration://"):
        q = urllib.parse.urlparse(data).query
        raw = base64.b64decode(urllib.parse.parse_qs(q)["data"][0])
        for secret, name, issuer in parse_migration_payload(raw):
            b32 = base64.b32encode(secret).decode().rstrip("=")
            label = f"{issuer or ''} {name or ''}".strip()
            out.append((label, b32))
            print(f"  account: {label:40s} secret: {mask(b32)} ({len(b32)} chars)")
    elif data.startswith("otpauth://"):
        parsed = urllib.parse.urlparse(data)
        label = urllib.parse.unquote(parsed.path.lstrip("/"))
        params = urllib.parse.parse_qs(parsed.query)
        b32 = params.get("secret", [""])[0]
        issuer = params.get("issuer", [""])[0]
        out.append((f"{issuer} {label}".strip(), b32))
        print(f"  account: {issuer} {label:30s} secret: {mask(b32)} ({len(b32)} chars)")
    else:
        sys.exit("unrecognised QR content")

    # Private temp file (0600 on POSIX; per-user temp ACLs on Windows) -
    # never a fixed path, so this works on any machine.
    fd, dst = tempfile.mkstemp(prefix="decoded_totp_secrets_", suffix=".txt")
    with os.fdopen(fd, "w") as f:
        for label, b32 in out:
            f.write(f"{label}\t{b32}\n")
    print(f"\nfull secrets written to {dst}")


if __name__ == "__main__":
    main()
