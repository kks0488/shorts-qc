"""Seeded, original test footage. Ground truth describes injected defects only."""
import json
from pathlib import Path

import cv2 as cv
import numpy as np

DEFECTS = ("blur", "black", "dark", "frozen", "flicker", "scene_cut",
           "caption_safe_zone", "caption_clipped", "letterbox", "aspect_ratio")
FPS = 12
WIDTH, HEIGHT = 324, 576


def _scene(seed, index, width=WIDTH, height=HEIGHT):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[:height, :width]
    # Fine original texture makes sharpness measurable; camera drift prevents
    # the negative controls from accidentally becoming frozen frames.
    noise = rng.integers(-28, 29, (height, width), dtype=np.int16)
    gray = np.clip(135 + 24*np.sin(xx/53) + 15*np.cos(yy/89) + noise, 65, 210).astype(np.uint8)
    frame = cv.cvtColor(np.roll(gray, index, axis=1), cv.COLOR_GRAY2BGR)
    x = int(width*.2 + (index*7) % max(1, int(width*.5)))
    cv.circle(frame, (x, int(height*.35)), int(width*.10), (75, 175, 225), -1)
    cv.rectangle(frame, (int(width*.15), int(height*.55)),
                 (int(width*.72), int(height*.66)), (195, 120, 75), -1)
    return frame


def _caption(frame, mode):
    h, w = frame.shape[:2]
    text = "BRIGHT DAY"
    font, scale, thickness = cv.FONT_HERSHEY_SIMPLEX, w/450, 2
    (tw, th), baseline = cv.getTextSize(text, font, scale, thickness)
    x = int(w*.09) if mode != "caption_clipped" else -int(w*.025)
    y = int(h*.90) if mode == "caption_safe_zone" else int(h*.70)
    cv.rectangle(frame, (max(0, x-6), y-th-8), (min(w-1, x+tw+6), y+baseline+5), (24, 24, 24), -1)
    cv.putText(frame, text, (x, y), font, scale, (255, 255, 255), thickness, cv.LINE_AA)
    return frame


def _frame(kind, seed, index, total, width=WIDTH, height=HEIGHT):
    frame = _scene(seed, 0 if kind == "frozen" else index, width, height)
    if kind == "blur":
        frame = cv.GaussianBlur(frame, (51, 51), 13)
        # A gentle continuous light ramp avoids conflating low-detail blur with
        # an actual frozen image, without introducing a flash or dark exposure.
        frame = np.clip(frame.astype(np.float32)+(index-total/2)*1.5, 0, 255).astype(np.uint8)
    elif kind == "black":
        frame[:] = 0
    elif kind == "dark":
        frame = np.clip(frame.astype(np.float32)*.17, 0, 255).astype(np.uint8)
    elif kind == "flicker":
        # Fixed structure plus alternating exposure isolates flicker from cuts.
        frame = _scene(seed, 0, width, height)
        frame = np.clip(frame.astype(np.int16)+(35 if index % 2 else -35), 0, 255).astype(np.uint8)
    elif kind == "scene_cut" and index >= total//2:
        frame = _scene(seed+103, index, width, height).astype(np.int16)
        frame[:height//2] -= 65
        frame[height//2:] += 65
        frame = np.clip(frame, 15, 245).astype(np.uint8)
    elif kind in ("caption_safe_zone", "caption_clipped"):
        frame = _caption(frame, kind)
    elif kind == "letterbox":
        frame[:int(height*.08)] = 0
        frame[-int(height*.08):] = 0
    return frame


def _labels(kind, index, total):
    flags = {defect: False for defect in DEFECTS}
    if kind in DEFECTS:
        flags[kind] = True
    if kind == "frozen":
        flags[kind] = index > 0
    elif kind == "flicker":
        # Two opposing derivatives are available from frame two; the analyzer
        # retroactively marks the first derivative (frame one).
        flags[kind] = index > 0
    elif kind == "scene_cut":
        flags[kind] = index == total//2
    elif kind == "caption_clipped":
        flags["caption_safe_zone"] = True
    return flags


def _write_video(path, frames, width, height):
    writer = cv.VideoWriter(str(path), cv.VideoWriter_fourcc(*"mp4v"), FPS, (width, height))
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not create the synthetic MP4")
    try:
        for frame in frames:
            writer.write(frame)
    finally:
        writer.release()


def generate_dataset(output_dir, seed=2026):
    """Write 22 two-second MP4s with distinct calibration and heldout seeds."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": "1.0", "seed": seed, "fps": FPS,
                "provenance": "Original seeded NumPy/OpenCV imagery; no external footage",
                "ground_truth": "Per-frame injected defects; edge-contact text is a clipping risk proxy",
                "splits": {"calibration": "threshold exploration only", "heldout": "reported evaluation only"},
                "clips": []}
    total = FPS*2
    for split_index, split in enumerate(("calibration", "heldout")):
        for kind_index, kind in enumerate(("clean",)+DEFECTS):
            clip_seed = int(seed)+split_index*100003+kind_index*997
            width, height = (400, HEIGHT) if kind == "aspect_ratio" else (WIDTH, HEIGHT)
            name = f"{split}-{kind}.mp4"
            _write_video(out/name, (_frame(kind, clip_seed, i, total, width, height) for i in range(total)), width, height)
            manifest["clips"].append({"file": name, "split": split, "seed": clip_seed,
                                      "injected": kind, "frames": [_labels(kind, i, total) for i in range(total)]})
    (out/"manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    return manifest


def generate_demo(output_path):
    """A twelve-second original clip with spaced, independently reviewable defects."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    schedule = [("clean", 2), ("blur", 2), ("black", 1), ("frozen", 1),
                ("dark", 2), ("flicker", 1), ("caption_safe_zone", 2), ("clean", 1)]
    frames, segments, start = [], [], 0
    for n, (kind, seconds) in enumerate(schedule):
        count = FPS*seconds
        frames.extend(_frame(kind, 90210+n*71, i, count) for i in range(count))
        segments.append({"type": kind, "start": start/FPS, "end": (start+count)/FPS})
        start += count
    _write_video(path, frames, WIDTH, HEIGHT)
    manifest = {"file": path.name, "fps": FPS, "duration": start/FPS,
                "provenance": "Original OpenCV-rendered synthetic footage", "segments": segments}
    path.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    return manifest
