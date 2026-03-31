# Arquivo: api/utils/smart_training.py

def get_ddl_from_information_schema(vn, db_name: str) -> list[str]:
    """
    Gera DDL (CREATE TABLE) statements a partir do INFORMATION_SCHEMA do SQL Server.
    """
    print("⏳ Gerando DDLs a partir do INFORMATION_SCHEMA...")
    
    # Query para obter a estrutura de todas as tabelas
    sql = f"""
        SELECT
            t.TABLE_SCHEMA,
            t.TABLE_NAME,
            c.COLUMN_NAME,
            c.DATA_TYPE,
            c.CHARACTER_MAXIMUM_LENGTH,
            c.IS_NULLABLE
        FROM INFORMATION_SCHEMA.TABLES as t
        JOIN INFORMATION_SCHEMA.COLUMNS as c
            ON t.TABLE_NAME = c.TABLE_NAME AND t.TABLE_SCHEMA = c.TABLE_SCHEMA
        WHERE t.TABLE_TYPE = 'BASE TABLE' AND t.TABLE_CATALOG = '{db_name}'
        ORDER BY t.TABLE_NAME, c.ORDINAL_POSITION
    """
    df_schema = vn.run_sql(sql)
    
    ddl_statements = []
    # Itera sobre cada tabela para construir o DDL
    for table_name, group in df_schema.groupby(['TABLE_SCHEMA', 'TABLE_NAME']):
        schema, name = table_name
        
        columns_sql = []
        for _, row in group.iterrows():
            column_str = f"    [{row['COLUMN_NAME']}] [{row['DATA_TYPE']}]"
            if row['DATA_TYPE'] in ['varchar', 'nvarchar', 'char']:
                length = int(row['CHARACTER_MAXIMUM_LENGTH']) if row['CHARACTER_MAXIMUM_LENGTH'] != -1 else 'MAX'
                column_str += f"({length})"
            if row['IS_NULLABLE'] == 'NO':
                column_str += " NOT NULL"
            columns_sql.append(column_str)
            
        ddl = f"CREATE TABLE [{schema}].[{name}] (\n" + ",\n".join(columns_sql) + "\n);"
        ddl_statements.append(ddl)
        
    print(f"✅ DDLs gerados para {len(ddl_statements)} tabelas.")
    return ddl_statements

def get_relationships_from_information_schema(vn, db_name: str) -> list[str]:
    """
    Gera sentenças de documentação sobre os relacionamentos (chaves estrangeiras).
    """
    print("⏳ Gerando documentação de relacionamentos (chaves estrangeiras)...")
    
    sql = f"""
        SELECT
            fk.TABLE_SCHEMA AS fk_table_schema,
            fk.TABLE_NAME AS fk_table_name,
            fk.COLUMN_NAME AS fk_column_name,
            pk.TABLE_SCHEMA AS pk_table_schema,
            pk.TABLE_NAME AS pk_table_name,
            pk.COLUMN_NAME AS pk_column_name
        FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS AS rc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS fk
            ON rc.CONSTRAINT_NAME = fk.CONSTRAINT_NAME
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS pk
            ON rc.UNIQUE_CONSTRAINT_NAME = pk.CONSTRAINT_NAME
        WHERE fk.TABLE_CATALOG = '{db_name}'
    """
    df_relationships = vn.run_sql(sql)
    
    relationship_docs = []
    for _, row in df_relationships.iterrows():
        doc = (
            f"A coluna [{row['fk_column_name']}] na tabela [{row['fk_table_schema']}].[{row['fk_table_name']}] "
            f"é uma chave estrangeira que se refere à coluna [{row['pk_column_name']}] "
            f"na tabela [{row['pk_table_schema']}].[{row['pk_table_name']}]."
        )
        relationship_docs.append(doc)
        
    print(f"✅ Documentação gerada para {len(relationship_docs)} relacionamentos.")
    return relationship_docs