# Arquivo: train.py (versão aprimorada)

from api.utils.vanna_instance import create_and_connect_vanna
from api.utils.smart_training import get_ddl_from_information_schema, get_relationships_from_information_schema
from api.utils.config import load_config

if __name__ == '__main__':
    vn = create_and_connect_vanna()
    config = load_config()
    db_name = config['db_name']

    # --- 1. TREINAMENTO ESTRUTURAL (O que já tínhamos) ---
    print("\n--- INICIANDO TREINAMENTO ESTRUTURAL ---")
    ddl_statements = get_ddl_from_information_schema(vn, db_name)
    for ddl in ddl_statements:
        vn.train(ddl=ddl)

    relationship_docs = get_relationships_from_information_schema(vn, db_name)
    for doc in relationship_docs:
        vn.train(documentation=doc)

    # --- 2. TREINAMENTO SEMÂNTICO (Ensinando o significado) ---
    print("\n--- INICIANDO TREINAMENTO SEMÂNTICO ---")
    
    # 2.1 Descrições de Tabelas e Colunas
    print("Ensinando sobre o propósito das tabelas e colunas...")
    vn.train(documentation="A tabela 'request' é o registro principal para cada solicitação de RMA (devolução ou garantia).")
    vn.train(documentation="A coluna 'request.created_at' armazena a data em que a solicitação foi aberta.")
    vn.train(documentation="A coluna 'rma_status.status' contém o código do status do processo, como 'PENDING' ou 'FINISHED'.")
    
    # 2.2 Glossário de Termos de Negócio
    print("Ensinando o glossário de termos de negócio...")
    vn.train(documentation="Regra: Ao filtrar um cliente pelo nome da empresa (company_name), a consulta SQL deve usar o operador LIKE com wildcards (ex: `WHERE c.company_name LIKE '%Beta%'`).")
    vn.train(documentation="Mapeamento: 'Devolução' significa request.rma_type_id = 1.")
    vn.train(documentation="Mapeamento: 'Garantia' significa request.rma_type_id = 2.")
    vn.train(documentation="Mapeamento: 'Concluída' ou 'Finalizada' significa rma_status.status = 'FINISHED'.")
    vn.train(documentation="Mapeamento: 'Cancelada' significa rma_status.status = 'CANCELED'.")
    vn.train(documentation="Regra de Data: 'Mês atual' usa MONTH(data) = MONTH(GETDATE()) AND YEAR(data) = YEAR(GETDATE()).")
    vn.train(documentation="Regra de Data: 'Últimos N meses' usa data >= DATEADD(month, -N, GETDATE()).")

    # --- 3. TREINAMENTO POR EXEMPLO (O mais poderoso) ---
    print("\n--- INICIANDO TREINAMENTO POR EXEMPLOS (FAQ) ---")

    # Exemplo 1: Ranking
    vn.train(
        question="Quais os 5 produtos com mais solicitações de garantia?",
        sql="""
SELECT TOP 5
    p.name,
    COUNT(r.id) as total_garantias
FROM 
    dbo.request r
JOIN 
    dbo.product p ON r.product_id = p.id
WHERE 
    r.rma_type_id = 2
GROUP BY
    p.name
ORDER BY
    total_garantias DESC;
"""
    )
    
    # Exemplo 2: JOIN com Status
    vn.train(
        question="Quantas solicitações foram concluídas no total?",
        sql="""
SELECT
    COUNT(r.id) AS total_concluidas
FROM
    dbo.request r
JOIN
    dbo.rma_status rs ON r.status_id = rs.id
WHERE
    rs.status = 'FINISHED';
"""
    )

    vn.train(
        question="Mostre o cliente com mais garantias nos últimos 6 meses",
        sql="""
SELECT TOP 1
    c.company_name,
    COUNT(r.id) AS total_requests
FROM 
    dbo.request r
JOIN 
    dbo.customer c ON r.customer_id = c.id
WHERE 
    r.rma_type_id = 2 
    AND r.created_at >= DATEADD(month, -6, GETDATE())
GROUP BY 
    c.company_name
ORDER BY 
    total_requests DESC;
"""
    )
    vn.train(
        question="Quantas solicitações foram concluídas no total?",
        sql="""
SELECT
    COUNT(r.id) AS total_concluidas
FROM
    dbo.request r
JOIN
    dbo.rma_status rs ON r.status_id = rs.id
WHERE
    rs.status = 'FINISHED';
"""
    )
    vn.train(
        question="Quantas solicitações foram criadas para o cliente Beta?",
        sql=""" SELECT COUNT(*) AS total_requests
        FROM dbo.request r
        JOIN dbo.client c
        ON r.client_cnpj = c.cnpj
        WHERE c.company_name LIKE '%Beta%'; """
    )

    # Exemplo: Tipos de RMA
    vn.train(
        question="Quais são os tipos de RMAs?",
        sql="""
SELECT DISTINCT
    rt.id,
    rt.name AS tipo
FROM
    dbo.rma_type rt
ORDER BY
    rt.name;
"""
    )

    vn.train(
        question="Quantas solicitações por tipo de RMA?",
        sql="""
SELECT
    rt.name AS tipo_rma,
    COUNT(r.id) AS total_solicitacoes
FROM
    dbo.request r
JOIN
    dbo.rma_type rt ON r.rma_type_id = rt.id
GROUP BY
    rt.name, r.rma_type_id
ORDER BY
    total_solicitacoes DESC;
"""
    )

    vn.train(documentation="A tabela 'rma_type' armazena os tipos de RMA disponíveis (Devolução, Garantia, etc).")
    vn.train(documentation="A coluna 'request.rma_type_id' faz referência à tabela rma_type.")

    
    print("\n🎉 Treinamento Avançado Concluído! 🎉")