import csv
import hashlib
import json
import math
import time
from pathlib import Path

import cv2 as cv
import numpy as np

from .config import Config

FIXES = {
    "blur": "Re-render or replace this segment; sharpening cannot recover missing detail.",
    "black": "Review and trim unintended black frames.",
    "dark": "Apply a modest exposure lift, then re-check highlights and noise.",
    "frozen": "Review stillness; trim only if it is an unintended hold.",
    "flicker": "Re-render with stable exposure; inspect flashing before publishing.",
    "scene_cut": "Review this abrupt transition; an intentional cut can be accepted.",
    "caption_safe_zone": "Move captions inside the highlighted safe rectangle and re-render.",
    "caption_clipped": "Re-render captions with edge margin; missing glyphs cannot be recovered.",
    "letterbox": "Review the border and source framing before padding to vertical format.",
    "aspect_ratio": "Fit inside a 9:16 canvas without stretching or cropping.",
}


def ssim(a, b):
    """Mean local structural similarity; luminance 8-bit constants C1/C2."""
    a, b = a.astype(np.float32), b.astype(np.float32)
    ma, mb = cv.GaussianBlur(a, (7, 7), 1.5), cv.GaussianBlur(b, (7, 7), 1.5)
    va = cv.GaussianBlur(a*a, (7, 7), 1.5) - ma*ma
    vb = cv.GaussianBlur(b*b, (7, 7), 1.5) - mb*mb
    cov = cv.GaussianBlur(a*b, (7, 7), 1.5) - ma*mb
    score = ((2*ma*mb+6.5025)*(2*cov+58.5225))/((ma*ma+mb*mb+6.5025)*(va+vb+58.5225))
    return float(np.mean(score))


def image_metrics(gray):
    lap = float(cv.Laplacian(gray, cv.CV_32F).var())
    small = cv.resize(gray, (128, 128)).astype(np.float32)
    small -= small.mean()
    spectrum = cv.dft(small, flags=cv.DFT_COMPLEX_OUTPUT)
    energy = np.fft.fftshift(cv.magnitude(spectrum[:, :, 0], spectrum[:, :, 1])) ** 2
    yy, xx = np.ogrid[-64:64, -64:64]
    fft = float(energy[xx*xx+yy*yy > 24**2].sum() / max(float(energy.sum()), 1e-9))
    hist = cv.calcHist([gray], [0], None, [32], [0, 256]).ravel()
    hist /= max(float(hist.sum()), 1)
    return lap, fft, hist


def letterbox(gray, cfg):
    # Detect contiguous nearly black bars from each edge, not dark central content.
    if gray.mean() < cfg.dark_mean:
        return False
    values = []
    for profile in ((gray < 12).mean(axis=1), (gray < 12).mean(axis=0)):
        for edge in (profile, profile[::-1]):
            count = 0
            for val in edge:
                if val < .98:
                    break
                count += 1
            values.append(count / len(profile))
    return max(values) >= cfg.border_fraction


class TextDetector:
    def __init__(self, path):
        self.model = cv.dnn_TextDetectionModel_DB(cv.dnn.readNet(str(path)))
        self.model.setBinaryThreshold(.3)
        self.model.setPolygonThreshold(.45)
        self.model.setMaxCandidates(200)
        self.model.setUnclipRatio(1.8)
        self.model.setInputParams(scale=1/255, size=(736, 736), mean=(123.675, 116.28, 103.53))
        self.model.setInputScale(1 / (255 * np.array([.229, .224, .225])))

    def detect(self, frame):
        polygons, scores = self.model.detect(frame)
        h, w = frame.shape[:2]
        boxes = []
        for polygon, score in zip(polygons, scores):
            x, y, bw, bh = cv.boundingRect(polygon)
            boxes.append({"x": round(x/w, 5), "y": round(y/h, 5),
                          "w": round(bw/w, 5), "h": round(bh/h, 5), "confidence": round(float(score), 4)})
        return boxes


def caption_flags(boxes, cfg):
    unsafe = any(b["x"] < cfg.safe_left or b["y"] < cfg.safe_top or
                 b["x"]+b["w"] > cfg.safe_right or b["y"]+b["h"] > cfg.safe_bottom for b in boxes)
    clipped = any(b["x"] <= .012 or b["y"] <= .012 or b["x"]+b["w"] >= .988 or b["y"]+b["h"] >= .988 for b in boxes)
    return unsafe, clipped


