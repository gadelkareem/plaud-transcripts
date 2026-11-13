"""Speaker labels for local transcripts: who speaks when, worked out on this machine.

Engine: sherpa-onnx speaker diarization (ONNX models on the CPU, no account needed).
The two models are downloaded once from the sherpa-onnx GitHub releases, about 30 MB.

Settings (environment variables):
  DIARIZE=0            turn speaker labels off
  DIARIZE_THRESHOLD    how different two voices must be to count as two speakers
                       (default: by recording length, see below; higher = fewer speakers)
  DIARIZE_MIN_SHARE    speakers with less of the talk time are folded away (default 0.02)
  DIARIZE_THREADS      CPU threads (default 4)
  DIARIZE_MODEL_DIR    where the models are kept (default: next to the Hugging Face cache)
"""
from __future__ import annotations

import os
import shutil
import tarfile
import urllib.request
from pathlib import Path

DIARIZE_VERSION = 2  # bump when speaker detection changes enough to redo old transcripts
DEFAULT_THRESHOLD = 0.55  # recordings up to 15 minutes
LONG_THRESHOLD = 0.7  # an hour or more: long audio splits one voice into several otherwise
DEFAULT_MIN_SHARE = 0.02  # speakers with less of the talk time than this are folded away
RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
SEGMENTATION_URL = RELEASES + "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
SEGMENTATION_FILE = "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
EMBEDDING_URL = RELEASES + "speaker-recongition-models/" + os.environ.get(
    "DIARIZE_EMBEDDING", "wespeaker_en_voxceleb_resnet34_LM.onnx"
)
_engine: dict = {}


def default_threshold(minutes: float) -> float:
    """0.55 up to 15 minutes, rising evenly to 0.7 at an hour (checked against Plaud's labels:
    0.55 was best on short recordings, 0.7 on 60-90 minute ones)."""
    share = min(max((minutes - 15) / 45, 0.0), 1.0)
    return round(DEFAULT_THRESHOLD + share * (LONG_THRESHOLD - DEFAULT_THRESHOLD), 2)


def enabled() -> bool:
    return os.environ.get("DIARIZE", "1") != "0"


def model_dir() -> Path:
    custom = os.environ.get("DIARIZE_MODEL_DIR")
    if custom:
        return Path(custom).expanduser()
    return Path(os.environ.get("HF_HOME", "~/.cache/huggingface")).expanduser() / "plaud_diarize"


def _fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "plaud-transcripts/1.0"})
    with urllib.request.urlopen(req, timeout=120) as res, open(part, "wb") as fh:
        shutil.copyfileobj(res, fh, 1024 * 1024)
    os.replace(part, dest)


def _models() -> tuple[Path, Path]:
    """Paths of the segmentation and voice-embedding models, downloading them once."""
    base = model_dir()
    segmentation = base / SEGMENTATION_FILE
    if not segmentation.exists():
        archive = base / "segmentation.tar.bz2"
        _fetch(SEGMENTATION_URL, archive)
        with tarfile.open(archive, "r:bz2") as tar:
            member = tar.getmember(SEGMENTATION_FILE)
            segmentation.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as src, open(segmentation, "wb") as dst:
                shutil.copyfileobj(src, dst)
        archive.unlink()
    embedding = base / urllib.request.unquote(EMBEDDING_URL.rsplit("/", 1)[1])
    if not embedding.exists():
        _fetch(EMBEDDING_URL, embedding)
    return segmentation, embedding


def _load(threshold: float):
    if threshold not in _engine:
        import sherpa_onnx

        segmentation, embedding = _models()
        threads = int(os.environ.get("DIARIZE_THREADS", "4"))
        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(segmentation)),
                num_threads=threads,
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(embedding), num_threads=threads),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=threshold),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        if not config.validate():
            raise RuntimeError("speaker detection models could not be loaded")
        _engine.clear()
        _engine[threshold] = sherpa_onnx.OfflineSpeakerDiarization(config)
    return _engine[threshold]


