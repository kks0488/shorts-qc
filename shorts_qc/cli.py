import argparse
from functools import partial
from http.server import ThreadingHTTPServer
from RangeHTTPServer import RangeRequestHandler
import json
import os
from pathlib import Path
import shutil

from .analysis import analyze
from .config import Config


def main():
    parser = argparse.ArgumentParser(description="OpenCV 5 quality gate for short-form video")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("analyze", "repair"):
        p = sub.add_parser(name)
        p.add_argument("video")
        p.add_argument("--out", required=True)
        p.add_argument("--config", help="JSON object of Config thresholds")
        p.add_argument("--text-model", default=os.environ.get("SHORTS_QC_TEXT_MODEL"))
    p = sub.add_parser("synthesize")
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=2026)
    p = sub.add_parser("evaluate")
    p.add_argument("manifest")
    p.add_argument("--out", required=True)
    p.add_argument("--text-model", default=os.environ.get("SHORTS_QC_TEXT_MODEL"))
    p = sub.add_parser("serve")
    p.add_argument("directory")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    try:
        if args.command in ("analyze", "repair"):
            config = Config(**json.loads(Path(args.config).read_text())) if args.config else Config()
            if args.command == "analyze":
                result = analyze(args.video, args.out, config=config, text_model=args.text_model)
                print(json.dumps(result["summary"], indent=2))
            else:
                from .repair import repair
                print(json.dumps(repair(args.video, args.out, config=config, text_model=args.text_model), indent=2))
        elif args.command == "synthesize":
            from .synthetic import generate_dataset
            generate_dataset(args.out, seed=args.seed)
            print(str(Path(args.out)/"manifest.json"))
        elif args.command == "evaluate":
            from .evaluate import evaluate_dataset
            print(json.dumps(evaluate_dataset(args.manifest, args.out, text_model=args.text_model), indent=2))
        else:
            root = Path(args.directory).resolve()
            root.mkdir(parents=True, exist_ok=True)
            viewer = Path(__file__).parent/"web"
            for path in viewer.iterdir():
                shutil.copy2(path, root/path.name)
            server = ThreadingHTTPServer((args.host, args.port), partial(RangeRequestHandler, directory=str(root)))
            print(f"Viewer: http://{args.host}:{args.port}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"shorts-qc: {exc}\n")


if __name__ == "__main__":
    main()
