"""Build an audited metadata-only Tianshou 0.4.10 wheel for the shared runtime.

Upstream pins protobuf 3.19 for an old Sphinx build (setup.py line 23), while
Tianshou contains no direct protobuf imports. TensorBoard owns the runtime
protobuf requirement. Preserve all upstream Python and license bytes and let
the pinned TensorBoard/protobuf pair supply that dependency. No pip --no-deps
workaround or edits to site-packages are used.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile

UPSTREAM_SHA256 = "4c52df8552a3c32e6d17996c5d89de3383a00d43648b6d0ffcbe52181a001b74"
VERSION = "0.4.10+joint.1"


def build(output: Path, source: Path | None = None) -> Path:
    """Verify upstream bytes and write a deterministic wheel with scoped metadata."""
    if source is None:
        with urllib.request.urlopen("https://pypi.org/pypi/tianshou/0.4.10/json", timeout=45) as response:
            metadata = json.load(response)
        release = next(item for item in metadata["urls"] if item["filename"] == "tianshou-0.4.10-py3-none-any.whl")
        if release["digests"]["sha256"] != UPSTREAM_SHA256:
            raise ValueError("Upstream wheel digest changed")
        with urllib.request.urlopen(release["url"], timeout=90) as response:
            payload = response.read()
    else:
        payload = source.read_bytes()
    if hashlib.sha256(payload).hexdigest() != UPSTREAM_SHA256:
        raise ValueError("Untrusted Tianshou wheel")
    old, new = "tianshou-0.4.10.dist-info", f"tianshou-{VERSION}.dist-info"
    files = {}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in archive.namelist():
            data = archive.read(name)
            if name.endswith(".py") and (b"google.protobuf" in data or b"import protobuf" in data):
                raise ValueError("Reassess patch: direct protobuf usage found")
            if name == f"{old}/RECORD":
                continue
            if name == f"{old}/METADATA":
                text = data.decode("utf-8")
                pin = "Requires-Dist: protobuf (~=3.19.0)\n"
                if text.count(pin) != 1:
                    raise ValueError("Unexpected upstream metadata")
                data = text.replace("Version: 0.4.10\n", f"Version: {VERSION}\n").replace(pin, "").encode()
            files[name.replace(old, new)] = data
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, data in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow([name, f"sha256={digest}", len(data)])
    writer.writerow([f"{new}/RECORD", "", ""])
    files[f"{new}/RECORD"] = record.getvalue().encode()
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"tianshou-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return target


def main() -> None:
    """Build from the verified PyPI artifact or a supplied original wheel."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("tmp/joint-wheels"))
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    print(build(args.output, args.source))


if __name__ == "__main__":
    main()
