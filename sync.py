#!/usr/bin/env python3
"""Download every Plaud recording, save a transcript next to it, build a local viewer.

Auth: reads the OAuth token that `plaud login` (Plaud's official CLI) stores in
~/.plaud/tokens.json and refreshes it when it expires, so there is no header to copy.

Transcripts: Plaud's own transcript is used when the recording has one (it has
speaker labels); otherwise the audio is transcribed locally with Whisper (MLX).
Recordings without a Plaud summary get a title and short summary from a local LLM.

Exit codes: 0 ok, 1 error, 2 not logged in (run.sh then runs the login and retries).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import platform
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIBRARY = Path(os.environ.get("PLAUD_LIBRARY", ROOT / "library")).expanduser()
AUDIO_DIR = LIBRARY / "audio"
TEXT_DIR = LIBRARY / "transcripts"
DATA_DIR = LIBRARY / "data"
MANIFEST = DATA_DIR / "manifest.json"

API_BASE = os.environ.get("PLAUD_API_BASE", "https://platform.plaud.ai/developer/api").rstrip("/")
REFRESH_URL = os.environ.get("PLAUD_REFRESH_URL", API_BASE + "/oauth/third-party/access-token/refresh")
TOKEN_FILE = Path(os.environ.get("PLAUD_TOKEN_FILE", "~/.plaud/tokens.json")).expanduser()
# Engines: MLX on Apple Silicon; faster-whisper + llama.cpp everywhere else (Windows, Linux).
BACKEND = os.environ.get("PLAUD_BACKEND") or (
    "mlx" if sys.platform == "darwin" and platform.machine() == "arm64" else "portable"
)
if BACKEND == "mlx":
    DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
    DEFAULT_SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "mlx-community/Qwen2.5-14B-Instruct-4bit")
else:
    DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
    DEFAULT_SUMMARY_MODEL = os.environ.get(
        "SUMMARY_MODEL", "bartowski/Qwen2.5-7B-Instruct-GGUF:Qwen2.5-7B-Instruct-Q4_K_M.gguf"
    )
MIN_SUMMARY_CHARS = 300  # shorter transcripts are their own summary
USER_AGENT = "plaud-transcripts/1.0"
EXIT_AUTH = 2


class NotLoggedIn(Exception):
    pass


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ----------------------------------------------------------------------------- auth
def _extra_headers() -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if os.environ.get("PLAUD_REGION"):
        headers["x-pld-region"] = os.environ["PLAUD_REGION"]
    return headers


def _load_tokens() -> dict:
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise NotLoggedIn("no saved Plaud login")


def _save_tokens(tokens: dict) -> None:
    TOKEN_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(tokens, fh, indent=2)
    os.replace(tmp, TOKEN_FILE)


def _refresh(tokens: dict) -> dict:
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise NotLoggedIn("saved login has no refresh token")
    body = urllib.parse.urlencode({"refresh_token": refresh_token}).encode()
    headers = {**_extra_headers(), "Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(REFRESH_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.loads(res.read())
    except urllib.error.HTTPError as err:
        if err.code in (400, 401, 403):
            raise NotLoggedIn(f"Plaud rejected the saved login (HTTP {err.code})")
        raise
    if not data.get("access_token"):
        raise NotLoggedIn("token refresh returned no access token")
    expires_in = data.get("expires_in")
    new = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token") or refresh_token,
        "token_type": data.get("token_type") or "Bearer",
    }
    if expires_in:
        new["expires_at"] = int(time.time() * 1000 + float(expires_in) * 1000)
    _save_tokens(new)  # same format the official CLI writes, so both keep working
    return new


def access_token(force_refresh: bool = False) -> str:
    tokens = _load_tokens()
    expires_at = tokens.get("expires_at")
    expiring = bool(expires_at) and time.time() * 1000 > float(expires_at) - 60_000
    if force_refresh or expiring or not tokens.get("access_token"):
        tokens = _refresh(tokens)
    return tokens["access_token"]


def api_get(path: str) -> dict:
    """GET an API path, refreshing the token once on 401 and retrying transient errors."""
    refreshed = False
    last: Exception | None = None
    for attempt in range(5):
        token = access_token()
        req = urllib.request.Request(
            API_BASE + path, headers={**_extra_headers(), "Authorization": f"Bearer {token}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return json.loads(res.read())
        except urllib.error.HTTPError as err:
            if err.code == 401 and not refreshed:
                access_token(force_refresh=True)
                refreshed = True
                continue
            if err.code in (401, 403):
                raise NotLoggedIn(f"Plaud refused the request (HTTP {err.code})")
            if err.code not in (429, 500, 502, 503, 504):
                raise
            last = err
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            last = err
        time.sleep(2 ** attempt)
    raise RuntimeError(f"Plaud API kept failing for {path}: {last}")


# ----------------------------------------------------------------------------- plaud data
def list_recordings(since_ms: int | None = None) -> list[dict]:
    out: list[dict] = []
    page, size = 1, 100
    while page <= 1000:
        query = {"page": page, "page_size": size}
        if since_ms is not None:
            query["start_date"] = since_ms
        data = api_get("/open/third-party/files/?" + urllib.parse.urlencode(query)).get("data") or []
        out.extend(data)
        if len(data) < size:
            break
        page += 1
    seen, unique = set(), []
    for rec in out:
        if rec.get("id") and rec["id"] not in seen:
            seen.add(rec["id"])
            unique.append(rec)
    return unique


def fetch_bytes(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def block_content(block: dict | None) -> str:
    """Text of a transcript/summary block: inline when present, else from its link."""
    if not block:
        return ""
    inline = block.get("data_content")
    if isinstance(inline, str) and inline:
        return inline
    link = block.get("data_link")
    if not link:
        return ""
    raw = fetch_bytes(link)
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def plaud_segments(detail: dict) -> list[dict] | None:
    """Plaud's transcript as [{s, e, sp, t}] (ms), or None when there is none."""
    block = next((b for b in detail.get("source_list") or [] if b.get("data_type") == "transaction"), None)
    text = block_content(block)
    if not text:
        return None
    try:
        raw = json.loads(text)
    except ValueError:
        return [{"s": 0, "e": 0, "sp": "", "t": text.strip()}]
    if not isinstance(raw, list):
        return None
    segs = []
    for seg in raw:
        if not isinstance(seg, dict):
            continue
        content = (seg.get("content") or "").strip()
        if content:
            segs.append({
                "s": int(seg.get("start_time") or 0),
                "e": int(seg.get("end_time") or 0),
                "sp": seg.get("speaker") or "",
                "t": content,
            })
    return segs or None


