import streamlit as st

st.set_page_config(page_title="Portal de Assistentes IA", layout="wide")

# CSS customizado
st.markdown(
    """
    <style>
        .stApp {
            background-color: #FFFFFF;
        }
        h1 {
            color: #4C2CCD !important;
        }
        .stTabs [role="tablist"] {
            border-bottom: 2px solid #ccc;
        }
        .stTabs [role="tab"] {
            color: #4C2CCD !important;
            font-weight: bold;
        }
        .stTabs [role="tab"][aria-selected="true"] {
            border-bottom: 3px solid #4C2CCD !important;
            color: #4C2CCD !important;
            background-color: #f2f2f2 !important;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

tab1, tab2 = st.tabs(["Assistente Dados", "Assistente Documentos"])

with tab1:
    st.markdown(
        """
        <iframe src="http://localhost:8501" width="100%" height="600"
        style="border:none; border-radius: 0px; margin:0; padding:0;"></iframe>
        """,
        unsafe_allow_html=True,
    )

with tab2:
    st.markdown(
        """
        <iframe src="http://localhost:8502" width="100%" height="600"
        style="border:none; border-radius: 0px; margin:0; padding:0;"></iframe>
        """,
        unsafe_allow_html=True,
    )
