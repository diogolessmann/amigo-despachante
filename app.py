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
                 salvar_nota_dev, listar_notas_dev,
                 lista_final_placa, listar_exercicios, atualizar_situacao_pag)
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
            "exercicio":       int(f.get("exercicio") or datetime.now().year),
            "situacao_pag":    f.get("situacao_pag",""),
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
        "exercicio":       int(f.get("exercicio") or datetime.now().year),
        "situacao_pag":    f.get("situacao_pag",""),
    })
    return redirect(url_for("detalhe_os", id=id))


# ══════════════════════════════════════════════════════════════════════════════
#  LISTA POR FINAL DE PLACA
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/lista")
def lista_placa():
    final    = request.args.get("final", "5")           # dígito final da placa
    exercicio = request.args.get("exercicio", datetime.now().year, type=int)
    situacao  = request.args.get("situacao", "")        # '' | pendente | concluido
    ordens    = lista_final_placa(final, exercicio, situacao or None)
    exercicios = listar_exercicios()
    # Garante que o ano atual sempre aparece na lista
    if datetime.now().year not in exercicios:
        exercicios.insert(0, datetime.now().year)
    # Conta pendentes / concluídos
    pendentes  = sum(1 for o in ordens if o["status"] not in ("concluida","cancelada"))
    concluidos = sum(1 for o in ordens if o["status"] == "concluida")
    mes_placa  = MESES[FINAIS_PLACA.get(final, 0)]
    return render_template("lista_placa.html",
        ordens=ordens, final=final, exercicio=exercicio,
        situacao=situacao, exercicios=exercicios,
        pendentes=pendentes, concluidos=concluidos,
        mes_placa=mes_placa,
        total=len(ordens))


@app.route("/lista/csv")
def lista_placa_csv():
    import csv, io
    final     = request.args.get("final", "5")
    exercicio = request.args.get("exercicio", datetime.now().year, type=int)
    situacao  = request.args.get("situacao", "")
    ordens    = lista_final_placa(final, exercicio, situacao or None)

    def _situacao_label(o):
        if o.get("situacao_pag"):
            return o["situacao_pag"]
        s = o.get("status", "")
        if s == "concluida":
            return "CONCLUÍDO"
        if s == "cancelada":
            return "CANCELADO"
        return "AGUARDANDO PAGAMENTO"

    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(["nome", "cpf", "renavam", "placa", "exercicio", "telefone", "situacao", "os_id"])
    for o in ordens:
        # Normaliza telefone para MandaZap (só dígitos com 55)
        tel = (o.get("telefone") or "").replace("(","").replace(")","").replace("-","").replace(" ","")
        if tel and not tel.startswith("55"):
            tel = "55" + tel
        w.writerow([
            o.get("cliente",""),
            o.get("cpf",""),
            o.get("renavam",""),
            o.get("placa",""),
            o.get("exercicio",""),
            tel,
            _situacao_label(o),
            o.get("os_id",""),
        ])
    buf.seek(0)
    from flask import Response
    filename = f"final{final}_ex{exercicio}.csv"
    return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/lista/situacao/<int:os_id>", methods=["POST"])
def set_situacao_pag(os_id):
    situacao = request.get_json(silent=True) or {}
    atualizar_situacao_pag(os_id, situacao.get("situacao_pag",""))
    return jsonify({"ok": True})


@app.route("/lista/disparar", methods=["POST"])
def lista_disparar():
    """Dispara mensagem WhatsApp para todos os contatos da lista via Evolution API."""
    import time
    try:
        import requests as _req
    except ImportError:
        return jsonify({"erro": "requests não instalado"}), 500

    data         = request.get_json(silent=True) or {}
    final        = data.get("final", "5")
    exercicio    = data.get("exercicio", datetime.now().year)
    situacao     = data.get("situacao", "pendente")
    mensagem_tpl = data.get("mensagem", "").strip()
    delay_s      = max(1, min(30, int(data.get("delay", 4))))

    if not mensagem_tpl:
        return jsonify({"erro": "Mensagem não pode estar vazia"}), 400

    evo_url      = os.environ.get("EVO_URL", "").rstrip("/")
    evo_key      = os.environ.get("EVO_KEY", "")
    evo_instance = os.environ.get("EVO_INSTANCE", "")

    if not evo_url or not evo_key or not evo_instance:
        return jsonify({"erro":
            "WhatsApp não configurado. Preencha EVO_URL, EVO_KEY e EVO_INSTANCE no arquivo .env"}), 400

    ordens  = lista_final_placa(final, int(exercicio), situacao or None)
    mes_str = MESES[FINAIS_PLACA.get(final, 0)]

    results = []
    for o in ordens:
        # Normaliza telefone → 55 + DDD + número (só dígitos)
        tel = (o.get("telefone") or "").replace("(","").replace(")","").replace("-","").replace(" ","").replace("+","")
        if not tel:
            results.append({"nome": o.get("cliente","?"), "status": "sem_telefone"})
            continue
        if not tel.startswith("55"):
            tel = "55" + tel

        nome_curto = (o.get("cliente") or "Cliente").split()[0].title()
        try:
            msg = mensagem_tpl.format(
                nome       = nome_curto,
                nome_completo = (o.get("cliente") or "").title(),
                placa      = (o.get("placa") or "").upper(),
                exercicio  = o.get("exercicio") or exercicio,
                mes        = mes_str,
                despachante= DESPACHANTE["nome"].title(),
                whatsapp   = DESPACHANTE["whatsapp_fmt"],
                cidade     = DESPACHANTE["cidade"],
            )
        except KeyError as e:
            return jsonify({"erro": f"Variável inválida na mensagem: {e}. Use apenas: {{nome}}, {{placa}}, {{exercicio}}, {{mes}}, {{despachante}}, {{whatsapp}}, {{cidade}}"}), 400

        try:
            resp = _req.post(
                f"{evo_url}/message/sendText/{evo_instance}",
                headers={"apikey": evo_key, "Content-Type": "application/json"},
                json={"number": tel, "text": msg},
                timeout=12,
            )
            ok = resp.status_code in (200, 201)
            results.append({
                "nome":  o.get("cliente",""),
                "tel":   tel,
                "status": "ok" if ok else "erro",
                "detalhe": "" if ok else resp.text[:120],
            })
        except Exception as e:
            results.append({"nome": o.get("cliente",""), "tel": tel, "status": "erro", "detalhe": str(e)[:120]})

        time.sleep(delay_s)

    sent   = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] != "ok")
    log.info(f"Campanha Final {final} Ex{exercicio}: {sent} enviados, {failed} falhas")
    return jsonify({"sent": sent, "failed": failed, "results": results})


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

DEV_TOKEN = os.environ.get("DEV_TOKEN", "lessmann2026")

@app.route("/dev/<token>")
def dev_page(token):
    if token != DEV_TOKEN:
        abort(404)
    notas = listar_notas_dev()
    return render_template("dev.html", notas=notas, now=datetime.now(), token=token)


@app.route("/dev/<token>/nota", methods=["POST"])
def dev_nota(token):
    if token != DEV_TOKEN:
        abort(404)
    titulo = request.form.get("titulo", "").strip() or "Sem título"
    texto  = request.form.get("texto", "").strip()
    if texto:
        salvar_nota_dev(titulo, texto)
    return redirect(url_for("dev_page", token=token))


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
