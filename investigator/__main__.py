import argparse
import json
from pathlib import Path

from .config import Config
from .engine import SAMPLES, investigate
from .models import BugReport
from .server import serve
from .setup import wizard
from .storage import Store


def main():
    parser = argparse.ArgumentParser(description="Evidence-backed local bug investigator")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "setup", "investigate", "process-once"])
    parser.add_argument("--text")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--sample", choices=list(SAMPLES), default="checkout")
    parser.add_argument("--user-id", default="")
    parser.add_argument("--repository", default=".")
    parser.add_argument("--diagnostics", type=Path)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    config = Config.load()
    if args.command == "setup":
        wizard()
    elif args.command == "serve" and not args.text:
        serve(config, args.port or int(config.number("PORT", 8000, 1024, 65535)))
    else:
        if not args.text and not args.demo:
            parser.error("Provide --text or use --demo for the bundled sample.")
        report = BugReport(text=args.text or SAMPLES[args.sample], mode="demo" if args.demo else "connected",
                           sample=args.sample, user_id=args.user_id, repository=args.repository,
                           diagnostics=args.diagnostics.read_text(encoding="utf-8") if args.diagnostics else "",
                           start=args.start, end=args.end)
        store = Store(config.runs)
        run = investigate(config, report, on_update=store.save)
        print(json.dumps({"id": run["id"], **run["diagnosis"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
