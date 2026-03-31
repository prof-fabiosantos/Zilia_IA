import uuid
import os
import io
import re
import traceback
from typing import Optional, List, Dict, Any

import fitz  # PyMuPDF
from PIL import Image

from qdrant_client import QdrantClient, models
import ollama
from fastmcp import FastMCP
from dotenv import load_dotenv
from ollama_ocr import OCRProcessor


load_dotenv()

mcp = FastMCP("Document Assistant Server")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
QDRANT_PATH        = "./vector_store_mcp"
COLLECTION_NAME    = "documentos_mcp"
UPLOAD_DIR         = "uploads_mcp"
CACHE_COLLECTION   = "cache_perguntas_mcp"
FEEDBACK_COLLECTION = "feedback_preferencias_mcp"
SESSIONS_COLLECTION = "sessoes_mcp"  # [SESSÕES] histórico de conversas persistidas

LLM                    = "granite4:latest"
EMBED_MODEL            = "nomic-embed-text:latest"
VECTOR_SIZE            = 768

# ---------------------------------------------------------------------------
# Cross-encoder para reranking (substitui o LLM pointwise)
# ---------------------------------------------------------------------------
# Modelo leve (~80MB), totalmente público (sem autenticação HuggingFace),
# roda bem em CPU, sem GPU necessária.
#
# Modelos testados e públicos (sem 401/autenticação):
#   - "cross-encoder/ms-marco-MiniLM-L6-v2"               ← padrão (inglês, ~80MB, mais rápido)
#   - "amberoad/bert-multilingual-passage-reranking-msmarco" (multilingual PT, ~440MB)
#   - "nreimers/mmarco-mMiniLMv2-L12-H384-v1"              (multilingual, ~120MB)
#
# Para baixar (requer internet uma única vez):
#   pip install sentence-transformers
#   python -c "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L6-v2')"
#
# Nota: o modelo ms-marco-MiniLM-L6-v2 foi treinado em inglês (MS MARCO).
# Para perguntas em português ele ainda funciona bem para reranking pois
# captura padrões de relevância query-documento que são transferíveis,
# mas o modelo multilingual é superior para PT-BR quando disponível.
CROSS_ENCODER_MODEL    = "cross-encoder/ms-marco-MiniLM-L6-v2"

# ---------------------------------------------------------------------------
# Chunking semântico
# ---------------------------------------------------------------------------
# Tamanho máximo de chunk em tokens (aproximação: 1 token ≈ 4 chars em PT-BR).
# Chunks menores → mais precisão no retrieval; maiores → mais contexto por chunk.
SEMANTIC_CHUNK_MAX_TOKENS   = 256   # ~1024 chars — equilibra precisão e contexto
SEMANTIC_CHUNK_MIN_TOKENS   = 40    # ~160 chars — evita chunks muito pequenos
# Sobreposição entre chunks adjacentes para não perder contexto na fronteira
SEMANTIC_CHUNK_OVERLAP_SENTS = 1    # número de sentenças de overlap entre chunks

# Thresholds de busca
CACHE_SCORE_THRESHOLD  = 0.92   # [MELHORIA] Reduzido de 0.97 → 0.92 para capturar variações lexicais
RAG_HIGH_THRESHOLD     = 0.5
RAG_LOW_THRESHOLD      = 0.3
FEEDBACK_THRESHOLD     = 0.70   # [FIX-B] Reduzido de 0.80 → 0.70 para capturar variações lexicais da pergunta

# Reranking
# [MELHORIA] Com cross-encoder, podemos aumentar RERANK_CANDIDATE_LIMIT
# sem impacto significativo de latência (~5-20ms por par vs ~1-3s no LLM).
# Mais candidatos → maior recall → melhor seleção final.
RERANK_CANDIDATE_LIMIT  = 40     # candidatos enviados para o reranker (era 20)
RERANK_TOP_K            = 6      # chunks finais para o contexto do LLM
# Chunks com rerank_score >= esse valor são protegidos do prefer-filter do feedback.
# O prefer-filter só restringe chunks de score baixo; chunks relevantes de qualquer
# fonte são sempre preservados para não sacrificar qualidade da resposta.
RERANK_PREFER_BYPASS    = 7.0    # escala 1-10: score ≥ 7 = claramente relevante

# Paginação segura
MAX_SCROLL_PAGES = 500          # [V6] evita loop infinito

# ---------------------------------------------------------------------------
# Constantes de proteção para OCR de imagens
# ---------------------------------------------------------------------------
# [V13] OCR DE IMAGEM SEM TIMEOUT E SEM PRÉ-PROCESSAMENTO
#   - qwen2.5vl:7b é um modelo multimodal pesado. Em hardware limitado e com imagens
#     de alta resolução (como fluxogramas A3/exportados de ferramentas de modelagem),
#     o OCR pode levar vários minutos ou travar indefinidamente, causando:
#       a) McpError: Connection closed no cliente (SSE timeout)
#       b) O servidor MCP fica preso e não responde a nenhuma outra requisição
#   Correção: timeout via threading + redimensionamento prévio da imagem +
#   timeout separado no main.py para a tool index_document.
#
# Dimensão máxima (pixels) do lado maior antes de enviar ao OCR.
# O qwen2.5vl internamente trabalha com resolução bem menor; enviar imagens
# enormes só aumenta o tempo de pré-processamento sem melhorar o resultado.
OCR_MAX_DIMENSION  = 2048    # px
# Tempo máximo (segundos) aguardado pelo OCR de UMA imagem/página.
# Se o modelo não responder nesse prazo, a tool retorna um aviso ao invés
# de ficar pendurada para sempre.
# Deve ser menor que MCP_TIMEOUT_INDEX no main.py para que o servidor consiga
# responder antes de o cliente desistir.
OCR_IMAGE_TIMEOUT  = 300     # 5 min por imagem/página


# ---------------------------------------------------------------------------
# Extração de texto
# ---------------------------------------------------------------------------
def _resize_image_if_needed(image_path: str) -> str:
    """
    [V13] Redimensiona imagens grandes antes de enviar ao OCR.
    O qwen2.5vl:7b fica muito mais lento (ou trava) com imagens acima de ~2048px.
    Fluxogramas exportados de ferramentas como Bizagi costumam ter resolução muito
    alta (3000-6000px), tornando esse pré-processamento essencial.
    Retorna o path da imagem redimensionada (temporária) ou o original se não precisar.
    """
    try:
        img = Image.open(image_path)
        w, h = img.size
        if max(w, h) <= OCR_MAX_DIMENSION:
            print(f"🖼️  Imagem {w}x{h}px — dentro do limite, sem redimensionamento.")
            return image_path

        scale   = OCR_MAX_DIMENSION / max(w, h)
        new_w   = int(w * scale)
        new_h   = int(h * scale)
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        tmp     = f"/tmp/resized_{os.path.basename(image_path)}"
        resized.save(tmp)
        print(f"🖼️  Imagem redimensionada: {w}x{h} → {new_w}x{new_h} px (salvo em {tmp})")
        return tmp
    except Exception as e:
        print(f"⚠️  Falha ao redimensionar imagem: {e} — usando original")
        return image_path

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(QDRANT_PATH, exist_ok=True)

# ---------------------------------------------------------------------------
# Inicialização dos clientes
# ---------------------------------------------------------------------------
try:
    qdrant_client  = QdrantClient(path=QDRANT_PATH)
    ollama_client  = ollama.Client()
    ocr            = OCRProcessor(model_name='qwen2.5vl:7b', base_url="http://localhost:11434/api/generate")
    print("✅ Clientes Qdrant, Ollama e OCR inicializados com sucesso.")
except Exception as e:
    # [V2] substituído exit() por raise com mensagem clara
    raise RuntimeError(f"❌ Falha crítica ao inicializar clientes: {e}") from e


# ---------------------------------------------------------------------------
# Helpers de segurança
# ---------------------------------------------------------------------------
def _safe_filename(filename: str) -> str:
    """[V4] Remove path separators e caracteres perigosos do filename."""
    # Extrai só o basename (evita ../../)
    name = os.path.basename(filename)
    # Remove qualquer caractere que não seja alfanumérico, ponto, hífen ou underscore
    name = re.sub(r"[^\w.\-]", "_", name)
    return name or "arquivo_desconhecido"


def _safe_upload_path(filename: str) -> str:
    """[V4] Retorna caminho absoluto garantindo que está dentro de UPLOAD_DIR."""
    safe_name = _safe_filename(filename)
    base = os.path.realpath(UPLOAD_DIR)
    candidate = os.path.realpath(os.path.join(base, safe_name))
    if not candidate.startswith(base + os.sep) and candidate != base:
        raise ValueError(f"Path traversal detectado para '{filename}'.")
    return candidate


# ---------------------------------------------------------------------------
# Funções de extração de payload (evita redefinição dupla — [V11])
# ---------------------------------------------------------------------------
def _hit_source(hit) -> str:
    return ((hit.payload or {}).get("source") or "")


def _hit_doc_type(hit) -> str:
    return ((hit.payload or {}).get("document_type") or "").lower()


# ---------------------------------------------------------------------------
# Setup do Qdrant
# ---------------------------------------------------------------------------
def _ensure_collection(name: str, vector_size: int = VECTOR_SIZE):
    """[V3] Cria coleção somente se não existir (recreate_collection foi depreciado)."""
    try:
        qdrant_client.get_collection(collection_name=name)
        print(f"✅ Coleção '{name}' já existe.")
    except Exception:
        print(f"⚠️  Coleção '{name}' não encontrada — criando...")
        qdrant_client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
        )
        print(f"✅ Coleção '{name}' criada com sucesso.")


