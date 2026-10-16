"""Measured per-frame confusion counts on heldout synthetic footage."""
import csv
import json
from pathlib import Path

from .analysis import analyze
from .synthetic import DEFECTS


def _score(tp, fp, fn, tn):
    return {"true_positive": tp, "false_positive": fp, "false_negative": fn, "true_negative": tn,
            "precision": round(tp/(tp+fp), 4) if tp+fp else None,
            "recall": round(tp/(tp+fn), 4) if tp+fn else None}


def evaluate_dataset(manifest_path, output_dir, text_model=None, split="heldout"):
    path, out = Path(manifest_path), Path(output_dir)
    manifest = json.loads(path.read_text())
    clips = [clip for clip in manifest["clips"] if clip["split"] == split]
    if not clips:
        raise ValueError(f"No clips in requested split: {split}")
    counts = {kind: [0, 0, 0, 0] for kind in DEFECTS}
    caption_types = {"caption_safe_zone", "caption_clipped"}
    clip_results = []
    for clip in clips:
        clip_out = out/Path(clip["file"]).stem
        report = analyze(path.parent/clip["file"], clip_out, text_model=text_model)
        with (clip_out/"metrics.csv").open(newline="") as file:
            rows = list(csv.DictReader(file))
        if len(rows) != len(clip["frames"]):
            raise ValueError(f"Decoded frame count differs from ground truth: {clip['file']}")
        for row, expected in zip(rows, clip["frames"]):
            for kind in DEFECTS:
                if not text_model and kind in caption_types:
                    continue
                truth, predicted = bool(expected[kind]), row[kind] == "True"
                counts[kind][0 if truth and predicted else 1 if predicted else 2 if truth else 3] += 1
        clip_results.append({"file": clip["file"], "seed": clip["seed"], "injected": clip["injected"],
                             "detected_types": report["summary"]["types"], "report": str(clip_out.relative_to(out)/"report.json")})
    result = {"schema_version": "1.0", "split": split, "seed": manifest["seed"],
              "clips": len(clips), "frames": sum(len(c["frames"]) for c in clips),
              "unit": "per decoded frame; no boundary tolerance", "thresholds": "Default Config; no heldout tuning",
              "caption_detection": "active" if text_model else "not run: model not provided",
              "metrics": {kind: ({"status": "not evaluated: DNN model not provided"} if not text_model and kind in caption_types
                                 else _score(*counts[kind])) for kind in DEFECTS},
              "clip_results": clip_results,
              "limitations": ["Small synthetic benchmark measures injected defects, not real-world accuracy.",
                              "Calibration and heldout clips use independent seeds but the same procedural generator.",
                              "Caption clipping labels use deliberately edge-truncated text; detection is a risk signal.",
                              "Text is sampled every 0.5 seconds; this benchmark uses persistent captions."]}
    out.mkdir(parents=True, exist_ok=True)
    (out/"evaluation.json").write_text(json.dumps(result, indent=2)+"\n")
    return result
