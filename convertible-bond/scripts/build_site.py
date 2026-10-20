"""Build a static Pages artifact containing both independent PWA apps."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
ROOT_FILES = ["index.html", "style.css", "app.js", "auth.js", "signal-state.mjs", "sw.js", "manifest.webmanifest",
              "icon-192.png", "icon-512.png", "maskable-512.png", "apple-touch-icon.png", "data.enc.json"]
CB_FILES = ["index.html", "style.css", "app.js", "sw.js", "manifest.webmanifest", "icon.svg",
            "icon-192.png", "icon-512.png", "apple-touch-icon.png"]
CB_DATA = ["strategy.json", "performance.json", "fit_curve.json"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", type=Path, default=Path("_site"))
    parser.add_argument("--cb-only", action="store_true")
    args = parser.parse_args()
    site = args.site
    site.mkdir(parents=True, exist_ok=True)
    if not args.cb_only:
        for name in ROOT_FILES:
            shutil.copy2(APP.parent / name, site / name)
    target = site / "convertible-bond"
    (target / "data").mkdir(parents=True, exist_ok=True)
    for name in CB_FILES:
        shutil.copy2(APP / name, target / name)
    data = {}
    for name in CB_DATA:
        data[name] = json.loads((APP / "data" / name).read_text(encoding="utf-8"))
        shutil.copy2(APP / "data" / name, target / "data" / name)
    strategy, fit = data["strategy.json"], data["fit_curve.json"]
    if strategy["asOf"] != fit["asOf"] or strategy["snapshotId"] != fit["snapshotId"]:
        raise RuntimeError("Market snapshot versions do not match")
    if len(fit["points"]) != fit["totalActive"]:
        raise RuntimeError("Incomplete fitted market snapshot")
    (site / ".nojekyll").touch()
    print(f"Static artifact ready: {target}; market date {strategy['asOf']}")


if __name__ == "__main__":
    main()