def setup_qdrant():
    _ensure_collection(COLLECTION_NAME)
    _ensure_collection(CACHE_COLLECTION)
    _ensure_collection(FEEDBACK_COLLECTION)
    _ensure_collection(SESSIONS_COLLECTION)  # [SESSÕES]


setup_qdrant()


# ---------------------------------------------------------------------------
# Extração de texto
# ---------------------------------------------------------------------------
def get_text_from_pdf(pdf_path: str, ocr_instance: OCRProcessor, dpi: int = 300) -> str:
    """Extração híbrida: texto embutido ou OCR quando a página é escaneada."""
    pdf = fitz.open(pdf_path)
    total_pages = len(pdf)
    print(f"📄 Documento carregado: {total_pages} páginas.")
    all_text = ""

    for page_number in range(total_pages):
        page = pdf[page_number]
        print(f"\n📑 Processando página {page_number + 1}/{total_pages}...")

        text_embedded = page.get_text("text").strip()
        if len(text_embedded) > 50:
            print("✅ Texto embutido detectado — OCR não necessário.")
            all_text += f"\n--- Página {page_number + 1} (texto embutido) ---\n{text_embedded}\n"
        else:
            print("📷 Página provavelmente escaneada — aplicando OCR...")
            zoom   = dpi / 72
            matrix = fitz.Matrix(zoom, zoom)
            pix    = page.get_pixmap(matrix=matrix)
            image  = Image.open(io.BytesIO(pix.tobytes("png")))
            tmp_path = f"/tmp/page_{page_number + 1}.png"

            # [V5] try/finally garante deleção do temporário mesmo em caso de erro
            try:
                image.save(tmp_path)

                # Executa OCR com timeout para não travar o servidor
                import threading
                page_result: dict = {"text": None, "error": None}

                def _run_page_ocr():
                    try:
                        page_result["text"] = ocr_instance.process_image(
                            image_path=tmp_path,
                            format_type="markdown",
                            language="Portuguese",
                            custom_prompt=(
                                "Extract all visible text from the image. "
                                "Return all text exactly as it appears, without interpretation or translation."
                            ),
                        )
                    except Exception as exc:
                        page_result["error"] = exc

                t = threading.Thread(target=_run_page_ocr, daemon=True)
                t.start()
                t.join(timeout=OCR_IMAGE_TIMEOUT)

                if t.is_alive():
                    print(f"⏱️  OCR da página {page_number + 1} excedeu {OCR_IMAGE_TIMEOUT}s — pulando.")
                    all_text += (
                        f"\n--- Página {page_number + 1} (OCR) ---\n"
                        f"[Timeout: OCR demorou mais de {OCR_IMAGE_TIMEOUT}s]\n"
                    )
                elif page_result["error"] is not None:
                    raise page_result["error"]
                else:
                    result = page_result["text"] or ""
                    if result.strip():
                        print("✅ OCR concluído com sucesso.")
                        all_text += f"\n--- Página {page_number + 1} (OCR) ---\n{result}\n"
                    else:
                        print("⚠️ OCR não encontrou texto nesta página.")
                        all_text += f"\n--- Página {page_number + 1} (OCR) ---\n[Sem texto detectado]\n"

            except Exception as ocr_error:
                print(f"❌ Erro no OCR da página {page_number + 1}: {ocr_error}")
                all_text += f"\n--- Página {page_number + 1} (OCR) ---\n[Erro: {ocr_error}]\n"
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

    print("\n✅ Extração concluída para todas as páginas.")
    return all_text if all_text.strip() else "⚠️ Nenhum texto foi extraído deste PDF."


def get_text_from_image(file_path: str, ocr_instance: OCRProcessor) -> str:
    """
    [V13] Extrai texto de imagens via OCR (qwen2.5vl:7b) com três camadas de proteção:

    1. Pré-processamento: redimensiona para OCR_MAX_DIMENSION antes de enviar ao modelo.
       Fluxogramas de alta resolução (ex.: exportações do Bizagi) costumam ter 3000-6000px,
       o que trava o qwen2.5vl em hardware limitado.

    2. Timeout via thread: se o Ollama não responder em OCR_IMAGE_TIMEOUT segundos,
       a função retorna uma mensagem de aviso ao invés de ficar pendurada.
       Isso evita que o servidor MCP trave e cause McpError: Connection closed no cliente.

    3. Limpeza de temporários: o arquivo redimensionado é deletado em qualquer cenário
       (sucesso, timeout ou exceção).
    """
    import threading

    print(f"🖼️  Iniciando OCR de '{os.path.basename(file_path)}'...")

    # 1) Redimensiona se necessário
    effective_path = _resize_image_if_needed(file_path)
    used_resize    = effective_path != file_path

    result_holder: dict = {"text": None, "error": None}

    def _run_ocr():
        try:
            result_holder["text"] = ocr_instance.process_image(
                image_path=effective_path,
                format_type="markdown",
                custom_prompt="""Extract all visible text from the image. If the image is a Portuguese-language flowchart illustrating a process, extract:

1. The process title (in Portuguese),
2. The full sequence of all process steps (including decision points, actions, and outcomes),
3. The labels of decision connectors (like "Sim" or "Não"),
4. All paths, including alternative branches.

Return the extracted text in the order of the flow, from the start to all possible endpoints. Do not skip any paths or boxes, even if they are in lower or side branches. Do not add any interpretation or translation — just extract and return all Portuguese text exactly as it appears.
""",
                language="Portuguese"
            )
        except Exception as e:
            result_holder["error"] = e

    # 2) Executa em thread com timeout para não bloquear o servidor
    ocr_thread = threading.Thread(target=_run_ocr, daemon=True)
    ocr_thread.start()
    ocr_thread.join(timeout=OCR_IMAGE_TIMEOUT)

    # 3) Sempre limpa o temporário de redimensionamento
    if used_resize and os.path.exists(effective_path):
        try:
            os.remove(effective_path)
        except Exception:
            pass

    # Avalia o resultado
    if ocr_thread.is_alive():
        # Thread ainda rodando → modelo travou
        msg = (
            f"⚠️ OCR timeout após {OCR_IMAGE_TIMEOUT}s: o modelo demorou demais para "
            f"processar '{os.path.basename(file_path)}'. "
            "Sugestões: (1) reduza a resolução da imagem antes de enviar, "
            "(2) converta para PDF com texto embutido, "
            "(3) verifique se o Ollama tem memória suficiente disponível."
        )
        print(f"⏱️  {msg}")
        return msg

    if result_holder["error"] is not None:
        err = result_holder["error"]
        print(f"❌ Erro no OCR de '{file_path}': {err}")
        return f"❌ Erro ao processar imagem '{os.path.basename(file_path)}': {err}"

    text = (result_holder["text"] or "").strip()
    if not text:
        return f"⚠️ OCR não encontrou texto em '{os.path.basename(file_path)}'."

    print(f"✅ OCR concluído — {len(text)} caracteres extraídos.")
    return text


def _normalize_query(query: str) -> str:
    """
    [MELHORIA] Normaliza a query antes de gerar o embedding para o cache.
    Isso aumenta a taxa de acerto do cache semântico em perguntas lexicalmente
    diferentes mas semanticamente idênticas (ex.: maiúsculas, pontuação, artigos).

    Etapas:
      1. Lowercase
      2. Remove pontuação final (? ! .)
      3. Colapsa espaços múltiplos
    Não remove stop words para não alterar o significado semântico.
    """
    normalized = query.strip().lower()
    normalized = re.sub(r"[?!.]+$", "", normalized).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def get_text_from_docx(file_path: str) -> str:
    """
    [MELHORIA] Extrai texto de arquivos .docx via python-docx.
    Preserva parágrafos e conteúdo de tabelas.
    Requer: pip install python-docx

    Nota: o pacote é instalado como 'python-docx' mas importado como 'docx'.
    O Pylance pode reportar "import não resolvido" se os stubs de tipo não
    estiverem presentes — isso é um falso positivo, o código funciona normalmente.
    Para instalar: pip install python-docx
    """
    try:
        import importlib
        python_docx = importlib.import_module("docx")  # python-docx → módulo 'docx'
        doc = python_docx.Document(file_path)
        parts = []
        for para in doc.paragraphs:
            if para.text.strip():
                parts.append(para.text.strip())
        for table in doc.tables:
            for row in table.rows:
                row_texts = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if row_texts:
                    parts.append(" | ".join(row_texts))
        text = "\n\n".join(parts)
        print(f"✅ DOCX extraído: {len(text)} caracteres, {len(doc.paragraphs)} parágrafos.")
        return text if text.strip() else "⚠️ Nenhum texto encontrado no arquivo DOCX."
    except ImportError:
        return "❌ python-docx não instalado. Execute: pip install python-docx"
    except Exception as e:
        return f"❌ Erro ao ler DOCX '{os.path.basename(file_path)}': {e}"


def get_text_from_txt(file_path: str) -> str:
    """
    [MELHORIA] Lê arquivos .txt com detecção automática de encoding.
    Tenta UTF-8, depois latin-1 como fallback.
    """
    for encoding in ("utf-8", "latin-1", "cp1252"):
        try:
            with open(file_path, "r", encoding=encoding) as f:
                text = f.read()
            print(f"✅ TXT lido com encoding '{encoding}': {len(text)} caracteres.")
            return text if text.strip() else "⚠️ Arquivo de texto vazio."
        except UnicodeDecodeError:
            continue
        except Exception as e:
            return f"❌ Erro ao ler TXT '{os.path.basename(file_path)}': {e}"
    return "❌ Não foi possível decodificar o arquivo de texto."


