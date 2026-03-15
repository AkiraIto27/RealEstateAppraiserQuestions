#!/usr/bin/env python3

import argparse
import gzip
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer
import requests

DEFAULT_EMBED_MODEL = "cl-nagoya/ruri-v3-310m"
DEFAULT_LLM_MODEL = "qwen2.5:7b-instruct"
DEFAULT_LLM_BACKEND = "ollama"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_OPENAI_BASE_URL = "http://localhost:8000/v1"
DEFAULT_MAX_CHARS = 1200
DEFAULT_BATCH_SIZE = 16
DEFAULT_TOP_K = 6
DEFAULT_TEMPERATURE = 0.2
DEFAULT_CONTEXT_CHARS = 12000
DEFAULT_LOG_EVERY = 10
DEFAULT_ERROR_LOG = "rag_errors.txt"
DEFAULT_DIST_DIR = "dist_with_ai"
DEFAULT_INPUT_BUNDLES_DIR = "dist/bundles"
DEFAULT_OUTPUT_BUNDLES_DIR = "dist_with_ai/bundles"
DEFAULT_LLM_RETRIES = 2
DEFAULT_RETRY_BACKOFF = 2.0
DEFAULT_TOPIC_FILTER_MODE = "auto"
DEFAULT_MAX_REGENERATIONS = 2
DEFAULT_VERIFICATION_REPORT_DIR = "dist_with_ai/verification"
DEFAULT_FAILED_IDS_FILE = "dist_with_ai/verification/failed_ids.txt"

ENV_LLM_BACKEND = "RAG_LLM_BACKEND"
ENV_LLM_BASE_URL = "RAG_LLM_BASE_URL"
ENV_LLM_MODEL = "RAG_LLM_MODEL"
ENV_LLM_API_KEY = "RAG_LLM_API_KEY"
ENV_LLM_EXTRA_BODY = "RAG_LLM_EXTRA_BODY"


def list_subdirs(dir_path: Path) -> List[str]:
    if not dir_path.exists():
        return []
    return sorted([p.name for p in dir_path.iterdir() if p.is_dir()])


def pick_latest_date_dir(root: Path) -> str:
    subdirs = list_subdirs(root)
    if not subdirs:
        raise FileNotFoundError(f"No date directories under {root}")
    return subdirs[-1]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def parse_json_object(raw: str, label: str) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def clean_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


def normalize_ollama_base_url(base_url: str) -> str:
    normalized = clean_base_url(base_url)
    if normalized.endswith("/api/chat"):
        normalized = normalized[: -len("/api/chat")]
    return normalized


def normalize_openai_base_url(base_url: str) -> str:
    normalized = clean_base_url(base_url)
    if normalized.endswith("/chat/completions"):
        normalized = normalized[: -len("/chat/completions")]
    if not urlsplit(normalized).path.rstrip("/").endswith("/v1"):
        normalized = normalized + "/v1"
    return normalized


def raise_for_status_with_body(resp: requests.Response) -> None:
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        body = resp.text.strip()
        if len(body) > 500:
            body = body[:500] + "..."
        raise RuntimeError(f"HTTP {resp.status_code}: {body}") from exc


def parse_law_header(text: str, fallback_name: str, fallback_id: str) -> Tuple[str, str]:
    law_name = fallback_name
    law_id = fallback_id
    for line in text.splitlines():
        if line.startswith("# "):
            law_name = line[2:].strip() or law_name
            continue
        if line.lower().startswith("- law_id:"):
            law_id = line.split(":", 1)[1].strip() or law_id
            continue
    return law_name, law_id


def split_sections(text: str) -> List[Tuple[str, str]]:
    sections: List[Tuple[str, str]] = []
    header: Optional[str] = None
    body_lines: List[str] = []

    for line in text.splitlines():
        if line.startswith("## "):
            if header is not None:
                body = "\n".join(body_lines).strip()
                sections.append((header, body))
            header = line[3:].strip()
            body_lines = []
        else:
            if header is not None:
                body_lines.append(line)

    if header is not None:
        body = "\n".join(body_lines).strip()
        sections.append((header, body))

    return sections


def chunk_section(law_name: str, header: str, body: str, max_chars: int) -> List[str]:
    prefix = f"{law_name}\n{header}".strip()
    if not body:
        return [prefix]

    full = f"{prefix}\n{body}".strip()
    if len(full) <= max_chars:
        return [full]

    parts = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if not parts:
        return [full]

    chunks: List[str] = []
    cur: List[str] = []
    cur_len = len(prefix) + 1

    for part in parts:
        part_len = len(part) + 2
        if cur and cur_len + part_len > max_chars:
            chunk = prefix + "\n" + "\n\n".join(cur)
            chunks.append(chunk.strip())
            cur = [part]
            cur_len = len(prefix) + 1 + part_len
        else:
            cur.append(part)
            cur_len += part_len

    if cur:
        chunk = prefix + "\n" + "\n\n".join(cur)
        chunks.append(chunk.strip())

    return chunks


def iter_law_chunks(laws_index_dir: Path, max_chars: int) -> Iterable[Dict[str, str]]:
    law_files = sorted([p for p in laws_index_dir.glob("*.txt") if p.is_file()])
    for law_path in law_files:
        text = read_text(law_path)
        law_name, law_id = parse_law_header(text, law_path.stem, law_path.stem)
        sections = split_sections(text)
        for section_idx, (header, body) in enumerate(sections, start=1):
            chunks = chunk_section(law_name, header, body, max_chars)
            for chunk_idx, chunk in enumerate(chunks, start=1):
                yield {
                    "id": f"{law_id}:{section_idx}:{chunk_idx}",
                    "text": chunk,
                    "law_name": law_name,
                    "law_id": law_id,
                    "section": header,
                    "section_index": str(section_idx),
                    "chunk_index": str(chunk_idx),
                    "source_file": law_path.name,
                }


def build_embed_model(model_name: str, device: str, trust_remote_code: bool) -> SentenceTransformer:
    return SentenceTransformer(model_name, device=device, trust_remote_code=trust_remote_code)


def get_chroma_client(path: Path) -> chromadb.PersistentClient:
    return chromadb.PersistentClient(path=str(path), settings=Settings(anonymized_telemetry=False))


def get_collection(client: chromadb.PersistentClient, name: str, create: bool = True):
    if create:
        return client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})
    return client.get_collection(name=name)


def index_laws(args: argparse.Namespace) -> int:
    laws_index_root = Path(args.laws_index)
    date_dir = args.date or pick_latest_date_dir(laws_index_root)
    laws_index_dir = laws_index_root / date_dir

    if not laws_index_dir.exists():
        raise FileNotFoundError(f"laws_index dir not found: {laws_index_dir}")

    print(f"[index] laws_index_dir={laws_index_dir}")

    chroma_root = Path(args.chroma_dir)
    chroma_root.mkdir(parents=True, exist_ok=True)

    collection_name = args.collection or f"laws_{date_dir}"

    client = get_chroma_client(chroma_root)
    if args.force:
        try:
            client.delete_collection(collection_name)
        except Exception:
            pass

    collection = get_collection(client, collection_name, create=True)

    model = build_embed_model(args.model, args.device, args.trust_remote_code)

    total = 0
    batch_count = 0
    batch_texts: List[str] = []
    batch_metas: List[Dict[str, str]] = []
    batch_ids: List[str] = []

    def flush_batch() -> None:
        nonlocal batch_texts, batch_metas, batch_ids, total, batch_count
        if not batch_texts:
            return
        t0 = time.time()
        embed_texts = [args.doc_prefix + t for t in batch_texts]
        embeddings = model.encode(
            embed_texts,
            batch_size=args.batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        collection.add(
            ids=batch_ids,
            documents=batch_texts,
            embeddings=embeddings.tolist(),
            metadatas=batch_metas,
        )
        total += len(batch_texts)
        batch_count += 1
        ms = int((time.time() - t0) * 1000)
        if args.log_every > 0 and (batch_count == 1 or batch_count % args.log_every == 0):
            print(f"[index] batch={batch_count} total_chunks={total} last_batch={len(batch_texts)} ms={ms}")
        batch_texts, batch_metas, batch_ids = [], [], []

    for chunk in iter_law_chunks(laws_index_dir, args.max_chars):
        batch_texts.append(chunk["text"])
        batch_ids.append(chunk["id"])
        batch_metas.append(
            {
                "law_name": chunk["law_name"],
                "law_id": chunk["law_id"],
                "section": chunk["section"],
                "section_index": chunk["section_index"],
                "chunk_index": chunk["chunk_index"],
                "source_file": chunk["source_file"],
            }
        )
        if len(batch_texts) >= args.batch_size:
            flush_batch()

    flush_batch()

    meta = {
        "date": date_dir,
        "model": args.model,
        "collection": collection_name,
        "max_chars": args.max_chars,
        "doc_prefix": args.doc_prefix,
    }
    meta_path = chroma_root / f"{collection_name}_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    print(f"[index] done total_chunks={total} collection={collection_name} path={chroma_root}")
    return 0


def normalize_choices(q: Dict) -> Optional[List[Dict[str, str]]]:
    c = q.get("choices") or q.get("options") or q.get("choices_list")
    if c is None:
        return None
    if isinstance(c, list):
        out = []
        for idx, item in enumerate(c):
            if isinstance(item, str):
                out.append({"key": idx + 1, "text": item})
            elif isinstance(item, dict):
                out.append({"key": item.get("key", idx + 1), "text": item.get("text", item.get("label", ""))})
            else:
                out.append({"key": idx + 1, "text": str(item)})
        return out
    if isinstance(c, dict):
        out = []
        for k in sorted(c.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x)):
            out.append({"key": int(k) if str(k).isdigit() else k, "text": str(c[k])})
        return out
    return None


def is_explanation_empty(q: Dict) -> bool:
    e = q.get("explanation")
    return e is None or (isinstance(e, str) and e.strip() == "")


def build_query(q: Dict) -> str:
    statement = q.get("statement") or q.get("question") or q.get("stem") or ""
    choices = normalize_choices(q) or []
    lines = [statement.strip()] if statement else []
    for c in choices:
        lines.append(f"{c['key']}. {c['text']}")
    return "\n".join(lines).strip()


def expand_law_name_aliases(law_name: str) -> List[str]:
    aliases = [law_name.strip()]
    match = re.fullmatch(r"(.+?)（略称：(.+?)）", law_name.strip())
    if not match:
        return dedupe_strings(aliases)
    base_name = match.group(1).strip()
    aliases.append(base_name)
    for alias in match.group(2).split(","):
        alias = alias.strip()
        if alias:
            aliases.append(alias)
    return dedupe_strings(aliases)


