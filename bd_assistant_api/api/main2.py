# Arquivo: api/main.py

import json
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.concurrency import run_in_threadpool

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Módulos locais com responsabilidades separadas
from .schemas import QuestionRequest, ChatResponse, ConfirmInteractionRequest
from .utils.vanna_instance import create_and_connect_vanna
from .utils.training import train_vanna_model
from .utils.memory2 import MemoryManager
from .utils.config import load_config
from .utils.report_generator import ReportGenerator


# -- INICIALIZAÇÃO DA API --
app = FastAPI(
    title="Zilla IA Backend",
    description="API para interagir com o modelo Vanna e gerar insights de dados.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Objetos globais para armazenar instâncias
vanna_instance = None
memory_manager = None

# -- CONFIGURAÇÃO DE RATE LIMITING --
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.on_event("startup")
def startup_event():
    """
    Orquestra a inicialização: cria, conecta e treina a instância do Vanna,
    e inicializa o gerenciador de memória.
    """
    global vanna_instance, memory_manager

    # Passo 1: Cria e conecta a instância do Vanna
    vanna_instance = create_and_connect_vanna()
    # Passo 2: Treina a instância conectada
    train_vanna_model(vanna_instance)

    # Passo 3: Inicializa o gerenciador de memória
    config = load_config()
    memory_manager = MemoryManager(chroma_path=config["chroma_memory_path"])

    # Verificação de persistência
    if memory_manager.is_using_persistent_storage():
        print("✅ PERSISTÊNCIA ATIVA: Os dados serão mantidos entre reinicializações")
    else:
        print("⚠️ ⚠️ ⚠️ AVISO CRÍTICO: Usando armazenamento efêmero (em memória)")
        print("⚠️ ⚠️ ⚠️ Todos os dados de memória serão PERDIDOS ao reiniciar a API!")
        print("⚠️ ⚠️ ⚠️ Para corrigir: Verifique as permissões do diretório 'data/chroma_db'")

    print("🚀 Instância do Vanna pronta para uso!")
    print("💾 Gerenciador de memória inicializado!")


# -- ENDPOINTS DA API --
@app.get("/", summary="Endpoint de saúde da API")
@limiter.limit("30/minute")
async def read_root(request: Request):
    return {"status": "Zilla IA API está online!"}


@app.get("/health", summary="Status detalhado da API")
@limiter.limit("30/minute")
async def health_check(request: Request):
    """Retorna status detalhado da API e persistência de memória"""
    global memory_manager

    persistence_status = (
        "ATIVO ✅"
        if memory_manager and memory_manager.is_using_persistent_storage()
        else "INATIVO ⚠️"
    )

    return {
        "status": "online",
        "vanna": "ativo" if vanna_instance else "falho",
        "memory_persistence": persistence_status,
        "message": "API Zilla IA funcionando normalmente",
    }


def _process_chat_sync(question: str) -> ChatResponse:
    """
    Executa o pipeline do /chat de forma SÍNCRONA (para rodar em threadpool).
    Isso evita bloquear o event loop do FastAPI.
    """
    global vanna_instance, memory_manager

    if vanna_instance is None:
        raise HTTPException(status_code=503, detail="Vanna AI não está inicializado.")

    response = ChatResponse(question=question)

    # ===== ETAPA 1: BUSCAR NA MEMÓRIA =====
    if memory_manager:
        similar_results = memory_manager.search_similar(question, top_k=3, threshold=0.65)

        if similar_results:
            best_match = similar_results[0]
            similarity = best_match.get("similarity", 0.0)
            metadata = best_match.get("metadata", {}) or {}

            print(f"🎯 Resultado similar encontrado na memória (similaridade: {similarity:.2%})")

            dynamic_threshold = memory_manager.get_threshold(question)
            print(f"📊 Threshold dinâmico para esta pergunta: {dynamic_threshold:.2%}")

            if similarity >= dynamic_threshold:
                response.sql = metadata.get("sql", "")
                response.summary = metadata.get("summary", "")
                response.dataframe = json.loads(metadata.get("dataframe", "[]"))
                response.from_memory = True
                response.memory_id = best_match.get("memory_id", "")
                response.similarity = similarity

                chart_json = metadata.get("chart", "")
                if chart_json:
                    try:
                        response.chart = json.loads(chart_json)
                    except Exception:
                        pass

                print(f"✅ Resposta recuperada do cache com {similarity:.2%} de similaridade")
                return response

            print(
                f"⚠️ Similaridade {similarity:.2%} abaixo do threshold {dynamic_threshold:.2%} - gerando nova resposta"
            )

    # ===== ETAPA 2: GERAR NOVA RESPOSTA =====
    print("🔄 Gerando nova resposta (não encontrado na memória ou similaridade baixa)")

    try:
        sql = vanna_instance.generate_sql(question=question, allow_llm_to_see_data=True)
    except Exception as llm_error:
        print(f"❌ Erro ao gerar SQL: {llm_error}")
        response.error = "Erro ao gerar SQL"
        response.summary = "Desculpe! Não foi possível gerar a consulta. Tente reformular a consulta."
        return response

    # Validar SQL
    if not sql or not isinstance(sql, str) or len(sql.strip()) < 5:
        response.summary = "Não consegui gerar uma consulta SQL válida."
        response.error = "SQL generation failed."
        return response

    sql_normalized = sql.upper().strip()
    if not any(keyword in sql_normalized for keyword in ["SELECT", "EXEC", "CALL"]):
        response.summary = (
            "Não encontrei dados. Tente novamente; se persistir, reformule a consulta."
        )
        response.error = "SQL generation failed - invalid syntax."
        return response

    response.sql = sql

    # ===== EXECUTAR SQL COM TIMEOUT =====
    try:
        print(f"⏱️ Executando SQL com timeout de {vanna_instance.sql_timeout}s...")
        df = vanna_instance.run_sql_with_timeout(sql=sql)
        print("✅ SQL executado com sucesso!")
    except TimeoutError as timeout_error:
        print(f"⏱️ TIMEOUT: {timeout_error}")
        response.error = "Query timeout"
        response.summary = (
            f"A consulta demorou mais de {vanna_instance.sql_timeout} segundos e foi cancelada. "
            f"Tente:\n"
            f"- Usar filtros mais específicos (ex: 'últimos 30 dias' em vez de 'todo histórico')\n"
            f"- Perguntar sobre um cliente específico\n"
            f"- Limitar os resultados (ex: 'top 10', 'últimos 5')"
        )
        return response
    except Exception as sql_error:
        print(f"❌ Erro ao executar SQL: {sql_error}")
        response.error = f"SQL execution error: {str(sql_error)}"
        response.summary = (
            "Desculpe! Não foi possível executar a consulta no banco de dados. "
            "Reformule a sua consulta e tente novamente."
        )
        return response

    if df is None or df.empty:
        response.summary = "A consulta foi executada, mas não retornou dados."
        return response

    # Prepara dados para resposta
    dataframe_json = json.dumps(json.loads(df.to_json(orient="records", date_format="iso")))
    response.dataframe = json.loads(dataframe_json)

    try:
        response.summary = vanna_instance.generate_summary(question=question, df=df)
    except Exception as summary_error:
        print(f"⚠️ Erro ao gerar resumo: {summary_error}")
        response.summary = "Dados retornados com sucesso, mas não foi possível gerar o resumo."

    # Gráfico
    if vanna_instance.should_generate_chart(df=df):
        try:
            plotly_code = vanna_instance.generate_plotly_code(question=question, sql=sql, df=df)
            if plotly_code:
                fig = vanna_instance.get_plotly_figure(plotly_code=plotly_code, df=df)
                if fig:
                    response.chart = json.loads(fig.to_json())
        except Exception as chart_error:
            print(f"⚠️ Erro ao gerar gráfico: {chart_error}")

    response.memory_id = None
    response.from_memory = False
    return response


@app.post("/chat", response_model=ChatResponse, summary="Processa uma pergunta")
@limiter.limit("10/minute")
async def handle_chat(request: Request, question_request: QuestionRequest):
    """
    Processa uma pergunta do usuário.

    Rate Limit: 10 requisições por minuto por IP
    """
    question = question_request.question

    try:
        # Executa o pipeline em threadpool para não bloquear o event loop
        return await run_in_threadpool(_process_chat_sync, question)
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Erro inesperado no /chat: {e}")
        import traceback

        traceback.print_exc()
        response = ChatResponse(question=question)
        response.error = str(e)
        response.summary = "Ocorreu um erro inesperado. Tente novamente."
        return response


@app.post("/confirm-interaction", summary="Confirma que uma interação foi útil ou não")
@limiter.limit("20/minute")
async def confirm_interaction(request: Request, confirm_request: ConfirmInteractionRequest):
    """
    Endpoint para confirmar se uma resposta foi útil ou não.
    - Se is_useful=true: SALVA a resposta na memória
    - Se is_useful=false: Descarta (não salva na memória)

    Rate Limit: 20 requisições por minuto por IP
    """
    global memory_manager

    if not memory_manager:
        raise HTTPException(status_code=503, detail="Memory Manager não está inicializado.")

    try:
        if confirm_request.is_useful:
            memory_id = memory_manager.save_interaction(
                question=confirm_request.question,
                sql=confirm_request.sql,
                dataframe_json=confirm_request.dataframe_json,
                summary=confirm_request.summary,
                chart_json=confirm_request.chart_json or "",
                is_confirmed=True,
            )

            print(f"💾 Interação salva na memória com ID: {memory_id}")

            return {
                "status": "success",
                "message": "Resposta marcada como útil e salva na memória!",
                "memory_id": memory_id,
            }

        print("❌ Resposta marcada como não útil (não foi salva na memória)")
        return {
            "status": "success",
            "message": "Resposta marcada como não útil e não foi salva.",
        }

    except Exception as e:
        print(f"Erro ao confirmar interação: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao processar confirmação: {str(e)}")


@app.get("/memory-stats", summary="Retorna estatísticas da memória")
@limiter.limit("30/minute")
async def memory_stats(request: Request):
    """
    Endpoint para obter estatísticas sobre a memória armazenada.

    Rate Limit: 30 requisições por minuto por IP
    """
    global memory_manager

    if not memory_manager:
        raise HTTPException(status_code=503, detail="Memory Manager não está inicializado.")

    return memory_manager.get_memory_stats()


@app.post("/generate-report", summary="Gera relatório da conversa")
@limiter.limit("5/minute")
async def generate_report(request: Request, report_request: dict):
    """
    Gera relatório inteligente baseado no histórico de conversa.

    Body:
    {
        "messages": [...],
        "format": "docx"  # "docx" ou "json"
    }
    """
    global vanna_instance

    if vanna_instance is None:
        raise HTTPException(status_code=503, detail="Vanna AI não está inicializado.")

    try:
        messages = report_request.get("messages", [])
        report_format = report_request.get("format", "docx").lower()

        if not messages:
            raise HTTPException(
                status_code=400,
                detail="Histórico de mensagens vazio. Não há dados para gerar relatório.",
            )

        if len(messages) < 2:
            raise HTTPException(
                status_code=400,
                detail="Histórico muito curto. Faça pelo menos uma pergunta antes de gerar o relatório.",
            )

        report_gen = ReportGenerator(vanna_instance)

        if report_format == "docx":
            print(f"🤖🔄📄 IA gerando relatório DOCX com {len(messages)} mensagens...")

            file_stream = report_gen.generate_docx_report(
                messages=messages,
                title="Relatório de Análise de Dados - Zilla IA",
            )

            filename = f"relatorio_zillia_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"

            return StreamingResponse(
                file_stream,
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )

        if report_format == "json":
            print(f"🤖🔄📄 IA gerando relatório JSON com {len(messages)} mensagens...")
            json_report = report_gen.generate_json_report(messages=messages)
            return {"status": "success", "format": "json", "report": json_report}

        raise HTTPException(
            status_code=400,
            detail=f"Formato '{report_format}' não suportado. Use 'docx' ou 'json'.",
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Erro ao gerar relatório: {e}")
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Erro ao gerar relatório: {str(e)}")
