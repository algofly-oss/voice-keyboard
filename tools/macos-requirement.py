#!/usr/bin/env python3
"""Writes the compiled designated requirement for the macOS client:

    identifier "<id>" and certificate leaf = H"<SHA-1 of the signing certificate>"

macOS keeps a program's Accessibility permission while each new build meets the
requirement recorded when it was allowed. With this one, every build signed
with the same certificate qualifies; an ad-hoc signature would pin one build.

Apple's csreq compiles this expression, but only runs on macOS; this is the
same binary form (Security framework, requirement blob, kind 1 = expression).
Usage: tools/macos-requirement.py <certificate.crt> <identifier> <output.req>
"""
import hashlib
import ssl
import struct
import sys

OP_IDENT, OP_ANCHOR_HASH, OP_AND = 2, 4, 6
LEAF = 0  # certificate slot: 0 the leaf, -1 the anchor


def data(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b + b"\0" * (-len(b) % 4)


cert_path, identifier, out = sys.argv[1:4]
der = ssl.PEM_cert_to_DER_cert(open(cert_path).read())
expr = (struct.pack(">I", OP_AND)
        + struct.pack(">I", OP_IDENT) + data(identifier.encode())
        + struct.pack(">Ii", OP_ANCHOR_HASH, LEAF) + data(hashlib.sha1(der).digest()))
blob = struct.pack(">III", 0xFADE0C00, 12 + len(expr), 1) + expr
open(out, "wb").write(blob)
print(f'{out}: identifier "{identifier}" and certificate leaf = H"{hashlib.sha1(der).hexdigest()}"')
