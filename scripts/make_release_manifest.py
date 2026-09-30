from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization


def canonical(value: dict[str, object]) -> bytes:
    signed = {
        "schema": value["schema"],
        "version": value["version"],
        "url": value["url"],
        "sha256": value["sha256"],
        "size": value["size"],
        "notes": value.get("notes", ""),
    }
    return json.dumps(signed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--asset", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--notes", required=True)
    parser.add_argument("--output", type=Path, default=Path("update.json"))
    args = parser.parse_args()
    private_pem = os.environ.get("OTA_SIGNING_PRIVATE_KEY", "").encode()
    if not private_pem:
        raise SystemExit("OTA_SIGNING_PRIVATE_KEY is required")
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    digest = hashlib.sha256(args.asset.read_bytes()).hexdigest()
    value: dict[str, object] = {
        "schema": 1,
        "version": args.version.removeprefix("v"),
        "url": args.url,
        "sha256": digest,
        "size": args.asset.stat().st_size,
        "notes": args.notes,
    }
    value["signature"] = base64.b64encode(private_key.sign(canonical(value))).decode("ascii")
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