def build_issues(rows, fps, cfg):
    issues = []
    for kind in FIXES:
        start = None
        for i in range(len(rows)+1):
            flagged = i < len(rows) and bool(rows[i][kind])
            if flagged and start is None:
                start = i
            if not flagged and start is not None:
                length = (i-start)/fps
                minimum = cfg.freeze_seconds if kind == "frozen" else (0 if kind in ("scene_cut", "aspect_ratio", "flicker") else cfg.min_issue_seconds)
                if length+1e-6 >= minimum:
                    issues.append({"type": kind, "start": round(start/fps, 4), "end": round(i/fps, 4),
                                   "severity": "review" if kind in ("scene_cut", "letterbox", "dark") else "error",
                                   "message": FIXES[kind], "frames": i-start})
                else:
                    for j in range(start, i):
                        rows[j][kind] = False
                start = None
    return sorted(issues, key=lambda x: (x["start"], x["type"]))


def contact_sheet(samples, report, output):
    canvas = np.full((620, 960, 3), (24, 22, 18), dtype=np.uint8)
    cfg = Config(**report["config"])
    for n, (timestamp, frame) in enumerate(samples[:8]):
        x, y = (n % 4)*240, (n//4)*310
        h, w = frame.shape[:2]
        scale = min(220/w, 264/h)
        frame = cv.resize(frame, (round(w*scale), round(h*scale)))
        h, w = frame.shape[:2]
        ox, oy = x+(240-w)//2, y+25
        canvas[oy:oy+h, ox:ox+w] = frame
        cv.rectangle(canvas, (ox+int(w*cfg.safe_left), oy+int(h*cfg.safe_top)),
                     (ox+int(w*cfg.safe_right), oy+int(h*cfg.safe_bottom)), (130, 220, 80), 1)
        labels = sorted({issue["type"] for issue in report["issues"] if issue["start"] <= timestamp < issue["end"]})
        cv.putText(canvas, f"{timestamp:.2f}s", (x+12, y+18), cv.FONT_HERSHEY_SIMPLEX, .45, (235, 240, 245), 1)
        cv.putText(canvas, ", ".join(labels)[:31] or "Clear sample", (x+10, y+304), cv.FONT_HERSHEY_SIMPLEX, .34, (160, 195, 245), 1)
    if not cv.imwrite(str(output), canvas):
        raise OSError("Unable to write contact sheet")


def analyze(input_path, output_dir, config=None, text_model=None):
    if not cv.__version__.startswith("5."):
        raise RuntimeError(f"OpenCV 5 is required; found {cv.__version__}")
    cfg = config if isinstance(config, Config) else Config(**(config or {}))
    source, out = Path(input_path), Path(output_dir)
    if not source.is_file():
        raise ValueError("Input must be a local video file")
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    detector = TextDetector(text_model) if text_model else None
    cap = cv.VideoCapture(str(source))
    try:
        fps = float(cap.get(cv.CAP_PROP_FPS))
        width, height = int(cap.get(cv.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv.CAP_PROP_FRAME_HEIGHT))
        count = int(cap.get(cv.CAP_PROP_FRAME_COUNT))
        if not cap.isOpened() or not math.isfinite(fps) or fps <= 0 or min(width, height) < 16:
            raise ValueError("Video cannot be decoded or has invalid metadata")
        if width*height > 16_777_216 or count > cfg.max_frames or count/fps > cfg.max_seconds:
            raise ValueError("Video exceeds configured frame, duration, or resolution limit")
        rows, samples, previous, previous_hist, boxes = [], [], None, None, []
        last_text = -math.inf
        next_sample = 0
        sample_period = max(count/fps/8, 1/fps)
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            idx = len(rows)
            if idx >= cfg.max_frames or idx/fps >= cfg.max_seconds:
                raise ValueError("Decoded video exceeds configured limits")
            ratio = min(1, cfg.max_dimension/max(frame.shape[:2]))
            frame = cv.resize(frame, (round(frame.shape[1]*ratio), round(frame.shape[0]*ratio)))
            gray = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)
            mean = float(gray.mean())
            lap, fft, hist = image_metrics(gray)
            black = mean < cfg.black_mean and float((gray < 18).mean()) >= cfg.black_fraction
            diff, similarity, hist_diff, structure_diff, delta = 0., 1., 0., 0., 0.
            if previous is not None:
                diff = float(cv.absdiff(previous, gray).mean())
                similarity = ssim(previous, gray)
                hist_diff = float(cv.compareHist(previous_hist, hist, cv.HISTCMP_BHATTACHARYYA))
                delta = mean-float(previous.mean())
                structure_diff = float(np.abs((gray.astype(np.float32)-mean) - (previous.astype(np.float32)-float(previous.mean()))).mean())
            if detector and idx/fps-last_text >= cfg.text_interval-1e-6:
                boxes = detector.detect(frame)
                last_text = idx/fps
            unsafe, clipped = caption_flags(boxes, cfg)
            row = {"frame": idx, "time": round(idx/fps, 5), "mean": round(mean, 4), "laplacian": round(lap, 4),
                   "fft_energy": round(fft, 6), "difference": round(diff, 4), "ssim": round(similarity, 6),
                   "histogram_distance": round(hist_diff, 6), "luminance_delta": round(delta, 4),
                   "structure_difference": round(structure_diff, 4), "text_boxes": boxes,
                   "blur": not black and mean >= cfg.dark_mean and lap < cfg.blur_laplacian and fft < cfg.blur_fft,
                   "black": black, "dark": not black and mean < cfg.dark_mean,
                   "frozen": previous is not None and not black and diff < cfg.freeze_diff and similarity > .99,
                   "scene_cut": previous is not None and diff > cfg.cut_diff and similarity < cfg.cut_ssim and structure_diff > cfg.flicker_structure_diff,
                   "flicker": False, "caption_safe_zone": unsafe, "caption_clipped": clipped,
                   "letterbox": letterbox(gray, cfg), "aspect_ratio": abs(width/height-9/16) > cfg.aspect_tolerance}
            rows.append(row)
            # Exposure oscillation: two opposing large derivatives within a quarter second.
            if len(rows) >= 3 and not black and structure_diff < cfg.flicker_structure_diff:
                for prior in rows[max(1, len(rows)-1-max(1, int(fps*.25))): -1]:
                    if abs(delta) >= cfg.flicker_delta and abs(prior["luminance_delta"]) >= cfg.flicker_delta and delta*prior["luminance_delta"] < 0 and prior["structure_difference"] < cfg.flicker_structure_diff and not prior["black"]:
                        for r in rows[prior["frame"]:]:
                            r["flicker"] = True
                        break
            if idx/fps >= next_sample and len(samples) < 8:
                samples.append((idx/fps, frame.copy()))
                next_sample += sample_period
            previous, previous_hist = gray, hist
    finally:
        cap.release()
    if not rows:
        raise ValueError("Video contains no decodable frames")
    if count > 0 and len(rows) < count-1:
        raise ValueError("Truncated video: decoding ended before the advertised frame count")
    issues = build_issues(rows, fps, cfg)
    digest = hashlib.sha256()
    with source.open("rb") as file:
        for chunk in iter(lambda: file.read(1024*1024), b""):
            digest.update(chunk)
    report = {"schema_version": "1.0", "engine": {"name": "OpenCV", "version": cv.__version__},
              "mode": "local-analysis", "cloud_status": "not deployed", "source": {"name": source.name, "sha256": digest.hexdigest(),
              "width": width, "height": height, "fps": fps, "frames": len(rows), "duration": round(len(rows)/fps, 5)},
              "config": cfg.to_dict(), "caption_detection": {"status": "active" if detector else "not run",
              "model": "OpenCV Zoo PP-OCRv3 DB English" if detector else None, "interval_seconds": cfg.text_interval},
              "issues": issues, "summary": {"status": "needs_review" if issues or not detector else "clear",
              "issue_count": len(issues), "types": sorted({i["type"] for i in issues})},
              "fix_plan": [{"type": kind, "action": FIXES[kind], "automatic_candidate": kind in ("black", "frozen", "dark", "aspect_ratio")} for kind in sorted({i["type"] for i in issues})],
              "artifacts": {"metrics": "metrics.csv", "contact_sheet": "contact-sheet.jpg"},
              "limitations": ["Heuristic thresholds require calibration to the source style.",
                              "Intentional stills, fades and cuts require human review.",
                              "Safe zones are configurable generic guides, not platform guarantees.",
                              "Text detection cannot prove that missing glyphs were cropped; edge contact is a risk signal.",
                              "Variable frame rate timestamps are approximated using nominal FPS; normalize to CFR for precise edits."]}
    if not detector:
        report["limitations"].append("Caption checks were not run: provide the downloaded text model.")
    report["analysis_seconds"] = round(time.monotonic()-started, 4)
    with (out/"metrics.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=rows[0].keys(), lineterminator="\n")
        writer.writeheader()
        writer.writerows({**row, "text_boxes": json.dumps(row["text_boxes"])} for row in rows)
    contact_sheet(samples, report, out/"contact-sheet.jpg")
    (out/"report.json").write_text(json.dumps(report, indent=2)+"\n")
    return report