def get_text_from_csv(file_path: str) -> str:
    """
    [MELHORIA] Converte CSV em texto estruturado legível pelo LLM.
    Cada linha vira "campo1: valor1 | campo2: valor2" para preservar semântica.
    Requer: pip install pandas
    """
    try:
        import pandas as pd
        # Tenta detectar separador automaticamente
        df = pd.read_csv(file_path, sep=None, engine="python", dtype=str, encoding="utf-8")
        rows = []
        headers = list(df.columns)
        for _, row in df.iterrows():
            pairs = [f"{h}: {str(v).strip()}" for h, v in zip(headers, row) if str(v).strip() not in ("", "nan", "None")]
            if pairs:
                rows.append(" | ".join(pairs))
        text = "\n".join(rows)
        print(f"✅ CSV extraído: {len(df)} linhas, {len(headers)} colunas, {len(text)} caracteres.")
        return text if text.strip() else "⚠️ CSV sem conteúdo útil."
    except ImportError:
        return "❌ pandas não instalado. Execute: pip install pandas"
    except Exception as e:
        return f"❌ Erro ao ler CSV '{os.path.basename(file_path)}': {e}"


def _split_into_sentences(text: str) -> list[str]:
    """
    Divide texto em sentenças usando heurísticas para português do Brasil.
    Não depende de NLTK ou spaCy — funciona offline.

    Regras:
    - Quebra em '.', '!', '?' seguidos de espaço + letra maiúscula ou fim de linha.
    - Preserva abreviações comuns (Dr., Sr., Fig., pág., etc.) para não quebrar no meio.
    - Respeita marcadores de lista (linhas começando com '—', '-', '*', número+'.').
    - Remove sentenças vazias ou com menos de 10 chars.
    """
    # Preserva abreviações: substitui ponto por marcador temporário
    abbreviations = [
        r"(?<=[Dd]r)\.", r"(?<=[Ss]r)\.", r"(?<=[Ss]ra)\.", r"(?<=[Pp]rof)\.",
        r"(?<=[Aa]v)\.",  r"(?<=[Ff]ig)\.", r"(?<=[Pp]ág)\.", r"(?<=[Nn]º)\.",
        r"(?<=[Ee]x)\.",  r"(?<=[Ee]tc)\.", r"(?<=\d)\.",
    ]
    protected = text
    for abbr in abbreviations:
        protected = re.sub(abbr, "⟨PONTO⟩", protected)

    # Quebra nos terminadores de sentença reais
    # Padrão: ponto/excl/interrogação seguido de espaço+maiúscula OU fim de linha
    parts = re.split(r'(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÃÕÂÊÎÔÛ\-\*\d])|(?<=\n)', protected)

    sentences = []
    for part in parts:
        # Restaura abreviações
        part = part.replace("⟨PONTO⟩", ".").strip()
        if len(part) >= 10:
            sentences.append(part)

    return sentences if sentences else [text]


def _estimate_tokens(text: str) -> int:
    """Estimativa rápida de tokens: 1 token ≈ 4 chars em português."""
    return max(1, len(text) // 4)


def get_semantic_chunks(
    text: str,
    max_tokens: int = SEMANTIC_CHUNK_MAX_TOKENS,
    min_tokens: int = SEMANTIC_CHUNK_MIN_TOKENS,
    overlap_sents: int = SEMANTIC_CHUNK_OVERLAP_SENTS,
    source_hint: str = "",
) -> list[str]:
    """
    [MELHORIA] Chunking semântico baseado em sentenças.

    Diferença em relação ao chunking por caracteres:
    - O chunking anterior cortava a cada 950 chars, frequentemente no meio de
      uma frase, separando sujeito do predicado ou premissa da conclusão.
      Isso forçava o LLM a trabalhar com contexto truncado e sem coerência.
    - Este método agrupa sentenças completas até atingir max_tokens,
      garantindo que cada chunk termina numa fronteira semântica natural.

    Algoritmo:
    1. Divide o texto em sentenças via heurística PT-BR.
    2. Agrupa sentenças sequenciais até atingir max_tokens.
    3. Ao fechar um chunk, inclui overlap_sents sentenças do chunk anterior
       no início do próximo (preserva contexto na fronteira).
    4. Chunks abaixo de min_tokens são fundidos com o adjacente.
    5. Fallback para chunking por caracteres se a divisão em sentenças falhar.

    Args:
        text:          Texto completo extraído do documento.
        max_tokens:    Tamanho máximo do chunk em tokens (~4 chars/token).
        min_tokens:    Tamanho mínimo — chunks menores são fundidos.
        overlap_sents: Quantas sentenças do chunk anterior incluir no início do próximo.
        source_hint:   Nome do arquivo (usado apenas para logging).

    Returns:
        Lista de strings — cada string é um chunk semanticamente coerente.
    """
    if not text or not text.strip():
        return []

    sentences = _split_into_sentences(text)
    if not sentences:
        return [text]

    print(f"📐 Chunking semântico: {len(sentences)} sentenças "
          f"(max={max_tokens}tok, overlap={overlap_sents}sent)"
          + (f" [{source_hint}]" if source_hint else ""))

    chunks     = []
    current    = []          # sentenças do chunk em construção
    current_tk = 0           # tokens acumulados

    for sent in sentences:
        sent_tk = _estimate_tokens(sent)

        # Sentença maior que o limite: divide por caracteres como fallback
        if sent_tk > max_tokens:
            # Fecha chunk atual antes
            if current:
                chunks.append(" ".join(current))
                current, current_tk = [], 0
            # Divide a sentença longa em pedaços de max_tokens
            for i in range(0, len(sent), max_tokens * 4):
                piece = sent[i : i + max_tokens * 4].strip()
                if piece:
                    chunks.append(piece)
            continue

        # Chunk cheio: fecha e começa novo com overlap
        if current_tk + sent_tk > max_tokens and current:
            chunks.append(" ".join(current))
            # Overlap: reutiliza as últimas N sentenças no próximo chunk
            overlap = current[-overlap_sents:] if overlap_sents > 0 else []
            current    = overlap + [sent]
            current_tk = sum(_estimate_tokens(s) for s in current)
        else:
            current.append(sent)
            current_tk += sent_tk

    # Fecha último chunk
    if current:
        chunks.append(" ".join(current))

    # Funde chunks muito pequenos com o anterior
    merged = []
    for chunk in chunks:
        if merged and _estimate_tokens(chunk) < min_tokens:
            merged[-1] = merged[-1] + " " + chunk
        else:
            merged.append(chunk)

    print(f"  → {len(merged)} chunks gerados "
          f"(média ~{sum(_estimate_tokens(c) for c in merged) // max(len(merged),1)} tok/chunk)")
    return merged


def get_text_chunks(text: str, chunk_size: int = 950, chunk_overlap: int = 100) -> list[str]:
    """
    Mantido como fallback para compatibilidade.
    O pipeline de indexação usa get_semantic_chunks() por padrão.
    Chunking simples por caracteres — menos preciso semanticamente.
    """
    chunks = []
    for i in range(0, len(text), chunk_size - chunk_overlap):
        chunks.append(text[i:i + chunk_size])
    return chunks


def get_embeddings(text: str, model: str = EMBED_MODEL) -> list[float]:
    """
    Embeddings via Ollama.
    Usa a API atual ollama.embed() — ollama.embeddings(prompt=) foi depreciada
    e removida no cliente Ollama >= 0.4.x.
    """
    try:
        response = ollama.embed(model=model, input=text)
        # API nova retorna {"embeddings": [[...floats...]]}
        embeddings = response.get("embeddings") or response.get("embedding")
        if isinstance(embeddings, list) and embeddings:
            # Se vier lista de listas (batch), pega o primeiro vetor
            return embeddings[0] if isinstance(embeddings[0], list) else embeddings
        return []
    except Exception as e:
        print(f"❌ Erro ao gerar embedding com Ollama: {e}")
        return []


# ---------------------------------------------------------------------------
# Extração de tipo de documento da query
# ---------------------------------------------------------------------------
def extract_document_type_from_query(query: str) -> tuple[str, str]:
    """
    Extrai tipo de documento do prompt livre do usuário.
    Ex.: 'tipo: manual', 'use apenas o fluxograma', 'do tipo relatório'
    Retorna (tipo, query_sem_tipo).
    """
    match = re.search(
        r"(?:tipo:|tipo |use apenas o|use somente o|apenas do|do tipo)\s*([a-zA-Zãõáéíóúç\- ]+)",
        query, re.IGNORECASE
    )
    if match:
        doc_type      = match.group(1).strip().lower()
        query_sem_tipo = re.sub(match.group(0), "", query, flags=re.IGNORECASE).strip()
        return doc_type, query_sem_tipo
    return None, query


# ---------------------------------------------------------------------------
# Cross-encoder — carregamento lazy, apenas do cache local
# ---------------------------------------------------------------------------
# O cross-encoder é carregado uma única vez e mantido em memória.
# Usa local_files_only=True — nunca tenta fazer download automático.
#
# Para habilitar em ambiente COM internet:
#   pip install sentence-transformers
#   python -c "from sentence_transformers import CrossEncoder; \
#              CrossEncoder('cross-encoder/ms-marco-multilingual-MiniLM-L6-v2')"
# Nas execuções seguintes o modelo fica em ~/.cache/huggingface e carrega offline.
# ---------------------------------------------------------------------------
_cross_encoder_instance  = None
_cross_encoder_available: Optional[bool] = None


def _get_cross_encoder():
    """
    Retorna instância do cross-encoder se disponível localmente.
    Nunca faz download — usa local_files_only=True.
    Retorna None silenciosamente se não disponível (ativa fallback batch).
    """
    global _cross_encoder_instance, _cross_encoder_available

    if _cross_encoder_available is True:
        return _cross_encoder_instance
    if _cross_encoder_available is False:
        return None

    try:
        from sentence_transformers import CrossEncoder
        _cross_encoder_instance = CrossEncoder(
            CROSS_ENCODER_MODEL,
            local_files_only=True,
        )
        _cross_encoder_available = True
        print(f"✅ Cross-encoder local carregado: '{CROSS_ENCODER_MODEL}'")
        return _cross_encoder_instance
    except ImportError:
        print("ℹ️  sentence-transformers não instalado → reranker LLM batch ativo.\n"
              "   Para instalar: pip install sentence-transformers")
        _cross_encoder_available = False
        return None
    except Exception:
        print(f"ℹ️  Cross-encoder não disponível localmente → reranker LLM batch ativo.\n"
              f"   Para baixar (requer internet uma vez):\n"
              f"   python -c \"from sentence_transformers import CrossEncoder; "
              f"CrossEncoder('{CROSS_ENCODER_MODEL}')\"")
        _cross_encoder_available = False
        return None


def _rerank_with_cross_encoder(query: str, hits: list, top_k: int, ce) -> list:
    """Reranking com cross-encoder local (rápido, preciso, offline após download)."""
    import math

    pairs      = [(query, (hit.payload or {}).get("text", "")[:512]) for hit in hits]
    raw_scores = ce.predict(pairs)

    def _sigmoid(x):
        return 1.0 / (1.0 + math.exp(-float(x)))

    scored = []
    for i, (raw, hit) in enumerate(zip(raw_scores, hits)):
        score  = round(_sigmoid(raw) * 10, 2)
        source = _hit_source(hit)
        page   = (hit.payload or {}).get("page", "N/A")
        print(f"  Chunk {i+1} [{source} p.{page}]: cross_score={score:.2f} (logit={raw:.3f})")
        scored.append((score, hit))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_hits = [hit for _, hit in scored[:top_k]]
    for score, hit in scored[:top_k]:
        if hit.payload is not None:
            hit.payload["_rerank_score"] = score
    return top_hits


# ---------------------------------------------------------------------------
# Reranker LLM em BATCH — 1 chamada para todos os chunks
# ---------------------------------------------------------------------------
# Vantagem vs pointwise anterior (N chamadas individuais):
#   Antes: 6 chunks × ~3s/chamada = ~18s só de reranking.
#   Agora: todos os chunks em 1 chamada = ~4-6s total.
# O LLM vê todos os chunks ao mesmo tempo e ranqueia relativamente,
# o que tende a ser mais preciso que pontuar cada um isoladamente.
# ---------------------------------------------------------------------------
_RERANK_BATCH_PROMPT = """Você é um sistema de avaliação de relevância para RAG técnico.
Abaixo estão {n} trechos de documentos numerados de 1 a {n}.
Avalie a relevância de CADA trecho para responder à pergunta.

Pergunta: {query}

{chunks_block}

INSTRUÇÕES:
- Responda APENAS com uma linha por trecho no formato: <número>:<pontuação>
- Pontuação de 1 a 10:  1-3 = irrelevante | 4-6 = parcialmente relevante | 7-10 = muito relevante
- Não escreva nada além das linhas número:pontuação.

Exemplo:
1:8
2:3
3:6"""


def _rerank_with_llm_batch(query: str, hits: list, top_k: int) -> list:
    """
    Reranking LLM em batch — envia todos os chunks em UMA única chamada.
    Ativo por padrão quando o cross-encoder não está disponível localmente.
    """
    chunks_lines = []
    for i, hit in enumerate(hits, 1):
        text = (hit.payload or {}).get("text", "")[:600]
        chunks_lines.append(f"[{i}] {text}")

    prompt = _RERANK_BATCH_PROMPT.format(
        n=len(hits),
        query=query,
        chunks_block="\n\n".join(chunks_lines),
    )

    response = ollama.chat(
        model=LLM,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0},
    )
    raw_output = (response["message"]["content"] or "").strip()

    # Parseia "número:pontuação" — tolerante a variações de formatação
    scores_map: dict[int, float] = {}
    for line in raw_output.splitlines():
        m = re.match(r"^\[?(\d+)\]?\s*[:\-]\s*(\d+(?:\.\d+)?)", line.strip())
        if m:
            scores_map[int(m.group(1))] = max(1.0, min(10.0, float(m.group(2))))

    scored = []
    for i, hit in enumerate(hits, 1):
        score  = scores_map.get(i, 3.0)
        source = _hit_source(hit)
        page   = (hit.payload or {}).get("page", "N/A")
        print(f"  Chunk {i} [{source} p.{page}]: batch_score={score:.1f}")
        scored.append((score, hit))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_hits = [hit for _, hit in scored[:top_k]]
    for score, hit in scored[:top_k]:
        if hit.payload is not None:
            hit.payload["_rerank_score"] = score
    return top_hits


