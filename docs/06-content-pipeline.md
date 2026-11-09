# Content Pipeline — sources, normalisation, ranking, LLM script

## 1. Sources (`services/api/config/sources.yaml`)

```yaml
version: 1
http:
  user_agent: "Chronos-AI/0.1 (+https://github.com/allenvarghese05/chronos-ai; personal morning brief)"
  timeout_sec: 15
  max_bytes: 2_000_000
  max_redirects: 3
sources:
  - key: hf_daily_papers
    kind: hf_api
    url: https://huggingface.co/api/daily_papers
    category: ai
    quota: 2
    weight: 1.3
  - key: arxiv_cs_ai_cl_lg
    kind: arxiv_api
    url: "http://export.arxiv.org/api/query?search_query=cat:cs.AI+OR+cat:cs.CL+OR+cat:cs.LG&sortBy=submittedDate&sortOrder=descending&max_results=25"
    category: ai
    quota: 1
    weight: 0.9
  - key: hn_front
    kind: hn_rss
    url: https://news.ycombinator.com/rss
    category: tech
    quota: 1
    weight: 1.0
    min_points: 150          # resolved via Algolia item API when available; else accept
  - key: bbc_world
    kind: rss
    url: http://feeds.bbci.co.uk/news/world/rss.xml
    category: world
    quota: 2
    weight: 1.0
  - key: cbs_top
    kind: rss
    url: https://www.cbsnews.com/latest/rss/main
    category: world
    quota: 1
    weight: 0.8
  - key: ie_india
    kind: rss
    url: https://indianexpress.com/section/india/feed/
    category: india
    quota: 1
    weight: 1.0
  - key: ie_tech
    kind: rss
    url: https://indianexpress.com/section/technology/feed/
    category: tech
    quota: 1
    weight: 0.7
  - key: ndtv_top
    kind: rss
    url: https://feeds.feedburner.com/ndtvnews-top-stories
    category: india
    quota: 1
    weight: 0.9
selection:
  total_stories: 6           # fits ~60 s with ~9 s per story + cover/outro
  category_min: { ai: 2, world: 1, india: 1 }
  freshness_hours: 20        # older items ignored, except to reach a category_min or fill an empty category (D36)
  cross_source_boost: 0.25   # same story in ≥ 2 sources
```

Fetcher behaviour:

- `rss` → `feedparser` with the configured UA, `ETag`/`Last-Modified` stored on `sources`; HTML in
  descriptions stripped with `selectolax`; `media:content`/`enclosure` → `image_url`.
- `hf_api` → JSON; fields `paper.id`, `paper.title`, `paper.summary`, `paper.upvotes`,
  `thumbnail`; `external_id = paper.id`.
- `arxiv_api` → Atom via `feedparser`; `external_id` = arXiv id without version; respect the
  3-second politeness rule (single request, so trivially met); categories into `extra_json`.
- `hn_rss` → RSS gives title + `comments` link; fetch
  `https://hn.algolia.com/api/v1/items/<id>` for points (best-effort, 5 s timeout); `url` is the
  outbound link, `external_id` the HN id.
- Every fetch is wrapped by the SSRF-guarded HTTP client (`docs/09-security.md`); failures
  increment `consecutive_failures` and never abort the job.

## 2. Normalisation and dedupe (`content/normalise.py`)

1. Canonical URL: lowercase scheme/host, strip `utm_*`, `fbclid`, `ref`, fragments, trailing `/`;
   AMP → canonical when pattern known.
2. `url_hash = sha256(canonical)`; `title_simhash` (64-bit) over normalised title tokens.
3. Duplicate if same `url_hash`, or (per **D35**) both titles have ≥ 4 content tokens with word-set
   Jaccard ≥ 0.6, published within 36 h → keep the higher-weight source's row and record the other
   as `mention` in `brief_items`. (`title_simhash` is still stored; it is not the deciding rule.)
4. `summary` truncated to 1 000 chars; `published_at` parsed with `dateutil`, UTC; missing →
   `fetched_at`.

## 3. Ranking (`content/rank.py`)

