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
from typing import Dict, Iterable, List, Optional, Tuple

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer
import requests

DEFAULT_EMBED_MODEL = "cl-nagoya/ruri-v3-310m"
DEFAULT_LLM_MODEL = "qwen2.5:7b-instruct"
DEFAULT_MAX_CHARS = 1200
DEFAULT_BATCH_SIZE = 16
DEFAULT_TOP_K = 6
DEFAULT_TEMPERATURE = 0.2
DEFAULT_CONTEXT_CHARS = 12000
DEFAULT_LOG_EVERY = 10
DEFAULT_ERROR_LOG = "rag_errors.txt"


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


def load_law_name_map(laws_index_dir: Path) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for law_path in sorted([p for p in laws_index_dir.glob("*.txt") if p.is_file()]):
        text = read_text(law_path)
        law_name, law_id = parse_law_header(text, law_path.stem, law_path.stem)
        mapping[law_name] = law_id
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
    for docs, metas, dists in results:
        for doc, meta, dist in zip(docs, metas, dists):
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


def ollama_chat(
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: Optional[float],
    json_mode: bool,
    timeout: int,
) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
    }
    if temperature is not None:
        payload["options"] = {"temperature": temperature}
    if json_mode:
        payload["format"] = "json"

    resp = requests.post(base_url.rstrip("/") + "/api/chat", json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return data.get("message", {}).get("content", "")


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


def explain_bundles(args: argparse.Namespace) -> int:
    laws_index_root = Path(args.laws_index)
    date_dir = args.date or pick_latest_date_dir(laws_index_root)
    laws_index_dir = laws_index_root / date_dir
    if not laws_index_dir.exists():
        raise FileNotFoundError(f"laws_index dir not found: {laws_index_dir}")

    dist_dir = Path(args.dist)
    bundles_dir = Path(args.bundles)

    bundle_files = sorted([p for p in bundles_dir.glob("*.jsonl.gz")])
    if args.bundle:
        target = args.bundle
        target_path = bundles_dir / target if not os.path.isabs(target) else Path(target)
        bundle_files = [p for p in bundle_files if p == target_path]

    if not bundle_files:
        raise FileNotFoundError("No bundle files found")

    chroma_root = Path(args.chroma_dir)
    collection_name = args.collection or f"laws_{date_dir}"

    client = get_chroma_client(chroma_root)
    collection = get_collection(client, collection_name, create=False)

    model = build_embed_model(args.model, args.device, args.trust_remote_code)

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
        "必ず提供されたContext（法令抜粋）の記載に基づいて解説してください。Contextに無い内容は推測しないでください。\n"
        "ContextがNO_CONTEXTの場合は、根拠が見つからない旨を説明し、law_citationsは空配列にしてください。\n"
        "出力は日本語で、JSONのみを返してください。キーは explanation（string）と law_citations（array of strings）です。\n"
        "explanationの構成は次の順にしてください：\n"
        "1) 正解はX番。 2) 理由（法令根拠ベース） 3) 各選択肢が正誤になる理由（1〜5または提示されたchoicesに対応）\n"
        "law_citations には、参照した条文を「法令名 第○条（必要なら項・号）」の形式で列挙してください（複数可）。\n"
        "根拠条文を特定できない場合は、その旨をexplanationに明記し、law_citationsは空配列にしてください。"
    )

    for bundle_path in bundle_files:
        tmp_path = bundle_path.with_suffix(bundle_path.suffix + ".tmp")
        generated = 0
        skipped = 0
        processed = 0

        with gzip.open(bundle_path, "rt", encoding="utf-8") as fin, gzip.open(tmp_path, "wt", encoding="utf-8") as fout:
            for line in fin:
                line = line.strip("\n")
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    fout.write(line + "\n")
                    continue

                processed += 1

                qid = obj.get("id", f"line{processed}")

                if only_ids is not None and qid not in only_ids:
                    fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
                    continue

                if args.limit > 0 and generated >= args.limit:
                    fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
                    continue

                if not args.force and not is_explanation_empty(obj):
                    skipped += 1
                    fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
                    continue

                query = build_query(obj)
                topic = (obj.get("topic") or "").strip()

                matches = match_law_names(topic, law_names)
                if len(matches) == 1:
                    docs, metas, dists = query_chroma(
                        collection,
                        model,
                        query,
                        args.max_results,
                        args.query_prefix,
                        law_filter=matches[0],
                    )
                elif len(matches) > 1:
                    results = []
                    per_k = max(1, args.max_results // len(matches))
                    for law in matches:
                        results.append(
                            query_chroma(collection, model, query, per_k, args.query_prefix, law_filter=law)
                        )
                    docs, metas, dists = merge_ranked_results(results, args.max_results)
                else:
                    docs, metas, dists = query_chroma(collection, model, query, args.max_results, args.query_prefix)

                context = build_context(docs, metas, args.max_context_chars)

                user_prompt = (
                    "Context:\n"
                    f"{context}\n\n"
                    "Question:\n"
                    f"{obj.get('statement','')}\n\n"
                    "Choices:\n"
                )

                choices = normalize_choices(obj) or []
                for c in choices:
                    user_prompt += f"{c['key']}. {c['text']}\n"

                user_prompt += f"\nAnswer: {obj.get('answer','')}\n"
                user_prompt += "\nWrite the explanation and citations as JSON."

                start = time.time()
                try:
                    reply = ollama_chat(
                        args.ollama_url,
                        args.llm_model,
                        system_prompt,
                        user_prompt,
                        args.temperature,
                        json_mode=True,
                        timeout=args.timeout,
                    )
                    parsed = parse_json_response(reply)
                    if not parsed:
                        raise ValueError("Model output is not valid JSON")
                    explanation = parsed.get("explanation", "")
                    law_citations = parsed.get("law_citations", [])
                    if not isinstance(law_citations, list):
                        law_citations = []
                    if not isinstance(explanation, str):
                        explanation = str(explanation)

                    obj["explanation"] = explanation
                    obj["law_citations"] = law_citations
                    if "updated_at" in obj:
                        obj["updated_at"] = datetime.now(timezone.utc).isoformat()

                    generated += 1
                    if args.log_per_question:
                        ms = int((time.time() - start) * 1000)
                        print(f"[q] id={qid} ms={ms} results={len(docs)}")
                except Exception as e:
                    print(f"[warn] id={qid} err={e}")
                    error_ids.append(qid)
                    if log_path and qid not in logged_error_ids:
                        with open(log_path, "a", encoding="utf-8") as f:
                            f.write(qid + "\n")
                        logged_error_ids.add(qid)

                fout.write(json.dumps(obj, ensure_ascii=False) + "\n")

                if generated and generated % 5 == 0:
                    print(f"[{bundle_path.name}] generated={generated} skipped={skipped} processed={processed}")

        if args.dry_run:
            tmp_path.unlink(missing_ok=True)
            print(f"[dry-run] {bundle_path} generated={generated} skipped={skipped}")
        else:
            tmp_path.replace(bundle_path)
            print(f"[update] {bundle_path} generated={generated} skipped={skipped}")

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

        matches = match_law_names(args.topic or "", law_names)
        if len(matches) == 1:
            docs, metas, dists = query_chroma(
                collection,
                model,
                q,
                args.max_results,
                args.query_prefix,
                law_filter=matches[0],
            )
        elif len(matches) > 1:
            results = []
            per_k = max(1, args.max_results // len(matches))
            for law in matches:
                results.append(query_chroma(collection, model, q, per_k, args.query_prefix, law_filter=law))
            docs, metas, dists = merge_ranked_results(results, args.max_results)
        else:
            docs, metas, dists = query_chroma(collection, model, q, args.max_results, args.query_prefix)

        context = build_context(docs, metas, args.max_context_chars)
        user_prompt = f"Context:\n{context}\n\nQuestion:\n{q}\n"

        try:
            reply = ollama_chat(
                args.ollama_url,
                args.llm_model,
                system_prompt,
                user_prompt,
                args.temperature,
                json_mode=False,
                timeout=args.timeout,
            )
            print(reply)
        except Exception as e:
            print(f"[warn] {e}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local RAG pipeline using Chroma + Ollama")
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

    p_explain = sub.add_parser("explain", help="fill explanations in dist/bundles")
    add_common(p_explain)
    p_explain.add_argument("--dist", default="dist", help="dist dir")
    p_explain.add_argument("--bundles", default="dist/bundles", help="bundles dir")
    p_explain.add_argument("--bundle", default="", help="single bundle file to process")
    p_explain.add_argument("--limit", type=int, default=0, help="limit generated per bundle")
    p_explain.add_argument("--force", action="store_true", help="regenerate even if explanation exists")
    p_explain.add_argument("--dry-run", action="store_true", help="do not write output")
    p_explain.add_argument("--max-results", type=int, default=DEFAULT_TOP_K, help="top-k retrieval")
    p_explain.add_argument("--max-context-chars", type=int, default=DEFAULT_CONTEXT_CHARS, help="max context chars")
    p_explain.add_argument("--llm-model", default=DEFAULT_LLM_MODEL, help="ollama model name")
    p_explain.add_argument("--ollama-url", default="http://localhost:11434", help="ollama base url")
    p_explain.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE, help="LLM temperature")
    p_explain.add_argument("--timeout", type=int, default=120, help="LLM request timeout seconds")
    p_explain.add_argument("--log-per-question", action="store_true", help="log per question timing")
    p_explain.add_argument("--only-ids", default="", help="comma-separated question IDs to process")
    p_explain.add_argument("--ids-file", default="", help="file with question IDs to process (one per line)")
    p_explain.add_argument("--error-log", default=DEFAULT_ERROR_LOG, help="append failed question IDs to this file")
    p_explain.add_argument("--no-error-log", action="store_true", help="disable error log file")
    p_explain.set_defaults(func=explain_bundles)

    p_chat = sub.add_parser("chat", help="interactive RAG chat")
    add_common(p_chat)
    p_chat.add_argument("--topic", default="", help="optional topic to filter law name")
    p_chat.add_argument("--max-results", type=int, default=DEFAULT_TOP_K, help="top-k retrieval")
    p_chat.add_argument("--max-context-chars", type=int, default=DEFAULT_CONTEXT_CHARS, help="max context chars")
    p_chat.add_argument("--llm-model", default=DEFAULT_LLM_MODEL, help="ollama model name")
    p_chat.add_argument("--ollama-url", default="http://localhost:11434", help="ollama base url")
    p_chat.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE, help="LLM temperature")
    p_chat.add_argument("--timeout", type=int, default=120, help="LLM request timeout seconds")
    p_chat.set_defaults(func=chat_loop)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
