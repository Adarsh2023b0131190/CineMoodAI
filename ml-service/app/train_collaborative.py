"""Build or refresh the local sparse collaborative-filtering cache."""

import os
from pathlib import Path

from app.services.catalog import MovieCatalog
from app.services.collaborative import CollaborativeRecommender


def main():
    data_dir = Path(os.getenv("DATA_DIR", str(Path(__file__).resolve().parents[2]))).expanduser().resolve()
    artifact_dir = Path(os.getenv("ARTIFACT_DIR", str(Path(__file__).resolve().parents[1] / "artifacts"))).expanduser()
    if not artifact_dir.is_absolute():
        artifact_dir = (Path.cwd() / artifact_dir).resolve()
    catalog = MovieCatalog.from_csv(data_dir / "movies.csv")
    model = CollaborativeRecommender.from_csv(
        data_dir / "ratings.csv",
        catalog,
        artifact_dir=artifact_dir,
    )
    print("Collaborative model ready:", model.stats)


if __name__ == "__main__":
    main()
