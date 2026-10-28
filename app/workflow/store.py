"""Workflow files: .runtime/workflows/<ticket_id>.json, written atomically after every transition."""

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from app.contracts import ROOT, atomic_json
from app.workflow.record import Workflow
from app.workflow.schema import SAFE_ID_PATTERN


class WorkflowStore:
    SAFE_ID = re.compile(SAFE_ID_PATTERN)

    def __init__(self, directory=None):
        default = Path(os.environ.get("EURAPCO_WORKFLOW_DIR", ROOT / ".runtime" / "workflows"))
        self.directory = Path(directory or default)

    def path(self, ticket_id):
        # ticket_id comes from untrusted input; never let it escape the directory.
        if not self.SAFE_ID.fullmatch(ticket_id):
            raise ValueError("ticket_id must contain only letters, digits, underscores or hyphens (max 100).")
        return self.directory / f"{ticket_id}.json"

    def load(self, ticket_id):
        path = self.path(ticket_id)
        return Workflow.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None

    def save(self, workflow):
        workflow.updated_at = datetime.now(timezone.utc)
        atomic_json(self.path(workflow.ticket_id), workflow.model_dump(mode="json"))
