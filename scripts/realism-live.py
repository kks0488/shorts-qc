"""Evaluate original creator-style shorts through the public LIVE AWS API.

Run on pilot. Saves reports, decoded-frame confusion counts and creator-readable
summaries. Presigned upload URLs never leave process memory.
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

KINDS = ("blur", "black", "caption_safe_zone", "caption_clipped")


def http(url, method="GET", data=None, headers=None):
    for attempt in range(5):
        try:
            with urlopen(Request(url, data=data, method=method, headers=headers or {}), timeout=60) as response:
                return response.read()
        except HTTPError as error:
            if error.code not in {429, 503} or attempt == 4:
                raise
            time.sleep(2**attempt)


def score(tp, fp, fn, tn):
    return dict(tp=tp, fp=fp, fn=fn, tn=tn,
                precision=round(tp/(tp+fp), 4) if tp+fp else None,
                recall=round(tp/(tp+fn), 4) if tp+fn else None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    endpoint = args.endpoint.rstrip("/")
    totals = {kind:[0, 0, 0, 0] for kind in KINDS}
    result = dict(captured_at=datetime.now(timezone.utc).isoformat(), endpoint=endpoint,
                  provenance=manifest["provenance"], unit="per decoded frame; no boundary tolerance",
                  clips=[], limitations=["Two original screen-demo styles with paired clean controls; not real-world accuracy.",
                    "Blur and black are exact injected intervals. Text flags are edge/safe-zone risks, not proof of lost words.",
                    "Intentional stillness, app UI text, and transitions also require a creator's judgment."])
    summary = ["# Live AWS creator-video review", "", result["captured_at"], "",
               manifest["provenance"], "", "Two 12-second shorts, plus a clean control for each. "
               "Each damaged short has blur at 2–3s, black at 5–5.5s, and a bottom-cut caption at 8–10s.", ""]
    for clip in manifest["clips"]:
        data = (args.manifest.parent/clip["file"]).read_bytes()
        upload = json.loads(http(endpoint+"/api/uploads", "POST", json.dumps(dict(filename=clip["file"],
            size=len(data), sha256=hashlib.sha256(data).hexdigest())).encode(), {"content-type":"application/json"}))
        http(upload["upload_url"], "PUT", data, upload["headers"])
        deadline = time.monotonic()+300
        while time.monotonic() < deadline:
            job = json.loads(http(endpoint+upload["status_url"]))
            if job["status"] in {"COMPLETE", "FAILED", "TIMED_OUT"}:
                break
            time.sleep(3)
        if job["status"] != "COMPLETE":
            raise RuntimeError(f"Live AWS job did not complete: {job['status']}")
        report = json.loads(http(endpoint+job["report_url"]))
        assert report["mode"] == "aws-analysis" and report["caption_detection"]["status"] == "active"
        assert report["source"]["sha256"] == hashlib.sha256(data).hexdigest()
        stem = Path(clip["file"]).stem
        (args.out/f"{stem}.json").write_text(json.dumps(report, indent=2)+"\n")
        prefix = job["report_url"].rsplit("/", 1)[0]
        metrics = http(endpoint+prefix+"/metrics.csv").decode()
        (args.out/f"{stem}.csv").write_text(metrics)
        rows = list(csv.DictReader(io.StringIO(metrics)))
        assert len(rows) == report["source"]["frames"] == manifest["fps"]*manifest["duration"]
        counts = {kind:[0, 0, 0, 0] for kind in KINDS}
        for row in rows:
            for kind in KINDS:
                truth = any(a <= float(row["time"]) < b for a,b in clip["defects"].get(kind, []))
                predicted = row[kind] == "True"
                index = 0 if truth and predicted else 1 if predicted else 2 if truth else 3
                counts[kind][index] += 1
                totals[kind][index] += 1
        item = dict(file=clip["file"], job_id=job["job_id"], request_id=report["cloud"]["request_id"],
                    report_url=endpoint+job["report_url"], metrics={k:score(*v) for k,v in counts.items()},
                    frozen_frames=sum(row["frozen"] == "True" for row in rows),
                    additional_findings=[i for i in report["issues"] if i["type"] not in KINDS])
        result["clips"].append(item)
        summary += [f"## {clip['file']}", "", f"[Live AWS report]({item['report_url']})", ""]
        if not report["issues"]:
            summary += ["No findings in this control.", ""]
        for issue in report["issues"]:
            summary += [f"- {issue['start']:.2f}–{issue['end']:.2f}s · {issue['type']}: {issue['message']}"]
        summary += [""]
        print(f"COMPLETE {clip['file']}: {report['summary']['types']}", flush=True)
    result["metrics"] = {k:score(*v) for k,v in totals.items()}
    summary += ["## Measured results", "", "Per decoded frame; no boundary tolerance. All control frames and all off-target "
                "frames count as negatives. Additional findings above are retained, including intentional holds.", "",
                "| Check | Precision | Recall | TP / FP / FN |", "| --- | ---: | ---: | --- |"]
    for kind, m in result["metrics"].items():
        summary += [f"| {kind} | {m['precision']} | {m['recall']} | {m['tp']} / {m['fp']} / {m['fn']} |"]
    summary += ["", *result["limitations"], ""]
    (args.out/"realism-results.json").write_text(json.dumps(result, indent=2)+"\n")
    (args.out/"summary.md").write_text("\n".join(summary))
    (args.out/"manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    print(json.dumps(result["metrics"], indent=2))


if __name__ == "__main__":
    main()
