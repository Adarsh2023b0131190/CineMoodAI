"""Download and unpack the private production data bundle before starting FastAPI."""

import hashlib
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

MAX_BUNDLE_BYTES = 1_000_000_000
MAX_EXPANDED_BYTES = 1_000_000_000
EXPECTED_FILES = {
    "movies.csv",
    "BollywoodMovieDetail.csv",
    "collaborative-v1/manifest.json",
    "collaborative-v1/centered-items.npz",
    "collaborative-v1/interaction-items.npz",
    "collaborative-v1/collaborative-matrices.npz",
}


def _download_bundle(url: str, expected_sha256: str, target: Path) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("CINEMOOD_DATA_BUNDLE_URL must be a private HTTPS download URL")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha256):
        raise RuntimeError("CINEMOOD_DATA_BUNDLE_SHA256 must contain a 64-character SHA-256 digest")

    digest = hashlib.sha256()
    received = 0
    request = urllib.request.Request(url, headers={"User-Agent": "CineMood-Data-Bootstrap/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as destination:
            declared_size = response.headers.get("Content-Length")
            if declared_size and int(declared_size) > MAX_BUNDLE_BYTES:
                raise RuntimeError("CineMood data bundle exceeds the permitted download size")
            while chunk := response.read(1024 * 1024):
                received += len(chunk)
                if received > MAX_BUNDLE_BYTES:
                    raise RuntimeError("CineMood data bundle exceeds the permitted download size")
                digest.update(chunk)
                destination.write(chunk)
    except RuntimeError:
        raise
    except (OSError, urllib.error.URLError, ValueError) as exc:
        # Do not echo the signed URL, which can contain temporary credentials.
        raise RuntimeError("CineMood data bundle download failed; verify its URL and storage access") from None
    if digest.hexdigest().lower() != expected_sha256.lower():
        raise RuntimeError("CineMood data bundle SHA-256 verification failed")


def _extract_bundle(bundle_path: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(bundle_path) as bundle:
            entries = [entry for entry in bundle.infolist() if not entry.is_dir()]
            names = {entry.filename for entry in entries}
            if names != EXPECTED_FILES or len(entries) != len(EXPECTED_FILES):
                raise RuntimeError("CineMood data bundle has missing or unexpected files")
            expanded_size = 0
            for entry in entries:
                member = PurePosixPath(entry.filename)
                if member.is_absolute() or ".." in member.parts or "\\" in entry.filename:
                    raise RuntimeError("CineMood data bundle contains an unsafe file path")
                expanded_size += entry.file_size
            if expanded_size > MAX_EXPANDED_BYTES:
                raise RuntimeError("CineMood data bundle expands beyond the permitted size")
            for entry in entries:
                target = destination.joinpath(*PurePosixPath(entry.filename).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    except RuntimeError:
        raise
    except (OSError, zipfile.BadZipFile, EOFError) as exc:
        raise RuntimeError("CineMood data bundle is invalid or could not be unpacked") from exc


def prepare_data() -> Path:
    data_root = Path(os.getenv("DATA_DIR", "/var/data")).expanduser().resolve()
    bundle_url = os.getenv("CINEMOOD_DATA_BUNDLE_URL", "").strip()
    bundle_sha256 = os.getenv("CINEMOOD_DATA_BUNDLE_SHA256", "").strip()
    if not bundle_url:
        raise RuntimeError("CINEMOOD_DATA_BUNDLE_URL is required to provision the ML catalog and model")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", bundle_sha256):
        raise RuntimeError("CINEMOOD_DATA_BUNDLE_SHA256 must contain a 64-character SHA-256 digest")

    versioned_dir = data_root / "bundles" / bundle_sha256.lower()
    required = [versioned_dir / "movies.csv", versioned_dir / "BollywoodMovieDetail.csv"]
    required.extend(versioned_dir / "collaborative-v1" / name for name in EXPECTED_FILES if name.startswith("collaborative-v1/"))
    if all(path.is_file() for path in required):
        return versioned_dir
    if versioned_dir.exists():
        raise RuntimeError("A partial CineMood data bundle already exists on the persistent disk; use a new bundle digest path")

    bundle_parent = versioned_dir.parent
    bundle_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cinemood-bundle-", dir=bundle_parent) as staging_name:
        staging = Path(staging_name)
        archive_path = staging / "bundle.zip"
        extracted = staging / "extracted"
        extracted.mkdir()
        _download_bundle(bundle_url, bundle_sha256, archive_path)
        _extract_bundle(archive_path, extracted)
        if not all((extracted / path.relative_to(versioned_dir)).is_file() for path in required):
            raise RuntimeError("CineMood data bundle is missing required runtime files")
        final_staging = staging / "runtime"
        final_staging.mkdir()
        for name in EXPECTED_FILES:
            source = extracted.joinpath(*PurePosixPath(name).parts)
            target = final_staging.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        os.rename(final_staging, versioned_dir)
    return versioned_dir


def main() -> None:
    data_dir = prepare_data()
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["COLLABORATIVE_ARTIFACT_DIR"] = str(data_dir / "collaborative-v1")
    print("CineMood ML data bundle is ready; starting FastAPI.", flush=True)
    os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "app.main:app", "--host", os.getenv("HOST", "0.0.0.0"), "--port", os.getenv("PORT", "10000")])


if __name__ == "__main__":
    main()
