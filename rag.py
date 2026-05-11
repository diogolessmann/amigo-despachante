"""
rag.py — Núcleo do Amigo Despachante
Embeddings PT-BR (gratuito) + ChromaDB + Groq (gratuito)
Suporta: PDF, DOCX, DOC (via antiword)
"""
import os
import subprocess
import pdfplumber
import chromadb
from sentence_transformers import SentenceTransformer
from groq import Groq
import logging

log = logging.getLogger("amigo")

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR   = os.environ.get("DATA_DIR", os.path.dirname(__file__))
CHROMA_DIR = os.path.join(DATA_DIR, "data", "chroma")
PDFS_DIR   = os.path.join(DATA_DIR, "data", "pdfs")
DOCS_DIR   = os.path.join(DATA_DIR, "data", "docs")
GROQ_KEY   = os.environ.get("GROQ_API_KEY", "")
MODEL_CHAT = "llama-3.3-70b-versatile"   # grátis no Groq
MODEL_EMB  = "paraphrase-multilingual-MiniLM-L12-v2"  # PT-BR grátis

# Cache dos clientes (inicializa uma vez)
_embedder = None
_chroma   = None
_collection = None
_groq     = None


def get_embedder():
    global _embedder
    if _embedder is None:
        log.info("Carregando modelo de embeddings...")
        cache = os.path.join(DATA_DIR, ".cache", "sentence_transformers")
        os.makedirs(cache, exist_ok=True)
        _embedder = SentenceTransformer(MODEL_EMB, cache_folder=cache)
    return _embedder