def load_law_name_map(laws_index_dir: Path) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for law_path in sorted([p for p in laws_index_dir.glob("*.txt") if p.is_file()]):
        text = read_text(law_path)
        law_name, law_id = parse_law_header(text, law_path.stem, law_path.stem)
        for alias in expand_law_name_aliases(law_name):
            mapping[alias] = law_id
    return mapping


def load_law_file_map(laws_index_dir: Path) -> Dict[str, Path]:
    mapping: Dict[str, Path] = {}
    for law_path in sorted([p for p in laws_index_dir.glob("*.txt") if p.is_file()]):
        text = read_text(law_path)
        law_name, _law_id = parse_law_header(text, law_path.stem, law_path.stem)
        for alias in expand_law_name_aliases(law_name):
            mapping[alias] = law_path
    return mapping


def match_law_names(topic: str, law_names: List[str]) -> List[str]:
    if not topic:
        return []
    topic = topic.strip()
    if not topic:
        return []
    if topic in law_names:
        return [topic]
    matches = [name for name in law_names if name in topic or topic in name]
    return matches


def query_chroma(
    collection,
    model: SentenceTransformer,
    query: str,
    top_k: int,
    query_prefix: str,
    law_filter: Optional[str] = None,
) -> Tuple[List[str], List[Dict], List[float]]:
    query_text = query_prefix + query
    embedding = model.encode([query_text], normalize_embeddings=True)

    kwargs = {
        "query_embeddings": embedding.tolist(),
        "n_results": top_k,
        "include": ["documents", "metadatas", "distances"],
    }
    if law_filter:
        kwargs["where"] = {"law_name": law_filter}

    res = collection.query(**kwargs)
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]
    return docs, metas, dists


def merge_ranked_results(results: List[Tuple[List[str], List[Dict], List[float]]], top_k: int):
    merged = []
    seen = set()
    for docs, metas, dists in results:
        for doc, meta, dist in zip(docs, metas, dists):
            key = (
                meta.get("law_id", ""),
                meta.get("section_index", ""),
                meta.get("chunk_index", ""),
                meta.get("source_file", ""),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append((dist, doc, meta))
    merged.sort(key=lambda x: x[0])
    merged = merged[:top_k]
    out_docs = [m[1] for m in merged]
    out_metas = [m[2] for m in merged]
    out_dists = [m[0] for m in merged]
    return out_docs, out_metas, out_dists


def build_context(docs: List[str], metas: List[Dict], max_chars: int) -> str:
    parts: List[str] = []
    total = 0
    for idx, (doc, meta) in enumerate(zip(docs, metas), start=1):
        header = f"[{idx}] law_name={meta.get('law_name','')} section={meta.get('section','')}"
        block = header + "\n" + doc.strip()
        if total + len(block) > max_chars and parts:
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts) if parts else "NO_CONTEXT"


def build_context_from_existing_citations(
    raw_law_citations: Any,
    law_file_map: Dict[str, Path],
    max_chars: int,
) -> str:
    if isinstance(raw_law_citations, str):
        raw_law_citations = [raw_law_citations]
    if not isinstance(raw_law_citations, list):
        return "NO_CONTEXT"

    parts: List[str] = []
    seen = set()
    total = 0
    ref_index = 1001
    for raw_citation in raw_law_citations:
        parsed_citation = parse_citation_text(raw_citation)
        if not parsed_citation:
            continue
        lookup_key = (parsed_citation["law_name"], parsed_citation["article"])
        if lookup_key in seen:
            continue
        seen.add(lookup_key)

        law_path = law_file_map.get(parsed_citation["law_name"])
        if not law_path or not law_path.exists():
            continue
        text = read_text(law_path)
        sections = split_sections(text)
        for section, body in sections:
            if extract_article_from_section(section) != parsed_citation["article"]:
                continue
            block = (
                f"[{ref_index}] law_name={parsed_citation['law_name']} section={section}\n"
                f"{parsed_citation['law_name']}\n"
                f"{section}\n"
                f"{body.strip()}"
            ).strip()
            if total + len(block) > max_chars and parts:
                return "\n\n".join(parts)
            parts.append(block)
            total += len(block)
            ref_index += 1
            break

    return "\n\n".join(parts) if parts else "NO_CONTEXT"


def merge_context_texts(primary: str, secondary: str, max_chars: int) -> str:
    primary = primary.strip()
    secondary = secondary.strip()
    if primary in {"", "NO_CONTEXT"}:
        return secondary if secondary else "NO_CONTEXT"
    if secondary in {"", "NO_CONTEXT"}:
        return primary if primary else "NO_CONTEXT"
    merged = primary
    secondary_compact = compact_text(secondary)
    if secondary_compact and secondary_compact not in compact_text(primary):
        merged = primary + "\n\n" + secondary
    if len(merged) <= max_chars:
        return merged
    return merged[:max_chars].rstrip()


def retrieve_docs(
    collection,
    model: SentenceTransformer,
    query: str,
    topic: str,
    law_names: List[str],
    top_k: int,
    query_prefix: str,
    topic_filter_mode: str,
) -> Tuple[List[str], List[Dict], List[float]]:
    matches = match_law_names(topic, law_names)
    if topic_filter_mode == "off" or not matches:
        return query_chroma(collection, model, query, top_k, query_prefix)

    if topic_filter_mode in {"auto", "strict"}:
        if len(matches) == 1:
            return query_chroma(collection, model, query, top_k, query_prefix, law_filter=matches[0])
        results = []
        per_k = max(1, top_k // len(matches))
        for law in matches:
            results.append(query_chroma(collection, model, query, per_k, query_prefix, law_filter=law))
        return merge_ranked_results(results, top_k)

    results = [query_chroma(collection, model, query, top_k, query_prefix)]
    if len(matches) == 1:
        results.append(query_chroma(collection, model, query, top_k, query_prefix, law_filter=matches[0]))
    else:
        per_k = max(1, top_k // len(matches))
        for law in matches:
            results.append(query_chroma(collection, model, query, per_k, query_prefix, law_filter=law))
    return merge_ranked_results(results, top_k)


class GenerationBackend:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        temperature: Optional[float],
        timeout: int,
        max_tokens: int,
        top_p: float,
        top_k: int,
        presence_penalty: float,
        thinking_mode: str,
        extra_body: Dict[str, Any],
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.top_p = top_p
        self.top_k = top_k
        self.presence_penalty = presence_penalty
        self.thinking_mode = thinking_mode
        self.extra_body = extra_body
        self.session = requests.Session()

    def chat(self, system_prompt: str, user_prompt: str, json_mode: bool) -> str:
        raise NotImplementedError


class OllamaBackend(GenerationBackend):
    def chat(self, system_prompt: str, user_prompt: str, json_mode: bool) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
        }
        options: Dict[str, Any] = {}
        if self.temperature is not None:
            options["temperature"] = self.temperature
        if self.top_p > 0:
            options["top_p"] = self.top_p
        if self.top_k > 0:
            options["top_k"] = self.top_k
        if self.max_tokens > 0:
            options["num_predict"] = self.max_tokens
        if options:
            payload["options"] = options
        if json_mode:
            payload["format"] = "json"
        if self.extra_body:
            payload.update(self.extra_body)

        resp = self.session.post(
            normalize_ollama_base_url(self.base_url) + "/api/chat",
            json=payload,
            timeout=self.timeout,
        )
        raise_for_status_with_body(resp)
        data = resp.json()
        return data.get("message", {}).get("content", "")


class OpenAICompatibleBackend(GenerationBackend):
    def chat(self, system_prompt: str, user_prompt: str, json_mode: bool) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.top_p > 0:
            payload["top_p"] = self.top_p
        if self.max_tokens > 0:
            payload["max_tokens"] = self.max_tokens
        if self.presence_penalty != 0:
            payload["presence_penalty"] = self.presence_penalty
        if self.top_k > 0:
            payload["top_k"] = self.top_k
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        chat_template_kwargs = dict(self.extra_body.get("chat_template_kwargs", {}))
        if self.thinking_mode == "off":
            chat_template_kwargs["enable_thinking"] = False
        elif self.thinking_mode == "on":
            chat_template_kwargs["enable_thinking"] = True
        if chat_template_kwargs:
            payload["chat_template_kwargs"] = chat_template_kwargs

        for key, value in self.extra_body.items():
            if key == "chat_template_kwargs":
                continue
            payload[key] = value

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        resp = self.session.post(
            normalize_openai_base_url(self.base_url) + "/chat/completions",
            json=payload,
            headers=headers,
            timeout=self.timeout,
        )
        raise_for_status_with_body(resp)
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(str(item.get("text", "")))
            return "\n".join(t for t in texts if t).strip()
        return content


def build_generation_backend(args: argparse.Namespace) -> GenerationBackend:
    extra_body = parse_json_object(args.extra_body, "--extra-body")
    backend = args.backend
    if backend == "openai":
        return OpenAICompatibleBackend(
            base_url=args.base_url,
            model=args.llm_model,
            api_key=args.api_key,
            temperature=args.temperature,
            timeout=args.timeout,
            max_tokens=args.max_tokens,
            top_p=args.top_p,
            top_k=args.top_k,
            presence_penalty=args.presence_penalty,
            thinking_mode=args.thinking_mode,
            extra_body=extra_body,
        )
    return OllamaBackend(
        base_url=args.base_url,
        model=args.llm_model,
        api_key="",
        temperature=args.temperature,
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        top_p=args.top_p,
        top_k=args.top_k,
        presence_penalty=0.0,
        thinking_mode="auto",
        extra_body=extra_body,
    )


def parse_json_response(text: str) -> Optional[Dict]:
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def retry_delay_seconds(attempt: int, backoff: float) -> float:
    return max(0.0, backoff * attempt)


def call_backend_with_retries(
    backend: GenerationBackend,
    system_prompt: str,
    user_prompt: str,
    json_mode: bool,
    retries: int,
    retry_backoff: float,
    retry_label: str = "",
) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 2):
        try:
            return backend.chat(system_prompt, user_prompt, json_mode=json_mode)
        except Exception as e:
            last_error = e
            if attempt > retries:
                break
            delay = retry_delay_seconds(attempt, retry_backoff)
            prefix = f"[retry] {retry_label}".strip()
            print(f"{prefix} attempt={attempt} wait={delay:.1f}s err={e}")
            time.sleep(delay)
    raise last_error or RuntimeError("LLM call failed")


def normalize_choice_key(value: Any) -> str:
    text = str(value).strip()
    if text.isdigit():
        return str(int(text))
    return text


