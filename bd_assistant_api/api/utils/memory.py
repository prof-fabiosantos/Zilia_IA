# Arquivo: api/utils/memory.py
"""
Módulo para gerenciar a memória/cache de interações usando Chroma DB.
Armazena perguntas e respostas bem-sucedidas para reutilização futura.
"""

import json
import chromadb
from typing import Optional, Dict, Any
import hashlib
from datetime import datetime


class MemoryManager:
    """
    Gerancia a memória de interações usando Chroma DB.
    Permite armazenar e recuperar pares (pergunta, SQL, resposta).
    """

    def __init__(self, chroma_path: str = "data/chroma_memory"):
        """
        Inicializa o gerenciador de memória.
        
        Args:
            chroma_path: Caminho para o banco de dados Chroma
        """
        import os
        
        self.chroma_path = chroma_path
        
        # Garante que o diretório existe
        try:
            os.makedirs(chroma_path, exist_ok=True)
        except PermissionError as e:
            print(f"❌ ERRO DE PERMISSÃO: Não foi possível criar diretório {chroma_path}")
            print(f"   Detalhes: {e}")
            print(f"   Usando armazenamento em memória (dados serão perdidos)")
        
        try:
            # Verifica se o diretório é gravável
            test_file = os.path.join(chroma_path, ".write_test")
            try:
                with open(test_file, 'w') as f:
                    f.write("test")
                os.remove(test_file)
                is_writable = True
            except (PermissionError, OSError) as e:
                print(f"⚠️ Diretório {chroma_path} não é gravável: {e}")
                is_writable = False
            
            if is_writable:
                self.client = chromadb.PersistentClient(path=chroma_path)
                print(f"✅ Chroma PersistentClient inicializado em {os.path.abspath(chroma_path)}")
                persistence_status = "PERSISTENTE (dados salvos em disco)"
                is_persistent = True
            else:
                raise PermissionError("Diretório não gravável")
                
        except Exception as e:
            print(f"❌ ERRO ao inicializar PersistentClient: {e}")
            print(f"⚠️ ⚠️ ⚠️ FALLBACK: Usando EphemeralClient")
            print(f"⚠️ ⚠️ ⚠️ CONSEQUÊNCIA: Todos os dados de memória serão PERDIDOS ao reiniciar!")
            print(f"⚠️ ⚠️ ⚠️ SOLUÇÃO: Execute: chmod -R 755 {chroma_path}")
            self.client = chromadb.EphemeralClient()
            persistence_status = "EFÊMERO (dados em memória - serão perdidos ao reiniciar)"
            is_persistent = False
               
        # Nome da coleção para memória de consultas
        self.collection_name = "query_memory"
        
        # Obtém ou cria a coleção
        try:
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={"description": "Armazena pares de consultas e respostas bem-sucedidas"}
            )
            total_memories = self.collection.count()
            print(f"✅ Memory Manager inicializado ({persistence_status})")
            print(f"💾 {total_memories} memórias existentes no banco de dados")
        except Exception as e:
            print(f"❌ Erro ao criar coleção: {e}")
            # Tenta deletar e recriar
            try:
                self.client.delete_collection(name=self.collection_name)
            except:
                pass
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={"description": "Armazena pares de consultas e respostas bem-sucedidas"}
            )
        
        self.is_persistent = is_persistent

    def is_using_persistent_storage(self) -> bool:
        """
        Verifica se o gerenciador de memória está usando armazenamento persistente.
        
        Returns:
            True se os dados são persistentes em disco, False se efêmeros (em memória)
        """
        return self.is_persistent

    def _generate_id(self, question: str) -> str:
        """
        Gera um ID único baseado na pergunta.
        
        Args:
            question: A pergunta do usuário
            
        Returns:
            ID único em hash SHA-256
        """
        return hashlib.sha256(question.encode()).hexdigest()[:16]

    def search_similar(self, question: str, top_k: int = 3, threshold: float = 0.7) -> list[Dict[str, Any]]:
        """
        Busca por perguntas similares na memória.
        
        Args:
            question: A pergunta do usuário
            top_k: Número máximo de resultados a retornar
            threshold: Limite de similaridade (0-1). Resultados abaixo disso são ignorados.
                      Chroma não retorna score diretamente, apenas ordena por relevância.
        
        Returns:
            Lista de resultados similares com seus metadados
        """
        try:
            results = self.collection.query(
                query_texts=[question],
                n_results=top_k,
                include=["documents", "metadatas", "distances"]
            )
            
            if not results or not results["documents"] or len(results["documents"][0]) == 0:
                return []
            
            # Chroma retorna distâncias (menores são melhores)
            # Convertemos para similaridade: 1 / (1 + distância)
            similar_items = []
            for i, (doc, metadata, distance) in enumerate(zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0]
            )):
                # Converte distância para similaridade (0-1)
                similarity = 1 / (1 + distance)
                
                if similarity >= threshold:
                    item = {
                        "question": doc,
                        "similarity": similarity,
                        "metadata": metadata
                    }
                    similar_items.append(item)
            
            return similar_items
            
        except Exception as e:
            print(f"❌ Erro ao buscar na memória: {e}")
            return []

    def save_interaction(
        self,
        question: str,
        sql: str,
        dataframe_json: str,
        summary: str,
        chart_json: Optional[str] = None,
        is_confirmed: bool = False
    ) -> str:
        """
        Salva uma interação bem-sucedida na memória.
        
        Args:
            question: A pergunta do usuário
            sql: A consulta SQL gerada
            dataframe_json: O resultado como JSON
            summary: O resumo gerado pela IA
            chart_json: O gráfico Plotly como JSON (opcional)
            is_confirmed: Se foi confirmado pelo usuário
            
        Returns:
            ID da memória salva
        """
        try:
            interaction_id = self._generate_id(question)
            
            # Prepara os metadados
            metadata = {
                "sql": sql,
                "summary": summary,
                "dataframe": dataframe_json,
                "chart": chart_json or "",
                "is_confirmed": str(is_confirmed),
                "created_at": datetime.now().isoformat(),
                "question_original": question[:100],  # Armazena snippet da pergunta
            }
            
            # Adiciona à coleção
            self.collection.add(
                ids=[interaction_id],
                documents=[question],
                metadatas=[metadata]
            )
            
            print(f"💾 Memória salva: {interaction_id}")
            return interaction_id
            
        except Exception as e:
            print(f"❌ Erro ao salvar na memória: {e}")
            return ""

    def get_interaction(self, interaction_id: str) -> Optional[Dict[str, Any]]:
        """
        Recupera uma interação específica da memória.
        
        Args:
            interaction_id: ID da memória
            
        Returns:
            Dicionário com a interação ou None se não encontrada
        """
        try:
            result = self.collection.get(ids=[interaction_id])
            
            if result and result["documents"]:
                return {
                    "question": result["documents"][0],
                    "metadata": result["metadatas"][0]
                }
            return None
            
        except Exception as e:
            print(f"❌ Erro ao recuperar memória: {e}")
            return None

    def confirm_interaction(self, interaction_id: str, is_useful: bool) -> bool:
        """
        Marca uma interação como confirmada/útil pelo usuário.
        
        Args:
            interaction_id: ID da memória
            is_useful: Se foi útil ou não
            
        Returns:
            True se confirmado com sucesso, False caso contrário
        """
        try:
            interaction = self.get_interaction(interaction_id)
            if not interaction:
                return False
            
            # Atualiza os metadados
            metadata = interaction["metadata"]
            metadata["is_confirmed"] = str(True)
            metadata["is_useful"] = str(is_useful)
            metadata["confirmed_at"] = datetime.now().isoformat()
            
            # Atualiza na coleção (usando upsert)
            self.collection.upsert(
                ids=[interaction_id],
                documents=[interaction["question"]],
                metadatas=[metadata]
            )
            
            print(f"✅ Memória confirmada: {interaction_id} (útil: {is_useful})")
            return True
            
        except Exception as e:
            print(f"❌ Erro ao confirmar memória: {e}")
            return False

    def get_memory_stats(self) -> Dict[str, Any]:
        """
        Retorna estatísticas sobre a memória armazenada.
        
        Returns:
            Dicionário com estatísticas
        """
        try:
            total = self.collection.count()
            
            # Tenta contar confirmadas
            all_items = self.collection.get(include=["metadatas"])
            confirmed = sum(
                1 for meta in all_items["metadatas"]
                if meta.get("is_confirmed") == "True"
            )
            
            return {
                "total_memories": total,
                "confirmed_memories": confirmed,
                "collection_name": self.collection_name
            }
            
        except Exception as e:
            print(f"❌ Erro ao obter estatísticas: {e}")
            return {"error": str(e)}

    def clear_memory(self) -> bool:
        """
        Limpa toda a memória armazenada.
        
        Returns:
            True se limpado com sucesso
        """
        try:
            self.client.delete_collection(name=self.collection_name)
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={"description": "Armazena pares de consultas e respostas bem-sucedidas"}
            )
            print("🗑️  Memória limpa com sucesso")
            return True
            
        except Exception as e:
            print(f"❌ Erro ao limpar memória: {e}")
            return False
    
    def get_threshold(self, question: str) -> float:
        """Determina threshold baseado no tipo de pergunta"""
        q_lower = question.lower()
        
        # Consultas específicas (cidades, nomes, IDs)
        specific_keywords = ['cidade', 'cliente', 'produto', 'cnpj', 'nome']
        if any(kw in q_lower for kw in specific_keywords):
            return 0.85
        
        # Consultas numéricas/analíticas
        numeric_keywords = ['total', 'quantidade', 'soma', 'média', 'count']
        if any(kw in q_lower for kw in numeric_keywords):
            return 0.90
        
        # Consultas genéricas
        return 0.75
