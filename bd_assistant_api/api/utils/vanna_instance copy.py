# Arquivo: api/utils/vanna_instance.py

from vanna.ollama import Ollama
from vanna.chromadb import ChromaDB_VectorStore
from .config import load_config
import signal
import platform

class MyVanna(ChromaDB_VectorStore, Ollama):
    def __init__(self, config=None):
        ChromaDB_VectorStore.__init__(self, config=config.get('chromadb'))
        Ollama.__init__(self, config=config.get('ollama'))
        
        # Configuração de timeout
        self.sql_timeout = 30  # segundos (padrão)
        self.is_windows = platform.system() == 'Windows'
    
    def run_sql_with_timeout(self, sql: str, timeout: int = None):
        """
        Executa SQL com timeout para evitar consultas que travam.
        
        Args:
            sql: Consulta SQL a executar
            timeout: Tempo máximo em segundos (padrão: self.sql_timeout)
            
        Returns:
            DataFrame com resultados
            
        Raises:
            TimeoutError: Se a consulta exceder o tempo limite
            Exception: Outros erros de execução SQL
        """
        import threading
        
        timeout = timeout or self.sql_timeout
        
        # SOLUÇÃO MULTIPLATAFORMA (funciona no Windows e Linux)
        result = [None]
        error = [None]
        
        def run_query():
            """Thread que executa a query"""
            try:
                result[0] = self.run_sql(sql)
            except Exception as e:
                error[0] = e
        
        # Cria thread para executar query
        thread = threading.Thread(target=run_query, daemon=True)
        thread.start()
        
        # Aguarda até o timeout
        thread.join(timeout)
        
        # Verifica se terminou
        if thread.is_alive():
            # Thread ainda rodando = timeout
            print(f"⏱️ Timeout: Consulta excedeu {timeout}s e foi cancelada")
            raise TimeoutError(
                f"A consulta SQL excedeu o limite de {timeout} segundos. "
                f"Tente simplificar a pergunta ou usar filtros mais específicos."
            )
        
        # Verifica se houve erro
        if error[0]:
            raise error[0]
        
        return result[0]
    
    def set_timeout(self, seconds: int):
        """
        Configura o timeout padrão para consultas SQL.
        
        Args:
            seconds: Tempo em segundos (recomendado: 15-60)
        """
        if seconds < 5:
            print("⚠️ Timeout muito baixo! Recomendado: mínimo 10 segundos")
        self.sql_timeout = seconds
        print(f"✅ Timeout SQL configurado para {seconds} segundos")


def create_and_connect_vanna():
    """
    Cria a instância do Vanna e estabelece a conexão com o banco de dados.
    """
    app_config = load_config()

    # Configuração específica para os componentes do Vanna
    vanna_config = {
        'chromadb': {'path': app_config["chromadb_path"]},
        'ollama': {
            'model': app_config["ollama_model"],
            'options': {'num_predict': 2048} 
        }
    }
    
    print(f"🔌 Configurando Vanna com o modelo Ollama '{app_config['ollama_model']}'...")
    vn = MyVanna(config=vanna_config)

    # Configura timeout (pode vir do .env ou usar padrão)
    sql_timeout = int(app_config.get('sql_timeout', 30))
    vn.set_timeout(sql_timeout)

    odbc_conn_str = (
        f"DRIVER={{{app_config['odbc_driver']}}};"
        f"SERVER={app_config['db_host']},{app_config['db_port']};"
        f"DATABASE={app_config['db_name']};"
        f"UID={app_config['db_user']};"
        f"PWD={app_config['db_password']};"
        "Encrypt=no;"
        "TrustServerCertificate=yes;"
    )

    print(f"🔌 Conectando ao SQL Server em {app_config['db_host']}:{app_config['db_port']}...")
    
    try:
        vn.connect_to_mssql(odbc_conn_str=odbc_conn_str)
        print("✅ Conexão com SQL Server estabelecida.")
    except Exception as e:
        print(f"❌ ERRO ao conectar ao SQL Server: {e}")
        raise ConnectionError(
            f"Não foi possível conectar ao SQL Server.\n"
            f"Verifique: servidor rodando, credenciais corretas, banco existe.\n"
            f"Erro: {str(e)}"
        )
    
    return vn