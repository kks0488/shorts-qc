"""Bounded perception -> decision -> ffmpeg action -> OpenCV verification loop."""
import hashlib
import json
import subprocess
from pathlib import Path

from .analysis import analyze


def retained_intervals(report, trim_types):
    duration = report["source"]["duration"]
    removed = sorted((max(0., i["start"]), min(duration, i["end"])) for i in report["issues"] if i["type"] in trim_types)
    merged = []
    for start, end in removed:
        if merged and start <= merged[-1][1]+1e-5:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    kept, cursor = [], 0.
    for start, end in merged:
        if start-cursor >= 1/report["source"]["fps"]:
            kept.append((cursor, start))
        cursor = end
    if duration-cursor >= 1/report["source"]["fps"]:
        kept.append((cursor, duration))
    return kept


def repair(source, output_dir, *, text_model=None, config=None, trim_types=("black", "frozen"), brighten=True, reference=None):
    out = Path(output_dir)
    if not Path(source).is_file():
        raise ValueError("Input must be a local file")
    source = Path(source).resolve()
    before = analyze(source, out/"before", config=config, text_model=text_model)
    if reference is not None:
        return restore_from_reference(source, Path(reference).resolve(), out, before, text_model, config)
    kept = retained_intervals(before, set(trim_types))
    if not kept:
        raise ValueError("Repair refused: no video remains after proposed trims")
    if len(kept) > 40:
        raise ValueError("Repair refused: more than 40 retained segments; review manually")
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(source)],
                                     check=True, capture_output=True, text=True, timeout=30).stdout)
    has_audio = any(s["codec_type"] == "audio" for s in probe["streams"])
    vfr = any(s["codec_type"] == "video" and s.get("avg_frame_rate") != s.get("r_frame_rate") for s in probe["streams"])
    if vfr:
        raise ValueError("Repair requires constant frame rate; normalize VFR footage first")
    steps = [{"tool": "opencv.analyze", "result": before["summary"]}]
    filters = []
    dark_intervals = [i for i in before["issues"] if i["type"] == "dark"] if brighten else []
    video_source = "0:v"
    if dark_intervals:
        enable = "+".join(f"between(t,{i['start']:.6f},{i['end']:.6f})" for i in dark_intervals)
        filters.append(f"[0:v]eq=brightness=0.08:enable='{enable}'[exposed]")
        video_source = "exposed"
    # split avoids multiple consumers of a filter output (raw input can be reused).
    if len(kept) > 1:
        filters.append(f"[{video_source}]split={len(kept)}"+"".join(f"[vin{i}]" for i in range(len(kept))))
        if has_audio:
            filters.append(f"[0:a]asplit={len(kept)}"+"".join(f"[ain{i}]" for i in range(len(kept))))
    for i, (start, end) in enumerate(kept):
        vi = f"vin{i}" if len(kept) > 1 else video_source
        ai = f"ain{i}" if len(kept) > 1 else "0:a"
        filters.append(f"[{vi}]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[v{i}]")
        if has_audio:
            filters.append(f"[{ai}]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{i}]")
    inputs = "".join(f"[v{i}]"+(f"[a{i}]" if has_audio else "") for i in range(len(kept)))
    filters.append(inputs+f"concat=n={len(kept)}:v=1:a={int(has_audio)}[joined]"+("[audio]" if has_audio else ""))
    width, height = before["source"]["width"], before["source"]["height"]
    target_h = max(height, int(width*16/9))
    target_h = ((target_h+31)//32)*32
    target_w = target_h*9//16
    filters.append(f"[joined]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=0x181c23,setsar=1[video]")
    target = out/"repaired.mp4"
    if target.resolve() == source:
        raise ValueError("Repair output must differ from original")
    if target.exists():
        raise ValueError("Repair output already exists; choose a new output directory")
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-i", str(source),
               "-filter_complex", ";".join(filters), "-map", "[video]"]
    if has_audio:
        command += ["-map", "[audio]", "-c:a", "aac", "-b:a", "128k"]
    command += ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target)]
    steps.append({"decision": "trim evidence intervals; lift only dark intervals; fit 9:16 without cropping",
                  "retained_intervals": kept, "brightened_intervals": dark_intervals, "human_control": "explicit repair command; original preserved"})
    subprocess.run(command, check=True, timeout=300, capture_output=True)
    steps.append({"tool": "ffmpeg", "argv": command, "output": target.name})
    after = analyze(target, out/"after", config=config, text_model=text_model)
    steps.append({"tool": "opencv.analyze", "result": after["summary"],
                  "decision": "human_review_required" if after["issues"] else "candidate_ready_for_human_review"})
    trace = {"policy": "deterministic bounded agent; no LLM or API key required", "steps": steps,
             "before_issue_count": len(before["issues"]), "after_issue_count": len(after["issues"]),
             "original_preserved": True, "limitations": "No semantic guarantee. Intentional stills may be removed; inspect output and audio before publishing."}
    (out/"trace.json").write_text(json.dumps(trace, indent=2)+"\n")
    return trace


def restore_from_reference(source, reference, out, before, text_model, config):
    """Replace flagged spans from an explicitly supplied, aligned original export.

    This cannot recover detail or words from damaged pixels. The caller must own
    and visually confirm the reference is the same edit, before the defects.
    """
    if not reference.is_file() or reference == source:
        raise ValueError("Reference must be a separate, aligned original video")
    target = out/"repaired.mp4"
    if target.exists() or target.resolve() in {source, reference}:
        raise ValueError("Repair output already exists or would overwrite an input")
    metadata = []
    for path in (source, reference):
        streams = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=30).stdout)["streams"]
        video = next(s for s in streams if s["codec_type"] == "video")
        if video.get("avg_frame_rate") != video.get("r_frame_rate"):
            raise ValueError("Reference restoration requires constant frame rate")
        metadata.append(tuple(video.get(k) for k in ("width", "height", "avg_frame_rate", "nb_frames")))
    if metadata[0] != metadata[1] or metadata[0][-1] is None:
        raise ValueError("Reference must match dimensions, frame rate and frame count")
    spans = [(i["start"], i["end"]) for i in before["issues"] if i["type"] != "scene_cut"]
    # Frame-rounded bounds prevent decimal report timestamps from leaving a
    # single damaged frame at either side of a replacement.
    fps = before["source"]["fps"]
    enable = "+".join(f"gte(n,{round(a*fps)})*lt(n,{round(b*fps)})" for a, b in spans) or "0"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-i", str(source), "-i", str(reference),
        "-filter_complex", f"[0:v][1:v]overlay=enable='{enable}':eof_action=pass[v]", "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(target)]
    subprocess.run(command, check=True, capture_output=True, timeout=300)
    after = analyze(target, out/"after", config=config, text_model=text_model)
    trace = {"policy": "source-aware restoration from explicitly supplied aligned original; no generative recovery",
        "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
        "replaced_intervals": spans, "before_issue_count": len(before["issues"]), "after_issue_count": len(after["issues"]),
        "original_preserved": True, "audio": "damaged input audio copied unchanged; audio defects are not repaired",
        "steps": [{"tool": "opencv.analyze", "result": before["summary"]}, {"tool": "ffmpeg", "argv": command},
            {"tool": "opencv.analyze", "result": after["summary"], "decision": "human_review_required" if after["issues"] else "candidate_ready_for_human_review"}],
        "limitations": "Requires the matching clean source export. Cannot reconstruct blur or missing text without it. Visual and audio review remain required."}
    (out/"trace.json").write_text(json.dumps(trace, indent=2)+"\n")
    return trace
