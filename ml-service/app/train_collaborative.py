"""Offline build for a versioned collaborative model artifact bundle."""

import argparse
from pathlib import Path

from app.services.catalog import MovieCatalog
from app.services.collaborative import CollaborativeRecommender


def main():
    default_data_dir = Path(__file__).resolve().parents[2]
    default_output_dir = Path(__file__).resolve().parents[1] / "artifacts" / "collaborative-v1"
    parser = argparse.ArgumentParser(description="Build CineMood's versioned collaborative inference artifacts offline.")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir, help="Directory containing movies.csv, BollywoodMovieDetail.csv, and ratings.csv")
    parser.add_argument("--output-dir", type=Path, default=default_output_dir, help="New output directory; existing directories are never overwritten")
    args = parser.parse_args()
    data_dir = args.data_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    movies_path = data_dir / "movies.csv"
    bollywood_path = data_dir / "BollywoodMovieDetail.csv"
    ratings_path = data_dir / "ratings.csv"
    catalog = MovieCatalog.from_csv(movies_path, bollywood_path if bollywood_path.is_file() else None)
    model = CollaborativeRecommender.from_csv(
        ratings_path,
        catalog,
    )
    artifact_path = model.save_production_artifacts(output_dir)
    print(f"Production collaborative artifact ready at {artifact_path}: {model.stats}")


if __name__ == "__main__":
    main()
