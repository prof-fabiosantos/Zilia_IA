import streamlit as st
import requests
import pandas as pd
import plotly.graph_objects as go
import json
from PIL import Image
import base64
import time
import logging
from datetime import datetime

# No topo do arquivo
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# --- CONFIGURAÇÃO DA PÁGINA E ESTILO ---
st.set_page_config(
    page_title="Zilla IA - Data Insights",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- CONSTANTES ---
API_URL = "http://localhost:8001/chat"
CONFIRM_URL = "http://localhost:8001/confirm-interaction"
MEMORY_STATS_URL = "http://localhost:8001/memory-stats"
REPORT_URL = "http://localhost:8001/generate-report"


# --- Lê o arquivo da fonte e converte em base64 ---
try:
    with open("fonts/Orbitron-Medium.ttf", "rb") as f:
        font_base64 = base64.b64encode(f.read()).decode()
    
    # Injeta a fonte no estilo global
    st.markdown(f"""
        <style>
        @font-face {{
            font-family: 'Orbitron';
            src: url(data:font/ttf;base64,{font_base64}) format('truetype');
            font-weight: normal;
        }}
        h1 {{
            font-family: 'Orbitron', sans-serif !important;
        }}
        </style>
    """, unsafe_allow_html=True)
except FileNotFoundError:
    # Aplicação continua sem a fonte customizada
    print("⚠️ Fonte Orbitron não encontrada, usando fonte padrão")
except Exception as e:
    print(f"⚠️ Erro ao carregar fonte: {e}")

# --- Injeta a fonte no estilo global ---
st.markdown(f"""
    <style>
    @font-face {{
        font-family: 'Orbitron';
        src: url(data:font/ttf;base64,{font_base64}) format('truetype');
        font-weight: normal;
    }}
    h1 {{
        font-family: 'Orbitron', sans-serif !important;
    }}
    </style>
""", unsafe_allow_html=True)


# Estilo CSS customizado para aplicar a identidade visual
st.markdown("""
    <style>
        /* Cor da barra lateral */
        [data-testid="stSidebar"] {
            background-color: #4C2CCD !important;
        }
        /* Cor dos elementos na barra lateral */
        [data-testid="stSidebar"] * {
            color: white;
        }
        /* Estilo dos títulos */
        h1, h2, h3 {
            color: #4C2CCD;
        }
        /* Estilo do container de mensagens do assistente */
        
        [data-testid="stAppViewContainer"] { background-color: white !important; color: black !important; }
        .stMarkdown, .stText, .stChatMessageContent { color: black !important; }
        .stChatMessage > div { background-color: #f5f5f5 !important; color: black !important; }
        .stChatMessage:nth-child(even) > div { background-color: #ffffff !important; }    

        footer[data-testid="stChatInput"] { background-color: #4C2CCD !important; }
        [data-testid="stChatInput"] textarea { background-color: #4C2CCD !important; color: white !important; }
        [data-testid="stChatInput"] label { color: white !important; }
        ::placeholder { color: white !important; }   
        
        /* Estilo para badge de memória */
        .memory-badge {
            display: inline-block;
            background-color: #4C2CCD;
            color: white;
            padding: 5px 10px;
            border-radius: 5px;
            font-size: 0.85em;
            margin: 5px 0;
        }
    </style>
""", unsafe_allow_html=True)

st.session_state.messages = st.session_state.get("messages", [])


# Adicionar função (antes do loop de mensagens):
def generate_report():
    """Gera relatório da conversa atual."""
    try:
        if len(st.session_state.messages) < 2:
            st.warning("⚠️ Faça pelo menos uma pergunta antes de gerar o relatório!")
            return
        
        with st.spinner("🔄📄IA gerando relatório, por favor aguarde"):
            payload = {
                "messages": st.session_state.messages,
                "format": "docx"
            }
            
            response = requests.post(REPORT_URL, json=payload, timeout=90)
            
            if response.status_code == 429:
                st.error("⏳ Limite atingido. Aguarde 1 minuto.")
                return
            
            if response.status_code == 400:
                error_detail = response.json().get("detail", "Erro")
                st.error(f"❌ {error_detail}")
                return
            
            if response.status_code != 200:
                st.error(f"❌ Erro ao gerar relatório (código {response.status_code})")
                return
            
            filename = f"relatorio_zillia_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
            
            st.success("✅ Relatório gerado com sucesso!")
            
            st.download_button(
                label="📥 Baixar Relatório (DOCX)",
                data=response.content,
                file_name=filename,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            
    except requests.exceptions.Timeout:
        st.error("⏱️ Timeout na geração. Tente novamente.")
    except Exception as e:
        st.error(f"❌ Erro: {str(e)}")


def export_conversation():
    """Exporta conversa como JSON"""
    import json
   
    conversation = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "messages": st.session_state.messages
    }
    return json.dumps(conversation, indent=2, ensure_ascii=False)
    

# --- LAYOUT DA PÁGINA ---

# Barra Lateral
with st.sidebar:
    try:
        logo = Image.open('logo_zillia.png') 
        st.image(logo, width="stretch")  
    except FileNotFoundError:
        st.warning("⚠️ Logo não encontrado. Usando título texto.")
        st.title("📊 Zillia IA")
    except Exception as e:
        print(f"⚠️ Erro ao carregar logo: {e}")
        st.title("📊 Zillia IA")
    
    st.subheader("Como funciona?")
    st.markdown("""
    1.  **Faça uma consulta** sobre os dados no campo de chat.
    2.  A IA irá interpretar a sua consulta.
    3.  Se encontrar uma resposta similar na memória, mostrará.
    4.  Caso contrário, executará no banco de dados.
    5.  Confirme se a resposta foi útil para melhorar o sistema!
    """)

    # Exibe estatísticas da memória
    st.markdown("---")
    st.subheader("💾 Estatísticas da Memória")
    try:
        response = requests.get(MEMORY_STATS_URL, timeout=5)
        response.raise_for_status()
        memory_stats = response.json()
        
        col1, col2 = st.columns(2)
        with col1:
            st.metric("📚 Total", memory_stats.get("total_memories", 0))
        with col2:
            st.metric("✅ Confirmadas", memory_stats.get("confirmed_memories", 0))
        
    except requests.exceptions.Timeout:
        st.warning("⏱️ Timeout ao carregar estatísticas (API lenta)")
    except requests.exceptions.ConnectionError:
        st.warning("🔌 API offline - estatísticas indisponíveis")
    except requests.exceptions.HTTPError as e:
        st.warning(f"⚠️ Erro na API: {e.response.status_code}")
    except Exception as e:
        st.warning(f"⚠️ Erro inesperado: {type(e).__name__}")
        print(f"Erro detalhado: {e}")

    
    st.markdown("---")
    st.subheader("🛠️ Ferramentas")
    
    if st.button("📄 Gerar Relatório"):
        generate_report()
    
    st.caption("💡 Gere relatório inteligente com insights e recomendações.")     

    # Na sidebar
    if st.button("🗑️ Limpar Conversa"):
        st.session_state.messages = [{
            "role": "assistant",
            "content": "Olá! Como posso ajudar você a analisar seus dados hoje?"
        }]
        st.session_state.response_cache = {}
        st.session_state.evaluated_responses = set()
        st.rerun()

    st.caption("💡 Limpe as conversas antigas para manter o sistema performático.")

    if st.download_button(
        label="💾 Exportar Conversa",
        data=export_conversation(),
        file_name=f"conversa_{time.strftime('%Y%m%d_%H%M%S')}.json",
        mime="application/json"
    ):
        st.success("✅ Conversa exportada!")

    st.caption("💡 Exporte a conversa atual para um arquivo JSON.")

# Título Principal
st.markdown("<h1 style='text-align: center; font-size: 2.5em; color: #4C2CCD;'>🤖 Assistente de Análise de Dados</h1>", unsafe_allow_html=True)
st.markdown("<h3 style='text-align: center; font-size: 1em; color: #4C2CCD;'>Essa IA responde a consultas e gera gráficos a partir do banco de dados do iRMA.</h3>", unsafe_allow_html=True)
st.markdown("<h3 style='text-align: center; font-size: 1em; color: #000000;'>Exemplos de solicitações:<br> Quais solicitações foram criadas para o cliente Beta Soluções S.A? <br> Gere um gráfico de pizza para somente os 2 tipos de solicitações de RMA: Devolução, Garantia <br>Liste as RMAs que foram abertas, mas não tiveram nenhuma atualização de status  </h3>", unsafe_allow_html=True)

# --- LÓGICA DO CHAT ---

# Inicializa o histórico do chat na sessão
if "messages" not in st.session_state:
    st.session_state.messages = [{
        "role": "assistant",
        "content": "Olá! Como posso ajudar você a analisar seus dados hoje?"
    }]

# Rastreia se feedback foi enviado para a resposta atual
if "new_response_evaluated" not in st.session_state:
    st.session_state.new_response_evaluated = False

# Rastreia última resposta para feedback
if "last_response_data" not in st.session_state:
    st.session_state.last_response_data = None

# Armazena respostas por índice para acesso aos botões de feedback
if "response_cache" not in st.session_state:
    st.session_state.response_cache = {}

# Rastreia quais respostas já foram avaliadas (feedback enviado)
if "evaluated_responses" not in st.session_state:
    st.session_state.evaluated_responses = set()

# Função helper para enviar feedback de uma resposta
def send_feedback(response_data: dict, is_useful: bool):
    """
    Envia feedback para a API
    - Se útil: salva a resposta na memória
    - Se não útil: apenas descarta
    """
    try:
        # Validar dados básicos
        if not response_data or not response_data.get("question"):
            st.error("❌ Erro: Dados da resposta incompletos")
            return False
        
        payload = {
            "is_useful": is_useful,
            "question": response_data.get("question", ""),
            "sql": response_data.get("sql", ""),
            "dataframe_json": json.dumps(response_data.get("dataframe", [])),
            "summary": response_data.get("summary", ""),
            "chart_json": json.dumps(response_data.get("chart", {})) if response_data.get("chart") else None
        }
        
        #print(f"📤 Enviando feedback: is_useful={is_useful}")
        #print(f"📝 Payload: {payload}")

        logger.info(f"📤 Enviando feedback: is_useful={is_useful}")
        logger.debug(f"📝 Payload: {payload}")
        
        
        try:
            response = requests.post(
                CONFIRM_URL,
                json=payload,
                timeout=10
            )

            #print(f"📥 Resposta da API: status={response.status_code}")
            logger.info(f"📥 Resposta da API: status={response.status_code}")

            if response.status_code == 200:
                result = response.json()
                if is_useful:
                    st.success(f"✅ Obrigado! Resposta salva na memória!")
                    if result.get("memory_id"):
                        st.info(f"Memory ID: `{result['memory_id']}`")
                else:
                    st.info(f"❌ Resposta marcada como não útil (não foi salva)")
                return True
            else:
                error_msg = f"Erro ao registrar feedback (status {response.status_code})"
                st.error(error_msg)
                return False
        
        except requests.exceptions.Timeout:
            st.warning("⏱️ Timeout ao enviar feedback. Tente novamente.")
            return False
        except requests.exceptions.ConnectionError:
            st.error("🔌 API offline. Não foi possível salvar feedback.")
            return False
        except Exception as e:
            st.error(f"Erro ao enviar feedback: {str(e)}")
            return False
                            
    except Exception as e:
        error_msg = f"Erro ao enviar feedback: {str(e)}"
        print(f"❌ {error_msg}")
        st.error(error_msg)
        return False

# Exibe as mensagens do histórico
for idx, message in enumerate(st.session_state.messages):
    with st.chat_message(message["role"]):
        # Conteúdo da mensagem pode ser uma string (para o usuário ou msg inicial) ou um dicionário (para respostas da API)
        if isinstance(message["content"], dict):
            response_data = message["content"]
            
            # Mostra resposta
            st.markdown(response_data.get("summary", "*Nenhum resumo disponível.*"))
            
            # Mostra badge de memória se aplicável
            if response_data.get("from_memory"):
                similarity = response_data.get("similarity", 0)
                st.markdown(
                    f"<div class='memory-badge'>💾 Recuperado da memória ({similarity*100:.0f}% similar)</div>",
                    unsafe_allow_html=True
                )

            # Expander para a consulta SQL
            if response_data.get("sql"):
                with st.expander("Ver consulta SQL gerada"):
                    st.code(response_data["sql"], language="sql")

            # Exibe o DataFrame se existir
            if response_data.get("dataframe"):
                df = pd.DataFrame(response_data["dataframe"])
                st.dataframe(df)

            # Exibe o gráfico Plotly se existir
            if response_data.get("chart"):
                try:
                    fig = go.Figure(response_data["chart"])
                    # Usa um key único baseado no índice e hash do gráfico
                    chart_key = f"chart_{idx}_{hash(str(response_data.get('chart', '')))}"
                    st.plotly_chart(
                        fig,
                        config={
                            "displayModeBar": False,
                            "responsive": True
                        },
                        use_container_width=True,
                        key=chart_key
                    )
                except Exception as e:
                    st.error(f"Não foi possível renderizar o gráfico: {e}")
            
            # Mostra botões de feedback se houver dados suficientes E resposta não é da memória
            if response_data.get("sql") and not response_data.get("error") and not response_data.get("from_memory"):
                # Cria chave única para esta resposta
                response_key = f"response_{idx}_{id(response_data)}"
                # Armazena em cache para acesso posterior
                st.session_state.response_cache[response_key] = response_data
                
                # Verifica se já foi avaliada
                already_evaluated = response_key in st.session_state.evaluated_responses
                
                st.markdown("---")
                st.write("**Foi útil?**")
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("👍 Útil", key=f"useful_{idx}", help="Salva esta resposta na memória", disabled=already_evaluated):
                        # Recupera dados do cache
                        cached_data = st.session_state.response_cache.get(response_key, response_data)
                        result = send_feedback(cached_data, True)
                        if result:
                            st.session_state.evaluated_responses.add(response_key)
                with col2:
                    if st.button("👎 Não útil", key=f"not_useful_{idx}", help="Descarta sem salvar", disabled=already_evaluated):
                        # Recupera dados do cache
                        cached_data = st.session_state.response_cache.get(response_key, response_data)
                        result = send_feedback(cached_data, False)
                        if result:
                            st.session_state.evaluated_responses.add(response_key)
        else:
            st.markdown(message["content"])


# Captura a pergunta do usuário
if prompt := st.chat_input("Digite sua solicitação aqui..."):
    # VALIDAÇÃO ANTES DE ENVIAR
    prompt_clean = prompt.strip()
    
    # Verifica se não está vazio
    if not prompt_clean:
        st.error("❌ A pergunta não pode estar vazia!")
        st.stop()
    
    # Verifica tamanho mínimo
    if len(prompt_clean) < 3:
        st.error("❌ A pergunta deve ter pelo menos 3 caracteres.")
        st.stop()
    
    # Verifica tamanho máximo
    if len(prompt_clean) > 500:
        st.error("❌ A pergunta é muito longa (máximo 500 caracteres).")
        st.stop()
    
    # Usa prompt limpo
    st.session_state.messages.append({"role": "user", "content": prompt_clean})

    with st.chat_message("user", avatar="👤"):
        st.markdown(prompt_clean)
  

    # Prepara para exibir a resposta do assistente
    with st.chat_message("assistant", avatar="🤖"):
        message_placeholder = st.empty()        
        message_placeholder.markdown("Estou pensando... 🧠 para gerar a resposta... por favor aguarde") 
        try:
            # Envia a pergunta para a API
            # Timeout de 60s com mensagem de progresso
            response = requests.post(
                API_URL, 
                json={"question": prompt_clean}, 
                timeout=60  # 1 minuto é suficiente
            )
            
            # TRATA ERRO 429 (Rate Limit)
            if response.status_code == 429:
                error_data = response.json()
                
                # Extrai tempo de reset se disponível
                reset_time = response.headers.get('X-RateLimit-Reset')
                
                if reset_time:
                    import datetime
                    reset_datetime = datetime.datetime.fromtimestamp(int(reset_time))
                    wait_seconds = (reset_datetime - datetime.datetime.now()).total_seconds()
                    wait_minutes = max(1, int(wait_seconds / 60))
                    
                    message_placeholder.error(
                        f"⏳ Limite de requisições atingido!\n\n"
                        f"Você pode fazer mais perguntas em aproximadamente {wait_minutes} minuto(s).\n\n"
                        f"💡 Dica: Use o cache! Perguntas similares são respondidas instantaneamente."
                    )
                else:
                    message_placeholder.error(
                        f"⏳ Limite de requisições atingido!\n\n"
                        f"Aguarde 1 minuto antes de fazer mais perguntas.\n\n"
                        f"Limite atual: 10 perguntas por minuto."
                    )
                
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": "Limite de requisições atingido. Aguarde um momento."
                })
                st.stop()
            
            response.raise_for_status()

            response_data = response.json()
            
            # Armazena no session state
            st.session_state.last_response_data = response_data
            
            # Reseta flag de avaliação
            st.session_state.new_response_evaluated = False           
           
            # Limpa placeholder
            message_placeholder.empty()           
            
            # Adiciona ao histórico
            st.session_state.messages.append({"role": "assistant", "content": response_data})
            
            # Força rerun para renderizar pelo histórico (evita duplicação)
            st.rerun()

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                # Já tratado acima
                pass
            elif e.response.status_code == 422:
                # Tenta extrair detalhes do erro
                try:
                    error_detail = e.response.json()
                    if "detail" in error_detail:
                        details = error_detail["detail"]
                        if isinstance(details, list) and len(details) > 0:
                            error_msg = details[0].get("msg", "Erro de validação")
                            error_message = f"❌ {error_msg}"
                        else:
                            error_message = f"❌ Solicitação inválida: {error_detail.get('detail', 'Erro desconhecido')}"
                    else:
                        error_message = "❌ Solicitação inválida. Verifique sua pergunta."
                except:
                    error_message = "❌ Solicitação inválida. Por favor, reformule sua solicitação."
                
                message_placeholder.error(error_message)
                st.session_state.messages.append({"role": "assistant", "content": error_message})
            else:
                error_message = f"❌ Problema com a conexão, informe a TI que ocorreu esse erro: {e.response.status_code}"
                message_placeholder.error(error_message)
                st.session_state.messages.append({"role": "assistant", "content": error_message})
        
        except requests.exceptions.RequestException as e:
            error_message = f"❌ Não foi possível processar a solicitação. Verifique se a sua solicitação está correta e/ou tente novamente mais tarde."
            #error_message = f"Não foi possível processar a solicitação. Verifique se ela está rodando e acessível. (Erro: {e})"
            message_placeholder.error(error_message)
            st.session_state.messages.append({"role": "assistant", "content": error_message})
        
