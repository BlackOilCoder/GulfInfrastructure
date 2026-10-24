"""Write an ignored local Compose .env using the existing data installation."""
import argparse
from pathlib import Path

from bathymetry import default_directory
from config import ROOT, data_directory, structures_file


def quote_path(path):
    # Compose single quotes preserve $, #, spaces and backslashes literally.
    return "'" + Path(path).resolve().as_posix().replace("'", "\\'") + "'"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bathymetry-dir", type=Path, default=default_directory())
    args = parser.parse_args()
    paths = {"BLUEWATER_HOST_DATA": data_directory(),
             "BLUEWATER_HOST_STRUCTURES": structures_file(),
             "BLUEWATER_HOST_BATHYMETRY": args.bathymetry_dir}
    for name, path in paths.items():
        if not path.exists():
            parser.error(f"{name} does not exist: {path}")
    if not (args.bathymetry_dir / "manifest.json").is_file():
        parser.error("The selected bathymetry folder has no manifest.json.")
    destination = ROOT / ".env"
    content = "# Local Compose configuration; never commit this file.\n"
    content += "\n".join(f"{name}={quote_path(path)}" for name, path in paths.items())
    content += ("\nBLUEWATER_HTTP_PORT=8080\nBLUEWATER_RASTER_CONCURRENCY=4\n"
                "BLUEWATER_RELEASE=local\nBLUEWATER_COPERNICUS_USERNAME=\nBLUEWATER_COPERNICUS_PASSWORD=\n")
    try:
        with destination.open("x", encoding="utf-8", newline="\n") as output:
            output.write(content)
    except FileExistsError:
        raise SystemExit(".env already exists; edit it directly. Existing settings and credentials were preserved.")
    print("Created .env from the existing data installation. Start with: docker compose up --build -d web")


if __name__ == "__main__":
    main()
