"""
app.py — Despachante Lessmann · Sistema Gerenciador + IA
"""
import logging, os, threading
from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, abort, flash, session)
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S")
log = logging.getLogger("despachante")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "lessmann-desp-2026-secret")

from rag import chat, db_stats, ingest_all, ingest_pdf, ingest_doc, PDFS_DIR, DOCS_DIR
from db  import (init_db, stats_dashboard, SERVICOS, SERVICOS_GRUPOS, FINAIS_PLACA, MESES,
                 STATUS_LABELS, criar_os, get_os, listar_os, atualizar_os,
                 atualizar_os_status, criar_cliente, get_cliente, atualizar_cliente,
                 buscar_cliente_cpf, criar_veiculo,
                 buscar_veiculo_placa, get_documentos_os,
                 salvar_nota_dev, listar_notas_dev)
from datetime import datetime

# ── Config despachante (injetado em todos os templates) ─────────────────────
DESPACHANTE = {
    "nome":       os.environ.get("DESP_NOME",       "DIOGO KAUE LESSMANN"),
    "cpf":        os.environ.get("DESP_CPF",        "060.625.099-99"),
    "cnpj":       os.environ.get("DESP_CNPJ",       "28.858.795/0001-92"),
    "credencial": os.environ.get("DESP_CREDENCIAL",  "2095"),
    "cidade":     os.environ.get("DESP_CIDADE",     "SCHROEDER"),
    "citran":     os.environ.get("DESP_CITRAN",     "Guaramirim"),
    "whatsapp":   os.environ.get("DESP_WHATSAPP",   "47991011351"),
    "whatsapp_fmt": "(47) 99101-1351",
}

