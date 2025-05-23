from langchain.embeddings.huggingface import HuggingFaceEmbeddings
from langchain_core.prompts import PromptTemplate, ChatPromptTemplate, MessagesPlaceholder, FewShotChatMessagePromptTemplate
from langchain_pinecone import PineconeVectorStore
from langchain_core.output_parsers import StrOutputParser
from huggingface_hub import hf_hub_download
from langchain.llms import LlamaCpp
from langchain.chains import create_history_aware_retriever, create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.runnables.history import RunnableWithMessageHistory
from config import answer_examples
import os

store = {}


def get_session_history(session_id: str) -> BaseChatMessageHistory:
    if session_id not in store:
        store[session_id] = ChatMessageHistory()
    return store[session_id]


def get_retriever():
    embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-mpnet-base-v2")
    index_name = "tax-markdown-index"

    database = PineconeVectorStore.from_existing_index(index_name=index_name, embedding=embeddings)
    retriever = database.as_retriever(search_kwargs={"k": 4})

    return retriever


def get_history_retriever():
    llm = get_llm()
    retriever = get_retriever()

    contextualize_q_system_prompt = """Given a chat history and the latest user question \
    which might reference context in the chat history, formulate a standalone question \
    which can be understood without the chat history. Do NOT answer the question, \
    just reformulate it if needed and otherwise return it as is."""
     
    contextualize_q_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", contextualize_q_system_prompt),
            MessagesPlaceholder("chat_history"),
            ("human", "{input}"),
        ]
    )

    history_aware_retriever = create_history_aware_retriever(
        llm, retriever, contextualize_q_prompt
    )

    return history_aware_retriever


def get_llm():
    model_path = hf_hub_download(
        repo_id="tensorblock/llama-2-ko-7b-GGUF",
        filename="llama-2-ko-7b-Q4_K_M.gguf",     
        cache_dir=os.path.expanduser("~/.cache/llama-2-ko-7b")
    )
   
    return LlamaCpp(
        model_path=model_path,
        n_ctx=4096,
        max_tokens=512,
        temperature=0.2,
        n_threads=os.cpu_count() or 4,  
    )


def get_dictionary_chain():
    dictionary = ["사람을 나타내는 표현 -> 거주자"]
    llm = get_llm()

    prompt = PromptTemplate(
        input_variables=["question"],
        template=(f"""
            사용자의 질문을 보고, 우리의 사전을 참고해서 질문을 변형해주세요.
            사전에 명시된 부분에 해당하는 단어만 그대로 변경합니다.
            사전: {dictionary}
            질문: {{question}}
            단, 변경할 필요가 없으면 사용자의 질문을 변경하지 않아도 됩니다. 그런 경우 질문만 그대로 리턴해주세요.
            """
        )
    )

    dictionary_chain = prompt | llm | StrOutputParser()
    return dictionary_chain


def get_rag_chain():
    llm = get_llm()
    
    example_prompt = ChatPromptTemplate.from_messages(
        [
            ("human", "{input}"),
            ("ai", "{answer}"),
        ]
    )
    few_shot_prompt = FewShotChatMessagePromptTemplate(
        example_prompt=example_prompt,
        examples=answer_examples,
    )

    qa_system_prompt = """
    당신은 소득세법 전문가입니다. 사용자의 소득세법에 관한 질문에 답변해주세요
    아래에 제공된 문서를 활용해서 답변해주시고
    답변은 알 수 없다면 모른다고 답변해주세요
    답변을 제공할 때는 소득세법 (XX조) 에 따르면 이라고 시작하면서 답변해주시고
    2-3 문장정도의 짧은 내용의 답변을 원합니다.

    {context}"""
    qa_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", qa_system_prompt), 
            few_shot_prompt,
            MessagesPlaceholder("chat_history"),
            ("human", "{input}"),
        ]
    )

    history_aware_retriever = get_history_retriever()

    question_answer_chain = create_stuff_documents_chain(llm, qa_prompt)

    rag_chain = create_retrieval_chain(history_aware_retriever, question_answer_chain)

    conversational_rag_chain = RunnableWithMessageHistory(
        rag_chain,
        get_session_history,
        input_messages_key="input",
        history_messages_key="chat_history",
        output_messages_key="answer",
    ).pick("answer")

    return conversational_rag_chain


def get_ai_response(user_message): 
    dictionary_chain = get_dictionary_chain()
    rag_chain = get_rag_chain()

    tax_chain = {"input": dictionary_chain} | rag_chain

    ai_response = tax_chain.stream({"question": user_message}, config={
        "configurable": {"session_id": "abc123"},
    })

    return ai_response