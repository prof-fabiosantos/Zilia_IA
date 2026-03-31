# Arquivo: api/utils/training.py

def train_vanna_model(vn):
    """
    Esta função agora apenas confirma que o treinamento existe.
    O treinamento pesado é feito pelo script train.py.
    """
    # Apenas uma mensagem para confirmar que a API está usando o conhecimento existente
    print("✅ Conhecimento pré-treinado carregado pelo ChromaDB.")
    
    # Verifica se há algum dado de treinamento, só para garantir
    training_data = vn.get_training_data()
    if training_data.empty:
        print("⚠️ Atenção: Nenhum dado de treinamento encontrado. Execute o script train.py para treinar o modelo.")
    
    return vn