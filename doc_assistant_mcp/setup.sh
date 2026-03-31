#!/bin/bash
# =============================================================================
# setup.sh — Configuração inicial do Assistente de Suporte Técnico
# =============================================================================
# Execute uma única vez no ambiente (desenvolvimento ou produção Linux):
#   chmod +x setup.sh && ./setup.sh
#
# O que este script faz:
#   1. Instala dependências Python
#   2. Baixa modelos Ollama (LLM + embeddings)
#   3. Baixa cross-encoder do HuggingFace para cache local
# =============================================================================

set -e  # para na primeira falha

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║       Assistente de Suporte Técnico — Setup          ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# -----------------------------------------------------------------------------
# 1) Dependências Python
# -----------------------------------------------------------------------------
echo "📦 [1/3] Instalando dependências Python..."
pip install -r requirements.txt
pip install sentence-transformers
echo "✅ Dependências instaladas."
echo ""

# -----------------------------------------------------------------------------
# 2) Modelos Ollama
# -----------------------------------------------------------------------------
echo "🤖 [2/3] Baixando modelos Ollama..."

echo "  ⬇️  nomic-embed-text (embeddings ~274MB)..."
ollama pull nomic-embed-text:latest

echo "  ⬇️  granite4 (LLM desenvolvimento)..."
ollama pull granite4:latest

echo "  ⬇️  gpt-oss-20b (LLM produção ~12GB — pode demorar vários minutos)..."
ollama pull gpt-oss-20b:latest

echo "  ⬇️  qwen2.5vl:7b (OCR de imagens/fluxogramas)..."
ollama pull qwen2.5vl:7b

echo "✅ Modelos Ollama prontos."
echo ""

# -----------------------------------------------------------------------------
# 3) Cross-encoder HuggingFace (cache local)
# -----------------------------------------------------------------------------
echo "🔀 [3/3] Baixando cross-encoder para cache local (~91MB)..."
python -c "
from sentence_transformers import CrossEncoder
print('  ⬇️  cross-encoder/ms-marco-MiniLM-L6-v2...')
CrossEncoder('cross-encoder/ms-marco-MiniLM-L6-v2')
print('  ✅ Cross-encoder em cache.')
"
echo ""

# -----------------------------------------------------------------------------
# Resumo
# -----------------------------------------------------------------------------
echo "╔══════════════════════════════════════════════════════╗"
echo "║                  ✅ Setup concluído!                 ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  Para iniciar o servidor MCP:                        ║"
echo "║    python mcp_server.py                              ║"
echo "║                                                      ║"
echo "║  Para iniciar a interface web:                       ║"
echo "║    streamlit run app.py                              ║"
echo "║                                                      ║"
echo "║  Para usar o modelo de produção (20b),               ║"
echo "║  edite mcp_server.py:                                ║"
echo "║    LLM = \"gpt-oss-20b:latest\"                        ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
