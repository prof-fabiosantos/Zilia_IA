# Assistente de Documentos com IA (Versão MCP)

Este projeto é um assistente de IA para análise de documentos utilizando uma arquitetura baseada no **Model Context Protocol (MCP)**. Ele integra um servidor backend com ferramentas MCP e um frontend interativo em Streamlit.

O sistema combina:
- **Ollama** para embeddings e geração de respostas usando o modelo `llama3.1:8b`.
- **Qdrant** como banco vetorial para RAG (busca com recuperação de contexto).
- **Tesseract OCR** para leitura de texto de imagens.
- **FastMCP** para gerenciamento das ferramentas como servidor MCP.

---

## 🧩 Arquitetura

### 🔧 Servidor MCP (`mcp_server.py`)
Servidor FastAPI baseado em `FastMCP`:
- Extrai texto de PDFs e imagens.
- Usa o modelo `bge-m3:latest` (via Ollama) para gerar embeddings de 1024 dimensões.
- Indexa os embeddings no Qdrant (ajustado para `size=1024`).
- Expõe as ferramentas MCP:
  - `index_document`: Indexa conteúdo dos arquivos.
  - `ask_question`: Busca vetorial + resposta via LLM.

### 🖥️ Cliente / Frontend (`app.py`)
Interface Streamlit:
- Realiza upload de documentos.
- Interage com o servidor MCP para indexação e perguntas.
- Exibe o histórico de conversas com o assistente.

---

## ✅ Pré-requisitos

1. **Python 3.8+**
2. **Ollama** — instale via [https://ollama.com](https://ollama.com)
3. **Modelos Ollama**
   - Para geração de embeddings:
     ```bash
     ollama pull bge-m3
     ```
   - Para respostas:
     ```bash
     ollama pull llama3.1:8b
     ```
4. **Tesseract OCR**
   - Necessário para imagens.
   - Instale em: [https://github.com/tesseract-ocr/tesseract](https://github.com/tesseract-ocr/tesseract)
   - Ajuste o caminho no `mcp_server.py`:
     ```python
     pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
     ```

---

## 🚀 Como Executar o Projeto

### 1. Clonar e preparar o ambiente

```bash
cd C:\Users\Fabio\llm\doc_assistant_mcp
.venv\Scripts\activate
pip install -r requirements.txt