@app.context_processor
def inject_globals():
    hoje = datetime.now()
    return dict(
        desp=DESPACHANTE,
        servicos=SERVICOS,
        servicos_grupos=SERVICOS_GRUPOS,
        status_labels=STATUS_LABELS,
        hoje=hoje,
        mes_atual=hoje.month,
        meses=MESES,
        finais_placa_nav=sorted(FINAIS_PLACA.items(), key=lambda x: x[1]),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def dashboard():
    stats   = stats_dashboard()
    recentes = listar_os(limit=8)
    mes      = datetime.now().month
    finais   = {str(f): MESES[m] for f, m in FINAIS_PLACA.items()}
    return render_template("dashboard.html",
        stats=stats, recentes=recentes,
        finais=finais, mes_atual=mes)


# ══════════════════════════════════════════════════════════════════════════════
#  ORDENS DE SERVIÇO
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/os")
def lista_os():
    status = request.args.get("status")
    busca  = request.args.get("q", "").strip()
    page   = request.args.get("page", 1, type=int)
    limit  = 20
    offset = (page - 1) * limit
    ordens = listar_os(status=status or None, busca=busca or None,
                       limit=limit, offset=offset)
    return render_template("os/lista.html",
        ordens=ordens, status_sel=status, busca=busca, page=page)


@app.route("/os/nova", methods=["GET", "POST"])
def nova_os():
    if request.method == "POST":
        f = request.form

        # 1. Cliente
        cliente_id = f.get("cliente_id") or None
        if not cliente_id:
            dados_cli = {
                "tipo": f.get("cli_tipo","PF"),
                "nome": f.get("cli_nome","").strip(),
                "cpf":  f.get("cli_cpf","").strip(),
                "cnpj": f.get("cli_cnpj","").strip(),
                "rg":   f.get("cli_rg","").strip(),
                "nascimento":  f.get("cli_nasc",""),
                "nome_mae":    f.get("cli_mae",""),
                "telefone":    f.get("cli_tel",""),
                "email":       f.get("cli_email",""),
                "cep":         f.get("cli_cep",""),
                "logradouro":  f.get("cli_rua",""),
                "numero":      f.get("cli_num",""),
                "complemento": f.get("cli_comp",""),
                "bairro":      f.get("cli_bairro",""),
                "cidade":      f.get("cli_cidade",""),
                "uf":          f.get("cli_uf","SC"),
            }
            if dados_cli["nome"]:
                # Tenta reaproveitar cliente existente pelo CPF
                existente = buscar_cliente_cpf(dados_cli["cpf"]) if dados_cli["cpf"] else None
                if existente:
                    cliente_id = existente["id"]
                    atualizar_cliente(cliente_id, dados_cli)
                else:
                    cliente_id = criar_cliente(dados_cli)

        # 2. Veículo
        veiculo_id = f.get("veiculo_id") or None
        if not veiculo_id and f.get("v_placa","").strip():
            dados_vei = {
                "placa":           f.get("v_placa","").upper().replace("-",""),
                "renavam":         f.get("v_renavam",""),
                "chassi":          f.get("v_chassi",""),
                "marca":           f.get("v_marca",""),
                "modelo":          f.get("v_modelo",""),
                "ano_fab":         f.get("v_anofab") or None,
                "ano_mod":         f.get("v_anomod") or None,
                "cor":             f.get("v_cor",""),
                "especie":         f.get("v_especie","Automóvel"),
                "tipo_veiculo":    f.get("v_tipo",""),
                "categoria":       f.get("v_categoria","Particular"),
                "combustivel":     f.get("v_combustivel",""),
                "num_crv":         f.get("v_crv",""),
                "proprietario_id": cliente_id,
            }
            veiculo_id = criar_veiculo(dados_vei)

        # Validação: telefone obrigatório
        tel = f.get("cli_tel","").strip()
        if not tel and not cliente_id:
            # tenta pegar do cliente já existente
            pass
        elif not tel:
            cli = get_cliente(cliente_id) if cliente_id else None
            tel = (cli or {}).get("telefone","")
        if not tel:
            # Redireciona de volta com erro
            from flask import flash
            flash("⚠️ Telefone do cliente é obrigatório.", "erro")
            return redirect(url_for("nova_os"))

        # 3. O.S.
        dados_os = {
            "cliente_id":      cliente_id,
            "veiculo_id":      veiculo_id,
            "servico":         f.get("servico","outros"),
            "honorarios":      float(f.get("honorarios") or 0),
            "custos":          float(f.get("custos") or 0),
            "pago":            float(f.get("pago") or 0),
            "forma_pagamento": f.get("forma_pagamento",""),
            "observacoes":     f.get("observacoes",""),
        }
        os_id = criar_os(dados_os)
        return redirect(url_for("detalhe_os", id=os_id))

    # GET — verifica se veio busca prévia de placa
    placa_pre = request.args.get("placa","")
    veiculo   = buscar_veiculo_placa(placa_pre) if placa_pre else None
    cliente   = None
    if veiculo and veiculo.get("proprietario_id"):
        cliente = get_cliente(veiculo["proprietario_id"])

    return render_template("os/nova.html",
        veiculo=veiculo, cliente=cliente, placa_pre=placa_pre)


@app.route("/os/<int:id>")
def detalhe_os(id):
    os_   = get_os(id)
    if not os_: abort(404)
    docs  = get_documentos_os(id)
    return render_template("os/detalhe.html", os=os_, docs=docs)


@app.route("/os/<int:id>/status", methods=["POST"])
def atualizar_status(id):
    status = request.form.get("status","aberta")
    pago   = request.form.get("pago")
    atualizar_os_status(id, status, float(pago) if pago else None)
    return redirect(url_for("detalhe_os", id=id))


@app.route("/os/<int:id>/editar", methods=["POST"])
def editar_os(id):
    f = request.form
    atualizar_os(id, {
        "servico":         f.get("servico","outros"),
        "honorarios":      float(f.get("honorarios") or 0),
        "custos":          float(f.get("custos") or 0),
        "pago":            float(f.get("pago") or 0),
        "forma_pagamento": f.get("forma_pagamento",""),
        "observacoes":     f.get("observacoes",""),
    })
    return redirect(url_for("detalhe_os", id=id))


# ══════════════════════════════════════════════════════════════════════════════
#  IMPRESSÃO — Protocolo RENAVAM (3 vias)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/print/protocolo/<int:os_id>")
def print_protocolo(os_id):
    os_ = get_os(os_id)
    if not os_: abort(404)
    docs_check = request.args.getlist("doc")
    finalidade = SERVICOS.get(os_["servico"], os_["servico"])
    return render_template("print/protocolo.html",
        os=os_, finalidade=finalidade, docs_check=docs_check,
        hoje=datetime.now())


# ══════════════════════════════════════════════════════════════════════════════
#  IA — CHAT
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/chat")
def chat_page():
    rag_stats = db_stats()
    return render_template("chat.html", rag_stats=rag_stats)


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


# ══════════════════════════════════════════════════════════════════════════════
#  API — Busca veículo/cliente
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/busca/placa/<placa>")
def api_busca_placa(placa):
    v = buscar_veiculo_placa(placa)
    if not v:
        return jsonify({"encontrado": False})
    c = get_cliente(v["proprietario_id"]) if v.get("proprietario_id") else None
    return jsonify({"encontrado": True, "veiculo": v, "cliente": c})


@app.route("/api/busca/cpf/<cpf>")
def api_busca_cpf(cpf):
    c = buscar_cliente_cpf(cpf)
    if not c:
        return jsonify({"encontrado": False})
    return jsonify({"encontrado": True, "cliente": c})


# ══════════════════════════════════════════════════════════════════════════════
#  API — Stats / Docs
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/stats")
def api_stats():
    rag = db_stats()
    sys = stats_dashboard()
    return jsonify({**rag, **sys})

@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    def _run():
        try: ingest_all()
        except Exception as e: log.error(f"Ingest error: {e}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "iniciado"})

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "pdf" not in request.files:
        return jsonify({"erro": "Nenhum arquivo"}), 400
    f   = request.files["pdf"]
    ext = f.filename.lower().rsplit(".",1)[-1] if "." in f.filename else ""
    safe_name = secure_filename(f.filename)
    if ext == "pdf":
        os.makedirs(PDFS_DIR, exist_ok=True)
        path = os.path.join(PDFS_DIR, safe_name)
        f.save(path)
        ingestor = ingest_pdf
    elif ext in ("doc","docx"):
        os.makedirs(DOCS_DIR, exist_ok=True)
        path = os.path.join(DOCS_DIR, safe_name)
        f.save(path)
        ingestor = ingest_doc
    else:
        return jsonify({"erro": "Aceitos: PDF, DOC, DOCX"}), 400
    def _run():
        try: ingestor(path)
        except Exception as e: log.error(f"Erro ao processar {f.filename}: {e}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "processando", "arquivo": f.filename})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": "Despachante Lessmann", **db_stats()})


# ══════════════════════════════════════════════════════════════════════════════
#  API — OCR de imagem (Ctrl+V → preenche formulário)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/ocr", methods=["POST"])
def api_ocr():
    import re, json as _json
    data      = request.get_json(silent=True) or {}
    img_b64   = (data.get("imagem") or "").strip()
    mime      = data.get("mime", "image/png")
    if not img_b64:
        return jsonify({"erro": "Nenhuma imagem recebida"}), 400

    prompt = """Analise esta imagem de documento ou tela de sistema de despachante/DETRAN.
Extraia TODOS os dados visíveis de veículo e do proprietário/cliente.
Retorne APENAS um objeto JSON válido, sem texto extra, com os campos abaixo
(use null para campos não encontrados na imagem):

{
  "placa": null,
  "renavam": null,
  "chassi": null,
  "marca": null,
  "modelo": null,
  "ano_fab": null,
  "ano_mod": null,
  "cor": null,
  "especie": null,
  "categoria": null,
  "combustivel": null,
  "num_crv": null,
  "nome": null,
  "cpf": null,
  "cnpj": null,
  "rg": null,
  "nascimento": null,
  "nome_mae": null,
  "telefone": null,
  "email": null,
  "cep": null,
  "logradouro": null,
  "numero": null,
  "complemento": null,
  "bairro": null,
  "cidade": null,
  "uf": null
}

IMPORTANTE: Retorne SOMENTE o JSON, nada mais."""

    try:
        from rag import get_groq
        groq = get_groq()
        resp = groq.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }],
            max_tokens=1024,
            temperature=0.1,
        )
        texto = resp.choices[0].message.content.strip()
        # Extrai JSON da resposta
        match = re.search(r"\{[\s\S]*\}", texto)
        if not match:
            return jsonify({"erro": "IA não retornou JSON válido", "raw": texto[:300]}), 422
        dados = _json.loads(match.group())
        # Remove nulls para não sobrescrever campos já preenchidos
        dados = {k: v for k, v in dados.items() if v is not None and v != ""}
        log.info(f"OCR extraiu {len(dados)} campos: {list(dados.keys())}")
        return jsonify({"ok": True, "dados": dados, "campos": len(dados)})
    except Exception as e:
        log.error(f"OCR error: {e}")
        return jsonify({"erro": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
#  DEV — Página privada de roadmap e anotações
# ══════════════════════════════════════════════════════════════════════════════

DEV_PASSWORD = os.environ.get("DEV_PASSWORD", "lessmann2026")

@app.route("/dev", methods=["GET", "POST"])
def dev_page():
    # Login via POST (senha no form)
    if request.method == "POST":
        if request.form.get("senha") == DEV_PASSWORD:
            session["dev_ok"] = True
            return redirect(url_for("dev_page"))
        return render_template("dev_login.html", erro=True)

    # Não autenticado → tela de login
    if not session.get("dev_ok"):
        return render_template("dev_login.html", erro=False)

    notas = listar_notas_dev()
    return render_template("dev.html", notas=notas, now=datetime.now())


@app.route("/dev/nota", methods=["POST"])
def dev_nota():
    if not session.get("dev_ok"):
        return redirect(url_for("dev_page"))
    titulo = request.form.get("titulo", "").strip() or "Sem título"
    texto  = request.form.get("texto", "").strip()
    if texto:
        salvar_nota_dev(titulo, texto)
    return redirect(url_for("dev_page"))


@app.route("/dev/sair")
def dev_sair():
    session.pop("dev_ok", None)
    return redirect(url_for("dev_page"))


# ══════════════════════════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════════════════════════

def _startup():
    try:
        init_db()
        s   = stats_dashboard()
        rag = db_stats()
        log.info(f"DB OK — {s['os_total']} O.S. | {s['clientes']} clientes | "
                 f"{s['veiculos']} veículos | {rag['chunks']} chunks RAG")
    except Exception as e:
        log.error(f"Startup error: {e}")

with app.app_context():
    _startup()

if __name__ == "__main__":
    app.run(debug=True, port=5002)
