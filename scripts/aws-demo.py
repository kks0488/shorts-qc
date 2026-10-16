"""Exercise the public AWS API; keep evidence, never presigned URLs or credentials."""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def aws(*args):
    result = subprocess.run(["aws", *args, "--region", "ap-southeast-2", "--output", "json"],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def http(url, method="GET", data=None, headers=None):
    with urlopen(Request(url, data=data, method=method, headers=headers or {}), timeout=40) as r:
        body = r.read()
        return r.status, json.loads(body) if body else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("clips", nargs="*", type=Path)
    parser.add_argument("--stack", default="shorts-qc")
    args = parser.parse_args()
    stack = aws("cloudformation", "describe-stacks", "--stack-name", args.stack)["Stacks"][0]
    outputs = {v["OutputKey"]: v["OutputValue"] for v in stack["Outputs"]}
    endpoint = outputs["ViewerURL"].rstrip("/")
    evidence = {"captured_at": datetime.now(timezone.utc).isoformat(), "region": "ap-southeast-2",
                "endpoint": endpoint, "stack_status": stack["StackStatus"], "outputs": outputs, "runs": []}
    if not args.clips:
        evidence = json.loads(Path("media/aws-evidence.json").read_text())
        evidence["refreshed_at"] = datetime.now(timezone.utc).isoformat()
    for index, clip in enumerate(args.clips):
        data = clip.read_bytes()
        started = time.monotonic()
        _, upload = http(endpoint + "/api/uploads", "POST", json.dumps({"filename": clip.name,
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}).encode(), {"content-type": "application/json"})
        # The signed URL stays only in process memory, never in stdout or evidence.
        status, _ = http(upload["upload_url"], "PUT", data, upload["headers"])
        assert status == 200
        replay_status = None
        try:
            http(upload["upload_url"], "PUT", data, upload["headers"])
        except HTTPError as error:
            replay_status = error.code
        assert replay_status == 412, f"Expected single-write enforcement, got {replay_status}"
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            _, result = http(endpoint + upload["status_url"])
            if result["status"] in {"COMPLETE", "FAILED"}:
                break
            time.sleep(2)
        assert result["status"] == "COMPLETE", result
        _, report = http(endpoint + result["report_url"])
        assert report["mode"] == "aws-analysis" and report["caption_detection"]["status"] == "active"
        evidence["runs"].append({"clip": clip.name, "upload_id": upload["upload_id"],
            "job_id": result["job_id"], "request_id": report["cloud"]["request_id"],
            "upload_to_report_seconds": round(time.monotonic() - started, 3),
            "analysis_seconds": report["analysis_seconds"], "frames": report["source"]["frames"],
            "opencv": report["engine"]["version"], "caption_detection": report["caption_detection"]["status"],
            "issues": len(report["issues"]), "report_url": endpoint + result["report_url"],
            "viewer_url": endpoint + "/?report=" + result["report_url"], "replay_http_status": replay_status})
        if index == 0:
            report["artifacts"]["metrics"] = f'/reports/{result["job_id"]}/metrics.csv'
            report["artifacts"]["contact_sheet"] = f'/reports/{result["job_id"]}/contact-sheet.jpg'
            report["source"]["name"] = "demo.mp4"
            # Only viewer routing changes; measurements and AWS request ID are unchanged.
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as f:
                json.dump(report, f, indent=2)
                f.flush()
                subprocess.run(["aws", "s3", "cp", f.name,
                    f's3://{outputs["ReportBucket"]}/demo/report.json', "--content-type", "application/json",
                    "--region", "ap-southeast-2", "--only-show-errors"], check=True)
        print(json.dumps(evidence["runs"][-1]), flush=True)
    evidence["resources"] = aws("cloudformation", "list-stack-resources", "--stack-name", args.stack)["StackResourceSummaries"]
    evidence["free_plan"] = json.loads(subprocess.run(["aws", "freetier", "get-account-plan-state", "--region", "us-east-1"], check=True, capture_output=True, text=True).stdout)
    evidence["lambda_configuration"] = []
    for fn in [outputs["ApiFunction"], outputs["AnalyzeFunction"]]:
        config = aws("lambda", "get-function-configuration", "--function-name", fn)
        item = {k: config[k] for k in ["FunctionName", "MemorySize", "Timeout", "LastModified", "State", "PackageType"]}
        item.update(aws("lambda", "get-function-concurrency", "--function-name", fn))
        evidence["lambda_configuration"].append(item)
    evidence["cloudwatch_logs"] = aws("logs", "filter-log-events", "--log-group-name", outputs["Logs"],
        "--start-time", str(int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp() * 1000)), "--limit", "100")["events"]
    now = datetime.now(timezone.utc)
    queries = [{"Id": metric.lower(), "MetricStat": {"Metric": {"Namespace": "AWS/Lambda", "MetricName": metric,
        "Dimensions": [{"Name": "FunctionName", "Value": outputs["AnalyzeFunction"]}]},
        "Period": 60, "Stat": "Maximum" if metric == "Duration" else "Sum"}, "ReturnData": True}
        for metric in ["Duration", "Invocations", "Errors"]]
    evidence["cloudwatch_metrics"] = aws("cloudwatch", "get-metric-data", "--metric-data-queries", json.dumps(queries),
        "--start-time", (now - timedelta(minutes=30)).isoformat(), "--end-time", now.isoformat())
    Path("media/aws-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print("Saved media/aws-evidence.json (no credentials or signed URLs).")


if __name__ == "__main__":
    main()
