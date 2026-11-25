"""Print the API's OpenAPI document as JSON, for generating the web app's
TypeScript types. No server runs and no database is opened:

    cd server && uv run --frozen python -m app.openapi_export > openapi.json

Importing the app creates the data directory beside the configured database
(app.db), so the database path is pointed at a throwaway directory first;
the app's real data, wherever ATRIOLYRIC_DATABASE_PATH says, is never
touched."""

import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="atriolyric-openapi-") as scratch:
        os.environ["ATRIOLYRIC_DATABASE_PATH"] = str(Path(scratch) / "unused.db")
        # Not a cycle: app.main reads the database path at import, so it is
        # imported only once the throwaway path is set.
        from app.main import app

        json.dump(app.openapi(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
