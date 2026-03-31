# Arquivo: api/utils/vanna_instance.py

from vanna.ollama import Ollama
from vanna.chromadb import ChromaDB_VectorStore
from .config import load_config
import platform


class MyVanna(ChromaDB_VectorStore, Ollama):
    def __init__(self, config=None):
        ChromaDB_VectorStore.__init__(self, config=config.get("chromadb"))
        Ollama.__init__(self, config=config.get("ollama"))

        # Configuração de timeout
        self.sql_timeout = 30  # segundos (padrão)
        self.is_windows = platform.system() == "Windows"

        # Armazena a connection string ODBC para execução com timeout REAL
        self._odbc_conn_str = None

    def run_sql_with_timeout(self, sql: str, timeout: int = None):
        """
        Executa SQL com timeout REAL no driver (pyodbc) — compatível com pyodbc 5.2.0
        e ODBC Driver 18 for SQL Server.

        Observação:
        - pyodbc 5.2.0 normalmente NÃO tem cursor.timeout
        - usamos conn.timeout e tentamos attrs_before(SQL_ATTR_QUERY_TIMEOUT) quando disponível
        """
        timeout = int(timeout or self.sql_timeout)

        # Se não temos conn_str, cai no método original (sem garantia de cancelamento real)
        if not self._odbc_conn_str:
            return self.run_sql(sql)

        import pyodbc
        import pandas as pd

        conn = None
        cursor = None
        try:
            # Login timeout (connect). Não é o mesmo que query timeout, mas ajuda.
            connect_kwargs = {"timeout": timeout}

            # Tenta setar query timeout no nível ODBC (nem sempre disponível em todas builds)
            try:
                connect_kwargs["attrs_before"] = {pyodbc.SQL_ATTR_QUERY_TIMEOUT: timeout}
            except Exception:
                pass

            conn = pyodbc.connect(self._odbc_conn_str, **connect_kwargs)

            # Timeout de QUERY no nível da conexão (mais compatível no pyodbc)
            # Aplica para operações executadas pelos cursors dessa conexão.
            try:
                conn.timeout = timeout
            except Exception:
                pass

            cursor = conn.cursor()
            cursor.execute(sql)

            # Sem resultset (ex.: alguns EXEC)
            if cursor.description is None:
                try:
                    conn.commit()
                except Exception:
                    pass
                return pd.DataFrame()

            columns = [col[0] for col in cursor.description]

            # fetchall pode ser pesado; mantive igual ao seu comportamento original.
            rows = cursor.fetchall()
            return pd.DataFrame.from_records(rows, columns=columns)

        except pyodbc.Error as e:
            msg = str(e)
            # Timeout no ODBC/SQL Server costuma vir como HYT00
            if "HYT00" in msg or "timeout" in msg.lower() or "time-out" in msg.lower():
                raise TimeoutError(
                    f"A consulta SQL excedeu o limite de {timeout} segundos. "
                    f"Tente simplificar a pergunta ou usar filtros mais específicos."
                )
            raise

        finally:
            try:
                if cursor is not None:
                    cursor.close()
            except Exception:
                pass
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass


    def set_timeout(self, seconds: int):
        """
        Configura o timeout padrão para consultas SQL.

        Args:
            seconds: Tempo em segundos (recomendado: 15-60)
        """
        seconds = int(seconds)
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
        "chromadb": {"path": app_config["chromadb_path"]},
        "ollama": {
            "model": app_config["ollama_model"],
            "options": {"num_predict": 2048},
        },
    }

    print(f"🔌 Configurando Vanna com o modelo Ollama '{app_config['ollama_model']}'...")
    vn = MyVanna(config=vanna_config)

    # Configura timeout (pode vir do .env ou usar padrão)
    sql_timeout = int(app_config.get("sql_timeout", 30))
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
        # Conexão usada pelo Vanna internamente (para outras features)
        vn.connect_to_mssql(odbc_conn_str=odbc_conn_str)

        # Guarda conn str para execução com timeout REAL via pyodbc
        vn._odbc_conn_str = odbc_conn_str

        print("✅ Conexão com SQL Server estabelecida.")
    except Exception as e:
        print(f"❌ ERRO ao conectar ao SQL Server: {e}")
        raise ConnectionError(
            "Não foi possível conectar ao SQL Server.\n"
            "Verifique: servidor rodando, credenciais corretas, banco existe.\n"
            f"Erro: {str(e)}"
        )

    return vn