_RERANK_PROMPT_TEMPLATE = """Você é um sistema de avaliação de relevância para um assistente técnico.
Dado a pergunta do usuário e um trecho de documento, avalie quão útil esse trecho é para RESPONDER a pergunta.
Responda APENAS com um número inteiro de 1 a 10.

Pergunta: {query}

Trecho:
{chunk}

Pontuação (apenas o número inteiro):"""


def _rerank_with_llm_pointwise(query: str, hits: list, top_k: int) -> list:
    """Reranking LLM pointwise — último fallback (N chamadas individuais)."""
    scored = []
    for i, hit in enumerate(hits):
        chunk_text = (hit.payload or {}).get("text", "")
        if not chunk_text:
            scored.append((0.0, hit))
            continue
        prompt = _RERANK_PROMPT_TEMPLATE.format(query=query, chunk=chunk_text[:1500])
        try:
            response = ollama.chat(
                model=LLM,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0},
            )
            raw    = (response["message"]["content"] or "").strip()
            digits = re.search(r"\d+(?:\.\d+)?", raw)
            score  = float(digits.group()) if digits else 1.0
            score  = max(1.0, min(10.0, score))
        except Exception as e:
            print(f"  ⚠️ Pointwise falhou chunk {i+1}: {e}")
            score = getattr(hit, "score", 0.0) * 10
        source = _hit_source(hit)
        page   = (hit.payload or {}).get("page", "N/A")
        print(f"  Chunk {i+1} [{source} p.{page}]: pointwise_score={score:.1f}")
        scored.append((score, hit))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_hits = [hit for _, hit in scored[:top_k]]
    for score, hit in scored[:top_k]:
        if hit.payload is not None:
            hit.payload["_rerank_score"] = score
    return top_hits


def _rerank_hits(query: str, hits: list, top_k: int = RERANK_TOP_K) -> list:
    """
    Orquestra reranking com prioridade decrescente de qualidade/velocidade:

      1. Cross-encoder local (sentence-transformers)
         Melhor qualidade (~50-200ms). Requer modelo em cache local.

      2. LLM batch (granite4, 1 chamada) ← PADRÃO quando sem cross-encoder
         Boa qualidade (~4-6s). Sem dependências extras.

      3. LLM pointwise (granite4, N chamadas)
         Qualidade ok, lento (~20-60s). Fallback se batch falhar.

      4. Score vetorial original
         Sem reranking. Garantia de que o pipeline nunca quebra.
    """
    if not hits:
        return hits

    # Nível 1: cross-encoder local
    ce = _get_cross_encoder()
    if ce is not None:
        print(f"🔀 Reranking {len(hits)} chunks com cross-encoder → top {top_k}...")
        try:
            result = _rerank_with_cross_encoder(query, hits, top_k, ce)
            print(f"✅ Cross-encoder concluído. {len(result)} chunks.")
            return result
        except Exception as e:
            print(f"⚠️ Cross-encoder falhou: {e} → LLM batch...")

    # Nível 2: LLM batch (padrão sem cross-encoder)
    print(f"🔀 Reranking {len(hits)} chunks com LLM batch → top {top_k}...")
    try:
        result = _rerank_with_llm_batch(query, hits, top_k)
        print(f"✅ LLM batch concluído. {len(result)} chunks.")
        return result
    except Exception as e:
        print(f"⚠️ LLM batch falhou: {e} → pointwise...")

    # Nível 3: LLM pointwise
    print(f"🔀 Reranking {len(hits)} chunks com LLM pointwise → top {top_k}...")
    try:
        result = _rerank_with_llm_pointwise(query, hits, top_k)
        print(f"✅ LLM pointwise concluído. {len(result)} chunks.")
        return result
    except Exception as e:
        print(f"⚠️ Todos rerankers falharam: {e} → score vetorial.")

    # Nível 4: score vetorial (fallback final)
    hits_sorted = sorted(hits, key=lambda h: getattr(h, "score", 0.0), reverse=True)
    fallback    = hits_sorted[:top_k]
    for hit in fallback:
        if hit.payload is not None:
            hit.payload["_rerank_score"] = round(getattr(hit, "score", 0.0) * 10, 2)
    return fallback


# ---------------------------------------------------------------------------
# Tools MCP
# ---------------------------------------------------------------------------

