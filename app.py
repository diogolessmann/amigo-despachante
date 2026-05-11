"""
app.py — Amigo Despachante SC
Assistente de IA gratuito para despachantes de Santa Catarina
"""
import logging
import os
import threading
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("amigo")

app = Flask(__name__)

from rag import chat, db_stats, ingest_all, ingest_pdf, ingest_doc, get_collection, PDFS_DIR, DOCS_DIR
import os


# ── Páginas ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    stats = db_stats()
    return render_template("index.html", stats=stats)


# ── API Chat ──────────────────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data      = request.get_json(silent=True) or {}
    pergunta  = (data.get("pergunta") or "").strip()
    historico = data.get("historico") or []

    if not pergunta:
        return jsonify({"erro": "Pergunta vazia"}), 400

    try:
        resultado = chat(pergunta, historico)
        return jsonify(resultado)
    except Exception as e:
        log.error(f"Erro no chat: {e}")
        return jsonify({"erro": str(e)}), 500


# ── API Admin ─────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    return jsonify(db_stats())


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """Processa todos os PDFs da pasta data/pdfs/"""
    def _run():
        try:
            ingest_all()
        except Exception as e:
            log.error(f"Erro na ingestão: {e}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "iniciado"})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Faz upload de um PDF, DOC ou DOCX e processa."""
    if "pdf" not in request.files:
        return jsonify({"erro": "Nenhum arquivo enviado"}), 400

    f   = request.files["pdf"]
    ext = f.filename.lower().rsplit(".", 1)[-1] if "." in f.filename else ""

    if ext == "pdf":
        os.makedirs(PDFS_DIR, exist_ok=True)
        path    = os.path.join(PDFS_DIR, f.filename)
        f.save(path)
        ingestor = ingest_pdf
    elif ext in ("doc", "docx"):
        os.makedirs(DOCS_DIR, exist_ok=True)
        path    = os.path.join(DOCS_DIR, f.filename)
        f.save(path)
        ingestor = ingest_doc
    else:
        return jsonify({"erro": "Aceitos: PDF, DOC, DOCX"}), 400

    def _run():
        try:
            ingestor(path)
        except Exception as e:
            log.error(f"Erro ao processar {f.filename}: {e}")
    threading.Thread(target=_run, daemon=True).start()

    return jsonify({"status": "processando", "arquivo": f.filename})


@app.route("/health")
def health():
    s = db_stats()
    return jsonify({"status": "ok", "app": "Amigo Despachante SC", **s})


# ── Startup ───────────────────────────────────────────────────────────────────

def _startup():
    try:
        s = db_stats()
        log.info(f"DB: {s['chunks']} chunks | {s['documentos']} documentos")
        if s["chunks"] == 0:
            log.info("Base vazia — processe os PDFs em /api/ingest")
    except Exception as e:
        log.error(f"Startup error: {e}")

with app.app_context():
    _startup()

if __name__ == "__main__":
    app.run(debug=True, port=5002)
