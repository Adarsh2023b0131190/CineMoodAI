"""Package the movie catalogs and versioned CF artifacts for private deployment storage."""

import argparse
import hashlib
import zipfile
from pathlib import Path

MEMBERS = (
    "movies.csv",
    "BollywoodMovieDetail.csv",
    "collaborative-v1/manifest.json",
    "collaborative-v1/centered-items.npz",
    "collaborative-v1/interaction-items.npz",
    "collaborative-v1/collaborative-matrices.npz",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--artifact-dir", type=Path, default=Path(__file__).resolve().parents[1] / "artifacts" / "collaborative-v1")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / ".deployment-data" / "cinemood-ml-bundle.zip")
    args = parser.parse_args()
    data_dir = args.data_dir.expanduser().resolve()
    artifact_dir = args.artifact_dir.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing data bundle: {output}")

    sources = {
        "movies.csv": data_dir / "movies.csv",
        "BollywoodMovieDetail.csv": data_dir / "BollywoodMovieDetail.csv",
        **{
            name: artifact_dir / Path(name).name
            for name in MEMBERS[2:]
        },
    }
    missing = [name for name, path in sources.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Required ML bundle inputs are missing: {', '.join(missing)}")
    if (data_dir / "ratings.csv").resolve() in (path.resolve() for path in sources.values()):
        raise RuntimeError("ratings.csv must never be included in the deployment bundle")

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_STORED) as bundle:
        for name, path in sources.items():
            bundle.write(path, arcname=name)

    digest = hashlib.sha256()
    with output.open("rb") as archive:
        for chunk in iter(lambda: archive.read(1024 * 1024), b""):
            digest.update(chunk)
    print(f"Created private ML data bundle: {output}")
    print(f"Bundle bytes: {output.stat().st_size}")
    print(f"Bundle SHA-256: {digest.hexdigest()}")
    print("Included: movie catalogs and collaborative model only; ratings.csv and credentials are excluded.")


if __name__ == "__main__":
    main()