def normalize_judgment(value: Any) -> str:
    text = str(value).strip()
    normalized = text.replace("。", "")
    mapping = {
        "正しい": "正しい",
        "正解": "正しい",
        "正答": "正しい",
        "correct": "正しい",
        "true": "正しい",
        "誤り": "誤り",
        "不正解": "誤り",
        "incorrect": "誤り",
        "false": "誤り",
        "誤": "誤り",
    }
    return mapping.get(normalized.lower(), mapping.get(normalized, text))


def dedupe_strings(items: List[Any]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in {"true", "1", "yes", "y", "pass", "passed", "ok"}


FULLWIDTH_DIGIT_TRANS = str.maketrans("０１２３４５６７８９", "0123456789")
CONTEXT_HEADER_RE = re.compile(r"^\[(?P<ref>\d+)\]\s+law_name=(?P<law_name>.*?)\s+section=(?P<section>.*)$")
ARTICLE_TOKEN_RE = re.compile(r"\((第[0-9０-９_]+条)\)")
PARAGRAPH_MARKER_RE = re.compile(r"^（第([0-9０-９]+)項）\s*$")
CITATION_RE = re.compile(
    r"^(?P<law_name>.+?)\s*(?P<article>第[0-9]+条(?:の[0-9]+)?)(?:\s*(?P<paragraph>第[0-9]+項))?(?:\s*(?P<item>第[0-9]+号))?$"
)
KANJI_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
KANJI_UNITS = {"十": 10, "百": 100, "千": 1000}


def normalize_ascii_digits(text: Any) -> str:
    return str(text).translate(FULLWIDTH_DIGIT_TRANS)


def japanese_number_to_int(text: str) -> Optional[int]:
    normalized = normalize_ascii_digits(text).strip()
    if not normalized:
        return None
    if normalized.isdigit():
        return int(normalized)

    total = 0
    current = 0
    seen = False
    for ch in normalized:
        if ch in KANJI_DIGITS:
            current = KANJI_DIGITS[ch]
            seen = True
            continue
        if ch in KANJI_UNITS:
            unit = KANJI_UNITS[ch]
            total += (current or 1) * unit
            current = 0
            seen = True
            continue
        return None
    if not seen:
        return None
    return total + current


def normalize_legal_unit_token(text: Any, suffix: str) -> str:
    normalized = normalize_ascii_digits(text).strip()
    pattern = rf"第(.+?){suffix}(?:の(.+))?$"
    match = re.fullmatch(pattern, normalized)
    if not match:
        return normalized
    primary = japanese_number_to_int(match.group(1))
    secondary_raw = match.group(2) or ""
    secondary = japanese_number_to_int(secondary_raw) if secondary_raw else None
    if primary is None:
        return normalized
    token = f"第{primary}{suffix}"
    if secondary is not None:
        token += f"の{secondary}"
    return token


def normalize_article_token(token: Any) -> str:
    text = normalize_legal_unit_token(token, "条")
    match = re.fullmatch(r"第([0-9_]+)条", text)
    if not match:
        return text
    body = match.group(1)
    if "_" not in body:
        return text
    parts = [part for part in body.split("_") if part]
    if not parts:
        return text
    return "第" + parts[0] + "条" + "".join(f"の{part}" for part in parts[1:])


def normalize_paragraph_token(token: Any) -> str:
    text = normalize_legal_unit_token(token, "項")
    if not text:
        return ""
    if text.startswith("第") and text.endswith("項"):
        return text
    if text.isdigit():
        return f"第{int(text)}項"
    match = re.fullmatch(r"第([0-9]+)項", text)
    if match:
        return f"第{int(match.group(1))}項"
    return text


def normalize_item_token(token: Any) -> str:
    text = normalize_legal_unit_token(token, "号")
    if not text:
        return ""
    if text.startswith("第") and text.endswith("号"):
        return text
    if text.isdigit():
        return f"第{int(text)}号"
    match = re.fullmatch(r"第([0-9]+)号", text)
    if match:
        return f"第{int(match.group(1))}号"
    return text


def normalize_citation_text(text: Any) -> str:
    normalized = normalize_ascii_digits(text).replace("\u3000", " ").strip()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(
        r"第([0-9]+(?:_[0-9]+)+)条",
        lambda m: normalize_article_token(f"第{m.group(1)}条"),
        normalized,
    )
    normalized = re.sub(
        r"第([0-9一二三四五六七八九十百千〇零]+)条(?:の([0-9一二三四五六七八九十百千〇零]+))?",
        lambda m: normalize_article_token(
            f"第{m.group(1)}条" + (f"の{m.group(2)}" if m.group(2) else "")
        ),
        normalized,
    )
    normalized = re.sub(
        r"第([0-9一二三四五六七八九十百千〇零]+)項",
        lambda m: normalize_paragraph_token(f"第{m.group(1)}項"),
        normalized,
    )
    normalized = re.sub(
        r"第([0-9一二三四五六七八九十百千〇零]+)号",
        lambda m: normalize_item_token(f"第{m.group(1)}号"),
        normalized,
    )
    normalized = re.sub(r"(第[0-9]+条(?:の[0-9]+)?)\s+(第[0-9]+項)", r"\1\2", normalized)
    normalized = re.sub(r"(第[0-9]+項)\s+(第[0-9]+号)", r"\1\2", normalized)
    normalized = re.sub(r"(第[0-9]+条(?:の[0-9]+)?)\s+(第[0-9]+号)", r"\1\2", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def parse_citation_text(text: Any) -> Optional[Dict[str, str]]:
    normalized = normalize_citation_text(text)
    match = CITATION_RE.fullmatch(normalized)
    if not match:
        return None
    law_name = match.group("law_name").strip()
    article = normalize_article_token(match.group("article"))
    paragraph = normalize_paragraph_token(match.group("paragraph") or "")
    item = normalize_item_token(match.group("item") or "")
    return {
        "law_name": law_name,
        "article": article,
        "paragraph": paragraph,
        "item": item,
        "citation": format_citation(law_name, article, paragraph, item),
    }


def format_citation(law_name: str, article: str, paragraph: str = "", item: str = "") -> str:
    base = f"{law_name.strip()} {normalize_article_token(article)}".strip()
    paragraph_text = normalize_paragraph_token(paragraph)
    item_text = normalize_item_token(item)
    return (base + paragraph_text + item_text).strip()


def compact_text(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def merge_unique_text(existing: str, addition: str) -> str:
    existing = existing.strip()
    addition = addition.strip()
    if not addition:
        return existing
    if not existing:
        return addition
    compact_existing = compact_text(existing)
    compact_addition = compact_text(addition)
    if compact_addition and compact_addition in compact_existing:
        return existing
    if compact_existing and compact_existing in compact_addition:
        return addition
    return existing + "\n" + addition


def article_sort_key(article: str) -> Tuple[int, ...]:
    normalized = normalize_article_token(article)
    match = re.fullmatch(r"第([0-9]+)条(?:の([0-9]+))?", normalized)
    if not match:
        return (10**9,)
    first = int(match.group(1))
    rest = int(match.group(2)) if match.group(2) else 0
    return first, rest


def paragraph_sort_key(paragraph: str) -> Tuple[int, ...]:
    normalized = normalize_paragraph_token(paragraph)
    match = re.fullmatch(r"第([0-9]+)項", normalized)
    if not match:
        return (10**9,)
    return (int(match.group(1)),)


def extract_article_from_section(section: str) -> str:
    normalized = normalize_ascii_digits(section).strip()
    match = ARTICLE_TOKEN_RE.search(normalized)
    if match:
        return normalize_article_token(match.group(1))
    match = re.search(r"(第[0-9]+条(?:の[0-9]+)?)", normalized)
    if match:
        return normalize_article_token(match.group(1))
    return ""


def split_context_blocks(context: str) -> List[Dict[str, Any]]:
    if not context or context.strip() == "NO_CONTEXT":
        return []

    blocks: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for raw_line in context.splitlines():
        line = raw_line.rstrip("\n")
        match = CONTEXT_HEADER_RE.match(line)
        if match:
            if current:
                blocks.append(current)
            current = {
                "ref": int(match.group("ref")),
                "law_name": match.group("law_name").strip(),
                "section": match.group("section").strip(),
                "lines": [],
            }
            continue
        if current is not None:
            current["lines"].append(line)

    if current:
        blocks.append(current)
    return blocks


def extract_context_citation_catalog(context: str) -> Dict[str, Any]:
    articles: Dict[Tuple[str, str], Dict[str, Any]] = {}
    blocks = split_context_blocks(context)

    for block in blocks:
        law_name = block["law_name"]
        section = block["section"]
        article = extract_article_from_section(section)
        if not article:
            continue

        lines = list(block["lines"])
        if lines and lines[0].strip() == law_name:
            lines = lines[1:]
        if lines and lines[0].strip() == section:
            lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]

        key = (law_name, article)
        entry = articles.setdefault(
            key,
            {
                "law_name": law_name,
                "article": article,
                "source_refs": set(),
                "paragraphs": {},
            },
        )
        entry["source_refs"].add(block["ref"])

        current_paragraph = "第1項"
        current_lines: List[str] = []

        def flush_current() -> None:
            nonlocal current_lines
            text = "\n".join(line.strip() for line in current_lines if line.strip()).strip()
            if not text:
                current_lines = []
                return
            para_key = normalize_paragraph_token(current_paragraph)
            para_entry = entry["paragraphs"].setdefault(
                para_key,
                {"text": "", "source_refs": set(), "sort_ref": block["ref"]},
            )
            para_entry["text"] = merge_unique_text(para_entry["text"], text)
            para_entry["source_refs"].add(block["ref"])
            para_entry["sort_ref"] = min(int(para_entry["sort_ref"]), int(block["ref"]))
            current_lines = []

        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped:
                continue
            paragraph_match = PARAGRAPH_MARKER_RE.match(stripped)
            if paragraph_match:
                flush_current()
                current_paragraph = normalize_paragraph_token(paragraph_match.group(1))
                continue
            current_lines.append(stripped)

        flush_current()

    citations: Dict[str, Dict[str, Any]] = {}
    requires_paragraph: Dict[Tuple[str, str], bool] = {}

    for key, article_data in articles.items():
        law_name = article_data["law_name"]
        article = article_data["article"]
        paragraph_keys = sorted(article_data["paragraphs"].keys(), key=paragraph_sort_key)
        requires_paragraph[key] = len(paragraph_keys) > 1 or any(paragraph != "第1項" for paragraph in paragraph_keys)

        if requires_paragraph[key]:
            for paragraph in paragraph_keys:
                para_entry = article_data["paragraphs"][paragraph]
                citation = format_citation(law_name, article, paragraph)
                citations[citation] = {
                    "citation": citation,
                    "law_name": law_name,
                    "article": article,
                    "paragraph": paragraph,
                    "text": para_entry["text"],
                    "source_refs": sorted(int(ref) for ref in para_entry["source_refs"]),
                    "sort_key": (int(para_entry["sort_ref"]), article_sort_key(article), paragraph_sort_key(paragraph)),
                }
        else:
            merged_text = ""
            sort_ref = 10**9
            source_refs = set()
            for paragraph in paragraph_keys:
                para_entry = article_data["paragraphs"][paragraph]
                merged_text = merge_unique_text(merged_text, para_entry["text"])
                sort_ref = min(sort_ref, int(para_entry["sort_ref"]))
                source_refs.update(int(ref) for ref in para_entry["source_refs"])
            citation = format_citation(law_name, article)
            citations[citation] = {
                "citation": citation,
                "law_name": law_name,
                "article": article,
                "paragraph": "",
                "text": merged_text,
                "source_refs": sorted(source_refs),
                "sort_key": (sort_ref, article_sort_key(article), (0,)),
            }

    return {
        "blocks": blocks,
        "citations": citations,
        "requires_paragraph": requires_paragraph,
        "compact_context": compact_text(context),
    }


def truncate_for_prompt(text: str, max_chars: int = 90) -> str:
    compacted = re.sub(r"\s+", " ", text).strip()
    if len(compacted) <= max_chars:
        return compacted
    return compacted[: max_chars - 3].rstrip() + "..."


def format_context_evidence_for_prompt(context_catalog: Dict[str, Any]) -> str:
    citations = list(context_catalog.get("citations", {}).values())
    if not citations:
        return "- Context から条・項ベースの根拠候補を抽出できませんでした。"

    citations.sort(key=lambda item: item["sort_key"])
    lines: List[str] = []
    for item in citations:
        refs = ",".join(f"[{ref}]" for ref in item["source_refs"])
        lines.append(f"- {refs} {item['citation']}: 「{truncate_for_prompt(item['text'])}」")
    return "\n".join(lines)


def build_explanation_user_prompt(
    obj: Dict,
    context: str,
    context_evidence: str,
    choices: List[Dict[str, Any]],
    expected_answer: Any,
    feedback_issues: Optional[List[str]] = None,
) -> str:
    lines = [
        "Context:",
        context,
        "",
        "Available Evidence Index:",
        context_evidence,
        "",
        "Question:",
        str(obj.get("statement", "")),
        "",
        f"Subject: {obj.get('subject', '')}",
        f"Topic: {obj.get('topic', '')}",
        "",
        "Choices:",
    ]
    for choice in choices:
        lines.append(f"{choice['key']}. {choice['text']}")
    lines.extend(
        [
            "",
            f"Official Answer: {expected_answer}",
            "",
            "Return JSON only with this schema:",
            "{",
            '  "lead_statement": "string",',
            '  "correct_choice": 1,',
            '  "choice_evaluations": [',
            '    {"key": 1, "judgment": "正しい or 誤り", "reason": "string", "citations": ["法令名 第○条第○項"], "supporting_quote": "Contextからの短い原文引用"}',
            "  ],",
            '  "law_citations": ["法令名 第○条第○項"]',
            "}",
            "",
            "Rules:",
            "- correct_choice must equal Official Answer.",
            "- Evaluate every choice exactly once.",
            "- Exactly one choice must be 正しい, and it must be the Official Answer.",
            "- reason must explain the decisive legal difference for that choice with law-based reasoning.",
            "- Mention the law name inside each reason.",
            "- Each choice_evaluation must contain at least one citation.",
            "- citation format must be exactly `法令名 第○条` or `法令名 第○条第○項`.",
            "- If Available Evidence Index shows separate paragraphs for the same article, cite the paragraph level.",
            "- supporting_quote must be a short exact quote copied from Context and supported by one of that choice's citations.",
            "- Preserve the statutory wording strength exactly. Do not strengthen or weaken expressions such as `しなければならない`, `努めなければならない`, `ものとする`, `してはならない`, `できる`, `意見を反映させる`, `意見を聴く`.",
            "- Never invent article or paragraph numbers that are not shown in Context / Available Evidence Index.",
            "- law_citations must equal the deduplicated union of all choice_evaluations[].citations.",
            "- If evidence is insufficient, say so explicitly instead of guessing, but do not fabricate citations.",
        ]
    )
    if feedback_issues:
        lines.extend(
            [
                "",
                "Previous output was rejected for these reasons. Fix all of them:",
                *[f"- {issue}" for issue in feedback_issues],
            ]
        )
    return "\n".join(lines)


def validate_structured_explanation(
    parsed: Dict[str, Any],
    choices: List[Dict[str, Any]],
    expected_answer: Any,
    context_catalog: Dict[str, Any],
) -> Tuple[List[str], Dict[str, Any]]:
    issues: List[str] = []
    expected_answer_key = normalize_choice_key(expected_answer)
    expected_keys = [normalize_choice_key(choice["key"]) for choice in choices]
    expected_key_set = set(expected_keys)
    raw_choice_evals = parsed.get("choice_evaluations") or parsed.get("choice_explanations") or parsed.get("choices")
    if not isinstance(raw_choice_evals, list):
        raw_choice_evals = []
        issues.append("choice_evaluations が配列で返っていません。")

    normalized_evals: List[Dict[str, Any]] = []
    seen_keys = set()
    positive_keys = set()
    aggregated_choice_citations: List[str] = []
    for item in raw_choice_evals:
        if not isinstance(item, dict):
            issues.append("choice_evaluations の要素にオブジェクト以外が含まれています。")
            continue
        raw_key = item.get("key")
        key = normalize_choice_key(raw_key)
        judgment = normalize_judgment(item.get("judgment", ""))
        reason = str(item.get("reason", "")).strip()
        raw_citations = item.get("citations", item.get("law_citations", item.get("citation", [])))
        if isinstance(raw_citations, str):
            raw_citations = [raw_citations]
        if not isinstance(raw_citations, list):
            issues.append(f"{raw_key} の citations が配列で返っていません。")
            raw_citations = []
        supporting_quote = str(item.get("supporting_quote", item.get("quote", ""))).strip()
        if key not in expected_key_set:
            issues.append(f"想定外の choice key が返されました: {raw_key}")
            continue
        if key in seen_keys:
            issues.append(f"choice key が重複しています: {raw_key}")
            continue
        seen_keys.add(key)
        if not reason:
            issues.append(f"{raw_key} の reason が空です。")
        if judgment not in {"正しい", "誤り"}:
            issues.append(f"{raw_key} の judgment が不正です: {item.get('judgment')}")
        if judgment == "正しい":
            positive_keys.add(key)

        normalized_citations: List[str] = []
        seen_citations = set()
        citation_law_names = set()
        for raw_citation in raw_citations:
            parsed_citation = parse_citation_text(raw_citation)
            if not parsed_citation:
                issues.append(f"{raw_key} の citation 形式が不正です: {raw_citation}")
                continue
            article_key = (parsed_citation["law_name"], parsed_citation["article"])
            citation = parsed_citation["citation"]
            if (
                parsed_citation["paragraph"] == "第1項"
                and not context_catalog.get("requires_paragraph", {}).get(article_key)
            ):
                article_level_citation = format_citation(parsed_citation["law_name"], parsed_citation["article"])
                if not context_catalog.get("citations") or article_level_citation in context_catalog["citations"]:
                    citation = article_level_citation
            if citation in seen_citations:
                continue
            seen_citations.add(citation)
            normalized_citations.append(citation)
            citation_law_names.add(parsed_citation["law_name"])

            if context_catalog.get("requires_paragraph", {}).get(article_key) and not parsed_citation["paragraph"]:
                issues.append(f"{raw_key} の citation {citation} は Context 上で項番号が必要です。")
            if context_catalog.get("citations"):
                if citation not in context_catalog["citations"]:
                    issues.append(f"{raw_key} の citation {citation} が Context に見当たりません。")

        if not normalized_citations:
            issues.append(f"{raw_key} の citations が空です。")
        if citation_law_names and not any(law_name in reason for law_name in citation_law_names):
            issues.append(f"{raw_key} の reason に citation で使った法令名が含まれていません。")

        if not supporting_quote:
            issues.append(f"{raw_key} の supporting_quote が空です。")
        elif len(supporting_quote) > 180:
            issues.append(f"{raw_key} の supporting_quote が長すぎます。")
        elif context_catalog.get("citations") and normalized_citations:
            quote_compact = compact_text(supporting_quote)
            if quote_compact not in context_catalog.get("compact_context", ""):
                issues.append(f"{raw_key} の supporting_quote が Context に存在しません。")
            else:
                matched_citation_quote = False
                for citation in normalized_citations:
                    entry = context_catalog["citations"].get(citation)
                    if entry and quote_compact and quote_compact in compact_text(entry.get("text", "")):
                        matched_citation_quote = True
                        break
                if not matched_citation_quote:
                    issues.append(f"{raw_key} の supporting_quote が cited context と対応していません。")

        aggregated_choice_citations.extend(normalized_citations)
        normalized_evals.append(
            {
                "key": key,
                "judgment": judgment,
                "reason": reason,
                "citations": normalized_citations,
                "supporting_quote": supporting_quote,
            }
        )

    missing_keys = [key for key in expected_keys if key not in seen_keys]
    for key in missing_keys:
        issues.append(f"choice key {key} の説明が不足しています。")

    correct_choice = normalize_choice_key(parsed.get("correct_choice", ""))
    if correct_choice != expected_answer_key:
        issues.append(f"correct_choice={parsed.get('correct_choice')} が official answer={expected_answer} と不一致です。")

    if positive_keys != {expected_answer_key}:
        issues.append("正しいと判定された選択肢が official answer と一致していません。")

    lead_statement = str(parsed.get("lead_statement") or parsed.get("overview") or "").strip()
    if not lead_statement:
        issues.append("lead_statement が空です。")

    normalized_evals.sort(key=lambda item: expected_keys.index(item["key"]) if item["key"] in expected_keys else 999)
    raw_law_citations = parsed.get("law_citations", [])
    if isinstance(raw_law_citations, str):
        raw_law_citations = [raw_law_citations]
    if not isinstance(raw_law_citations, list):
        issues.append("law_citations が配列で返っていません。")
        raw_law_citations = []

    law_citations: List[str] = []
    seen_law_citations = set()
    for raw_citation in raw_law_citations:
        parsed_citation = parse_citation_text(raw_citation)
        if not parsed_citation:
            issues.append(f"law_citations に不正な citation 形式があります: {raw_citation}")
            continue
        citation = parsed_citation["citation"]
        article_key = (parsed_citation["law_name"], parsed_citation["article"])
        if (
            parsed_citation["paragraph"] == "第1項"
            and not context_catalog.get("requires_paragraph", {}).get(article_key)
        ):
            article_level_citation = format_citation(parsed_citation["law_name"], parsed_citation["article"])
            if not context_catalog.get("citations") or article_level_citation in context_catalog["citations"]:
                citation = article_level_citation
        if citation in seen_law_citations:
            continue
        seen_law_citations.add(citation)
        law_citations.append(citation)

    expected_law_citations = dedupe_strings(aggregated_choice_citations)
    missing_law_citations = [citation for citation in expected_law_citations if citation not in set(law_citations)]
    extra_law_citations = [citation for citation in law_citations if citation not in set(expected_law_citations)]
    if missing_law_citations:
        issues.append("law_citations に不足があります: " + ", ".join(missing_law_citations))
    if extra_law_citations:
        issues.append("law_citations に choice_evaluations で未使用の citation があります: " + ", ".join(extra_law_citations))

    normalized = {
        "lead_statement": lead_statement,
        "correct_choice": expected_answer_key,
        "choice_evaluations": normalized_evals,
        "law_citations": expected_law_citations,
    }
    return issues, normalized


def render_explanation_text(structured: Dict[str, Any], expected_answer: Any) -> str:
    lead_statement = structured["lead_statement"].strip()
    expected_answer_key = normalize_choice_key(expected_answer)
    parts = [
        lead_statement,
        f"正解は{expected_answer_key}番です。",
        "各選択肢の解説は以下のとおりです。",
    ]
    for item in structured["choice_evaluations"]:
        citation_text = "、".join(item.get("citations", []))
        reason_lines = [f"{item['key']}: **{item['judgment']}**。", item["reason"]]
        if citation_text:
            reason_lines.append(f"根拠条文: {citation_text}")
        if item.get("supporting_quote"):
            reason_lines.append(f"条文引用: 「{item['supporting_quote']}」")
        parts.append("\n".join(reason_lines))
    return "\n\n".join(parts)


def review_generated_output(
    backend: GenerationBackend,
    obj: Dict,
    context: str,
    context_evidence: str,
    choices: List[Dict[str, Any]],
    expected_answer: Any,
    structured: Dict[str, Any],
    rendered_explanation: str,
    retries: int,
    retry_backoff: float,
) -> Dict[str, Any]:
    review_system_prompt = (
        "あなたは不動産鑑定士試験のAI解説の厳格な検証者です。\n"
        "問題文・公式answer・Retrieved Context・各選択肢の判定・生成済み解説の整合性を厳格に検証してください。\n"
        "外部知識は使わず、与えられた Context と Available Evidence Index だけで判断してください。\n"
        "少しでも条番号・項番号・文言の強さ・主体・例外の有無が怪しければ fail にしてください。\n"
        "出力はJSONのみ。キーは pass(boolean) と issues(array of strings) です。"
    )
    lines = [
        "Context:",
        context,
        "",
        "Available Evidence Index:",
        context_evidence,
        "",
        "Question:",
        str(obj.get("statement", "")),
        "",
        "Choices:",
    ]
    for choice in choices:
        lines.append(f"{choice['key']}. {choice['text']}")
    lines.extend(
        [
            "",
            f"Official Answer: {expected_answer}",
            "",
            "Structured Output:",
            json.dumps(structured, ensure_ascii=False, indent=2),
            "",
            "Rendered Explanation:",
            rendered_explanation,
            "",
            "Check the following:",
            "- Official Answer と correct_choice が一致しているか",
            "- 正しい判定が Official Answer の1件だけか",
            "- すべての選択肢 1..5 が説明されているか",
            "- explanation 本文が Official Answer と矛盾していないか",
            "- 各選択肢の説明が judgment と矛盾していないか",
            "- 各 choice_evaluation の citations が Context / Available Evidence Index に実在するか",
            "- 項が切られている条文で paragraph を落としていないか",
            "- supporting_quote が cited context の短い原文引用になっているか",
            "- reason が cited context の文言の強さを変えていないか（例: しなければならない / 努めなければならない / ものとする / してはならない / できる / 意見を反映させる / 意見を聴く）",
            "- 条番号・項番号・主体・要件・例外の有無に取り違えがないか",
            "- law_citations が各 choice の citations の和集合と一致しているか",
        ]
    )
    reply = call_backend_with_retries(
        backend,
        review_system_prompt,
        "\n".join(lines),
        json_mode=True,
        retries=retries,
        retry_backoff=retry_backoff,
        retry_label=f"id={obj.get('id', '')} review",
    )
    parsed = parse_json_response(reply)
    if not parsed:
        return {"pass": False, "issues": ["LLM review output is not valid JSON"]}
    issues = parsed.get("issues", [])
    if not isinstance(issues, list):
        issues = [str(issues)]
    return {
        "pass": normalize_bool(parsed.get("pass")),
        "issues": [str(issue).strip() for issue in issues if str(issue).strip()],
    }


def load_id_set(only_ids: str, ids_file: str) -> Optional[set]:
    ids: set = set()
    if only_ids:
        for part in only_ids.split(","):
            part = part.strip()
            if part:
                ids.add(part)
    if ids_file:
        path = Path(ids_file)
        if not path.exists():
            raise FileNotFoundError(f"ids file not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ids.add(line)
    return ids if ids else None


def load_existing_explanations(bundle_path: Path) -> Dict[str, Dict]:
    if not bundle_path.exists():
        return {}
    out: Dict[str, Dict] = {}
    try:
        with gzip.open(bundle_path, "rt", encoding="utf-8") as fin:
            for line in fin:
                line = line.strip("\n")
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                qid = obj.get("id")
                if not qid:
                    continue
                out[qid] = {
                    "explanation": obj.get("explanation"),
                    "law_citations": obj.get("law_citations"),
                    "updated_at": obj.get("updated_at"),
                }
    except Exception:
        return {}
    return out


def maybe_update_manifest(dist_dir: Path) -> None:
    manifest_path = dist_dir / "manifest.json"
    if not manifest_path.exists():
        return
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            now = datetime.now(timezone.utc).isoformat()
            if "updated_at" in data:
                data["updated_at"] = now
            if "generated_at" in data:
                data["generated_at"] = now
        manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except Exception:
        return


def normalize_existing_law_citations(
    raw_law_citations: Any,
    context_catalog: Dict[str, Any],
) -> Tuple[List[str], List[str]]:
    issues: List[str] = []
    if isinstance(raw_law_citations, str):
        raw_law_citations = [raw_law_citations]
    if raw_law_citations is None:
        raw_law_citations = []
    if not isinstance(raw_law_citations, list):
        return ["law_citations が配列ではありません。"], []

    normalized: List[str] = []
    seen = set()
    for raw_citation in raw_law_citations:
        parsed_citation = parse_citation_text(raw_citation)
        if not parsed_citation:
            issues.append(f"law_citations に不正な citation 形式があります: {raw_citation}")
            continue
        article_key = (parsed_citation["law_name"], parsed_citation["article"])
        citation = parsed_citation["citation"]
        context_lookup_citation = format_citation(
            parsed_citation["law_name"],
            parsed_citation["article"],
            parsed_citation["paragraph"],
        )
        if (
            parsed_citation["paragraph"] == "第1項"
            and not context_catalog.get("requires_paragraph", {}).get(article_key)
        ):
            article_level_citation = format_citation(parsed_citation["law_name"], parsed_citation["article"])
            if not context_catalog.get("citations") or article_level_citation in context_catalog["citations"]:
                citation = article_level_citation
                context_lookup_citation = article_level_citation
        if context_catalog.get("requires_paragraph", {}).get(article_key) and not parsed_citation["paragraph"]:
            issues.append(f"law_citations の citation {citation} は Context 上で項番号が必要です。")
        if context_catalog.get("citations") and context_lookup_citation not in context_catalog["citations"]:
            issues.append(f"law_citations の citation {citation} が Context に見当たりません。")
        if citation in seen:
            continue
        seen.add(citation)
        normalized.append(citation)

    if not normalized:
        issues.append("law_citations が空です。")
    return issues, normalized


def validate_existing_explanation(
    obj: Dict[str, Any],
    choices: List[Dict[str, Any]],
    context_catalog: Dict[str, Any],
) -> Tuple[List[str], Dict[str, Any]]:
    issues: List[str] = []
    explanation = str(obj.get("explanation") or "").strip()
    if not explanation:
        issues.append("explanation が空です。")

    expected_answer_key = normalize_choice_key(obj.get("answer", ""))
    answer_match = re.search(r"正解(?:は|[:：])\s*\**\s*([0-9０-９]+)\s*(?:番)?", explanation)
    if answer_match:
        answer_in_text = normalize_choice_key(normalize_ascii_digits(answer_match.group(1)))
        if answer_in_text != expected_answer_key:
            issues.append(
                f"explanation 本文の正解表示={answer_match.group(1)} が official answer={expected_answer_key} と不一致です。"
            )

    numeric_markers = [
        bool(re.search(rf"(^|\n)\s*(?:[\*\-]\s*)?(?:\*\*)?{re.escape(normalize_choice_key(choice['key']))}\s*[:：.．]", explanation))
        for choice in choices
    ]
    kana_labels = ["イ", "ロ", "ハ", "ニ", "ホ"][: len(choices)]
    kana_markers = [
        bool(re.search(rf"(^|\n)\s*(?:[\*\-]\s*)?(?:\*\*)?{label}\s*[:：.．]", explanation))
        for label in kana_labels
    ]
    if not all(numeric_markers) and not all(kana_markers):
        issues.append("explanation 本文に全選択肢ぶんの説明節が明確に見当たりません。")

    citation_issues, normalized_law_citations = normalize_existing_law_citations(
        obj.get("law_citations"),
        context_catalog,
    )
    issues.extend(citation_issues)

    normalized = {
        "explanation": explanation,
        "law_citations": normalized_law_citations,
    }
    return issues, normalized


def review_existing_output(
    backend: GenerationBackend,
    obj: Dict[str, Any],
    context: str,
    context_evidence: str,
    choices: List[Dict[str, Any]],
    expected_answer: Any,
    explanation: str,
    law_citations: List[str],
    retries: int,
    retry_backoff: float,
) -> Dict[str, Any]:
    review_system_prompt = (
        "あなたは不動産鑑定士試験の既存AI解説を厳格に査読する検証者です。\n"
        "外部知識は使わず、与えられた Context / Available Evidence Index と問題文だけで判断してください。\n"
        "少しでも条番号・項番号・文言の強さ・主体・要件・例外の有無が怪しければ fail にしてください。\n"
        "出力はJSONのみ。キーは pass(boolean) と issues(array of strings) です。"
    )
    lines = [
        "Context:",
        context,
        "",
        "Available Evidence Index:",
        context_evidence,
        "",
        "Question:",
        str(obj.get("statement", "")),
        "",
        "Choices:",
    ]
    for choice in choices:
        lines.append(f"{choice['key']}. {choice['text']}")
    lines.extend(
        [
            "",
            f"Official Answer: {expected_answer}",
            "",
            "Existing law_citations:",
            json.dumps(law_citations, ensure_ascii=False, indent=2),
            "",
            "Existing Explanation:",
            explanation,
            "",
            "Check the following:",
            "- Official Answer と explanation 本文の結論が一致しているか",
            "- 選択肢 1..5 がすべて個別に説明されているか",
            "- explanation の各記述が Context / Available Evidence Index の法令文言と整合するか",
            "- law_citations の条番号・項番号が Context に実在するか",
            "- 項が切られている条文で paragraph を落としていないか",
            "- explanation が法令文言の強さを変えていないか（例: しなければならない / 努めなければならない / ものとする / してはならない / できる / 意見を反映させる / 意見を聴く）",
            "- 説明に対して決定的な根拠条文の欠落がないか",
            "- law_citations と explanation 本文が相互に矛盾していないか",
        ]
    )
    reply = call_backend_with_retries(
        backend,
        review_system_prompt,
        "\n".join(lines),
        json_mode=True,
        retries=retries,
        retry_backoff=retry_backoff,
        retry_label=f"id={obj.get('id', '')} verify",
    )
    parsed = parse_json_response(reply)
    if not parsed:
        return {"pass": False, "issues": ["LLM review output is not valid JSON"]}
    issues = parsed.get("issues", [])
    if not isinstance(issues, list):
        issues = [str(issues)]
    return {
        "pass": normalize_bool(parsed.get("pass")),
        "issues": [str(issue).strip() for issue in issues if str(issue).strip()],
    }


def verify_bundles(args: argparse.Namespace) -> int:
    laws_index_root = Path(args.laws_index)
    date_dir = args.date or pick_latest_date_dir(laws_index_root)
    laws_index_dir = laws_index_root / date_dir
    if not laws_index_dir.exists():
        raise FileNotFoundError(f"laws_index dir not found: {laws_index_dir}")

    bundles_dir = Path(args.bundles)
    verification_report_dir = Path(args.verification_report_dir) if args.verification_report_dir else None
    if verification_report_dir:
        verification_report_dir.mkdir(parents=True, exist_ok=True)

    bundle_files = sorted([p for p in bundles_dir.glob("*.jsonl.gz")])
    if args.bundle:
        target = Path(args.bundle)
        target_base = target.name
        bundle_files = [p for p in bundle_files if p.name == target_base]
        if not bundle_files:
            fallback = bundles_dir / target_base
            if fallback.exists():
                bundle_files = [fallback]
    if not bundle_files:
        raise FileNotFoundError("No bundle files found")

    chroma_root = Path(args.chroma_dir)
    collection_name = args.collection or f"laws_{date_dir}"
    client = get_chroma_client(chroma_root)
    collection = get_collection(client, collection_name, create=False)

    model = build_embed_model(args.model, args.device, args.trust_remote_code)
    backend = build_generation_backend(args)

    law_map = load_law_name_map(laws_index_dir)
    law_file_map = load_law_file_map(laws_index_dir)
    law_names = sorted(law_map.keys())
    only_ids = load_id_set(args.only_ids, args.ids_file)
    failed_ids: List[str] = []

    for bundle_path in bundle_files:
        verification_records: List[Dict[str, Any]] = []
        processed = 0
        verified = 0

        with gzip.open(bundle_path, "rt", encoding="utf-8") as fin:
            for line in fin:
                line = line.strip("\n")
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                processed += 1
                qid = obj.get("id", f"line{processed}")
                if only_ids is not None and qid not in only_ids:
                    verification_records.append({"id": qid, "status": "skipped", "reason": "not_in_ids"})
                    continue
                if args.limit > 0 and verified >= args.limit:
                    verification_records.append({"id": qid, "status": "skipped", "reason": "limit_reached"})
                    continue

                query = build_query(obj)
                topic = (obj.get("topic") or "").strip()
                docs, metas, dists = retrieve_docs(
                    collection,
                    model,
                    query,
                    topic,
                    law_names,
                    args.max_results,
                    args.query_prefix,
                    args.topic_filter_mode,
                )

                retrieval_context = build_context(docs, metas, args.max_context_chars)
                citation_context = build_context_from_existing_citations(
                    obj.get("law_citations"),
                    law_file_map,
                    args.max_context_chars,
                )
                context = merge_context_texts(citation_context, retrieval_context, args.max_context_chars)
                context_catalog = extract_context_citation_catalog(context)
                context_evidence = format_context_evidence_for_prompt(context_catalog)
                choices = normalize_choices(obj) or []
                start = time.time()
                verified += 1

                deterministic_issues, normalized = validate_existing_explanation(obj, choices, context_catalog)
                review_result: Optional[Dict[str, Any]] = None
                issues = list(deterministic_issues)
                if not args.no_llm_review and not issues:
                    review_result = review_existing_output(
                        backend,
                        obj,
                        context,
                        context_evidence,
                        choices,
                        obj.get("answer", ""),
                        normalized["explanation"],
                        normalized["law_citations"],
                        retries=args.retries,
                        retry_backoff=args.retry_backoff,
                    )
                    if not review_result.get("pass"):
                        issues.extend(review_result.get("issues") or ["LLM review rejected the output"])

                status = "passed" if not issues else "failed"
                if status == "failed":
                    failed_ids.append(qid)

                verification_records.append(
                    {
                        "id": qid,
                        "status": status,
                        "expected_answer": normalize_choice_key(obj.get("answer", "")),
                        "law_citations": normalized["law_citations"],
                        "issues": issues if issues else (review_result.get("issues", []) if review_result else []),
                    }
                )
                if args.log_per_question or args.log_all:
                    ms = int((time.time() - start) * 1000)
                    print(f"[verify] id={qid} ms={ms} results={len(docs)} status={status}")

        if verification_report_dir:
            report_name = bundle_path.name.replace(".jsonl.gz", ".verify_existing.jsonl")
            report_path = verification_report_dir / report_name
            report_tmp_path = report_path.with_suffix(report_path.suffix + ".tmp")
            with open(report_tmp_path, "w", encoding="utf-8") as report_file:
                for record in verification_records:
                    report_file.write(json.dumps(record, ensure_ascii=False) + "\n")
            report_tmp_path.replace(report_path)
        print(f"[verify] {bundle_path.name} verified={verified} processed={processed}")

    failed_ids_path = Path(args.failed_ids_file)
    failed_ids_path.parent.mkdir(parents=True, exist_ok=True)
    failed_ids_path.write_text("".join(f"{qid}\n" for qid in dedupe_strings(failed_ids)), encoding="utf-8")
    print(f"[verify] failed_ids={len(dedupe_strings(failed_ids))} path={failed_ids_path}")
    return 0


def explain_bundles(args: argparse.Namespace) -> int:
    laws_index_root = Path(args.laws_index)
    date_dir = args.date or pick_latest_date_dir(laws_index_root)
    laws_index_dir = laws_index_root / date_dir
    if not laws_index_dir.exists():
        raise FileNotFoundError(f"laws_index dir not found: {laws_index_dir}")

    dist_dir = Path(args.dist)
    input_bundles_dir = Path(args.bundles)
    output_bundles_dir = Path(args.output_bundles)
    dist_dir.mkdir(parents=True, exist_ok=True)
    output_bundles_dir.mkdir(parents=True, exist_ok=True)
    verification_report_dir = Path(args.verification_report_dir) if args.verification_report_dir else None
    if verification_report_dir and not args.dry_run:
        verification_report_dir.mkdir(parents=True, exist_ok=True)

    bundle_files = sorted([p for p in input_bundles_dir.glob("*.jsonl.gz")])
    if args.bundle:
        target = Path(args.bundle)
        target_base = target.name
        bundle_files = [p for p in bundle_files if p.name == target_base]
        if not bundle_files:
            fallback_in = input_bundles_dir / target_base
            fallback_out = output_bundles_dir / target_base
            if fallback_in.exists():
                bundle_files = [fallback_in]
            elif fallback_out.exists():
                bundle_files = [fallback_out]

    if not bundle_files:
        raise FileNotFoundError("No bundle files found")

    chroma_root = Path(args.chroma_dir)
    collection_name = args.collection or f"laws_{date_dir}"

    client = get_chroma_client(chroma_root)
    collection = get_collection(client, collection_name, create=False)

    model = build_embed_model(args.model, args.device, args.trust_remote_code)
    backend = build_generation_backend(args)

    law_map = load_law_name_map(laws_index_dir)
    law_names = sorted(law_map.keys())
    only_ids = load_id_set(args.only_ids, args.ids_file)
    error_ids: List[str] = []
    error_log_path = "" if args.no_error_log else args.error_log
    logged_error_ids: set = set()
    log_path: Optional[Path] = None
    if error_log_path:
        log_path = Path(error_log_path)
        if log_path.exists():
            for line in log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    logged_error_ids.add(line)
        else:
            log_path.touch()

    system_prompt = (
        "あなたは不動産鑑定士試験（短答）の解説作成者です。\n"
        "必ず提供された Context / Available Evidence Index（法令抜粋）の記載に基づいて解説してください。Context に無い内容は推測しないでください。\n"
        "条番号・項番号は Context に現れているものだけを使い、存在しない条項を作らないでください。\n"
        "条文の文言の強さを変えてはいけません。特に `しなければならない` と `努めなければならない`、`ものとする` と `努めるものとする`、`してはならない` と `できる`、`意見を反映させる` と `意見を聴く` を取り違えないでください。\n"
        "各選択肢ごとに少なくとも1つの法令 citation と1つの supporting_quote を付け、supporting_quote は Context からの短い原文引用にしてください。\n"
        "Available Evidence Index で同一条に複数項が出ている場合は、必ず項番号まで示してください。\n"
        "law_citations は choice_evaluations で使った citations の重複除去済み和集合と一致させてください。\n"
        "Context が NO_CONTEXT の場合や根拠が足りない場合は、推測せず根拠不足を明記してください。\n"
        "出力はすべて日本語で行ってください。中国語や英語は使わないでください。\n"
        "出力はJSONのみを返してください。\n"
        "各選択肢について、なぜ正しいのか/誤りなのかを法令名付きで説明してください。"
    )

    def log_line(msg: str) -> None:
        if args.log_all:
            print(msg)

    for in_bundle_path in bundle_files:
        out_bundle_path = output_bundles_dir / in_bundle_path.name
        if not in_bundle_path.exists():
            raise FileNotFoundError(f"Bundle not found: {in_bundle_path}")
        tmp_path = out_bundle_path.with_suffix(out_bundle_path.suffix + ".tmp")
        generated = 0
        skipped = 0
        processed = 0
        line_no = 0
        existing = load_existing_explanations(out_bundle_path)
        verification_records: List[Dict[str, Any]] = []

        with gzip.open(in_bundle_path, "rt", encoding="utf-8") as fin, gzip.open(tmp_path, "wt", encoding="utf-8") as fout:
            for line in fin:
                line_no += 1
                line = line.strip("\n")
                if not line:
                    log_line(f"[q] line={line_no} status=empty_line")
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    log_line(f"[q] line={line_no} status=parse_error")
                    fout.write(line + "\n")
                    continue

                processed += 1

                qid = obj.get("id", f"line{processed}")
                prev = existing.get(qid)
                if prev:
                    if prev.get("explanation"):
                        obj["explanation"] = prev.get("explanation")
                    if prev.get("law_citations") is not None:
                        obj["law_citations"] = prev.get("law_citations")
                    if "updated_at" in obj and prev.get("updated_at"):
                        obj["updated_at"] = prev.get("updated_at")

                if only_ids is not None and qid not in only_ids:
                    log_line(f"[q] id={qid} status=skipped reason=not_in_ids")
                    verification_records.append({"id": qid, "status": "skipped", "reason": "not_in_ids"})
                    fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
                    continue

                if args.limit > 0 and generated >= args.limit:
                    log_line(f"[q] id={qid} status=skipped reason=limit_reached")
                    verification_records.append({"id": qid, "status": "skipped", "reason": "limit_reached"})
                    fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
                    continue

                if not args.force and not is_explanation_empty(obj):
                    skipped += 1
                    log_line(f"[q] id={qid} status=skipped reason=explanation_present")
                    verification_records.append({"id": qid, "status": "skipped", "reason": "explanation_present"})
                    fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
                    continue

                query = build_query(obj)
                topic = (obj.get("topic") or "").strip()
                docs, metas, dists = retrieve_docs(
                    collection,
                    model,
                    query,
                    topic,
                    law_names,
                    args.max_results,
                    args.query_prefix,
                    args.topic_filter_mode,
                )

                context = build_context(docs, metas, args.max_context_chars)
                context_catalog = extract_context_citation_catalog(context)
                context_evidence = format_context_evidence_for_prompt(context_catalog)
                choices = normalize_choices(obj) or []
                start = time.time()
                validation_issues: List[str] = []
                attempts_used = 0
                try:
                    review_result: Optional[Dict[str, Any]] = None
                    structured: Optional[Dict[str, Any]] = None
                    rendered_explanation = ""
                    for generation_attempt in range(1, args.max_regenerations + 2):
                        attempts_used = generation_attempt
                        user_prompt = build_explanation_user_prompt(
                            obj,
                            context,
                            context_evidence,
                            choices,
                            obj.get("answer", ""),
                            feedback_issues=validation_issues if validation_issues else None,
                        )
                        reply = call_backend_with_retries(
                            backend,
                            system_prompt,
                            user_prompt,
                            json_mode=True,
                            retries=args.retries,
                            retry_backoff=args.retry_backoff,
                            retry_label=f"id={qid} generation",
                        )
                        parsed = parse_json_response(reply)
                        if not parsed:
                            validation_issues = ["Model output is not valid JSON"]
                            if generation_attempt > args.max_regenerations:
                                raise ValueError(validation_issues[0])
                            continue
                        validation_issues, structured = validate_structured_explanation(
                            parsed,
                            choices,
                            obj.get("answer", ""),
                            context_catalog,
                        )
                        if validation_issues:
                            if generation_attempt > args.max_regenerations:
                                raise ValueError("; ".join(validation_issues))
                            continue

                        rendered_explanation = render_explanation_text(structured, obj.get("answer", ""))
                        if not args.no_llm_review:
                            review_result = review_generated_output(
                                backend,
                                obj,
                                context,
                                context_evidence,
                                choices,
                                obj.get("answer", ""),
                                structured,
                                rendered_explanation,
                                retries=args.retries,
                                retry_backoff=args.retry_backoff,
                            )
                            if not review_result.get("pass"):
                                validation_issues = review_result.get("issues") or ["LLM review rejected the output"]
                                if generation_attempt > args.max_regenerations:
                                    raise ValueError("; ".join(validation_issues))
                                continue
                        break

                    if not structured:
                        raise ValueError("Failed to build structured explanation")

                    obj["explanation"] = rendered_explanation
                    obj["law_citations"] = structured["law_citations"]
                    if "updated_at" in obj:
                        obj["updated_at"] = datetime.now(timezone.utc).isoformat()

                    generated += 1
                    verification_records.append(
                        {
                            "id": qid,
                            "status": "passed",
                            "expected_answer": normalize_choice_key(obj.get("answer", "")),
                            "generated_correct_choice": structured["correct_choice"],
                            "attempts": attempts_used,
                            "law_citations": structured["law_citations"],
                            "issues": review_result.get("issues", []) if review_result else [],
                        }
                    )
                    if args.log_per_question or args.log_all:
                        ms = int((time.time() - start) * 1000)
                        print(f"[q] id={qid} ms={ms} results={len(docs)} attempts={attempts_used}")
                except Exception as e:
                    print(f"[warn] id={qid} err={e}")
                    error_ids.append(qid)
                    verification_records.append(
                        {
                            "id": qid,
                            "status": "failed",
                            "expected_answer": normalize_choice_key(obj.get("answer", "")),
                            "attempts": attempts_used,
                            "issues": validation_issues or [str(e)],
                        }
                    )
                    if log_path and qid not in logged_error_ids:
                        with open(log_path, "a", encoding="utf-8") as f:
                            f.write(qid + "\n")
                        logged_error_ids.add(qid)
                    log_line(f"[q] id={qid} status=error err={e}")

                fout.write(json.dumps(obj, ensure_ascii=False) + "\n")

                if generated and generated % 5 == 0:
                    print(f"[{in_bundle_path.name}] generated={generated} skipped={skipped} processed={processed}")

        if args.dry_run:
            tmp_path.unlink(missing_ok=True)
            print(f"[dry-run] {out_bundle_path} generated={generated} skipped={skipped}")
        else:
            tmp_path.replace(out_bundle_path)
            if verification_report_dir:
                report_name = in_bundle_path.name.replace(".jsonl.gz", ".verification.jsonl")
                report_path = verification_report_dir / report_name
                report_tmp_path = report_path.with_suffix(report_path.suffix + ".tmp")
                with open(report_tmp_path, "w", encoding="utf-8") as report_file:
                    for record in verification_records:
                        report_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                report_tmp_path.replace(report_path)
            print(f"[update] {out_bundle_path} generated={generated} skipped={skipped}")

    if not args.dry_run:
        maybe_update_manifest(dist_dir)

    return 0


def chat_loop(args: argparse.Namespace) -> int:
    laws_index_root = Path(args.laws_index)
    date_dir = args.date or pick_latest_date_dir(laws_index_root)
    laws_index_dir = laws_index_root / date_dir

    chroma_root = Path(args.chroma_dir)
    collection_name = args.collection or f"laws_{date_dir}"

    client = get_chroma_client(chroma_root)
    collection = get_collection(client, collection_name, create=False)

    model = build_embed_model(args.model, args.device, args.trust_remote_code)
    backend = build_generation_backend(args)

    law_map = load_law_name_map(laws_index_dir)
    law_names = sorted(law_map.keys())

    system_prompt = (
        "You are a helpful assistant for Japanese law questions.\n"
        "Use only the provided law excerpts. If the excerpts do not support a claim, say it is not confirmed.\n"
        "If the context is NO_CONTEXT, say evidence not found.\n"
        "Answer in Japanese."
    )

    print("Enter a question (type 'exit' to quit)")
    while True:
        try:
            q = input("> ").strip()
        except EOFError:
            break
        if not q:
            continue
        if q.lower() in {"exit", "quit"}:
            break

        docs, metas, dists = retrieve_docs(
            collection,
            model,
            q,
            args.topic or "",
            law_names,
            args.max_results,
            args.query_prefix,
            args.topic_filter_mode,
        )

        context = build_context(docs, metas, args.max_context_chars)
        user_prompt = f"Context:\n{context}\n\nQuestion:\n{q}\n"

        try:
            reply = call_backend_with_retries(
                backend,
                system_prompt,
                user_prompt,
                json_mode=False,
                retries=args.retries,
                retry_backoff=args.retry_backoff,
                retry_label="chat",
            )
            print(reply)
        except Exception as e:
            print(f"[warn] {e}")

    return 0


def resolve_runtime_args(args: argparse.Namespace) -> argparse.Namespace:
    if not hasattr(args, "backend"):
        return args
    if args.backend not in {"ollama", "openai"}:
        raise ValueError(f"Unsupported backend: {args.backend}")

    if not getattr(args, "llm_model", ""):
        args.llm_model = env_first(ENV_LLM_MODEL, default=DEFAULT_LLM_MODEL)

    if not getattr(args, "base_url", ""):
        if args.backend == "openai":
            args.base_url = env_first(ENV_LLM_BASE_URL, "OPENAI_BASE_URL", default=DEFAULT_OPENAI_BASE_URL)
        else:
            args.base_url = env_first(ENV_LLM_BASE_URL, "OLLAMA_HOST", default=DEFAULT_OLLAMA_BASE_URL)

    if getattr(args, "ollama_url", "") and args.backend == "ollama":
        args.base_url = args.ollama_url

    if args.backend == "openai":
        args.base_url = normalize_openai_base_url(args.base_url)
        if not getattr(args, "api_key", ""):
            args.api_key = env_first(ENV_LLM_API_KEY, "OPENAI_API_KEY", default="")
    else:
        args.base_url = normalize_ollama_base_url(args.base_url)
        args.api_key = ""

    if not getattr(args, "extra_body", ""):
        args.extra_body = env_first(ENV_LLM_EXTRA_BODY, default="")

    return args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local RAG pipeline using Chroma + local/remote LLM backends")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--date", default="", help="laws_index date folder (YYYY-MM-DD)")
        p.add_argument("--laws-index", default="laws_index", help="laws_index root dir")
        p.add_argument("--chroma-dir", default=".chroma", help="Chroma persistent dir")
        p.add_argument("--collection", default="", help="Chroma collection name")
        p.add_argument("--model", default=DEFAULT_EMBED_MODEL, help="embedding model name")
        p.add_argument("--device", default="cpu", help="embedding device (cpu, mps, cuda)")
        p.add_argument("--trust-remote-code", action="store_true", help="trust_remote_code for embedding model")
        p.add_argument("--query-prefix", default="query: ", help="prefix for query embedding")
        p.add_argument("--doc-prefix", default="passage: ", help="prefix for document embedding")

    p_index = sub.add_parser("index", help="build Chroma index from laws_index")
    add_common(p_index)
    p_index.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="max chars per chunk")
    p_index.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="embedding batch size")
    p_index.add_argument("--force", action="store_true", help="delete existing collection first")
    p_index.add_argument("--log-every", type=int, default=DEFAULT_LOG_EVERY, help="log every N batches")
    p_index.set_defaults(func=index_laws)

    p_explain = sub.add_parser("explain", help="fill explanations into output bundles")
    add_common(p_explain)
    p_explain.add_argument("--dist", default=DEFAULT_DIST_DIR, help="output dist dir")
    p_explain.add_argument("--bundles", default=DEFAULT_INPUT_BUNDLES_DIR, help="input bundles dir")
    p_explain.add_argument("--output-bundles", default=DEFAULT_OUTPUT_BUNDLES_DIR, help="output bundles dir")
    p_explain.add_argument("--bundle", default="", help="single bundle file to process")
    p_explain.add_argument("--limit", type=int, default=0, help="limit generated per bundle")
    p_explain.add_argument("--force", action="store_true", help="regenerate even if explanation exists")
    p_explain.add_argument("--dry-run", action="store_true", help="do not write output")
    p_explain.add_argument("--max-results", type=int, default=DEFAULT_TOP_K, help="top-k retrieval")
    p_explain.add_argument("--max-context-chars", type=int, default=DEFAULT_CONTEXT_CHARS, help="max context chars")
    p_explain.add_argument("--backend", choices=["ollama", "openai"], default=env_first(ENV_LLM_BACKEND, default=DEFAULT_LLM_BACKEND), help="generation backend")
    p_explain.add_argument("--llm-model", default=env_first(ENV_LLM_MODEL, default=DEFAULT_LLM_MODEL), help="generation model name")
    p_explain.add_argument("--base-url", default="", help="LLM base URL (Ollama host or OpenAI-compatible /v1 root)")
    p_explain.add_argument("--ollama-url", default="", help="deprecated alias for --base-url when backend=ollama")
    p_explain.add_argument("--api-key", default="", help="API key for backend=openai (falls back to env)")
    p_explain.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE, help="LLM temperature")
    p_explain.add_argument("--top-p", type=float, default=1.0, help="sampling top-p")
    p_explain.add_argument("--top-k", type=int, default=0, help="sampling top-k (backend-dependent)")
    p_explain.add_argument("--presence-penalty", type=float, default=0.0, help="presence penalty (OpenAI-compatible)")
    p_explain.add_argument("--max-tokens", type=int, default=0, help="max output tokens (0 disables explicit cap)")
    p_explain.add_argument("--thinking-mode", choices=["auto", "on", "off"], default="auto", help="Qwen/vLLM thinking mode hint")
    p_explain.add_argument("--extra-body", default="", help="extra JSON object merged into request body")
    p_explain.add_argument("--timeout", type=int, default=120, help="LLM request timeout seconds")
    p_explain.add_argument("--retries", type=int, default=DEFAULT_LLM_RETRIES, help="retry count for LLM calls")
    p_explain.add_argument("--retry-backoff", type=float, default=DEFAULT_RETRY_BACKOFF, help="linear retry backoff seconds")
    p_explain.add_argument("--log-per-question", action="store_true", help="log per question timing")
    p_explain.add_argument("--log-all", action="store_true", help="log every line (including skips/errors)")
    p_explain.add_argument("--only-ids", default="", help="comma-separated question IDs to process")
    p_explain.add_argument("--ids-file", default="", help="file with question IDs to process (one per line)")
    p_explain.add_argument("--error-log", default=DEFAULT_ERROR_LOG, help="append failed question IDs to this file")
    p_explain.add_argument("--no-error-log", action="store_true", help="disable error log file")
    p_explain.add_argument("--topic-filter-mode", choices=["auto", "hybrid", "strict", "off"], default=DEFAULT_TOPIC_FILTER_MODE, help="how topic-based law filtering is applied")
    p_explain.add_argument("--max-regenerations", type=int, default=DEFAULT_MAX_REGENERATIONS, help="regenerate content when validation/review fails")
    p_explain.add_argument("--verification-report-dir", default=DEFAULT_VERIFICATION_REPORT_DIR, help="write per-question verification report JSONL here")
    p_explain.add_argument("--no-llm-review", action="store_true", help="skip the post-generation LLM consistency review")
    p_explain.set_defaults(func=explain_bundles)

    p_verify = sub.add_parser("verify", help="strictly review existing explanations and write failed_ids")
    add_common(p_verify)
    p_verify.add_argument("--bundles", default=DEFAULT_OUTPUT_BUNDLES_DIR, help="bundles dir that already contains explanations")
    p_verify.add_argument("--bundle", default="", help="single bundle file to process")
    p_verify.add_argument("--limit", type=int, default=0, help="limit verified per bundle")
    p_verify.add_argument("--max-results", type=int, default=DEFAULT_TOP_K, help="top-k retrieval")
    p_verify.add_argument("--max-context-chars", type=int, default=DEFAULT_CONTEXT_CHARS, help="max context chars")
    p_verify.add_argument("--backend", choices=["ollama", "openai"], default=env_first(ENV_LLM_BACKEND, default=DEFAULT_LLM_BACKEND), help="review backend")
    p_verify.add_argument("--llm-model", default=env_first(ENV_LLM_MODEL, default=DEFAULT_LLM_MODEL), help="review model name")
    p_verify.add_argument("--base-url", default="", help="LLM base URL (Ollama host or OpenAI-compatible /v1 root)")
    p_verify.add_argument("--ollama-url", default="", help="deprecated alias for --base-url when backend=ollama")
    p_verify.add_argument("--api-key", default="", help="API key for backend=openai (falls back to env)")
    p_verify.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE, help="LLM temperature")
    p_verify.add_argument("--top-p", type=float, default=1.0, help="sampling top-p")
    p_verify.add_argument("--top-k", type=int, default=0, help="sampling top-k (backend-dependent)")
    p_verify.add_argument("--presence-penalty", type=float, default=0.0, help="presence penalty (OpenAI-compatible)")
    p_verify.add_argument("--max-tokens", type=int, default=0, help="max output tokens (0 disables explicit cap)")
    p_verify.add_argument("--thinking-mode", choices=["auto", "on", "off"], default="auto", help="Qwen/vLLM thinking mode hint")
    p_verify.add_argument("--extra-body", default="", help="extra JSON object merged into request body")
    p_verify.add_argument("--timeout", type=int, default=120, help="LLM request timeout seconds")
    p_verify.add_argument("--retries", type=int, default=DEFAULT_LLM_RETRIES, help="retry count for LLM calls")
    p_verify.add_argument("--retry-backoff", type=float, default=DEFAULT_RETRY_BACKOFF, help="linear retry backoff seconds")
    p_verify.add_argument("--log-per-question", action="store_true", help="log per question timing")
    p_verify.add_argument("--log-all", action="store_true", help="log every line (including skips/errors)")
    p_verify.add_argument("--only-ids", default="", help="comma-separated question IDs to process")
    p_verify.add_argument("--ids-file", default="", help="file with question IDs to process (one per line)")
    p_verify.add_argument("--topic-filter-mode", choices=["auto", "hybrid", "strict", "off"], default=DEFAULT_TOPIC_FILTER_MODE, help="how topic-based law filtering is applied")
    p_verify.add_argument("--verification-report-dir", default=DEFAULT_VERIFICATION_REPORT_DIR, help="write per-question verification report JSONL here")
    p_verify.add_argument("--failed-ids-file", default=DEFAULT_FAILED_IDS_FILE, help="write failed question IDs here")
    p_verify.add_argument("--no-llm-review", action="store_true", help="skip the LLM review and run deterministic checks only")
    p_verify.set_defaults(func=verify_bundles)

    p_chat = sub.add_parser("chat", help="interactive RAG chat")
    add_common(p_chat)
    p_chat.add_argument("--topic", default="", help="optional topic to filter law name")
    p_chat.add_argument("--max-results", type=int, default=DEFAULT_TOP_K, help="top-k retrieval")
    p_chat.add_argument("--max-context-chars", type=int, default=DEFAULT_CONTEXT_CHARS, help="max context chars")
    p_chat.add_argument("--backend", choices=["ollama", "openai"], default=env_first(ENV_LLM_BACKEND, default=DEFAULT_LLM_BACKEND), help="generation backend")
    p_chat.add_argument("--llm-model", default=env_first(ENV_LLM_MODEL, default=DEFAULT_LLM_MODEL), help="generation model name")
    p_chat.add_argument("--base-url", default="", help="LLM base URL (Ollama host or OpenAI-compatible /v1 root)")
    p_chat.add_argument("--ollama-url", default="", help="deprecated alias for --base-url when backend=ollama")
    p_chat.add_argument("--api-key", default="", help="API key for backend=openai (falls back to env)")
    p_chat.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE, help="LLM temperature")
    p_chat.add_argument("--top-p", type=float, default=1.0, help="sampling top-p")
    p_chat.add_argument("--top-k", type=int, default=0, help="sampling top-k (backend-dependent)")
    p_chat.add_argument("--presence-penalty", type=float, default=0.0, help="presence penalty (OpenAI-compatible)")
    p_chat.add_argument("--max-tokens", type=int, default=0, help="max output tokens (0 disables explicit cap)")
    p_chat.add_argument("--thinking-mode", choices=["auto", "on", "off"], default="auto", help="Qwen/vLLM thinking mode hint")
    p_chat.add_argument("--extra-body", default="", help="extra JSON object merged into request body")
    p_chat.add_argument("--timeout", type=int, default=120, help="LLM request timeout seconds")
    p_chat.add_argument("--retries", type=int, default=DEFAULT_LLM_RETRIES, help="retry count for LLM calls")
    p_chat.add_argument("--retry-backoff", type=float, default=DEFAULT_RETRY_BACKOFF, help="linear retry backoff seconds")
    p_chat.add_argument("--topic-filter-mode", choices=["auto", "hybrid", "strict", "off"], default=DEFAULT_TOPIC_FILTER_MODE, help="how topic-based law filtering is applied")
    p_chat.set_defaults(func=chat_loop)

    return parser


def main() -> int:
    parser = build_parser()
    args = resolve_runtime_args(parser.parse_args())
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