def turns(samples, threshold: float | None = None) -> list[tuple[int, int, int]]:
    """Who speaks when in 16 kHz mono samples: [(start ms, end ms, speaker number)]."""
    if threshold is None:
        threshold = float(os.environ.get("DIARIZE_THRESHOLD") or default_threshold(len(samples) / 16000 / 60))
    engine = _load(threshold)
    if engine.sample_rate != 16000:
        raise RuntimeError(f"speaker detection expects {engine.sample_rate} Hz audio")
    result = engine.process(samples).sort_by_start_time()
    return [(int(t.start * 1000), int(t.end * 1000), int(t.speaker)) for t in result]


def label(segments: list[dict], speaker_turns: list[tuple[int, int, int]]) -> int:
    """Fill each segment's `sp` with "Speaker N" (the format Plaud uses); returns the count.

    A segment gets the speaker who talks for most of it; one that overlaps nobody takes
    the nearest turn. Speakers are numbered in the order they first appear.
    """
    if not segments:
        return 0
    if not speaker_turns:
        for seg in segments:
            seg["sp"] = ""
        return 0
    first = 0 # turns before this one end before the current segment starts
    for seg in segments:
        start, end = seg["s"], max(seg["e"], seg["s"] + 1)
        while first < len(speaker_turns) - 1 and speaker_turns[first][1] <= start:
            first += 1
        overlap: dict[int, int] = {}
        i = first
        while i < len(speaker_turns) and speaker_turns[i][0] < end:
            t_start, t_end, speaker = speaker_turns[i]
            shared = min(end, t_end) - max(start, t_start)
            if shared > 0:
                overlap[speaker] = overlap.get(speaker, 0) + shared
            i += 1
        if overlap:
            speaker = max(overlap, key=overlap.get)
        else:
            middle = (start + end) // 2
            nearby = speaker_turns[max(0, first - 1): i + 1]
            speaker = min(nearby, key=lambda t: min(abs(t[0] - middle), abs(t[1] - middle)))[2]
        seg["sp"] = speaker
    return merge_minor(segments)


def merge_minor(segments: list[dict], min_share: float | None = None) -> int:
    """Fold speakers with a tiny share of the talk time into the speaker next to them, then
    number the rest "Speaker 1", ... in order of first appearance; returns the count.

    Long or noisy recordings come out with a tail of "speakers" that are a cough, a door or
    a few words in a different tone. Works on any labels, so it can tidy stored transcripts.
    """
    if min_share is None:
        min_share = float(os.environ.get("DIARIZE_MIN_SHARE", DEFAULT_MIN_SHARE))
    talk: dict = {}
    for seg in segments:
        if seg.get("sp") not in ("", None):
            talk[seg["sp"]] = talk.get(seg["sp"], 0) + max(seg["e"] - seg["s"], 1)
    total = sum(talk.values())
    major = {sp for sp, ms in talk.items() if ms >= min_share * total}
    major = major or set(sorted(talk, key=talk.get)[-1:])  # many tiny ones: keep the biggest
    kept = [i for i, seg in enumerate(segments) if seg.get("sp") in major]
    names: dict = {}
    original = lambda s: s.get("_sp", s["sp"])  # a later neighbour is not renamed yet
    at = 0  # index in `kept` of the first major segment after the current one
    for i, seg in enumerate(segments):
        if seg.get("sp") in ("", None):
            seg["sp"] = ""
            continue
        while at < len(kept) and kept[at] < i:
            at += 1
        if seg["sp"] not in major:
            before = segments[kept[at - 1]] if at > 0 else None
            after = segments[kept[at]] if at < len(kept) else None
            if before and after:
                closer_before = seg["s"] - before["e"] <= after["s"] - seg["e"]
                seg["sp"] = original(before if closer_before else after)
            else:
                seg["sp"] = original(before or after)
        seg["_sp"] = seg["sp"]  # the label before renaming, for later neighbours
        seg["sp"] = names.setdefault(seg["sp"], f"Speaker {len(names) + 1}")
    for seg in segments:
        seg.pop("_sp", None)
    return len(names)
