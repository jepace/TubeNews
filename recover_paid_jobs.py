#!/usr/bin/env python3
"""Retrieve Supadata transcription jobs that were billed but never collected.

Background: until this was fixed, `fetch_transcript` called the Supadata SDK
without passing `mode`, so it used the SDK default of "auto". For a video with
no existing captions that falls back to speech-to-text, which Supadata bills by
video duration rather than per request. The API answered HTTP 202 with a job id
instead of a transcript; the code only looked for a `.content` attribute, found
none, recorded "no captions", and discarded the id — after the job had been paid
for.

This fetches those results so the transcripts are not lost as well as paid for,
and writes them into the archive where the normal pipeline will pick them up.

Usage:
    python3 recover_paid_jobs.py <job_id> [<job_id> ...]
    python3 recover_paid_jobs.py --job <job_id> --video <video_id> --channel <slug>

Find job ids in the Supadata dashboard (each 202 request shows one) or in the
daemon log after this fix landed:
    grep 'Billed for an async transcription job' /var/log/tubenews_daemon.log
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from supadata import Supadata  # noqa: E402

from TubeNews import CONFIG_FILE, STORAGE_ROOT  # noqa: E402


def _client() -> Supadata:
    key = json.loads(CONFIG_FILE.read_text()).get("supadata_api_key", "")
    if not key:
        sys.exit("No supadata_api_key in config.json")
    return Supadata(api_key=key)


def _format(segments) -> str:
    """Match the on-disk transcript.txt format the Gemini step expects."""
    return "\n".join(
        f"{int(getattr(seg, 'offset', 0) / 1000)}s --> {getattr(seg, 'text', '')}"
        for seg in segments
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_ids", nargs="*", help="Job ids to retrieve")
    ap.add_argument("--job", help="A single job id (alternative to positional)")
    ap.add_argument("--video", help="Video id to file the transcript under")
    ap.add_argument("--channel", help="Channel directory slug under content/")
    args = ap.parse_args()

    jobs = list(args.job_ids) + ([args.job] if args.job else [])
    if not jobs:
        ap.error("give at least one job id")

    client = _client()
    for job_id in jobs:
        print(f"\n=== {job_id} ===")
        try:
            result = client.get_results(job_id)
        except Exception as exc:
            print(f"  could not retrieve: {exc}")
            print("  (results may expire; the Supadata dashboard shows the original request)")
            continue

        segments = getattr(result, "content", None)
        if not segments:
            print(f"  no content on the result: {result!r}")
            continue

        text = _format(segments) if not isinstance(segments, str) else segments
        print(f"  retrieved {len(text):,} chars")

        if args.video and args.channel:
            out_dir = STORAGE_ROOT / args.channel / args.video
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "transcript.txt").write_text(text, encoding="utf-8")
            # Drop any "no transcript" write-off so the pipeline reprocesses it.
            meta = out_dir / "metadata.json"
            if meta.exists():
                try:
                    data = json.loads(meta.read_text())
                    if data.get("status") == "no_transcript_available":
                        meta.unlink()
                        print(f"  cleared stale {meta.name} so the daemon will reprocess")
                except (OSError, ValueError):
                    pass
            print(f"  wrote {out_dir / 'transcript.txt'}")
        else:
            out = Path(f"transcript_{job_id}.txt")
            out.write_text(text, encoding="utf-8")
            print(f"  wrote {out}  (pass --video/--channel to file it into content/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
