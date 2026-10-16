"""Bounded S3 -> OpenCV job. No AWS requests are made at import time."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import unquote_plus


def _clients():
    import boto3
    return boto3.client("s3"), boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])


def _validate_video(path):
    import cv2
    cap = cv2.VideoCapture(str(path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        width, height = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        pixels = width * height
        if not cap.isOpened() or any(not math.isfinite(value) or value <= 0 for value in (fps, frames, width, height)):
            raise ValueError("Unreadable video or missing duration metadata")
        if not frames.is_integer():
            raise ValueError("Invalid frame count metadata")
        if frames / fps > 120 or frames > 7200 or fps > 60.1 or pixels > 4_200_000:
            raise ValueError("Cloud limit: 120 seconds, 7200 frames, 60 fps, 4.2 megapixels")
    finally:
        cap.release()


def _analyze(path, output):
    from shorts_qc.analysis import analyze
    from shorts_qc.config import Config
    return analyze(path, output, config=Config(max_seconds=120, max_frames=7200),
        text_model=os.environ.get("TEXT_MODEL", "/opt/models/text.onnx"))


def _metric(name, value, unit="Count"):
    # Embedded Metric Format: CloudWatch extracts metrics from the ordinary log stream.
    print(json.dumps({"_aws": {"Timestamp": int(time.time() * 1000),
        "CloudWatchMetrics": [{"Namespace": "ShortsQC", "Dimensions": [[]],
        "Metrics": [{"Name": name, "Unit": unit}]}]}, name: value}))


def handler(event, context):
    s3, table = _clients()
    results = []
    for record in event.get("Records", []):
        if record.get("eventSource") != "aws:s3":
            continue
        obj = record["s3"]["object"]
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(obj["key"])
        if bucket != os.environ["UPLOAD_BUCKET"] or not key.startswith("incoming/"):
            raise ValueError("Unexpected upload bucket or key prefix")
        etag = obj.get("eTag", "").strip('"')
        if not etag:
            raise ValueError("S3 event must include an object eTag")
        job = hashlib.sha256(f"{bucket}\n{key}\n{etag}".encode()).hexdigest()
        now = int(time.time())
        lease = int(context.get_remaining_time_in_millis() / 1000) + 15
        try:
            table.put_item(Item={"job_id": job, "status": "PROCESSING", "upload_key": key,
                "lease_until": now + lease, "expires_at": now + 30 * 86400},
                ConditionExpression="attribute_not_exists(job_id) OR (#s <> :complete AND lease_until < :now)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":complete": "COMPLETE", ":now": now})
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                results.append({"job_id": job, "status": "duplicate"})
                continue
            raise
        started = time.monotonic()
        print(json.dumps({"job_id": job, "status": "processing"}))
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
            if head["ETag"].strip('"') != etag:
                table.update_item(Key={"job_id": job}, UpdateExpression="SET #s = :s",
                    ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "SUPERSEDED"})
                results.append({"job_id": job, "status": "superseded"})
                continue
            maximum = int(os.environ.get("MAX_UPLOAD_BYTES", "104857600"))
            if head["ContentLength"] <= 0 or head["ContentLength"] > maximum:
                raise ValueError("Cloud upload limit is 100 MiB")
            if Path(key).suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
                raise ValueError("Unsupported video extension")
            with tempfile.TemporaryDirectory(prefix="shorts-qc-") as temp:
                root = Path(temp)
                source = root / ("input" + Path(key).suffix.lower())
                response = s3.get_object(Bucket=bucket, Key=key, IfMatch=head["ETag"])
                total = 0
                try:
                    with source.open("wb") as stream:
                        for chunk in response["Body"].iter_chunks(chunk_size=1024 * 1024):
                            total += len(chunk)
                            if total > maximum:
                                raise ValueError("Cloud upload limit exceeded during download")
                            stream.write(chunk)
                finally:
                    response["Body"].close()
                _validate_video(source)
                output = root / "output"
                output.mkdir()
                report = _analyze(source, output)
                prefix = f"reports/{job}"
                report["mode"] = "aws-analysis"
                report["cloud_status"] = "deployed"
                report["cloud"] = {"job_id": job, "mode": "aws", "report_key": f"{prefix}/report.json",
                    "request_id": getattr(context, "aws_request_id", "local-test")}
                report["source"]["name"] = Path(key).name
                (output / "report.json").write_text(json.dumps(report, indent=2))
                for filename, mime in (("metrics.csv", "text/csv"), ("contact-sheet.jpg", "image/jpeg"), ("report.json", "application/json")):
                    s3.upload_file(str(output / filename), os.environ["REPORT_BUCKET"], f"{prefix}/{filename}",
                        ExtraArgs={"ContentType": mime, "CacheControl": "no-store"})
            table.update_item(Key={"job_id": job}, UpdateExpression="SET #s = :s, report_key = :r, completed_at = :t",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "COMPLETE", ":r": f"{prefix}/report.json", ":t": int(time.time())})
            _metric("Completed", 1)
            _metric("AnalysisSeconds", time.monotonic() - started, "Seconds")
            print(json.dumps({"job_id": job, "status": "complete", "report_key": f"{prefix}/report.json",
                "request_id": getattr(context, "aws_request_id", "local-test"),
                "elapsed_seconds": round(time.monotonic() - started, 3)}))
            results.append({"job_id": job, "status": "complete", "report_key": f"{prefix}/report.json"})
        except Exception as error:
            # Persist retryable status; Lambda's bounded asynchronous retries handle transient errors.
            table.update_item(Key={"job_id": job}, UpdateExpression="SET #s = :s, lease_until = :l, error_type = :e",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "FAILED", ":l": 0, ":e": type(error).__name__})
            _metric("Failed", 1)
            print(json.dumps({"job_id": job, "status": "failed", "error_type": type(error).__name__}))
            if isinstance(error, ValueError):
                results.append({"job_id": job, "status": "failed", "error_type": "ValueError"})
                continue
            raise
    return {"jobs": results}
