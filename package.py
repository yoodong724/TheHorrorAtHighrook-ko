"""Wrap a byte-delta patch with the Windows Steam installer."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
FILES = (
    "README.md", "OFL.txt", "install-steam.cmd", "restore-steam.cmd",
    "install.ps1", "steam-auto.ps1",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patch", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    entries = {name: (ROOT / "installer" / name).read_bytes() for name in FILES}
    entries["highrook-ko-test.patch.zip"] = args.patch.read_bytes()
    entries["SHA256SUMS.txt"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n"
        for name, data in sorted(entries.items())
    ).encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compresslevel=9)
    print(json.dumps({"files": len(entries), "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