@mcp.tool
def index_document(file_path: str, filename: str, document_type: str = "desconhecido") -> str:
    """
    Indexa um documento (PDF ou imagem) no Qdrant.

    Para imagens: aplica OCR com timeout e redimensionamento automático.
    Se o OCR retornar timeout/aviso mas ainda assim extraiu algum texto útil,
    o documento é indexado parcialmente. Se não extraiu nada, retorna erro
    sem deletar o arquivo (o usuário pode retentar).
    """
    # [V4] Sanitiza filename antes de qualquer operação
    safe_name = _safe_filename(filename)
    print(f"\n{'='*50}")
    print(f"📥 Indexando: {safe_name} (tipo: {document_type})")
    print(f"{'='*50}")
    file_extension = os.path.splitext(safe_name)[1].lower()

    try:
        if file_extension == ".pdf":
            text = get_text_from_pdf(file_path, ocr)
        elif file_extension in [".png", ".jpg", ".jpeg"]:
            text = get_text_from_image(file_path, ocr)
            # Log do texto extraído (truncado para não poluir o console)
            preview = text[:500] + "..." if len(text) > 500 else text
            print(f"📝 Texto extraído da imagem (prévia):\n{preview}")
        elif file_extension == ".docx":
            text = get_text_from_docx(file_path)
        elif file_extension == ".txt":
            text = get_text_from_txt(file_path)
        elif file_extension == ".csv":
            text = get_text_from_csv(file_path)
        else:
            error_msg = f"Formato de arquivo não suportado: {file_extension}"
            print(error_msg)
            _delete_file_on_error(file_path, safe_name, error_msg)
            return error_msg

        # Verifica se o OCR retornou apenas mensagem de timeout/erro sem conteúdo útil
        text_stripped = text.strip()
        is_ocr_warning = text_stripped.startswith("⚠️") or text_stripped.startswith("❌")
        has_useful_content = len(text_stripped) > 100 and not is_ocr_warning

        if not text_stripped:
            error_msg = f"Nenhum texto extraído de '{safe_name}'."
            print(error_msg)
            _delete_file_on_error(file_path, safe_name, error_msg)
            return error_msg

        if is_ocr_warning and not has_useful_content:
            # OCR travou ou falhou completamente — deleta o arquivo porque:
            # 1) O arquivo não foi indexado (inútil no uploads_mcp sem indexação)
            # 2) O cliente pode ter sofrido timeout e não conseguiu limpar
            # 3) O usuário pode reenviar após corrigir o problema
            _delete_file_on_error(file_path, safe_name, f"OCR falhou: {text_stripped[:80]}")
            warning_msg = (
                f"⚠️ '{safe_name}' não pôde ser indexado: {text_stripped}\n\n"
                "O arquivo foi removido automaticamente. "
                "Corrija o problema e envie novamente."
            )
            print(warning_msg)
            return warning_msg

        # [MELHORIA] Chunking semântico — respeita fronteiras de sentença
        chunks = get_semantic_chunks(text, source_hint=safe_name)

        # Fallback: se o chunking semântico retornar vazio, usa por caracteres
        if not chunks:
            print("⚠️ Chunking semântico retornou vazio — usando fallback por caracteres.")
            chunks = get_text_chunks(text)

        points = []
        for chunk_idx, chunk in enumerate(chunks):
            embedding = get_embeddings(chunk)
            if embedding:
                points.append(
                    models.PointStruct(
                        id=str(uuid.uuid4()),
                        vector=embedding,
                        payload={
                            "text":          chunk,
                            "source":        safe_name,
                            "document_type": document_type,
                            # Metadados de chunk para rastreabilidade
                            "chunk_index":   chunk_idx,
                            "chunk_total":   len(chunks),
                            "chunk_tokens":  _estimate_tokens(chunk),
                        },
                    )
                )

        if points:
            try:
                qdrant_client.upsert(collection_name=COLLECTION_NAME, points=points, wait=True)
                # Se o OCR teve aviso mas ainda gerou conteúdo, informa o usuário
                suffix = " ⚠️ (OCR parcial — qualidade pode ser inferior)" if is_ocr_warning else ""
                msg = (
                    f"✅ Documento '{safe_name}' (tipo: {document_type}) indexado com "
                    f"{len(points)} chunks.{suffix}"
                )
                print(msg)
                return msg
            except Exception as e:
                error_msg = f"Erro ao inserir dados no Qdrant: {e}"
                print(error_msg)
                _delete_file_on_error(file_path, safe_name, error_msg)
                return error_msg
        else:
            error_msg = f"Não foi possível gerar embeddings para '{safe_name}'."
            print(error_msg)
            _delete_file_on_error(file_path, safe_name, error_msg)
            return error_msg

    except Exception as e:
        error_msg = f"Erro inesperado durante indexação de '{safe_name}': {e}"
        print(error_msg)
        traceback.print_exc()
        _delete_file_on_error(file_path, safe_name, error_msg)
        return error_msg


@mcp.tool
def confirmar_resposta(pergunta: str, resposta: str) -> str:
    try:
        print(f"Confirmando resposta — Pergunta: {pergunta[:100]}...")
        # [FIX-1] Normaliza antes de embeddar para garantir que o embedding
        # salvo no cache seja idêntico ao que será gerado na próxima busca.
        # Sem isso, "Quais RMAs têm ECC?" e "quais rmas têm ecc" geravam
        # embeddings ligeiramente diferentes e não acertavam o cache.
        embedding = get_embeddings(_normalize_query(pergunta))
        if not embedding:
            return "Erro ao gerar embedding para pergunta."

        qdrant_client.upsert(
            collection_name=CACHE_COLLECTION,
            points=[models.PointStruct(
                id=str(uuid.uuid4()),
                vector=embedding,
                payload={"pergunta": pergunta, "resposta": resposta},
            )],
            wait=True
        )
        print("✅ Resposta confirmada e salva no cache.")
        return "Resposta salva no cache com sucesso."
    except Exception as e:
        print(f"❌ Erro ao salvar no cache: {e}")
        traceback.print_exc()
        return f"Erro ao salvar no cache: {e}"


@mcp.tool
def registrar_feedback(
    pergunta: str,
    prefer_document_types: Optional[List[str]] = None,
    avoid_document_types:  Optional[List[str]] = None,
    prefer_sources:        Optional[List[str]] = None,
    avoid_sources:         Optional[List[str]] = None,
    must_keywords:         Optional[List[str]] = None,
    query_rewrite:         str = "",
    note:                  str = ""
) -> str:
    """
    Aprende com feedback do usuário: armazena embedding da pergunta e preferências
    de retrieval (tipos/fontes a preferir ou evitar, keywords obrigatórias, reescrita).
    """
    try:
        if not pergunta or not pergunta.strip():
            return "Pergunta vazia. Nada foi salvo."

        # [FIX-1] Mesma normalização aplicada no cache e no ask_question,
        # garantindo que o embedding de feedback seja comparável aos demais.
        emb = get_embeddings(_normalize_query(pergunta))
        if not emb:
            return "Erro: não foi possível gerar embedding para salvar o feedback."

        payload: Dict[str, Any] = {
            "pergunta":              pergunta.strip(),
            "prefer_document_types": [x.strip().lower() for x in (prefer_document_types or []) if x and x.strip()],
            "avoid_document_types":  [x.strip().lower() for x in (avoid_document_types  or []) if x and x.strip()],
            "prefer_sources":        [x.strip() for x in (prefer_sources or []) if x and x.strip()],
            "avoid_sources":         [x.strip() for x in (avoid_sources  or []) if x and x.strip()],
            "must_keywords":         [x.strip() for x in (must_keywords  or []) if x and x.strip()],
            "query_rewrite":         (query_rewrite or "").strip(),
            "note":                  note or "",
        }

        qdrant_client.upsert(
            collection_name=FEEDBACK_COLLECTION,
            points=[models.PointStruct(
                id=str(uuid.uuid4()),
                vector=emb,
                payload=payload
            )],
            wait=True
        )
        print("✅ Feedback salvo:", payload)
        return "Feedback salvo ✅ Vou usar essas preferências em perguntas similares."
    except Exception as e:
        print(f"❌ Erro ao salvar feedback: {e}")
        traceback.print_exc()
        return f"Erro ao salvar feedback: {e}"