def plaud_summary(detail: dict) -> str:
    note = next((n for n in detail.get("note_list") or [] if n.get("data_type") == "auto_sum_note"), None)
    try:
        text = block_content(note)
    except Exception:
        return ""
    # drop Plaud's in-app image placeholders, e.g. [](plaud://image?...)
    return re.sub(r"!?\[[^\]]*\]\(plaud://[^)]*\)\s*", "", text).strip()


def download_audio(url: str, dest: Path) -> None:
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as res, open(part, "wb") as fh:
        shutil.copyfileobj(res, fh, 1024 * 1024)
    os.replace(part, dest)


# ----------------------------------------------------------------------------- naming / time
def parse_utc(value: str | None) -> datetime | None:
    """Plaud timestamps are UTC, usually without a zone suffix."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def slugify(name: str) -> str:
    name = re.sub(r"^\d{4}-\d{2}-\d{2}[ _T]\d{2}[:.]\d{2}([:.]\d{2})?$", "", name.strip())  # default names are just the date
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", name)
    name = re.sub(r"\s+", "-", name.strip())
    return name[:60].strip("-.")


def base_name(rec: dict) -> str:
    when = parse_utc(rec.get("start_at")) or parse_utc(rec.get("created_at"))
    stamp = when.astimezone().strftime("%Y-%m-%d_%H%M") if when else "undated"
    parts = [stamp, slugify(rec.get("name") or ""), rec["id"][-6:]]
    return "_".join(p for p in parts if p)


def fmt_clock(ms: int) -> str:
    total = int(ms // 1000)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def audio_extension(url: str) -> str:
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{2,5}", ext) else ".mp3"


# ----------------------------------------------------------------------------- local storage
def load_manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_manifest(manifest: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, MANIFEST)


def write_transcript(entry: dict, segments: list[dict], summary: str) -> None:
    """Write the readable .md, plus the .js the viewer loads (works from file://)."""
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "t").mkdir(parents=True, exist_ok=True)
    source = {"plaud": "Plaud", "whisper": "local Whisper"}.get(entry.get("source"), "none")
    lines = [
        f"# {entry.get('title') or entry['name']}",
        "",
        f"- Recorded: {entry.get('start_local') or 'unknown'}",
        f"- Duration: {fmt_clock(entry.get('duration_ms') or 0)}",
        f"- Audio: ../{entry['audio']}" if entry.get("audio") else "- Audio: not downloaded",
        f"- Transcript by: {source}" + (f" ({entry['language']})" if entry.get("language") else ""),
        f"- Plaud id: {entry['id']}",
        "",
    ]
    if summary:
        by = " (generated locally)" if entry.get("summary_by") == "local" else ""
        lines += [f"## Summary{by}", "", summary, ""]
    lines += ["## Transcript", ""]
    if not segments:
        lines.append("_No speech detected._")
    for seg in segments:
        speaker = f"**{seg['sp']}:** " if seg.get("sp") else ""
        lines.append(f"[{fmt_clock(seg['s'])}] {speaker}{seg['t']}")
        lines.append("")
    (TEXT_DIR / f"{entry['base']}.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    payload = json.dumps({"segments": segments, "summary": summary}, ensure_ascii=False)
    (DATA_DIR / "t" / f"{entry['id']}.js").write_text(
        f"window.__plaudTranscript({json.dumps(entry['id'])}, {payload});\n",
        encoding="utf-8",
    )
    entry["transcript"] = f"transcripts/{entry['base']}.md"
    entry["chars"] = sum(len(s["t"]) for s in segments)
    entry["has_summary"] = bool(summary)


def read_transcript(entry: dict) -> dict:
    """Read back what write_transcript stored for the viewer: {segments, summary}."""
    try:
        text = (DATA_DIR / "t" / f"{entry['id']}.js").read_text(encoding="utf-8")
        return json.loads(text[text.index(", {") + 2: text.rindex(");")])
    except (OSError, ValueError):
        return {"segments": [], "summary": ""}


TIMESTAMP_NAME = re.compile(r"^\d{4}-\d\d-\d\d[ _T]\d\d")  # Plaud's default name: no real title


def add_title_hints(manifest: dict) -> None:
    """Recordings with no real title (nothing to summarise) get one that says why."""
    for entry in manifest.values():
        if entry.get("source") not in ("plaud", "whisper"):
            continue
        if entry.get("title") or not TIMESTAMP_NAME.match(entry.get("name") or ""):
            continue
        data = read_transcript(entry)
        words = " ".join(seg["t"] for seg in data["segments"]).split()
        if words:
            why = "Untitled" if data.get("summary") else "No summary"
            entry["title"] = f"{why} · " + " ".join(words[:8]) + ("…" if len(words) > 8 else "")
        else:
            entry["title"] = "No speech"
        entry["title_by"] = "hint"
        entry.setdefault("tags", [])
        write_transcript(entry, data["segments"], data.get("summary") or "")


def build_viewer(manifest: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows = sorted(manifest.values(), key=lambda e: e.get("start_utc") or "", reverse=True)
    (DATA_DIR / "index.js").write_text(
        "window.PLAUD_INDEX = " + json.dumps(rows, ensure_ascii=False) + ";\n"
        f"window.PLAUD_BUILT = {json.dumps(datetime.now().astimezone().strftime('%Y-%m-%d %H:%M'))};\n",
        encoding="utf-8",
    )
    shutil.copyfile(ROOT / "viewer.html", LIBRARY / "index.html")


# ----------------------------------------------------------------------------- whisper
_whisper = None
WHISPER_VERSION = 2  # bump when local transcription changes enough to redo old transcripts
WINDOW_SECONDS = 30
CONFIDENT = 0.6      # a window's language counts only above this probability


def _load_whisper():
    global _whisper
    if _whisper is None:
        import mlx_whisper  # imported late: slow to load, and only needed for local transcription

        _whisper = mlx_whisper
    return _whisper


_portable: dict = {}


def _portable_whisper(model: str):
    """faster-whisper model (CPU, or an NVIDIA GPU when one is usable)."""
    if model not in _portable:
        from faster_whisper import WhisperModel

        _portable.clear()
        _portable[model] = WhisperModel(
            model,
            device=os.environ.get("WHISPER_DEVICE", "auto"),
            compute_type=os.environ.get("WHISPER_COMPUTE", "auto"),
            cpu_threads=int(os.environ.get("WHISPER_THREADS", "0")),
        )
    return _portable[model]


def load_samples(audio_path: Path):
    """The recording as 16 kHz mono float32 samples."""
    import numpy as np

    if BACKEND == "mlx":
        _load_whisper()
        from mlx_whisper.audio import load_audio

        return np.array(load_audio(str(audio_path)), dtype=np.float32)
    import subprocess

    done = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(audio_path),
         "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True,
    )
    if done.returncode != 0:
        raise RuntimeError("ffmpeg could not read the audio: " + done.stderr.decode("utf-8", "replace")[-300:])
    return np.frombuffer(done.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def window_languages(audio, model: str) -> list[tuple[str, float]]:
    """Whisper's best language guess and its probability for every 30 s window."""
    if BACKEND != "mlx":
        whisper_model = _portable_whisper(model)
        size = WINDOW_SECONDS * 16000
        count = max(1, -(-len(audio) // size))
        if not whisper_model.model.is_multilingual:
            return [("en", 1.0)] * count
        # Each check costs a full encoder pass, which is slow on a CPU: sample every Nth
        # window; the ones in between take the language of the nearest sampled window.
        every = max(1, int(os.environ.get("WHISPER_DETECT_EVERY", "4")))
        guesses = []
        for i in range(count):
            if i % every:
                guesses.append(("", 0.0))
                continue
            language, prob, _ = whisper_model.detect_language(audio[i * size:(i + 1) * size])
            guesses.append((language, float(prob)))
        return guesses

    import mlx.core as mx
    from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
    from mlx_whisper.transcribe import ModelHolder

    whisper_model = ModelHolder.get_model(model, mx.float16)
    count = max(1, -(-len(audio) // N_SAMPLES))
    if not whisper_model.is_multilingual:
        return [("en", 1.0)] * count
    guesses = []
    for i in range(count):
        chunk = audio[i * N_SAMPLES:(i + 1) * N_SAMPLES]
        mel = log_mel_spectrogram(chunk, n_mels=whisper_model.dims.n_mels)
        mel = pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16)
        _, probs = whisper_model.detect_language(mel)
        best = max(probs, key=probs.get)
        guesses.append((best, float(probs[best])))
    return guesses


def _transcribe_piece(piece, model: str, language: str) -> list[tuple[float, float, str]]:
    """(start s, end s, text) for one stretch of audio in one language."""
    if BACKEND != "mlx":
        segments, _ = _portable_whisper(model).transcribe(
            piece,
            language=language,
            beam_size=1,
            condition_on_previous_text=False,
            # Skipping silence is much faster on a CPU but also drops quiet speech: opt-in.
            vad_filter=os.environ.get("WHISPER_VAD", "") == "1",
        )
        return [(seg.start, seg.end, seg.text) for seg in segments]
    result = _load_whisper().transcribe(
        piece,
        path_or_hf_repo=model,
        language=language,
        verbose=None,
        condition_on_previous_text=False,  # stops one bad segment repeating through a long recording
    )
    return [(seg["start"], seg["end"], seg.get("text") or "") for seg in result.get("segments") or []]


def plan_languages(guesses: list[tuple[str, float]]) -> list[str]:
    """One language per 30 s window.

    Whisper normally fixes the language from the first 30 s, which is wrong for
    recordings that open with noise or switch languages. Here each window keeps its
    own language when Whisper is confident; unsure windows (noise, silence, overlap)
    take the language of the nearest confident window. Languages that only show up
    in a stray window are ignored as misdetections.
    """
    if len(guesses) == 1:
        return [guesses[0][0]]
    confident = [lang for lang, prob in guesses if prob >= CONFIDENT]
    counts = {lang: confident.count(lang) for lang in set(confident)}
    sampled = sum(1 for lang, _ in guesses if lang)
    need = 2 if sampled == len(guesses) else 1  # sparse sampling (CPU): one sure window counts
    keep = {lang for lang, n in counts.items() if n >= need and n >= 0.05 * len(confident)}
    if not keep:
        overall = max(counts, key=counts.get) if counts else max(guesses, key=lambda g: g[1])[0] or "en"
        return [overall] * len(guesses)
    sure = [lang if prob >= CONFIDENT and lang in keep else None for lang, prob in guesses]
    anchors = [i for i, lang in enumerate(sure) if lang]
    return [sure[min(anchors, key=lambda a: abs(a - i))] for i in range(len(sure))]


def language_label(plan: list[str]) -> str:
    """Languages by share of the recording, e.g. "ar+en"."""
    return "+".join(sorted(set(plan), key=lambda lang: -plan.count(lang)))


def whisper_segments(audio, model: str, plan: list[str]) -> list[dict]:
    """Transcribe locally, each stretch of the recording in its own language."""
    rate = 16000
    stretches: list[list] = []  # [language, first window, last window + 1]
    for i, language in enumerate(plan):
        if stretches and stretches[-1][0] == language:
            stretches[-1][2] = i + 1
        else:
            stretches.append([language, i, i + 1])
    segs: list[dict] = []
    for language, first, last in stretches:
        offset_ms = first * WINDOW_SECONDS * 1000
        piece = audio[first * WINDOW_SECONDS * rate: last * WINDOW_SECONDS * rate]
        if len(piece) < rate // 2:
            continue
        for start, end, text in _transcribe_piece(piece, model, language):
            text = (text or "").strip()
            if text:
                segs.append({
                    "s": offset_ms + int(start * 1000),
                    "e": offset_ms + int(end * 1000),
                    "sp": "",
                    "t": text,
                })
    return drop_repeats(segs)


def looks_like(text: str, language: str | None) -> bool:
    """Cheap check that an existing transcript is plausibly in `language` (by script)."""
    letters = [c for c in text if c.isalpha()]
    if not letters or language not in ("en", "ar"):
        return False
    arabic = sum(1 for c in letters if "\u0600" <= c <= "\u06ff") / len(letters)
    return arabic > 0.5 if language == "ar" else arabic < 0.1


# The same few words four or more times in a row inside one line: a Whisper loop on noise.
LOOPING = re.compile(r"(\b[\w']+(?:[ ,]+[\w']+){0,5})(?:[ ,.]+\1){3,}", re.I)


def drop_repeats(segs: list[dict], run: int = 3) -> list[dict]:
    """Remove runs of the same line repeated back to back.

    On silence or noise Whisper invents one short phrase and repeats it, either
    inside one line or as the same line for every 30 s window; real speech almost
    never repeats a line three times in a row.
    """
    segs = [seg for seg in segs if not LOOPING.search(seg["t"])]
    out: list[dict] = []
    i = 0
    while i < len(segs):
        j = i
        while j < len(segs) and segs[j]["t"].lower() == segs[i]["t"].lower():
            j += 1
        if j - i < run:
            out.extend(segs[i:j])
        i = j
    return out


# ----------------------------------------------------------------------------- main
def sync_one(rec: dict, entry: dict, args) -> None:
    """Download audio and Plaud's transcript for one recording (no local transcription)."""
    need_audio = not (entry.get("audio") and (LIBRARY / entry["audio"]).exists())
    need_plaud = entry.get("source") != "plaud" and not args.force_local
    if not (need_audio or need_plaud):
        return
    detail = api_get("/open/third-party/files/" + urllib.parse.quote(rec["id"], safe=""))
    if need_audio:
        url = detail.get("presigned_url")
        if url:
            AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            rel = f"audio/{entry['base']}{audio_extension(url)}"
            download_audio(url, LIBRARY / rel)
            entry["audio"] = rel
        else:
            entry["audio_note"] = "Plaud has no audio for this recording yet"
    if need_plaud:
        segments = plaud_segments(detail)
        if segments is not None:
            entry["source"] = "plaud"
            write_transcript(entry, segments, plaud_summary(detail))
    entry["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Arabic titles must not crash a Windows console
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--limit", type=int, help="only handle the N newest recordings")
    parser.add_argument("--since", help="only recordings from this date on (YYYY-MM-DD)")
    parser.add_argument("--no-local", action="store_true", help="skip local Whisper; only fetch audio and Plaud's transcripts")
    parser.add_argument("--force-local", action="store_true", help="transcribe everything locally, ignoring Plaud's transcripts")
    parser.add_argument("--refresh", action="store_true", help="re-check Plaud for transcripts of recordings transcribed locally")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Whisper model (default {DEFAULT_MODEL})")
    parser.add_argument("--language", default=os.environ.get("WHISPER_LANGUAGE") or None, help="force one Whisper language code; default detects it per 30 s section")
    parser.add_argument("--recheck-language", action="store_true", help=argparse.SUPPRESS)  # now the default
    parser.add_argument("--no-summary", action="store_true", help="skip local titles and summaries")
    parser.add_argument("--summary-model", default=DEFAULT_SUMMARY_MODEL, help=f"local LLM for titles and summaries (default {DEFAULT_SUMMARY_MODEL})")
    parser.add_argument("--check-auth", action="store_true", help="only check that the saved login works")
    args = parser.parse_args()

    if args.check_auth:
        api_get("/open/third-party/users/current")
        log("Plaud login OK.")
        return 0

    since_ms = None
    if args.since:
        since_ms = int(datetime.strptime(args.since, "%Y-%m-%d").astimezone().timestamp() * 1000)

    log("Listing recordings ...")
    recordings = list_recordings(since_ms)
    recordings.sort(key=lambda r: r.get("start_at") or r.get("created_at") or "", reverse=True)
    if args.limit:
        recordings = recordings[: args.limit]
    log(f"{len(recordings)} recording(s) in scope.")

    manifest = load_manifest()
    failures: list[tuple[str, str]] = []
    interrupted = False

    try:
        # Pass 1: audio + Plaud transcripts (fast).
        for i, rec in enumerate(recordings, 1):
            entry = manifest.setdefault(rec["id"], {"id": rec["id"]})
            entry.setdefault("base", base_name(rec))
            when = parse_utc(rec.get("start_at")) or parse_utc(rec.get("created_at"))
            entry.update({
                "name": rec.get("name") or entry["base"],
                "duration_ms": rec.get("duration") or 0,
                "start_utc": when.isoformat(timespec="seconds") if when else "",
                "start_local": when.astimezone().strftime("%Y-%m-%d %H:%M") if when else "",
            })
            settled = entry.get("source") in ("plaud", "whisper") and not (
                args.force_local or (args.refresh and entry.get("source") == "whisper")
            )
            if settled and entry.get("audio") and (LIBRARY / entry["audio"]).exists():
                continue
            log(f"[{i}/{len(recordings)}] {entry['base']}")
            try:
                sync_one(rec, entry, args)
                entry.pop("error", None)
            except NotLoggedIn:
                raise
            except Exception as err:  # keep going; one bad recording must not stop the rest
                entry["error"] = str(err)
                failures.append((entry["base"], str(err)))
                log(f"    failed: {err}")
            save_manifest(manifest)
        build_viewer(manifest)

        def needs_local(entry: dict) -> bool:
            if args.no_local or not entry.get("audio"):
                return False
            if args.force_local or entry.get("source") not in ("plaud", "whisper"):
                return True
            # Local transcripts made by an older version of the language detection are redone.
            return entry.get("source") == "whisper" and entry.get("whisper_v") != WHISPER_VERSION

        def needs_summary(entry: dict) -> bool:
            return (not args.no_summary
                    and entry.get("source") in ("plaud", "whisper")
                    and not entry.get("has_summary")
                    and (entry.get("chars") or 0) >= MIN_SUMMARY_CHARS)

        def needs_speakers(entry: dict) -> bool:
            import diarize

            return (diarize.enabled() and not args.no_local
                    and entry.get("source") == "whisper" and bool(entry.get("audio"))
                    and diarize.DIARIZE_VERSION not in (entry.get("diarize_v"), entry.get("diarize_failed")))

        def add_speakers(entry: dict, audio, segments: list[dict]) -> None:
            """Fill `sp` in a local transcript's segments ("Speaker 1", ...). Never fatal:
            a transcript without speaker labels is still a transcript."""
            import diarize

            started = time.time()
            try:
                count = diarize.label(segments, diarize.turns(audio)) if segments else 0
                entry.update(diarize_v=diarize.DIARIZE_VERSION, speakers=count)
                entry.pop("diarize_failed", None)
                log(f"    speakers: {count} ({time.time() - started:.0f}s)")
            except Exception as err:
                entry["diarize_failed"] = diarize.DIARIZE_VERSION  # not retried every hour
                log(f"    speaker labels failed: {err}")

        def summarise(entry: dict) -> None:
            import summarize

            try:
                data = read_transcript(entry)
                text = "\n".join(seg["t"] for seg in data["segments"])
                title, summary = summarize.title_and_summary(text, args.summary_model)
                if summary:
                    entry["title"] = title
                    entry["summary_by"] = "local"
                    entry.pop("title_by", None)
                    entry.pop("tags", None)
                    write_transcript(entry, data["segments"], summary)
                    log(f"    title: {title}")
                entry.pop("error", None)
            except Exception as err:
                entry["error"] = f"summary failed: {err}"
                failures.append((entry["base"], str(err)))
                log(f"    summary failed: {err}")
            save_manifest(manifest)

        # Pass 2: titles and summaries for finished transcripts Plaud has not summarised.
        todo = [manifest[r["id"]] for r in recordings
                if needs_summary(manifest[r["id"]]) and not needs_local(manifest[r["id"]])]
        if todo:
            log(f"\nWriting titles and summaries for {len(todo)} recording(s) with {args.summary_model}.")
            log("The first run downloads the model.\n")
        for i, entry in enumerate(todo, 1):
            log(f"[{i}/{len(todo)}] {entry['base']}")
            summarise(entry)
            if i % 5 == 0:
                build_viewer(manifest)
        build_viewer(manifest)

        # Pass 3: local Whisper for whatever has no (current) transcript, each followed by
        # its title and summary (slow).
        todo = [manifest[r["id"]] for r in recordings if needs_local(manifest[r["id"]])]
        if todo:
            hours = sum(e.get("duration_ms") or 0 for e in todo) / 3_600_000
            log(f"\nTranscribing {len(todo)} recording(s) locally ({hours:.1f} h of audio) with {args.model}.")
            log("The first run downloads the model. Ctrl-C is safe: finished work is kept.\n")
        for i, entry in enumerate(todo, 1):
            log(f"[{i}/{len(todo)}] {entry['base']} ({fmt_clock(entry.get('duration_ms') or 0)})")
            started = time.time()
            try:
                audio = load_samples(LIBRARY / entry["audio"])
                if args.language:
                    plan = [args.language]
                else:
                    plan = plan_languages(window_languages(audio, args.model))
                label = language_label(plan)
                keep = False
                if entry.get("source") == "whisper" and not args.force_local:
                    # Recheck: keep an old transcript that is already in the right language.
                    old_text = " ".join(seg["t"] for seg in read_transcript(entry)["segments"])
                    keep = not old_text or ("+" not in label and looks_like(old_text, label))
                if keep:
                    entry.update(language=label, whisper_v=WHISPER_VERSION)
                    log(f"    language {label}: transcript kept")
                else:
                    log(f"    language {label}")
                    segments = whisper_segments(audio, args.model, plan)
                    entry.update(source="whisper", language=label, whisper_v=WHISPER_VERSION,
                                 has_summary=False)
                    for stale in ("title", "title_by", "summary_by", "tags", "diarize_v", "diarize_failed", "speakers"):
                        entry.pop(stale, None)
                    write_transcript(entry, segments, "")  # saved before the slower speaker pass
                    log(f"    done in {time.time() - started:.0f}s")
                    if needs_speakers(entry):
                        add_speakers(entry, audio, segments)
                        write_transcript(entry, segments, "")
                entry.pop("error", None)
                del audio
            except Exception as err:
                entry["error"] = f"local transcription failed: {err}"
                failures.append((entry["base"], str(err)))
                log(f"    failed: {err}")
            save_manifest(manifest)
            if needs_summary(entry) and not entry.get("error"):
                summarise(entry)
            build_viewer(manifest)
        # Speaker labels for local transcripts that have none yet (made before this
        # existed, or kept by the language recheck). The text is not transcribed again.
        todo = [manifest[r["id"]] for r in recordings
                if needs_speakers(manifest[r["id"]]) and not needs_local(manifest[r["id"]])]
        if todo:
            hours = sum(e.get("duration_ms") or 0 for e in todo) / 3_600_000
            log(f"\nAdding speaker labels to {len(todo)} local transcript(s) ({hours:.1f} h of audio).\n")
        for i, entry in enumerate(todo, 1):
            log(f"[{i}/{len(todo)}] {entry['base']} ({fmt_clock(entry.get('duration_ms') or 0)})")
            try:
                data = read_transcript(entry)
                audio = load_samples(LIBRARY / entry["audio"]) if data["segments"] else None
                add_speakers(entry, audio, data["segments"])
                write_transcript(entry, data["segments"], data.get("summary") or "")
                del audio
            except Exception as err:
                failures.append((entry["base"], f"speaker labels failed: {err}"))
                log(f"    failed: {err}")
            save_manifest(manifest)
            if i % 5 == 0:
                build_viewer(manifest)
        # Pass 4: tags, from each recording's title and summary.
        add_title_hints(manifest)
        todo = [] if args.no_summary else [
            manifest[r["id"]] for r in recordings
            if manifest[r["id"]].get("source") in ("plaud", "whisper") and "tags" not in manifest[r["id"]]
        ]
        if todo:
            import summarize

            log(f"\nTagging {len(todo)} recording(s).\n")
        for i, entry in enumerate(todo, 1):
            try:
                data = read_transcript(entry)
                body = data.get("summary") or " ".join(seg["t"] for seg in data["segments"])
                title = entry.get("title") or entry.get("name") or ""
                entry["tags"] = summarize.tags_for(title, body, args.summary_model) if body.strip() else []
                log(f"[{i}/{len(todo)}] {entry['base']}: {', '.join(entry['tags'])}")
            except Exception as err:
                failures.append((entry["base"], f"tags failed: {err}"))
                log(f"[{i}/{len(todo)}] {entry['base']}: tags failed: {err}")
            if i % 10 == 0:
                save_manifest(manifest)
                build_viewer(manifest)
    except KeyboardInterrupt:
        interrupted = True
        log("\nStopped. Run again to continue where it left off.")

    add_title_hints(manifest)
    save_manifest(manifest)
    build_viewer(manifest)

    entries = list(manifest.values())
    by = lambda source: sum(1 for e in entries if e.get("source") == source)
    missing = sum(1 for e in entries if e.get("source") not in ("plaud", "whisper"))
    log(f"\nLibrary: {len(entries)} recordings | Plaud transcripts: {by('plaud')} | "
        f"local transcripts: {by('whisper')} | no transcript yet: {missing}")
    if failures:
        log(f"FAILED: {len(failures)} recording(s):")
        for name, err in failures[:20]:
            log(f"  - {name}: {err}")
    log(f"Browse: {LIBRARY / 'index.html'}")
    return 1 if failures or interrupted else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except NotLoggedIn as err:
        print(f"Not logged in to Plaud: {err}", file=sys.stderr)
        sys.exit(EXIT_AUTH)
