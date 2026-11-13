"""Title and short summary for a transcript, written by a local LLM (MLX)."""
from __future__ import annotations

import json
import os
import re

MAX_CHARS = 14_000  # transcript text given to the model; longer ones are sampled
MAX_CHARS_CPU = 8_000  # llama.cpp on a CPU reads the prompt slowly
_loaded: dict = {}
_CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")

PROMPT = """You label voice recordings so their owner can find them later.
Read the transcript below. It may be imperfect speech-to-text.

Reply with JSON only, in this shape:
{{"title": "...", "summary": "..."}}

- title: at most 10 words, specific to what is discussed. No date, no quotes.
- summary: 3 to 6 short bullet points, each on its own line starting with "- ".
  Cover who is speaking if clear, the topics, and any decisions, numbers or to-dos.
- Write both in the main language of the transcript.
- If the transcript is noise or has no real content, use the title "No clear speech"
  and say so in one bullet.

Transcript{note}:
\"\"\"
{text}
\"\"\"
"""


def _is_gguf(model_name: str) -> bool:
    return model_name.lower().endswith(".gguf")


def _sample(text: str, limit: int = MAX_CHARS) -> tuple[str, str]:
    """Fit a long transcript in the prompt: start, evenly spaced middle parts, end."""
    if len(text) <= limit:
        return text, ""
    head, tail, parts = limit * 5 // 14, limit * 3 // 14, 4
    size = (limit - head - tail) // parts
    middle = text[head:-tail]
    step = len(middle) // parts
    chunks = [text[:head]] + [middle[i * step: i * step + size] for i in range(parts)] + [text[-tail:]]
    return "\n[...]\n".join(chunks), " (excerpts from a long recording)"


def _load(model_name: str):
    if model_name not in _loaded:
        _loaded.clear()
        if _is_gguf(model_name):
            # llama.cpp: "<hf repo>:<file>.gguf" (downloaded once) or a path to a .gguf file.
            from llama_cpp import Llama

            options = dict(n_ctx=int(os.environ.get("SUMMARY_CONTEXT", "12288")), n_gpu_layers=-1, verbose=False)
            repo, _, filename = model_name.rpartition(":")
            if os.path.exists(model_name) or not repo or "/" not in repo:
                _loaded[model_name] = Llama(model_path=model_name, **options)
            else:
                # Not Llama.from_pretrained: it lists the repo online on every load, which
                # fails with HF_HUB_OFFLINE=1. This uses the cached file when there is one.
                from huggingface_hub import hf_hub_download

                _loaded[model_name] = Llama(model_path=hf_hub_download(repo, filename), **options)
        else:
            from mlx_lm import load

            _loaded[model_name] = load(model_name)
    return _loaded[model_name]


def _ask(model_name: str, content: str, max_tokens: int) -> str:
    messages = [{"role": "user", "content": content}]
    if _is_gguf(model_name):
        reply = _load(model_name).create_chat_completion(messages=messages, max_tokens=max_tokens, temperature=0.2)
        return reply["choices"][0]["message"]["content"] or ""
    from mlx_lm import generate

    model, tokenizer = _load(model_name)
    prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
    return generate(model, tokenizer, prompt=prompt, max_tokens=max_tokens, verbose=False)


def title_and_summary(text: str, model_name: str) -> tuple[str, str]:
    excerpt, note = _sample(text.strip(), MAX_CHARS_CPU if _is_gguf(model_name) else MAX_CHARS)
    content = PROMPT.format(text=excerpt, note=note)
    title, summary = _parse(_ask(model_name, content, 500))
    if _CJK.search(title + summary) and not _CJK.search(excerpt):
        # The model drifted into Chinese on a transcript that has none: ask again, in English.
        title, summary = _parse(_ask(model_name, content + "\nWrite the title and summary in English.", 500))
        if _CJK.search(title + summary):
            raise ValueError("model answered in the wrong language")
    return title, summary


COMMON_TAGS = (
    "work, meeting, phone call, family, friends, politics, news, religion, finance, health, "
    "legal, immigration, housing, travel, car, shopping, food, tech, ai, programming, "
    "education, music, podcast, sports, history, science, business"
)

TAG_PROMPT = """Give 3 to 5 tags for this voice recording so its owner can filter a large archive.
Reply with JSON only: {{"tags": ["...", "..."]}}

- Lowercase English, one or two words each, no "#".
- Use tags from this list when they fit: {common}.
- Add at most two more specific tags (a person, place, product or subject that is named).
- Tag what the recording is about, not the fact that it is a recording.

Title: {title}
{body}
"""


def tags_for(title: str, body: str, model_name: str) -> list[str]:
    """3-5 short English tags from a recording's title and summary (or transcript start)."""
    reply = _ask(model_name, TAG_PROMPT.format(common=COMMON_TAGS, title=title, body=body[:3000]), 80)
    match = re.search(r"\[(.*?)\]", reply.translate({0x201C: '"', 0x201D: '"'}), re.S)
    if not match:
        raise ValueError("model reply had no tag list")
    tags: list[str] = []
    for raw in re.findall(r'"([^"]+)"', match.group(1)):
        tag = " ".join(raw.lower().replace("#", "").replace("_", " ").split())
        if tag and len(tag) <= 30 and not _CJK.search(tag) and tag not in tags:
            tags.append(tag)
    return tags[:5]


def _parse(reply: str) -> tuple[str, str]:
    reply = reply.translate({0x201C: '"', 0x201D: '"', 0x201E: '"'})  # curly quotes break JSON
    match = re.search(r"\{.*\}", reply, re.S)
    data = None
    if match:
        try:
            data = json.loads(match.group(0), strict=False)
        except ValueError:
            pass
    if not isinstance(data, dict):
        # Not valid JSON (unescaped quote, cut off): pull the two fields out by hand.
        title = re.search(r'"title"\s*:\s*"(.*?)"\s*,\s*"summary"', reply, re.S)
        summary = re.search(r'"summary"\s*:\s*\[?\s*"(.*?)"?\s*\]?\s*\}?\s*$', reply, re.S)
        if not (title and summary):
            raise ValueError("model reply was not usable")
        data = {"title": title.group(1), "summary": summary.group(1).replace("\\n", "\n")}
    title = " ".join(str(data.get("title") or "").split()).strip('"')
    summary = data.get("summary") or ""
    if isinstance(summary, list):
        summary = "\n".join(f"- {str(item).lstrip('- ').strip()}" for item in summary)
    return title[:120], str(summary).strip()
