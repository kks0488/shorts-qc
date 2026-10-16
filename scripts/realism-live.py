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

KINDS = ("blur", "black", "caption_safe_zone", "caption_clipped", "frozen", "dark")


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


def gate_coverage(manifest, clips, checks, repair_requested):
    damaged = [c for c in manifest["clips"] if c["defects"]]
    controls = [c for c in manifest["clips"] if not c["defects"]]
    represented = {kind for c in damaged for kind in c["defects"]}
    repaired = [c["repaired_from"] for c in clips if c.get("repaired_from")]
    return dict(at_least_three_damaged=len(damaged) >= 3,
                at_least_one_clean=len(controls) >= 1,
                all_required_checks_injected=set(KINDS) <= represented,
                every_damaged_clip_repaired=bool(repair_requested) and bool(damaged)
                    and sorted(repaired) == sorted(c["file"] for c in damaged),
                every_live_check_passed=bool(checks) and len(checks) == len(manifest["clips"])+len(damaged)
                    and all(c["pass_gate"] for c in checks))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--repair", action="store_true", help="Restore flagged spans from each paired aligned original, then verify on AWS")
    parser.add_argument("--text-model", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    endpoint = args.endpoint.rstrip("/")
    if args.repair and (not args.text_model or not args.text_model.is_file()):
        parser.error("--repair requires --text-model")
    if args.repair and any(c["defects"] and not c.get("reference") for c in manifest["clips"]):
        parser.error("Every damaged clip needs its aligned clean reference for --repair")
    totals = {kind:[0, 0, 0, 0] for kind in KINDS}
    result = dict(captured_at=datetime.now(timezone.utc).isoformat(), endpoint=endpoint,
                  provenance=manifest["provenance"], unit="per decoded frame; no boundary tolerance",
                  clips=[], limitations=["Three original screen-demo styles with paired clean controls; not real-world accuracy.",
                    "Source restoration needs the aligned clean export; it cannot reconstruct lost detail from damaged pixels.",
                    "Blur and black are exact injected intervals. Text flags are edge/safe-zone risks, not proof of lost words.",
                    "Intentional stillness, app UI text, and transitions also require a creator's judgment."])
    summary = ["# Live AWS creator-video review", "", result["captured_at"], "",
               manifest["provenance"], "", "Three 12-second shorts, plus a clean control for each. "
               "Each damaged short has blur at 2–3s, black at 5–5.5s, and a bottom-cut caption at 8–10s. "
               "The studio short also has a freeze at 6–7.5s and low exposure at 10.5–11.5s.", ""]
    queue = list(manifest["clips"])
    checks = []
    for clip in queue:
        data = (args.manifest.parent/clip["file"]).read_bytes()
        started = time.monotonic()
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
        raw = http(endpoint+job["report_url"])
        elapsed = round(time.monotonic()-started, 4)
        report = json.loads(raw)
        assert report["mode"] == "aws-analysis" and report["caption_detection"]["status"] == "active"
        assert report["source"]["sha256"] == hashlib.sha256(data).hexdigest()
        stem = Path(clip["file"]).parent.name if clip.get("repaired_from") else Path(clip["file"]).stem
        (args.out/f"{stem}.json").write_bytes(raw)
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
                    analysis_seconds=report["analysis_seconds"], upload_to_report_seconds=elapsed,
                    repaired_from=clip.get("repaired_from"),
                    frozen_frames=sum(row["frozen"] == "True" for row in rows),
                    additional_findings=[i for i in report["issues"] if i["type"] not in KINDS])
        result["clips"].append(item)
        review = [f"# {clip['file']}", "", f"[Live AWS report]({item['report_url']})", "",
                  f"OpenCV {report['engine']['version']} · {report['source']['frames']} frames · "
                  f"analysis {item['analysis_seconds']:.3f}s · upload-to-report {elapsed:.3f}s", "",
                  f"Expected intervals: {json.dumps(clip['defects'], sort_keys=True)}", ""]
        summary += [f"## {clip['file']}", "", f"[Live AWS report]({item['report_url']})", ""]
        if not report["issues"]:
            summary += ["No findings in this control.", ""]
            review += ["No findings.", ""]
        for issue in report["issues"]:
            line = f"- {issue['start']:.2f}–{issue['end']:.2f}s · {issue['type']}: {issue['message']}"
            summary += [line]
            review += [line]
        matched = all(any(i["type"] == kind and abs(i["start"]-a) <= 1/manifest["fps"]+.0001
                          and abs(i["end"]-b) <= 1/manifest["fps"]+.0001 for i in report["issues"])
                      for kind, spans in clip["defects"].items() for a, b in spans)
        review_edges = [edge for kind in ("black", "dark") for span in clip["defects"].get(kind, []) for edge in span]
        unexpected = [i for i in report["issues"] if
                      (i["type"] == "scene_cut" and not any(abs(i["start"]-edge) <= 1/manifest["fps"]+.0001 for edge in review_edges))
                      or (i["type"] != "scene_cut" and i["type"] not in clip["defects"])]
        passed = (matched and not unexpected and all(fp == fn == 0 for _, fp, fn, _ in counts.values())
                  and (bool(clip["defects"]) or not report["issues"]))
        checks.append(dict(file=clip["file"], injected_intervals_matched_within_one_frame=matched,
                           unexpected_findings=unexpected, pass_gate=passed))
        review += ["", f"Gate checks: {'PASS' if passed else 'FAIL'} (event boundaries within one frame; zero frame-scoring FP/FN).",
                   "Scene-cut notices are accepted only at injected black/exposure boundaries; raw reports stay unchanged.",
                   "This is a controlled original screen composition, not natural-camera validation."]
        if clip.get("repaired_from"):
            review += ["Source-aware automatic restoration used the matching aligned original export.",
                       "This does not recover missing detail or words without that source; audio is copied unchanged."]
        (args.out/f"{stem}.md").write_text("\n".join(review)+"\n")
        if args.repair and clip.get("reference"):
            from shorts_qc.repair import repair
            folder = args.manifest.parent/f"{stem}-repair"
            trace = repair(args.manifest.parent/clip["file"], folder,
                           reference=args.manifest.parent/clip["reference"], text_model=args.text_model)
            (args.out/f"{stem}-repair-trace.json").write_bytes((folder/"trace.json").read_bytes())
            queue.append(dict(file=f"{stem}-repair/repaired.mp4", defects={}, repaired_from=clip["file"]))
        summary += [""]
        print(f"COMPLETE {clip['file']}: {report['summary']['types']}", flush=True)
    result["metrics"] = {k:score(*v) for k,v in totals.items()}
    result["gate_checks"] = checks
    result["coverage"] = gate_coverage(manifest, result["clips"], checks, args.repair)
    result["pass_gate"] = all(result["coverage"].values())
    result["frames"] = sum(sum(c["metrics"]["blur"][k] for k in ("tp", "fp", "fn", "tn")) for c in result["clips"])
    result["latency_note"] = "Wall-clock upload creation through result fetch, including 3s polling; excludes local rendering/restoration."
    summary += ["## Measured results", "", "Per decoded frame; no boundary tolerance. All control frames and all off-target "
                "frames count as negatives. Additional findings above are retained, including intentional holds.", "",
                "| Check | Precision | Recall | TP / FP / FN |", "| --- | ---: | ---: | --- |"]
    for kind, m in result["metrics"].items():
        summary += [f"| {kind} | {m['precision']} | {m['recall']} | {m['tp']} / {m['fp']} / {m['fn']} |"]
    summary += ["", *result["limitations"], ""]
    (args.out/"realism-results.json").write_text(json.dumps(result, indent=2)+"\n")
    (args.out/"summary.md").write_text("\n".join(summary))
    (args.out/"manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    print(json.dumps({"pass_gate":result["pass_gate"], "metrics":result["metrics"]}, indent=2))
    if not result["pass_gate"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
