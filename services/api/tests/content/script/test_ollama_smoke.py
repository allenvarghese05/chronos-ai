"""Local-only smoke: the real Ollama model writes a valid script (auto-skips without it).

Run with: ``uv run pytest -m ollama tests/content/script/test_ollama_smoke.py -s``.
"""

from __future__ import annotations

import httpx
import pytest
from sqlmodel import Session

from chronos.config import Settings
from chronos.content.script.llm import OllamaScriptGenerator
from chronos.content.script.service import ProviderPlan, generate_script
from chronos.db import create_db_engine
from tests.content.script.test_job import (
    DAY_STR,
    candidates_for,
    seed_day,
)

ENDPOINT = "http://127.0.0.1:11434"
MODEL = "qwen2.5:7b-instruct"


def _model_available() -> bool:
    try:
        tags = httpx.get(f"{ENDPOINT}/api/tags", timeout=2.0).json()
    except (httpx.HTTPError, ValueError):
        return False
    return any(m.get("name") == MODEL for m in tags.get("models", []))


@pytest.mark.ollama
def test_real_model_writes_valid_script(test_settings: Settings, tmp_db_url: str) -> None:
    if not _model_available():
        pytest.skip(f"Ollama at {ENDPOINT} with {MODEL} not available")
    import datetime

    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        now, _ = seed_day(session)
        cands = candidates_for(session, now)
    gen = OllamaScriptGenerator(ENDPOINT, MODEL, httpx.Client())
    script, prov = generate_script(
        cands, datetime.date.fromisoformat(DAY_STR), [ProviderPlan(gen, 3, 60.0)]
    )
    print(f"\nprovider={prov.provider} attempts={prov.attempts} latency_ms={prov.latency_ms} "
          f"tokens_in={prov.tokens_in} tokens_out={prov.tokens_out} digest={prov.digest} "
          f"words={script.total_words} errors={list(prov.errors)}")  # fmt: skip
    for scene in script.scenes:
        print(f"  [{scene.index}] {scene.type:5} {scene.title!r}: {scene.voiceover}")
    print(f"template_units={list(prov.template_units)}")
    assert not prov.used_fallback_template, prov.errors
    assert len(prov.template_units) <= 1, prov.errors  # at most one patched piece
    assert prov.digest.startswith("sha256:")
