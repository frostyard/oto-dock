#!/usr/bin/env python3
"""Initialize or inspect a pinned local Copilot preview installation offline.

Run as the proxy service user. The root's existing parent must be private (0700)
with no symlink components. No package install, download, account access, model
request, engine registration, or service restart is performed.
"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxy"))
from core.layers.copilot.provisioning import (  # noqa: E402
    CopilotProvisioningError, check_sdk, initialize, load,
)
from core.layers.copilot.runtime import RUNTIME_VERSION, SDK_VERSION  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("initialize", "check"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--archive", type=Path, help="Pinned official Linux x64 release archive")
    parser.add_argument("--forbid-root", action="append", type=Path, default=[],
                        help="Reject installation overlap with a workspace or sandbox mount root; repeatable")
    args = parser.parse_args(argv)
    if args.action == "initialize" and args.archive is None:
        parser.error("initialize requires --archive")
    if args.action == "check" and args.archive is not None:
        parser.error("check does not accept --archive")
    report = {"result": "failed", "action": args.action,
              "sdk_version": SDK_VERSION, "runtime_version": RUNTIME_VERSION}
    try:
        # Fail before provisioning if the selected interpreter lacks the optional
        # SDK. Metadata only: importing the SDK must not trigger lazy downloads.
        check_sdk()
        if args.action == "initialize":
            initialize(args.root, args.archive, forbidden_roots=tuple(args.forbid_root))
        else:
            load(args.root, forbidden_roots=tuple(args.forbid_root))
        report["result"] = "passed"
    except CopilotProvisioningError:
        report["error"] = "Copilot installation is unavailable; check the pinned dependencies, archive and private paths"
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
