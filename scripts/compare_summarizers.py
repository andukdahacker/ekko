#!/usr/bin/env python3
"""Side-by-side quality/latency spike: local (Ollama) vs Gemini summarizer.

The roadmap's rule for the local summarizer is "measure the quality gap vs
Gemini first" before defaulting to it. This runs BOTH over the SAME transcript
and prints their notes side by side plus wall-clock latency, so the comparison
is on identical input.

Transcript source (pick one):
  --meeting N     a stored meeting id from ~/.ekko/ekko.db (see `ekko list`)
  --file PATH     a text file of "Speaker: text" lines (one utterance per line)

Examples:
  python scripts/compare_summarizers.py --meeting 3
  python scripts/compare_summarizers.py --file sample.txt --local-model llama3.1:8b
  python scripts/compare_summarizers.py --meeting 3 --only local
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Run from a source checkout without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ekko.config import DEFAULT_CONFIG, load_config  # noqa: E402
from ekko.models import Segment, Transcript  # noqa: E402
from ekko.summarize.gemini import GeminiSummarizer  # noqa: E402
from ekko.summarize.local import DEFAULT_MODEL, LocalSummarizer  # noqa: E402


def transcript_from_file(path: Path) -> Transcript:
    segs = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        speaker, _, text = line.partition(":")
        if text:
            segs.append(Segment(start=0.0, end=0.0, text=text.strip(),
                                speaker=speaker.strip()))
        else:  # no "Speaker:" prefix — treat whole line as an unattributed utterance
            segs.append(Segment(start=0.0, end=0.0, text=line))
    return Transcript(segments=segs)


def transcript_from_meeting(meeting_id: int) -> Transcript:
    from ekko.store.sqlite import SqliteStore
    cfg = load_config()
    data_dir = Path(cfg.get("data_dir", "~/.ekko")).expanduser()
    store = SqliteStore(db_path=data_dir / "ekko.db")
    m = store.get(meeting_id)
    if m is None:
        sys.exit(f"No meeting with id {meeting_id} in {data_dir/'ekko.db'}.")
    return m.transcript


def run(name: str, summarizer, transcript: Transcript):
    t0 = time.perf_counter()
    try:
        summary = summarizer.summarize(transcript)
    except Exception as e:  # keep the other provider's result usable
        return {"name": name, "error": str(e), "seconds": time.perf_counter() - t0}
    return {"name": name, "summary": summary, "seconds": time.perf_counter() - t0}


def render(result: dict) -> str:
    head = f"### {result['name']}  ({result['seconds']:.1f}s)"
    if "error" in result:
        return f"{head}\n  ERROR: {result['error']}"
    s = result["summary"]
    lines = [head, f"  TL;DR: {s.tldr}", "  Key points:"]
    lines += [f"    - {p}" for p in s.key_points] or ["    (none)"]
    lines.append("  Decisions:")
    lines += [f"    - {d}" for d in s.decisions] or ["    (none)"]
    lines.append("  Action items:")
    lines += [f"    - [{a.owner or '?'}] {a.text}" for a in s.action_items] \
        or ["    (none)"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--meeting", type=int, help="stored meeting id")
    src.add_argument("--file", type=Path, help='text file of "Speaker: text" lines')
    ap.add_argument("--only", choices=["local", "gemini"],
                    help="run just one provider (default: both)")
    ap.add_argument("--local-model", default=DEFAULT_MODEL,
                    help=f"Ollama model (default {DEFAULT_MODEL})")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                    help="config.toml for the Gemini key/model")
    args = ap.parse_args()

    transcript = (transcript_from_meeting(args.meeting) if args.meeting
                  else transcript_from_file(args.file))
    n_lines = len(transcript.segments)
    n_chars = len(transcript.as_dialogue())
    print(f"Transcript: {n_lines} segments, {n_chars} chars\n")

    results = []
    if args.only != "gemini":
        results.append(run(f"local · {args.local_model}",
                           LocalSummarizer(model=args.local_model), transcript))
    if args.only != "local":
        sum_cfg = load_config(args.config).get("summarize", {}) \
            if args.config.exists() else {}
        try:
            gem = GeminiSummarizer(model=sum_cfg.get("model", "gemini-3.6-flash"),
                                   api_key=sum_cfg.get("api_key"))
            results.append(run("gemini", gem, transcript))
        except Exception as e:
            results.append({"name": "gemini", "error": str(e), "seconds": 0.0})

    print("\n\n".join(render(r) for r in results))


if __name__ == "__main__":
    main()
