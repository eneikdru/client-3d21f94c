"""Export the runtime API contract using an isolated temporary database."""
import json
import secrets
import tempfile
from pathlib import Path

from booking import create_app


def export_contract(destination):
    with tempfile.TemporaryDirectory() as directory:
        app = create_app(str(Path(directory) / 'schema.db'), secrets.token_urlsafe(48))
        schema = app.openapi()
    Path(destination).write_text(json.dumps(schema, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    export_contract(Path(__file__).with_name('openapi.json'))
