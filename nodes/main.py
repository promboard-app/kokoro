"""promboard.kokoro — Kokoro-82M(hexgrad) 프리셋 목소리 노드 코드.

노드 하나(kokoro)가 글을 읽어 목소리 WAV(24kHz 모노)를 낸다. 목소리는 공식 프리셋 41개 중 하나를 고르고(복제 없음),
언어는 목소리 이름 첫 글자로 정해진다(공식 KPipeline lang_code: a 미국 영어 · b 영국 영어 · e 스페인어 · f 프랑스어 ·
h 힌디어 · i 이탈리아어 · p 브라질 포르투갈어). 일본어(j) · 중국어(z) 목소리는 받아 쓰는 글자 → 발음 사전 패키지
(pyopenjtalk · unidic · jieba)가 소스 코드로만 나와 PromBoard 설치 방식(--only-binary)으로 깔 수 없어 뺐다.
설정: voice · speed(공식 KPipeline.__call__ 인자).

발음(G2P): 영어는 misaki(spaCy en_core_web_sm 품사 + 사전, 사전에 없는 낱말은 espeak-ng), 다른 언어는 espeak-ng.
espeak-ng 는 espeakng-loader 휠에 든 것을 쓴다(따로 설치하지 않는다).
모델(manifest requires.models, 허깅페이스에서 받는다)
  hexgrad/Kokoro-82M     kokoro-v1_0.pth(82M, 327MB) · config.json · voices/*.pt 41개 (Apache-2.0)
  spacy/en_core_web_sm   영어 품사 모델 3.7.1 (MIT) — misaki 가 실행 중에 받으려는 것을 받은 폴더로 돌린다
노드 코드 약속(NODES · ctx · 입출력 모양)은 PromBoard docs/Scrum4/02_스키마-v0.md §6.
"""
import os
import time

os.environ.setdefault("HF_HUB_OFFLINE", "1")  # 받은 파일만 쓴다(실행 환경은 인터넷이 막혀 있다)

REPO = "hexgrad/Kokoro-82M"
SPACY_NAME = "en_core_web_sm"
SPACY_FILE = "spacy/en_core_web_sm/config.cfg"
SAMPLE_RATE = 24000  # 공식 예제 sf.write(..., 24000)
MAX_TEXT_CHARS = 20000  # 한 번에 읽을 글 상한(대략 20분) — 실행 한 번 시간 예산 안에 끝나게

_STATE = {"model": None, "device": None, "pipelines": {}}


def _patch_spacy(ctx):
    """misaki 영어 G2P 는 spacy 품사 모델을 패키지로 찾고 없으면 받으려 한다 — 받은 폴더에서 읽게 돌린다."""
    import spacy
    import spacy.util

    if getattr(spacy, "_promboard_patched", False):
        return
    folder = os.path.dirname(ctx.model_path(SPACY_FILE))
    is_package, load = spacy.util.is_package, spacy.load
    spacy.util.is_package = lambda name: name == SPACY_NAME or is_package(name)
    spacy.load = lambda name, *a, **k: load(folder if name == SPACY_NAME else name, *a, **k)
    spacy._promboard_patched = True


def _pipeline(ctx, lang):
    """KModel 하나를 올려 두고, 언어마다 KPipeline(G2P)을 하나씩 만든다."""
    import torch
    from kokoro import KModel, KPipeline

    device = ctx.device
    if _STATE["model"] is None or _STATE["device"] != device:
        ctx.progress(0.05, "load-model")
        start = time.monotonic()
        config = ctx.model_path(f"{REPO}/config.json")
        weights = ctx.model_path(f"{REPO}/kokoro-v1_0.pth")
        _STATE["model"] = KModel(repo_id=REPO, config=config, model=weights).to(device).eval()
        _STATE["device"] = device
        _STATE["pipelines"] = {}
        ctx.log(f"Kokoro loaded on {device} in {time.monotonic() - start:.1f}s")
    if lang not in _STATE["pipelines"]:
        if lang in "ab":
            _patch_spacy(ctx)
        ctx.progress(0.1, "load-model")
        _STATE["pipelines"][lang] = KPipeline(lang_code=lang, repo_id=REPO, model=_STATE["model"])
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()
    return _STATE["pipelines"][lang]


def kokoro(ctx, inputs, settings):
    import numpy as np
    import soundfile as sf
    import torch

    text = (inputs.get("text") or "").strip()
    if not text:
        raise ValueError("읽을 글을 이어 주세요.")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"글이 너무 깁니다({len(text)}자). 한 번에 {MAX_TEXT_CHARS}자까지 읽습니다 — 나눠서 이어 주세요.")
    voice = settings["voice"]
    pipeline = _pipeline(ctx, voice[0])
    ctx.check_cancel()
    voice_path = ctx.model_path(f"{REPO}/voices/{voice}.pt")

    # 공식 KPipeline 은 줄바꿈으로 나눈 조각을 차례로 낸다(영어는 다시 510 음소 안쪽으로 나눈다). 조각마다 취소 · 진행률을 본다.
    total = max(1, len(text))
    done = 0
    pieces = []
    start = time.monotonic()
    try:
        for result in pipeline(text, voice=voice_path, speed=float(settings["speed"])):
            ctx.check_cancel()
            if result.audio is not None:
                pieces.append(result.audio.detach().cpu().numpy().astype(np.float32))
            done += len(result.graphemes or "")
            ctx.progress(0.15 + 0.8 * min(1.0, done / total), "generate")
    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache()
        raise RuntimeError("그래픽카드 메모리가 모자랍니다. 다른 AI 작업 · 프로그램을 닫고 다시 해 주세요.") from None
    took = time.monotonic() - start
    if not pieces:
        raise ValueError("읽을 수 있는 글자가 없습니다. 고른 목소리의 언어로 쓴 글인지 확인해 주세요.")
    audio = np.concatenate(pieces)
    out = ctx.out_path("kokoro.wav")
    sf.write(out, audio, SAMPLE_RATE, subtype="PCM_16")
    peak = torch.cuda.max_memory_reserved() / 2**20 if ctx.device == "cuda" else 0
    ctx.log(f"voice {voice}, {len(text)} chars → {audio.size / SAMPLE_RATE:.1f}s audio in {took:.1f}s, peak VRAM reserved {peak:.0f} MiB")
    ctx.progress(1.0, "done")
    return {"audio": out}


NODES = {"kokoro": kokoro}
