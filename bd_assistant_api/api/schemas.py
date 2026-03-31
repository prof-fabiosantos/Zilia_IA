# Arquivo: api/schemas.py
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Any

class QuestionRequest(BaseModel):
    """ 
    O corpo da requisição que o cliente envia.
    Valida que a pergunta não seja vazia e tenha tamanho adequado.
    """
    question: str = Field(
        ..., 
        min_length=3, 
        max_length=500,
        description="Pergunta do usuário em linguagem natural"
    )
    
    @field_validator('question')
    @classmethod
    def validate_question(cls, v: str) -> str:
        """
        Valida e limpa a pergunta do usuário.
        
        Args:
            v: Valor da pergunta
            
        Returns:
            Pergunta limpa (sem espaços extras)
            
        Raises:
            ValueError: Se a pergunta for inválida
        """
        # Remove espaços extras no início e fim
        v_stripped = v.strip()
        
        # Verifica se ficou vazio após remover espaços
        if not v_stripped:
            raise ValueError('A pergunta não pode estar vazia ou conter apenas espaços')
        
        # Verifica tamanho mínimo
        if len(v_stripped) < 3:
            raise ValueError('A pergunta deve ter pelo menos 3 caracteres')
        
        # Verifica se não é só pontuação
        if all(c in '.,;:!? ' for c in v_stripped):
            raise ValueError('A pergunta deve conter texto, não apenas pontuação')
        
        return v_stripped
    
class ChatResponse(BaseModel):
    """ A resposta completa que nossa API retorna. """
    question: str
    summary: Optional[str] = None
    sql: Optional[str] = None
    dataframe: Optional[Any] = None  # Será um JSON (array de objetos)
    chart: Optional[Any] = None      # Será a especificação JSON do Plotly
    error: Optional[str] = None
    memory_id: Optional[str] = None  # ID da memória se recuperado do cache
    similarity: Optional[float] = None  # Score de similaridade se recuperado do cache (0-1)
    from_memory: bool = False  # Indica se a resposta veio do cache

class ConfirmInteractionRequest(BaseModel):
    """ 
    Requisição para confirmar uma interação como útil ou não.
    Quando marcado como útil, salva na memória.
    """
    is_useful: bool
    question: str
    sql: str
    dataframe_json: str  # JSON stringificado do dataframe
    summary: str
    chart_json: Optional[str] = None  # JSON stringificado do gráfico (opcional)