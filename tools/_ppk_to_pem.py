"""Convert a PuTTY .ppk (ed25519, no passphrase) into OpenSSH PEM format."""
import base64
import struct
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def parse_ppk(text: str) -> tuple[bytes, bytes]:
    """Return (public_bytes, private_seed) from an unencrypted ed25519 PPK."""
    lines = text.splitlines()
    if not lines[0].startswith("PuTTY-User-Key-File"):
        raise ValueError("Not a PPK file")
    if "ssh-ed25519" not in lines[0]:
        raise ValueError("Only ssh-ed25519 PPK is supported here")

    pub_lines, priv_lines = [], []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("Public-Lines:"):
            n = int(line.split(":")[1])
            pub_lines = lines[i + 1 : i + 1 + n]
            i += 1 + n
            continue
        if line.startswith("Private-Lines:"):
            n = int(line.split(":")[1])
            priv_lines = lines[i + 1 : i + 1 + n]
            i += 1 + n
            continue
        i += 1
    pub_blob = base64.b64decode("".join(pub_lines))
    priv_blob = base64.b64decode("".join(priv_lines))

    # public blob format: string "ssh-ed25519" + string pubkey(32)
    name_len = struct.unpack(">I", pub_blob[:4])[0]
    name = pub_blob[4 : 4 + name_len].decode()
    assert name == "ssh-ed25519"
    key_len = struct.unpack(">I", pub_blob[4 + name_len : 8 + name_len])[0]
    pub_key = pub_blob[8 + name_len : 8 + name_len + key_len]
    assert key_len == 32

    # private blob: string seed(32)
    seed_len = struct.unpack(">I", priv_blob[:4])[0]
    seed = priv_blob[4 : 4 + seed_len]
    assert seed_len == 32
    return pub_key, seed


def main(src: str, dst: str) -> None:
    text = Path(src).read_text(encoding="utf-8")
    pub, seed = parse_ppk(text)
    key = Ed25519PrivateKey.from_private_bytes(seed)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    Path(dst).write_bytes(pem)
    print(f"Wrote OpenSSH PEM key -> {dst} ({len(pem)} bytes)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
