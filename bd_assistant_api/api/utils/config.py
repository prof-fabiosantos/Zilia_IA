# Arquivo: api/utils/config.py

import os
from dotenv import load_dotenv

def load_config():
    """
    Carrega as variáveis de ambiente a partir de um arquivo .env,
    e depois valida a presença das credenciais do banco de dados.
    """
    # Esta linha procura por um arquivo .env e carrega suas variáveis
    load_dotenv()

    config = {
        "db_host": os.getenv("MSSQL_SERVER"),
        "db_port": os.getenv("MSSQL_PORT"),
        "db_name": os.getenv("MSSQL_DATABASE"),
        "db_user": os.getenv("MSSQL_USER"),
        "db_password": os.getenv("MSSQL_PASSWORD"),
        "odbc_driver": os.getenv("ODBC_DRIVER", "ODBC Driver 18 for SQL Server"),
        "ollama_model": os.getenv("OLLAMA_MODEL", "gpt-oss:20b"),
        "chromadb_path": "data/chroma_db",
        "chroma_memory_path": "data/chroma_memory",
        "sql_timeout": os.getenv("SQL_TIMEOUT", "30")  # ADICIONA ESTA LINHA
    }

    if not all([config["db_name"], config["db_user"], config["db_password"]]):
        raise ValueError(
            "As variáveis de ambiente MSSQL_DATABASE, MSSQL_USER e MSSQL_PASSWORD devem ser definidas no arquivo .env."
        )

    print("✅ Configurações carregadas do arquivo .env e validadas.")
    return config