```
score(item) = source.weight
            × recency(item)                 # 1.0 at 0 h → 0.3 at freshness_hours, linear
            × popularity(item)              # HF upvotes / HN points, log-scaled to [1, 1.6]; RSS = 1
            × (1 + cross_source_boost × (n_sources − 1))
```
Selection: fill `category_min` first (best item per category), then by score until
`total_stories`, honouring per-source `quota`. Output: ordered list `[(item, category)]` with AI
items first, then tech, world, India (narrative order is fixed; the LLM may not reorder).

## 4. The script schema (`content/script/schema.py`)

```python
class Scene(BaseModel, extra="forbid"):
    index: int
    type: Literal["cover", "story", "outro"]
    title: str = Field(max_length=60)          # on-screen headline
    kicker: str | None = Field(None, max_length=24)  # e.g. "AI DROP", "WORLD", "INDIA"
    bullets: list[str] = Field(default_factory=list, max_length=3)   # each ≤ 48 chars
    voiceover: str = Field(min_length=10, max_length=320)            # ~9–12 s at 165 wpm
    item_id: int | None = None                  # FK to items for story scenes
    source_name: str | None = None
    source_url: HttpUrl | None = None
    visual: Literal["image", "broll", "gradient"] = "image"
    broll_query: str | None = Field(None, max_length=40)             # for Pexels

class ReelScript(BaseModel, extra="forbid"):
    local_date: date
    greeting: str = Field(max_length=80)
    scenes: list[Scene] = Field(min_length=4, max_length=9)
    total_words: int                            # validator: sum of voiceover words, 130–185
    tone: Literal["calm", "energetic"] = "calm"

    @model_validator(mode="after")
    def check_structure(self): ...              # first=cover, last=outro, indices contiguous,
                                                # every story has item_id present in the allowed set,
                                                # no URL/title not in the provided items (no hallucinated stories)
```

Hard guarantees enforced *outside* the LLM:
- Story facts come only from provided items; validator rejects any `item_id` not in the candidate
  list and any `source_url` that differs from the item's canonical URL.
- Word budget: 130–185 words total → 50–68 s at Kokoro's ~165 wpm.
- Profanity/unsafe filter on `voiceover`/`title` (simple deny list + the LLM instruction);
  violation → retry.

## 5. Prompting (`content/script/prompts/reel_script.v1.md`)

System:
> You write the voiceover for a 60-second vertical morning news reel for one person. Calm, precise,
> no hype, no filler, no "in today's news". Short sentences. Numbers spelled as digits. Never
> invent facts; use only the stories given. Each story: one headline ≤ 60 chars, up to 3 bullets
> ≤ 48 chars, a voiceover of 25–45 words. Return JSON only, matching the schema.

User (templated): date, weekday, greeting hint, the ordered candidate stories (title, summary,
source, url, item_id, category, popularity), the word budget, and the JSON schema. Few-shot: one
complete example reel (`prompts/example_reel.json`).

LLM call (`content/script/llm.py`):
- Ollama `POST /api/chat` with `format: <json schema>` (structured outputs), `options:
  {temperature: 0.4, num_ctx: 8192}`, `keep_alive: "2m"`; model pinned by name+digest in settings
  (`CHRONOS_OLLAMA_MODEL=qwen2.5:7b-instruct`, `CHRONOS_OLLAMA_DIGEST=sha256:...`); `chronos doctor`
  verifies the digest.
- Retries: 3 attempts; on validation error the error text is appended to the next attempt.
- Fallback providers behind the same `ScriptGenerator` protocol: `OpenAIScriptGenerator`
  (`response_format=json_schema`), `AnthropicScriptGenerator` (tool-use with the schema). Selected
  by `CHRONOS_LLM_PROVIDER` and only if its API key is present.
- Last resort `fallback.py`: deterministic template — headline = item title (truncated), bullets =
  first sentences of summary, voiceover = "From {source}: {title}. {first sentence}." This always
  validates.

After validation the brief row is `ready`, `brief_items` is written, and the `assets` job is
enqueued.

## 6. Quality checks (tests, see `12-testing-and-quality.md`)

- Fixture feeds (recorded responses under `tests/fixtures/feeds/`) for each source → golden
  normalised items.
- Property test: dedupe is idempotent and order-independent.
- Validator tests: hallucinated URL rejected; word budget enforced; scene order enforced.
- LLM tests use a `FakeScriptGenerator`; a `@pytest.mark.ollama` smoke test runs only when Ollama
  is reachable (skipped in CI).
