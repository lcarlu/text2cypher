"""LLM and embedding factories: OpenAI by default, Azure OpenAI when AZURE_OPENAI_ENDPOINT is set, Ollama when LOCAL_MODEL=true."""

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from text2cypher_backend.config import get_settings


def get_llm(callbacks=None) -> BaseChatModel:
    settings = get_settings()
    if settings.local_model:
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=settings.ollama_chat_model, temperature=settings.llm_temperature or 0, callbacks=callbacks
        )
    kwargs = {}
    if settings.llm_temperature is not None:
        kwargs["temperature"] = settings.llm_temperature
    if settings.llm_reasoning_effort:
        kwargs["reasoning"] = {"effort": settings.llm_reasoning_effort}
    if settings.azure_openai_endpoint:
        from langchain_openai import AzureChatOpenAI

        # Endpoint, key and api-version are read from AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, OPENAI_API_VERSION
        return AzureChatOpenAI(azure_deployment=settings.openai_chat_model, callbacks=callbacks, **kwargs)
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=settings.openai_chat_model, callbacks=callbacks, **kwargs)


def get_embeddings() -> Embeddings:
    settings = get_settings()
    if settings.local_model:
        from langchain_ollama import OllamaEmbeddings

        return OllamaEmbeddings(model=settings.ollama_embedding_model)
    if settings.azure_openai_endpoint:
        from langchain_openai import AzureOpenAIEmbeddings

        return AzureOpenAIEmbeddings(azure_deployment=settings.openai_embedding_model)
    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(model=settings.openai_embedding_model)
