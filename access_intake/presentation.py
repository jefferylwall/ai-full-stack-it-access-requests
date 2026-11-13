"""Present recorded errors with retry commands for the selected database."""

from pathlib import Path
import shlex
from typing import Any


def for_output(value: Any, database: str | Path) -> Any:
    """Copy a view while making its retry commands independent of input paths."""
    if isinstance(value, list):
        return [for_output(item, database) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: for_output(item, database) for key, item in value.items()}
    if "retry_command" in result and "document_id" in result:
        result["retry_command"] = shlex.join([
            "python3", "-m", "access_intake", "--database", str(Path(database).resolve()),
            "retry", result["document_id"], "--force",
        ])
    return result
