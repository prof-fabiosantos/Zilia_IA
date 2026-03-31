# Arquivo: api/utils/memory.py
"""
Módulo para gerenciar a memória/cache de interações usando Chroma DB.
Armazena perguntas e respostas bem-sucedidas para reutilização futura.
"""

import hashlib
from datetime import datetime
from typing import Optional, Dict, Any, List

import chromadb


class MemoryManager:
    """
    Gerencia a memória de interações usando Chroma DB.
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
            print("   Usando armazenamento em memória (dados serão perdidos)")

        try:
            # Verifica se o diretório é gravável
            test_file = os.path.join(chroma_path, ".write_test")
            try:
                with open(test_file, "w") as f:
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
            print("⚠️ ⚠️ ⚠️ FALLBACK: Usando EphemeralClient")
            print("⚠️ ⚠️ ⚠️ CONSEQUÊNCIA: Todos os dados de memória serão PERDIDOS ao reiniciar!")
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
                metadata={"description": "Armazena pares de consultas e respostas bem-sucedidas"},
            )
            total_memories = self.collection.count()
            print(f"✅ Memory Manager inicializado ({persistence_status})")
            print(f"💾 {total_memories} memórias existentes no banco de dados")
        except Exception as e:
            print(f"❌ Erro ao criar coleção: {e}")
            # Tenta deletar e recriar
            try:
                self.client.delete_collection(name=self.collection_name)
            except Exception:
                pass
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={"description": "Armazena pares de consultas e respostas bem-sucedidas"},
            )

        self.is_persistent = is_persistent

    def is_using_persistent_storage(self) -> bool:
        """Verifica se o gerenciador está usando armazenamento persistente."""
        return self.is_persistent

    def _generate_id(self, question: str) -> str:
        """Gera um ID determinístico baseado na pergunta."""
        return hashlib.sha256(question.encode()).hexdigest()[:16]

    def _safe_now_iso(self) -> str:
        return datetime.now().isoformat()

    def search_similar(self, question: str, top_k: int = 3, threshold: float = 0.7) -> List[Dict[str, Any]]:
        """
        Busca por perguntas similares na memória.

        Observação: Chroma retorna distâncias (menores são melhores).
        Convertemos para similaridade: 1 / (1 + distância).

        Retorna também o memory_id (ID do item na coleção), quando disponível.
        """
        try:
            # Algumas versões aceitam include=["ids", ...], outras não.
            # Tentamos com ids; se falhar, fazemos fallback sem ids.
            try:
                results = self.collection.query(
                    query_texts=[question],
                    n_results=top_k,
                    include=["documents", "metadatas", "distances", "ids"],
                )
                ids = results.get("ids", [[]])[0] if isinstance(results.get("ids", None), list) else []
            except Exception:
                results = self.collection.query(
                    query_texts=[question],
                    n_results=top_k,
                    include=["documents", "metadatas", "distances"],
                )
                ids = []

            if not results or not results.get("documents") or len(results["documents"][0]) == 0:
                return []

            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            dists = results.get("distances", [[]])[0]

            similar_items: List[Dict[str, Any]] = []

            for i, (doc, metadata, distance) in enumerate(zip(docs, metas, dists)):
                # Converte distância para similaridade (0-1)
                try:
                    similarity = 1 / (1 + float(distance))
                except Exception:
                    similarity = 0.0

                if similarity < threshold:
                    continue

                item = {
                    "question": doc,
                    "similarity": similarity,
                    "metadata": metadata or {},
                    # se ids vier vazio (fallback), devolve ""
                    "memory_id": ids[i] if i < len(ids) else "",
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
        is_confirmed: bool = False,
    ) -> str:
        """
        Salva uma interação na memória usando UPSERT com atualização segura.

        - Evita erro de "ID duplicado"
        - Preserva created_at caso já exista
        - Atualiza updated_at sempre
        """
        try:
            interaction_id = self._generate_id(question)

            now_iso = self._safe_now_iso()

            # Se já existir, preserva created_at
            existing_created_at = None
            try:
                existing = self.collection.get(ids=[interaction_id], include=["metadatas", "documents"])
                if existing and existing.get("metadatas") and len(existing["metadatas"]) > 0:
                    existing_meta = existing["metadatas"][0] or {}
                    existing_created_at = existing_meta.get("created_at")
            except Exception:
                existing_created_at = None

            metadata = {
                "sql": sql,
                "summary": summary,
                "dataframe": dataframe_json,
                "chart": chart_json or "",
                "is_confirmed": str(bool(is_confirmed)),
                "question_original": question[:100],
                "created_at": existing_created_at or now_iso,
                "updated_at": now_iso,
            }

            # UPSERT (seguro para repetição)
            self.collection.upsert(
                ids=[interaction_id],
                documents=[question],
                metadatas=[metadata],
            )

            print(f"💾 Memória salva/atualizada: {interaction_id}")
            return interaction_id

        except Exception as e:
            print(f"❌ Erro ao salvar na memória: {e}")
            return ""

    def get_interaction(self, interaction_id: str) -> Optional[Dict[str, Any]]:
        """Recupera uma interação específica da memória."""
        try:
            result = self.collection.get(ids=[interaction_id], include=["documents", "metadatas"])
            if result and result.get("documents"):
                return {
                    "question": result["documents"][0],
                    "metadata": result["metadatas"][0] if result.get("metadatas") else {},
                }
            return None
        except Exception as e:
            print(f"❌ Erro ao recuperar memória: {e}")
            return None

    def confirm_interaction(self, interaction_id: str, is_useful: bool) -> bool:
        """
        Marca uma interação como confirmada/útil pelo usuário.
        Usa upsert para garantir atualização mesmo se algo estiver inconsistente.
        """
        try:
            interaction = self.get_interaction(interaction_id)
            if not interaction:
                return False

            metadata = interaction.get("metadata", {}) or {}
            metadata["is_confirmed"] = str(True)
            metadata["is_useful"] = str(bool(is_useful))
            metadata["confirmed_at"] = self._safe_now_iso()
            metadata["updated_at"] = self._safe_now_iso()

            # Preserva created_at se existir
            if not metadata.get("created_at"):
                metadata["created_at"] = self._safe_now_iso()

            self.collection.upsert(
                ids=[interaction_id],
                documents=[interaction.get("question", "")],
                metadatas=[metadata],
            )

            print(f"✅ Memória confirmada: {interaction_id} (útil: {is_useful})")
            return True

        except Exception as e:
            print(f"❌ Erro ao confirmar memória: {e}")
            return False

    def get_memory_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas sobre a memória armazenada."""
        try:
            total = self.collection.count()

            all_items = self.collection.get(include=["metadatas"])
            metadatas = all_items.get("metadatas", []) or []

            confirmed = sum(1 for meta in metadatas if (meta or {}).get("is_confirmed") == "True")

            return {
                "total_memories": total,
                "confirmed_memories": confirmed,
                "collection_name": self.collection_name,
            }

        except Exception as e:
            print(f"❌ Erro ao obter estatísticas: {e}")
            return {"error": str(e)}

    def clear_memory(self) -> bool:
        """Limpa toda a memória armazenada."""
        try:
            self.client.delete_collection(name=self.collection_name)
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={"description": "Armazena pares de consultas e respostas bem-sucedidas"},
            )
            print("🗑️  Memória limpa com sucesso")
            return True
        except Exception as e:
            print(f"❌ Erro ao limpar memória: {e}")
            return False

    def get_threshold(self, question: str) -> float:
        """Determina threshold baseado no tipo de pergunta."""
        q_lower = question.lower()

        specific_keywords = ["cidade", "cliente", "produto", "cnpj", "nome"]
        if any(kw in q_lower for kw in specific_keywords):
            return 0.85

        numeric_keywords = ["total", "quantidade", "soma", "média", "count"]
        if any(kw in q_lower for kw in numeric_keywords):
            return 0.90

        return 0.75