def get_collection():
    global _chroma, _collection
    if _collection is None:
        os.makedirs(CHROMA_DIR, exist_ok=True)
        _chroma     = chromadb.PersistentClient(path=CHROMA_DIR)
        _collection = _chroma.get_or_create_collection(
            name="detran_sc",
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def get_groq():
    global _groq
    if _groq is None:
        _groq = Groq(api_key=GROQ_KEY)
    return _groq


# ── Extração de texto ─────────────────────────────────────────────────────────
def _extract_text_pdf(pdf_path: str) -> list:
    """Extrai texto de PDF página por página. Retorna lista de (page_num, text)."""
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append((page_num, text))
    return pages


def _extract_text_docx(path: str) -> list:
    """Extrai texto de .docx usando python-docx."""
    try:
        from docx import Document
        doc = Document(path)
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        return [(1, text)] if text.strip() else []
    except Exception as e:
        log.warning(f"python-docx falhou em {path}: {e}")
        return []


def _extract_text_doc(path: str) -> list:
    """Extrai texto de .doc binário via antiword (requer antiword no PATH)."""
    try:
        result = subprocess.run(
            ["antiword", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30
        )
        text = result.stdout.strip()
        if not text:
            # Tenta sem encoding (fallback)
            result = subprocess.run(
                ["antiword", path],
                capture_output=True, timeout=30
            )
            text = result.stdout.decode("latin-1", errors="replace").strip()
        return [(1, text)] if text else []
    except FileNotFoundError:
        log.warning("antiword não encontrado no PATH — instale antiword para suporte a .doc")
        return []
    except Exception as e:
        log.warning(f"antiword falhou em {path}: {e}")
        return []


def _text_to_chunks(pages: list, filename: str,
                    chunk_size: int = 800, overlap: int = 100) -> list:
    """Divide texto em chunks com overlap. pages = lista de (page_num, text)."""
    chunks = []
    for page_num, text in pages:
        words = text.split()
        i = 0
        while i < len(words):
            chunk_words = words[i:i + chunk_size]
            chunk_text  = " ".join(chunk_words)
            if len(chunk_text) > 50:
                chunks.append({
                    "text":     chunk_text,
                    "source":   filename,
                    "page":     page_num,
                    "chunk_id": f"{filename}_p{page_num}_c{i}",
                })
            i += chunk_size - overlap
    return chunks


def _save_chunks(chunks: list):
    """Gera embeddings e salva no ChromaDB, ignorando duplicatas."""
    if not chunks:
        return 0
    col = get_collection()
    emb = get_embedder()

    texts      = [c["text"]     for c in chunks]
    ids        = [c["chunk_id"] for c in chunks]
    metadatas  = [{"source": c["source"], "page": c["page"]} for c in chunks]
    embeddings = emb.encode(texts, show_progress_bar=False).tolist()

    existing   = col.get(ids=ids)["ids"]
    new_chunks = [(i, t, m, e) for i, t, m, e in
                  zip(ids, texts, metadatas, embeddings) if i not in existing]
    if new_chunks:
        col.add(
            ids        = [x[0] for x in new_chunks],
            documents  = [x[1] for x in new_chunks],
            metadatas  = [x[2] for x in new_chunks],
            embeddings = [x[3] for x in new_chunks],
        )
    return len(new_chunks)


# ── Ingestão de documentos ────────────────────────────────────────────────────
def ingest_pdf(pdf_path: str, chunk_size: int = 800, overlap: int = 100):
    """Processa um PDF e salva no ChromaDB."""
    filename = os.path.basename(pdf_path)
    log.info(f"Processando PDF: {filename}")

    pages  = _extract_text_pdf(pdf_path)
    chunks = _text_to_chunks(pages, filename, chunk_size, overlap)

    if not chunks:
        log.warning(f"Nenhum texto extraído de {filename}")
        return 0

    saved = _save_chunks(chunks)
    if saved:
        log.info(f"  ✅ {saved} chunks salvos de {filename}")
    else:
        log.info(f"  ⏭️  {filename} já processado")
    return saved


def ingest_doc(doc_path: str, chunk_size: int = 800, overlap: int = 100):
    """Processa um .doc ou .docx e salva no ChromaDB."""
    filename = os.path.basename(doc_path)
    ext      = filename.lower().rsplit(".", 1)[-1]
    log.info(f"Processando DOC: {filename}")

    if ext == "docx":
        pages = _extract_text_docx(doc_path)
    else:
        # Tenta docx primeiro (muitos .doc são docx renomeados)
        pages = _extract_text_docx(doc_path)
        if not pages:
            pages = _extract_text_doc(doc_path)

    chunks = _text_to_chunks(pages, filename, chunk_size, overlap)
    if not chunks:
        log.warning(f"Nenhum texto extraído de {filename}")
        return 0

    saved = _save_chunks(chunks)
    if saved:
        log.info(f"  ✅ {saved} chunks salvos de {filename}")
    else:
        log.info(f"  ⏭️  {filename} já processado")
    return saved


def ingest_all():
    """Processa todos os PDFs em data/pdfs/ e DOCs em data/docs/"""
    total = 0

    os.makedirs(PDFS_DIR, exist_ok=True)
    pdfs = [f for f in os.listdir(PDFS_DIR) if f.lower().endswith(".pdf")]
    for pdf in pdfs:
        total += ingest_pdf(os.path.join(PDFS_DIR, pdf))

    os.makedirs(DOCS_DIR, exist_ok=True)
    docs = [f for f in os.listdir(DOCS_DIR)
            if f.lower().endswith(".doc") or f.lower().endswith(".docx")]
    for doc in docs:
        total += ingest_doc(os.path.join(DOCS_DIR, doc))

    if not pdfs and not docs:
        log.warning(f"Nenhum documento encontrado em {PDFS_DIR} ou {DOCS_DIR}")
    return total


# ── Busca por contexto ────────────────────────────────────────────────────────
def search(query: str, n_results: int = 5) -> list:
    """Busca os chunks mais relevantes para a pergunta."""
    col = get_collection()
    emb = get_embedder()

    total = col.count()
    if total == 0:
        return []

    n = min(n_results, total)
    query_emb = emb.encode([query]).tolist()
    results   = col.query(
        query_embeddings = query_emb,
        n_results        = n,
        include          = ["documents", "metadatas", "distances"],
    )

    chunks = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        relevance = 1 - dist   # cosine → quanto maior, mais relevante
        if relevance > 0.3:    # filtra irrelevantes
            chunks.append({
                "text":      doc,
                "source":    meta.get("source", ""),
                "page":      meta.get("page", ""),
                "relevance": round(relevance, 2),
            })
    return chunks


# ── Geração de resposta ───────────────────────────────────────────────────────
SYSTEM_PROMPT = """Você é o Amigo Despachante SC, um assistente especializado nos
procedimentos do DETRAN/SC para auxiliar funcionários de escritórios de despachantes.

Regras:
- Responda SEMPRE em português brasileiro
- Baseie suas respostas nos documentos fornecidos no contexto
- Se a resposta estiver no contexto, cite de qual documento/página veio
- Se não encontrar a informação, diga claramente e sugira consultar o site do DETRAN/SC
- Seja direto, prático e use listas quando possível
- Quando solicitado, gere requerimentos/checklists formatados
- Nunca invente informações que não estejam no contexto"""


def chat(pergunta: str, historico: list = None) -> dict:
    """
    Gera resposta usando RAG.
    historico: lista de dicts {"role": "user"/"assistant", "content": "..."}
    """
    historico = historico or []

    # 1. Busca contexto relevante
    chunks = search(pergunta, n_results=5)

    # 2. Monta contexto para o LLM
    if chunks:
        contexto = "\n\n---\n\n".join([
            f"[{c['source']} — pág. {c['page']}]\n{c['text']}"
            for c in chunks
        ])
        context_msg = f"\n\nCONTEXTO DOS DOCUMENTOS DETRAN/SC:\n{contexto}\n\n"
    else:
        context_msg = "\n\n[Nenhum documento encontrado para essa consulta. Base de dados pode estar vazia.]\n\n"

    # 3. Monta mensagens
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Adiciona histórico (últimas 6 mensagens para não esgotar contexto)
    for msg in historico[-6:]:
        messages.append(msg)

    # Pergunta atual com contexto injetado
    messages.append({
        "role":    "user",
        "content": context_msg + "PERGUNTA DO FUNCIONÁRIO: " + pergunta,
    })

    # 4. Chama Groq
    groq_client = get_groq()
    response    = groq_client.chat.completions.create(
        model       = MODEL_CHAT,
        messages    = messages,
        temperature = 0.2,    # baixo = mais preciso/factual
        max_tokens  = 1024,
    )

    resposta = response.choices[0].message.content

    # 5. Fontes citadas
    fontes = list({f"{c['source']} (pág. {c['page']})" for c in chunks})

    return {
        "resposta": resposta,
        "fontes":   fontes,
        "chunks":   len(chunks),
    }


def db_stats() -> dict:
    """Retorna estatísticas do banco vetorial."""
    try:
        col = get_collection()
        total = col.count()
        metas = col.get(include=["metadatas"])["metadatas"] if total > 0 else []
        docs  = list({m.get("source", "") for m in metas})
        return {"chunks": total, "documentos": len(docs), "arquivos": docs}
    except Exception as e:
        return {"chunks": 0, "documentos": 0, "arquivos": [], "erro": str(e)}
