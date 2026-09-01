#!/usr/bin/env python3
"""Print a local-only preparation summary.

Despite its historical filename, this script deliberately has no Chrome,
browser-automation, HTTP, credential, upload, typing, Apply, or Submit code.
The candidate manually reviews and applies outside this program.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from jobapply_agent.workflow import prepare_application


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare local-only application review prompts; never opens a browser.")
    parser.add_argument("job_id", help="Local job identifier")
    parser.add_argument("--field-label", action="append", default=[], help="Inert local form-label note; no form is read or filled.")
    args = parser.parse_args()
    prepared = prepare_application(args.job_id, form_labels=args.field_label)
    print(
        json.dumps(
            {
                "job_id": prepared.job_id,
                "field_suggestions": dict(prepared.field_suggestions),
                "requires_user_answer": list(prepared.requires_user_answer),
                "stopped": prepared.stopped,
                "submission": "Manual only; this script cannot apply or submit.",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
