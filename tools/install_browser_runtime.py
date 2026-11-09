"""Project-only Playwright runtime; reuse verified local Node, not wheel's duplicate node.exe.
Fetch selected official wheel ranges, verify every extracted member against RECORD.
The full upstream wheel hash remains in browser-requirements.lock; this selective
runtime does not claim to have downloaded or hash-verified that full archive.
"""

import base64
import concurrent.futures
import csv
import hashlib
import io
import json
import re
import subprocess
import zipfile
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
URL = "https://files.pythonhosted.org/packages/96/0a/64db6851aa1852a4f76be28eea71d4e2bf9f3ce493ee2e66939426729046/playwright-1.64.1-py3-none-win_amd64.whl"


class RemoteZip(io.RawIOBase):
    def __init__(self, url):
        self.url = url
        self.pos = 0
        self.ranges = []
        self.downloaded = 0
        with httpx.Client(timeout=30) as client:
            r = client.head(url)
            r.raise_for_status()
            self.size = int(r.headers["content-length"])

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = (
            offset
            if whence == 0
            else self.pos + offset
            if whence == 1
            else self.size + offset
        )
        return self.pos

    def fetch(self, start, stop):
        with httpx.Client(timeout=45) as client:
            r = client.get(self.url, headers={"Range": f"bytes={start}-{stop - 1}"})
            r.raise_for_status()
            if r.status_code != 206 or len(r.content) != stop - start:
                raise ValueError("Official wheel did not honor exact range")
            return start, r.content

    def read(self, n=-1):
        if n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        start = self.pos
        self.pos += n
        for offset, data in self.ranges:
            if offset <= start and start + n <= offset + len(data):
                return data[start - offset : start - offset + n]
        offset, data = self.fetch(start, start + n)
        self.ranges.append((offset, data))
        self.downloaded += len(data)
        return data


if __name__ == "__main__":
    text = (ROOT / "config/browser-requirements.lock").read_text(encoding="utf-8")
    blocks = re.split(r"(?=^[A-Za-z0-9_-]+==)", text, flags=re.M)
    support = "".join(block for block in blocks if not block.startswith("playwright=="))
    lock = ROOT / ".tmp/browser-support.lock"
    lock.write_text(support, encoding="utf-8")
    subprocess.run(
        [
            str(ROOT / ".venv/Scripts/uv.exe"),
            "pip",
            "sync",
            "--python",
            str(ROOT / "tools/browser/python/Scripts/python.exe"),
            str(lock),
        ],
        check=True,
    )
    remote = RemoteZip(URL)
    archive = zipfile.ZipFile(remote)
    entries = archive.infolist()
    node = next(e for e in entries if e.filename == "playwright/driver/node.exe")
    following = min(
        e.header_offset for e in entries if e.header_offset > node.header_offset
    )
    spans = [(0, node.header_offset), (following, remote.size)]
    parts = [
        (start, min(stop, start + 1024 * 1024))
        for begin, stop in spans
        for start in range(begin, stop, 1024 * 1024)
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(remote.fetch, *part) for part in parts]
        collected = []
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            offset, data = future.result()
            collected.append((offset, data))
            print(f"Official wheel sections {index}/{len(parts)}", flush=True)
    # Merge adjacent ranges so ZIP member reads spanning chunks stay in memory.
    for offset, data in sorted(collected):
        if remote.ranges and remote.ranges[-1][0] + len(remote.ranges[-1][1]) == offset:
            old, previous = remote.ranges.pop()
            remote.ranges.append((old, previous + data))
        else:
            remote.ranges.append((offset, data))
    record = next(e for e in entries if e.filename.endswith(".dist-info/RECORD"))
    hashes = {
        name: (digest, size)
        for name, digest, size in csv.reader(io.StringIO(archive.read(record).decode()))
    }
    site = ROOT / "tools/browser/python/Lib/site-packages"
    count = 0
    for entry in entries:
        if entry.filename == node.filename or entry.is_dir():
            continue
        target = site / entry.filename
        if not target.resolve().is_relative_to(site.resolve()):
            raise ValueError("Wheel member escaped runtime")
        raw = archive.read(entry)
        digest, size = hashes.get(entry.filename, ("", ""))
        if digest:
            algorithm, expected = digest.split("=", 1)
            if (
                algorithm != "sha256"
                or base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
                .rstrip(b"=")
                .decode()
                != expected
                or len(raw) != int(size)
            ):
                raise ValueError("Wheel RECORD integrity failed")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        count += 1
    manifest = {
        "playwright": "1.64.1",
        "source": URL,
        "installation": "official wheel members verified against RECORD; bundled node.exe omitted",
        "full_wheel_downloaded": False,
        "full_wheel_sha256_verified": False,
        "verified_members": count,
        "node_runtime": "tools/node-v24.22.1-win-x64/node.exe",
        "browser_executable": "tools/browser/chrome/chrome.exe",
        "chrome_version": "156.0.8078.48",
        "chrome_origin": "program files copied from installed Google-signed Chrome; no user data copied",
        "browser_sha256": hashlib.sha256(
            (ROOT / "tools/browser/chrome/chrome.exe").read_bytes()
        ).hexdigest(),
    }
    (ROOT / "config/browser-runtime-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "installed": True,
                "playwright": "1.64.1",
                "verified_members": count,
                "node_reused": True,
            }
        ),
        flush=True,
    )