@mcp.tool
def list_document_types() -> List[str]:
    """Retorna todos os valores distintos de `document_type` existentes na coleção principal."""
    try:
        types  = set()
        offset = None
        pages  = 0  # [V6] contador de segurança

        while pages < MAX_SCROLL_PAGES:
            points, offset = qdrant_client.scroll(
                collection_name=COLLECTION_NAME,
                limit=512,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for p in points:
                dt = (p.payload or {}).get("document_type")
                if dt:
                    types.add(str(dt).strip().lower())
            pages += 1
            if offset is None:
                break

        return sorted(types)
    except Exception as e:
        print(f"❌ Erro list_document_types: {e}")
        return []


@mcp.tool
def list_sources() -> List[str]:
    """Retorna todos os valores distintos de `source` (nomes de arquivos) existentes na coleção principal."""
    try:
        sources = set()
        offset  = None
        pages   = 0  # [V6] contador de segurança

        while pages < MAX_SCROLL_PAGES:
            points, offset = qdrant_client.scroll(
                collection_name=COLLECTION_NAME,
                limit=512,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for p in points:
                src = (p.payload or {}).get("source")
                if src:
                    sources.add(str(src).strip())
            pages += 1
            if offset is None:
                break

        return sorted(sources, key=lambda s: s.lower())
    except Exception as e:
        print(f"❌ Erro list_sources: {e}")
        return []


def _delete_cache_entries_that_reference_filename(filename: str) -> int:
    """Remove entradas do CACHE_COLLECTION onde a 'resposta' contém o nome do arquivo."""
    deleted     = 0
    next_offset = None
    pages       = 0  # [V6]

    while pages < MAX_SCROLL_PAGES:
        points, next_offset = qdrant_client.scroll(
            collection_name=CACHE_COLLECTION,
            limit=256,
            with_vectors=False,
            with_payload=True,
            offset=next_offset,
        )
        if not points:
            break

        to_delete_ids = [
            p.id for p in points
            if filename in ((p.payload or {}).get("resposta", "") or "")
        ]

        if to_delete_ids:
            qdrant_client.delete(
                collection_name=CACHE_COLLECTION,
                points_selector=models.PointIdsList(points=to_delete_ids),
                wait=True,
            )
            deleted += len(to_delete_ids)

        pages += 1
        if next_offset is None:
            break

    return deleted


def _delete_file_on_error(file_path: str, filename: str, error_reason: str) -> None:
    """Deleta o arquivo da pasta uploads_mcp em caso de erro durante indexação."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"❌ Arquivo '{filename}' deletado automaticamente. Motivo: {error_reason}")
        else:
            print(f"⚠️ Arquivo '{filename}' não encontrado em {file_path}")
    except Exception as e:
        print(f"⚠️ Erro ao deletar arquivo '{filename}': {e}")


@mcp.tool
def delete_document(filename: str) -> str:
    """Exclui o arquivo físico, remove embeddings do Qdrant e invalida o cache."""
    # [V4] Sanitiza antes de operar
    safe_name = _safe_filename(filename)
    try:
        file_path = os.path.join(UPLOAD_DIR, safe_name)
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"✅ Arquivo físico removido: {file_path}")
        else:
            print(f"⚠️ Arquivo físico não encontrado: {file_path}")

        try:
            qdrant_client.delete(
                collection_name=COLLECTION_NAME,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[models.FieldCondition(key="source", match=models.MatchValue(value=safe_name))]
                    )
                ),
                wait=True,
            )
            print(f"✅ Embeddings removidos para source='{safe_name}'")
        except Exception as e:
            print(f"⚠️ Falha ao remover embeddings do Qdrant: {e}")

        try:
            removed_cache = _delete_cache_entries_that_reference_filename(safe_name)
            print(f"✅ Entradas removidas do cache: {removed_cache}")
        except Exception as e:
            print(f"⚠️ Falha ao varrer/remover cache: {e}")
            removed_cache = 0

        return f"Documento '{safe_name}' excluído. Embeddings removidos e {removed_cache} entrada(s) do cache invalidada(s)."
    except Exception as e:
        return f"❌ Erro ao excluir documento '{safe_name}': {e}"


# ---------------------------------------------------------------------------
# Tool principal: ask_question (pipeline RAG com Reranking)
# ---------------------------------------------------------------------------
@mcp.tool
def ask_question(query: str, force_fresh: bool = False, chat_history: Optional[List[Dict[str, str]]] = None) -> str:
    """
    Pipeline completo:
      0. Busca feedback aprendido (preferências do usuário)
      1. Verifica cache semântico (respostas confirmadas)
      2. [MELHORIA] HyDE: gera resposta hipotética para melhorar o embedding de busca
      3. Busca vetorial no Qdrant (candidatos amplos)
      4. Reranking via LLM (seleciona os chunks mais relevantes)
      5. Aplica filtros de feedback (prefer/avoid)
      6. Fallback de busca expandida se nenhum chunk passar
      7. Geração da resposta com o LLM (com histórico de conversa)

    Args:
        query:        Pergunta do usuário.
        force_fresh:  Se True, ignora o cache semântico.
        chat_history: Histórico de conversa [{role, content}, ...] para contexto multi-turno.
    """
    print(f"\n{'='*60}")
    print(f"Pergunta recebida: {query} | force_fresh={force_fresh}")
    print(f"{'='*60}")

    if not query or not query.strip():
        return "Por favor, faça uma pergunta."

    document_type, clean_query = extract_document_type_from_query(query)
    print(f"Tipo de documento extraído do prompt: {document_type}")

    # [MELHORIA] Normaliza a query para melhorar o acerto do cache semântico
    normalized_query = _normalize_query(clean_query)

    query_embedding = get_embeddings(normalized_query)
    if not query_embedding:
        return "❌ Não foi possível gerar o embedding para a sua pergunta. Verifique se o Ollama está rodando."

    # ------------------------------------------------------------------
    # 0) Busca feedback aprendido (preferências de retrieval)
    # ------------------------------------------------------------------
    feedback_payload: Optional[Dict[str, Any]] = None
    try:
        fb_hits = qdrant_client.search(
            collection_name=FEEDBACK_COLLECTION,
            query_vector=query_embedding,
            limit=1,
            score_threshold=FEEDBACK_THRESHOLD,
        )
        if fb_hits:
            feedback_payload = fb_hits[0].payload or {}
            print("🧠 Feedback aplicado (pergunta similar):", (feedback_payload.get("pergunta") or "")[:80])
    except Exception as e:
        print(f"⚠️ Erro ao buscar feedback: {e}")

    # ------------------------------------------------------------------
    # 1) Cache semântico — ignorado se force_fresh
    # ------------------------------------------------------------------
    if not force_fresh:
        try:
            cache_hits = qdrant_client.search(
                collection_name=CACHE_COLLECTION,
                query_vector=query_embedding,
                limit=1,
                score_threshold=CACHE_SCORE_THRESHOLD,  # [V8] threshold consolidado
            )
            if cache_hits:
                hit   = cache_hits[0]
                score = getattr(hit, "score", 1.0)
                if score >= CACHE_SCORE_THRESHOLD:
                    cache_payload   = hit.payload
                    resposta_cache  = cache_payload.get("resposta")
                    pergunta_cache  = cache_payload.get("pergunta")
                    print(f"💾 Cache HIT! Pergunta similar: {pergunta_cache}")
                    cache_response  = f"(Resposta do cache)\n{resposta_cache}"
                    if "📚 **Fontes consultadas:**" not in resposta_cache:
                        cache_response += "\n\n💾 **Esta resposta foi recuperada do cache de perguntas anteriores**"
                    return cache_response
        except Exception as e:
            print(f"⚠️ Erro ao buscar no cache: {e}")
    else:
        print("⏭️ Ignorando cache por solicitação (force_fresh=True).")

    # ------------------------------------------------------------------
    # 2) [MELHORIA] HyDE — Hypothetical Document Embeddings
    #
    # O embedding da query original ("Quais etapas do RMA?") representa
    # uma PERGUNTA. O embedding de um documento no Qdrant representa uma
    # RESPOSTA. Essa diferença de estilo semântico reduz a similaridade
    # coseno mesmo quando o documento responde diretamente.
    #
    # HyDE contorna isso: geramos uma resposta hipotética curta com o LLM,
    # calculamos o embedding DESSA resposta e usamos para buscar no Qdrant.
    # O embedding da resposta hipotética fica muito mais próximo dos chunks
    # reais, aumentando o recall sem custo extra de indexação.
    #
    # Se o HyDE falhar (LLM lento ou sem resposta), usa o embedding original.
    # ------------------------------------------------------------------
    search_embedding = query_embedding  # fallback: usa a query original
    try:
        hyde_prompt = (
            f"Você é um especialista técnico em processos RMA e sistemas iRMA. "
            f"Escreva um parágrafo curto (máximo 5 frases) que seria uma resposta típica "
            f"encontrada em um manual ou relatório técnico para a seguinte pergunta:\n\n"
            f"Pergunta: {clean_query}\n\n"
            f"Resposta técnica hipotética:"
        )
        hyde_response = ollama.chat(
            model=LLM,
            messages=[{"role": "user", "content": hyde_prompt}],
            options={"temperature": 0.3},
        )
        hyde_text = (hyde_response["message"]["content"] or "").strip()
        if hyde_text and len(hyde_text) > 30:
            hyde_embedding = get_embeddings(hyde_text)
            if hyde_embedding:
                search_embedding = hyde_embedding
                print(f"🔮 HyDE ativo — embedding gerado a partir de resposta hipotética ({len(hyde_text)} chars)")
    except Exception as hyde_err:
        print(f"⚠️ HyDE falhou, usando embedding original: {hyde_err}")

    # ------------------------------------------------------------------
    # 3) Busca vetorial principal (candidatos amplos para o reranker)
    # ------------------------------------------------------------------
    try:
        hits = qdrant_client.search(
            collection_name=COLLECTION_NAME,
            query_vector=search_embedding,
            limit=RERANK_CANDIDATE_LIMIT,
            score_threshold=RAG_LOW_THRESHOLD,
        )
        relevant_hits = [h for h in hits if getattr(h, "score", 0) >= RAG_HIGH_THRESHOLD]
        if not relevant_hits:
            relevant_hits = [h for h in hits if getattr(h, "score", 0) >= RAG_LOW_THRESHOLD]
            print(f"Threshold baixo (0.3) — {len(relevant_hits)} chunks candidatos")
        else:
            print(f"Threshold alto (0.5) — {len(relevant_hits)} chunks candidatos")

        # --------------------------------------------------------------
        # 3) [V10] RERANKING — seleciona os chunks mais relevantes
        # --------------------------------------------------------------
        if relevant_hits:
            try:
                relevant_hits = _rerank_hits(clean_query, relevant_hits, top_k=RERANK_TOP_K)
            except Exception as rerank_err:
                # Degradação graciosa: se o reranking falhar, usa os hits originais
                print(f"⚠️ Reranking falhou — usando hits vetoriais originais. Erro: {rerank_err}")

        # --------------------------------------------------------------
        # [NOVO] Gate de relevância pós-reranking
        # Se o melhor chunk não atingir score mínimo, os documentos
        # disponíveis não têm contexto suficiente para responder.
        # Retorna "não sei" sem chamar o LLM, evitando alucinações.
        # Ajuste MIN_RERANK_SCORE_TO_ANSWER conforme necessário:
        #   >= 5.0 → mais restritivo (recomendado para domínios fechados)
        #   >= 4.0 → equilibrado (padrão)
        #   >= 3.0 → mais permissivo
        # --------------------------------------------------------------
        MIN_RERANK_SCORE_TO_ANSWER = 4.0  # escala 1-10

        if relevant_hits:
            best_rerank_score = max(
                float((h.payload or {}).get("_rerank_score", 0.0))
                for h in relevant_hits
            )
            print(f"🎯 Melhor rerank_score pós-reranking: {best_rerank_score:.1f}")
            if best_rerank_score < MIN_RERANK_SCORE_TO_ANSWER:
                print(
                    f"⚠️ rerank_score={best_rerank_score:.1f} abaixo do mínimo "
                    f"({MIN_RERANK_SCORE_TO_ANSWER}) — contexto insuficiente, abortando geração."
                )
                return (
                    "Não encontrei informações suficientes nos documentos disponíveis "
                    "para responder a essa pergunta. Adicione mais documentos relevantes ou revise sua pergunta para obter uma resposta melhor."
                )

        # --------------------------------------------------------------
        # 4) Aplica filtros de feedback (prefer/avoid)
        # --------------------------------------------------------------
        if feedback_payload and relevant_hits:
            prefer_types   = set((feedback_payload.get("prefer_document_types") or []))
            avoid_types    = set((feedback_payload.get("avoid_document_types")  or []))
            prefer_sources = set((feedback_payload.get("prefer_sources")        or []))
            avoid_sources  = set((feedback_payload.get("avoid_sources")         or []))

            # ----------------------------------------------------------
            # REGRA HARD: remove o que foi explicitamente evitado.
            # Sem exceção — se o usuário pediu para evitar, evita SEMPRE.
            # [FIX-A] O bug anterior separava high_score_hits ANTES de
            # aplicar o avoid, fazendo chunks com rerank >= 7.0 passarem
            # mesmo estando em avoid_sources ou avoid_types.
            # Correção: avoid é aplicado primeiro, sobre TODOS os hits,
            # e só depois separamos high/low para o prefer-filter (soft).
            # ----------------------------------------------------------
            filtered = [
                h for h in relevant_hits
                if not (_hit_source(h)   in avoid_sources and avoid_sources)
                and not (_hit_doc_type(h) in avoid_types   and avoid_types)
            ]

            if len(filtered) < len(relevant_hits):
                removed = len(relevant_hits) - len(filtered)
                print(f"🚫 Avoid-filter removeu {removed} chunk(s) — "
                      f"avoid_sources={avoid_sources}, avoid_types={avoid_types}")

            # ----------------------------------------------------------
            # REGRA SOFT: prefer_types / prefer_sources.
            # Aplicada SOMENTE sobre os chunks que passaram pelo avoid.
            # Chunks com rerank_score >= RERANK_PREFER_BYPASS são
            # preservados do prefer-filter, mas NUNCA do avoid-filter.
            # ----------------------------------------------------------
            def _rerank_score(h) -> float:
                return float((h.payload or {}).get("_rerank_score", 0.0))

            # Chunks com score alto: imunes ao prefer-filter (mas já passaram pelo avoid)
            high_score_hits = [h for h in filtered if _rerank_score(h) >= RERANK_PREFER_BYPASS]
            # Chunks com score menor: sujeitos ao prefer-filter
            low_score_hits  = [h for h in filtered if _rerank_score(h) < RERANK_PREFER_BYPASS]

            if not document_type and prefer_types:
                only_pref = [h for h in low_score_hits if _hit_doc_type(h) in prefer_types]
                if only_pref:
                    low_score_hits = only_pref

            if prefer_sources:
                only_src = [h for h in low_score_hits if _hit_source(h) in prefer_sources]
                if only_src:
                    low_score_hits = only_src

            # Recombina: chunks de alta pontuação sempre presentes + baixos filtrados
            filtered = high_score_hits + [
                h for h in low_score_hits if h not in high_score_hits
            ]

            relevant_hits = filtered
            print(f"Após filtros de feedback: {len(relevant_hits)} chunks restantes "
                  f"({len(high_score_hits)} protegidos por score alto, "
                  f"{len(relevant_hits) - len(high_score_hits)} do prefer-filter)")

        context_payloads = [hit.payload for hit in relevant_hits]

        for i, hit in enumerate(relevant_hits[:5]):
            print(f"  Chunk {i+1}: {_hit_source(hit)} (score vetorial: {getattr(hit, 'score', 0):.3f})")

    except Exception as e:
        return f"❌ Erro ao buscar no Qdrant: {e}"

    # ------------------------------------------------------------------
    # Filtro por tipo explícito do prompt (tem prioridade sobre tudo)
    # ------------------------------------------------------------------
    if document_type:
        relevant_hits    = [h for h in relevant_hits if _hit_doc_type(h) == document_type]
        context_payloads = [h.payload for h in relevant_hits]
        if not context_payloads:
            return f"Não encontrei documentos do tipo '{document_type}' para responder."

    # ------------------------------------------------------------------
    # 5) Fallback de busca expandida (usa must_keywords / query_rewrite do feedback)
    # ------------------------------------------------------------------
    if not context_payloads and feedback_payload:
        must_keywords = [x.strip() for x in (feedback_payload.get("must_keywords") or []) if x and str(x).strip()]
        query_rewrite = (feedback_payload.get("query_rewrite") or "").strip()

        expanded_query = query_rewrite or clean_query
        if must_keywords:
            expanded_query = f"{expanded_query} {' '.join(must_keywords)}"

        try:
            expanded_emb = get_embeddings(expanded_query)
            if expanded_emb:
                hits2 = qdrant_client.search(
                    collection_name=COLLECTION_NAME,
                    query_vector=expanded_emb,
                    limit=RERANK_CANDIDATE_LIMIT,
                    score_threshold=0.3,  # [MELHORIA] era 0.15 — muito permissivo, aceitava chunks irrelevantes
                )
                fallback_hits = [h for h in hits2 if getattr(h, "score", 0) >= 0.2] or hits2

                # Reranking também no fallback
                if fallback_hits:
                    try:
                        fallback_hits = _rerank_hits(expanded_query, fallback_hits, top_k=RERANK_TOP_K)
                        # _rerank_hits já salva _rerank_score no payload de cada hit
                    except Exception:
                        pass

                avoid_types_fb    = set((feedback_payload.get("avoid_document_types") or []))
                avoid_sources_fb  = set((feedback_payload.get("avoid_sources")        or []))
                prefer_types_fb   = set((feedback_payload.get("prefer_document_types") or []))
                prefer_sources_fb = set((feedback_payload.get("prefer_sources")        or []))

                def _rerank_score_fb(h) -> float:
                    return float((h.payload or {}).get("_rerank_score", 0.0))

                def _is_high_score_fb(h) -> bool:
                    return _rerank_score_fb(h) >= RERANK_PREFER_BYPASS

                filtered2 = [
                    h for h in fallback_hits
                    if not (_hit_source(h)   in avoid_sources_fb and avoid_sources_fb)
                    and not (_hit_doc_type(h) in avoid_types_fb   and avoid_types_fb)
                ]
                if not document_type and prefer_types_fb:
                    only_pt = [h for h in filtered2 if _hit_doc_type(h) in prefer_types_fb]
                    if only_pt:
                        high_fb   = [h for h in filtered2 if h not in only_pt and _is_high_score_fb(h)]
                        filtered2 = only_pt + high_fb
                if prefer_sources_fb:
                    only_ps = [h for h in filtered2 if _hit_source(h) in prefer_sources_fb]
                    if only_ps:
                        high_fb   = [h for h in filtered2 if h not in only_ps and _is_high_score_fb(h)]
                        filtered2 = only_ps + high_fb

                relevant_hits    = filtered2
                context_payloads = [h.payload for h in filtered2]

                if context_payloads:
                    print(f"🔁 Fallback de busca funcionou: {len(context_payloads)} chunks recuperados")
        except Exception as e:
            print(f"⚠️ Erro no fallback de busca: {e}")

    if not context_payloads:
        return "Não encontrei informação relevante nos documentos para responder."

    # ------------------------------------------------------------------
    # Monta contexto e fontes
    # ------------------------------------------------------------------
    # [V7] Separador claro entre chunks para o LLM distinguir as fontes
    context_texts = [payload["text"] for payload in context_payloads]
    context_str   = "\n\n---\n\n".join(context_texts)

    source_info   = {}
    source_scores = {}
    for hit in relevant_hits:
        payload = hit.payload
        source  = payload.get("source", "")
        if not source:
            continue
        doc_type = payload.get("document_type", "desconhecido")
        score    = getattr(hit, "score", 0)
        if source not in source_info:
            source_info[source]   = doc_type
            source_scores[source] = score
        elif score > source_scores[source]:
            source_scores[source] = score

    used_sources = []
    for source, _ in sorted(source_scores.items(), key=lambda kv: kv[1], reverse=True):
        doc_type = source_info.get(source, "desconhecido")
        label    = f"{source} ({doc_type})" if doc_type != "desconhecido" else source
        used_sources.append(label)
        if len(used_sources) >= 12:
            break

    print(f"Fontes que serão usadas: {used_sources}")

    # ------------------------------------------------------------------
    # 7) Geração da resposta
    # ------------------------------------------------------------------
    # [MELHORIA] Prompt inteiramente em português com instruções de tom.
    # O granite4 performa melhor quando o prompt e o contexto estão no
    # mesmo idioma. Adicionamos instruções de tom: técnico, objetivo,
    # estruturado e honesto sobre limitações.
    system_prompt = """Você é um assistente técnico especializado em processos RMA e no sistema iRMA.
Seu papel é responder perguntas dos usuários com base exclusivamente nos documentos fornecidos.

Diretrizes de comportamento:
- Seja técnico, objetivo e preciso.
- Estruture respostas longas com subtítulos ou listas quando facilitar a leitura.
- Se a informação não estiver no contexto, diga claramente: "Não encontrei essa informação nos documentos disponíveis."
- Nunca invente informações ou extrapole além do que está nos documentos.
- Não mencione nomes de arquivos ou fontes dentro da resposta — as fontes serão listadas separadamente.
- Responda sempre em português do Brasil."""

    # [MELHORIA] Histórico de conversa: inclui as últimas N trocas para
    # permitir perguntas de acompanhamento ("e as etapas seguintes?").
    messages = [{"role": "system", "content": system_prompt}]

    if chat_history:
        # Inclui até as últimas 6 mensagens (3 trocas) para não inflar o contexto
        recent_history = chat_history[-6:]
        for turn in recent_history:
            role = turn.get("role", "user")
            content = turn.get("content", "")
            if role in ("user", "assistant") and content.strip():
                messages.append({"role": role, "content": content})

    user_message = f"""Com base no contexto abaixo, responda à pergunta do usuário.

Contexto dos documentos:
---
{context_str}
---

Pergunta: {clean_query}"""

    messages.append({"role": "user", "content": user_message})

    try:
        print("📤 Enviando prompt para o LLM...")
        response = ollama.chat(
            model=LLM,
            messages=messages,
            options={"temperature": 0.2},
        )
        # [V9] Trata possíveis problemas de encoding
        response_text = (response["message"]["content"] or "").encode("utf-8", errors="replace").decode("utf-8")

        if "Fontes:" in response_text:
            response_text = response_text.split("Fontes:")[0].strip()

        # [MELHORIA] Inclui rerank_scores no retorno para o front-end usar
        # no feedback silencioso (botão ✅). Formato: marcador oculto ao final.
        import json as _json
        rerank_scores_dict = {}
        for hit in relevant_hits:
            src = _hit_source(hit)
            score = float((hit.payload or {}).get("_rerank_score", 0.0))
            if src and score > rerank_scores_dict.get(src, 0.0):
                rerank_scores_dict[src] = score

        if used_sources:
            sources_text = "\n\n📚 **Fontes consultadas:**\n"
            for i, source in enumerate(used_sources, 1):
                # [MELHORIA] Indicador de confiança baseado no rerank_score
                filename = source.split(" (")[0].strip()
                rs = rerank_scores_dict.get(filename, 0.0)
                if rs >= 7.0:
                    confidence = "⭐⭐⭐"
                elif rs >= 4.0:
                    confidence = "⭐⭐"
                else:
                    confidence = "⭐"
                sources_text += f"{i}. 📄 {source} {confidence}\n"
            response_text += sources_text
        else:
            response_text += "\n\n⚠️ **Nenhuma fonte específica foi consultada**"

        # Anexa rerank_scores para o front-end (separado por marcador, invisível ao usuário)
        if rerank_scores_dict:
            response_text += f"\n\n__RERANK_SCORES__:{_json.dumps(rerank_scores_dict)}"

        print("✅ Resposta gerada.")
        return response_text
    except Exception as e:
        return f"❌ Erro ao gerar a resposta com o Ollama: {e}"


# =============================================================================
# [SESSÕES] Gerenciamento de sessões de chat persistidas no Qdrant
# =============================================================================

import json as _sessions_json
import datetime as _dt


def _session_embedding(name: str) -> list[float]:
    """Gera embedding do nome da sessão para indexação vetorial."""
    emb = get_embeddings(name)
    return emb if emb else [0.0] * VECTOR_SIZE


@mcp.tool
def gerar_nome_sessao(primeira_pergunta: str) -> str:
    """[SESSÕES] Gera nome curto (3-6 palavras) para a sessão com base na primeira pergunta."""
    prompt = (
        "Você é um assistente que cria títulos concisos para sessões de chat.\n"
        "Com base na pergunta abaixo, gere um título curto de 3 a 6 palavras em português "
        "que resume o assunto principal. Responda APENAS com o título, sem pontuação final, "
        "sem aspas, sem explicações.\n\n"
        f"Pergunta: {primeira_pergunta}\n\nTítulo:"
    )
    try:
        resp = ollama.chat(
            model=LLM,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.3},
        )
        name = (resp["message"]["content"] or "").strip().strip('"').strip("'")
        return name[:60] if name else "Nova sessão"
    except Exception as e:
        print(f"⚠️ Falha ao gerar nome de sessão: {e}")
        return "Nova sessão"


@mcp.tool
def salvar_sessao(
    session_id: str,
    name: str,
    messages: str,
    pinned: bool = False,
    created_at: str = "",
) -> str:
    """[SESSÕES] Cria ou atualiza sessão no Qdrant. messages = JSON string."""
    now = _dt.datetime.utcnow().isoformat()
    try:
        msgs = _sessions_json.loads(messages)
    except Exception:
        return "❌ Parâmetro 'messages' inválido."

    payload = {
        "session_id": session_id,
        "name":       name,
        "messages":   msgs,
        "pinned":     pinned,
        "created_at": created_at or now,
        "updated_at": now,
    }
    try:
        qdrant_client.upsert(
            collection_name=SESSIONS_COLLECTION,
            points=[models.PointStruct(
                id=session_id,
                vector=_session_embedding(name),
                payload=payload,
            )],
            wait=True,
        )
        return "ok"
    except Exception as e:
        return f"❌ Erro ao salvar sessão: {e}"


@mcp.tool
def listar_sessoes() -> str:
    """[SESSÕES] Retorna JSON com todas as sessões (pinadas primeiro, mais recentes primeiro)."""
    try:
        sessions = []
        offset, page = None, 0
        while page < MAX_SCROLL_PAGES:
            result, next_offset = qdrant_client.scroll(
                collection_name=SESSIONS_COLLECTION,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in result:
                p = point.payload or {}
                sessions.append({
                    "session_id":    p.get("session_id", str(point.id)),
                    "name":          p.get("name", "Sem título"),
                    "pinned":        p.get("pinned", False),
                    "created_at":    p.get("created_at", ""),
                    "updated_at":    p.get("updated_at", ""),
                    "message_count": len(p.get("messages", [])),
                })
            if next_offset is None:
                break
            offset, page = next_offset, page + 1

        sessions.sort(key=lambda s: (not s["pinned"], s["updated_at"]), reverse=False)
        sessions.sort(key=lambda s: s["updated_at"], reverse=True)
        sessions.sort(key=lambda s: not s["pinned"])
        return _sessions_json.dumps(sessions, ensure_ascii=False)
    except Exception as e:
        return _sessions_json.dumps({"error": str(e)})


@mcp.tool
def carregar_sessao(session_id: str) -> str:
    """[SESSÕES] Retorna JSON com payload completo (incluindo messages) de uma sessão."""
    try:
        results = qdrant_client.retrieve(
            collection_name=SESSIONS_COLLECTION,
            ids=[session_id],
            with_payload=True,
            with_vectors=False,
        )
        if not results:
            return _sessions_json.dumps({"error": "Sessão não encontrada."})
        return _sessions_json.dumps(results[0].payload or {}, ensure_ascii=False)
    except Exception as e:
        return _sessions_json.dumps({"error": str(e)})


@mcp.tool
def deletar_sessao(session_id: str) -> str:
    """[SESSÕES] Remove permanentemente uma sessão do Qdrant."""
    try:
        qdrant_client.delete(
            collection_name=SESSIONS_COLLECTION,
            points_selector=models.PointIdsList(points=[session_id]),
        )
        return "ok"
    except Exception as e:
        return f"❌ Erro ao deletar sessão: {e}"


@mcp.tool
def atualizar_sessao_meta(session_id: str, name: str = "", pinned: str = "") -> str:
    """[SESSÕES] Atualiza nome e/ou pin de uma sessão sem reescrever o histórico."""
    try:
        results = qdrant_client.retrieve(
            collection_name=SESSIONS_COLLECTION,
            ids=[session_id],
            with_payload=True,
            with_vectors=False,
        )
        if not results:
            return "❌ Sessão não encontrada."

        payload = dict(results[0].payload or {})
        changed = False
        new_embedding = None

        if name and name != payload.get("name"):
            payload["name"]       = name[:60]
            payload["updated_at"] = _dt.datetime.utcnow().isoformat()
            new_embedding         = _session_embedding(name)
            changed = True

        if pinned in ("true", "false"):
            payload["pinned"]     = (pinned == "true")
            payload["updated_at"] = _dt.datetime.utcnow().isoformat()
            changed = True

        if not changed:
            return "ok"

        qdrant_client.upsert(
            collection_name=SESSIONS_COLLECTION,
            points=[models.PointStruct(
                id=session_id,
                vector=new_embedding or _session_embedding(payload.get("name", "sessão")),
                payload=payload,
            )],
            wait=True,
        )
        return "ok"
    except Exception as e:
        return f"❌ Erro ao atualizar sessão: {e}"


if __name__ == "__main__":
    print("🚀 Iniciando servidor MCP em http://127.0.0.1:8002")
    mcp.run(transport="sse", host="127.0.0.1", port=8002)