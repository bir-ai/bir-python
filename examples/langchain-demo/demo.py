"""Dependency-free LangChain callback lifecycle demo for Bir."""

from __future__ import annotations

import argparse
from pathlib import Path

from bir import configure, load_traces, send_events
from bir.integrations.langchain import BirCallbackHandler

DEFAULT_TRACE_PATH = Path(__file__).resolve().parent / ".bir" / "traces.jsonl"


class FakeDocument:
    """Tiny stand-in for langchain_core.documents.Document."""

    def __init__(self, page_content: str, *, id: str, source: str) -> None:
        self.page_content = page_content
        self.id = id
        self.metadata = {"source": source}


def run_callback_lifecycle(question: str) -> str:
    """Simulate the callbacks LangChain sends during a simple RAG chain."""

    handler = BirCallbackHandler(capture_inputs=True, capture_outputs=True)
    chain_run_id = "demo-chain"
    retriever_run_id = "demo-retriever"
    llm_run_id = "demo-llm"
    tool_run_id = "demo-tool"

    handler.on_chain_start(
        {"id": ["langchain", "chains", "RetrievalQA"]},
        {"question": question},
        run_id=chain_run_id,
        tags=["local-demo"],
        metadata={"example": "langchain-demo"},
    )

    handler.on_retriever_start(
        {"name": "local_vector_search"},
        question,
        run_id=retriever_run_id,
        parent_run_id=chain_run_id,
    )
    documents = [
        FakeDocument(
            "Bir records local traces for LLM workflows.",
            id="doc-1",
            source="docs",
        )
    ]
    handler.on_retriever_end(documents, run_id=retriever_run_id)

    handler.on_tool_start(
        {"name": "format_citation"},
        {"document_id": "doc-1"},
        run_id=tool_run_id,
        parent_run_id=chain_run_id,
    )
    handler.on_tool_end({"citation": "[doc-1]"}, run_id=tool_run_id)

    prompt = f"Answer using this context: {documents[0].page_content}\nQuestion: {question}"
    handler.on_llm_start(
        {
            "id": ["langchain", "chat_models", "ChatOpenAI"],
            "kwargs": {"model": "demo-gpt-4o-mini"},
        },
        [prompt],
        run_id=llm_run_id,
        parent_run_id=chain_run_id,
    )
    answer = f"Bir records local traces for LLM workflows. [doc-1] Question: {question}"
    handler.on_llm_end(
        {
            "generations": [[{"text": answer}]],
            "llm_output": {
                "token_usage": {
                    "prompt_tokens": len(prompt.split()),
                    "completion_tokens": len(answer.split()),
                },
            },
        },
        run_id=llm_run_id,
    )

    handler.on_chain_end({"answer": answer}, run_id=chain_run_id)
    return answer


def main() -> None:
    parser = argparse.ArgumentParser(description="Record Bir events from a LangChain-shaped callback lifecycle.")
    parser.add_argument(
        "--question",
        default="How does Bir work with LangChain?",
        help="Question to answer in the demo trace.",
    )
    parser.add_argument(
        "--trace-path",
        default=str(DEFAULT_TRACE_PATH),
        help="JSONL trace output path.",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Send recorded events to the Bir FastAPI server after writing the local trace.",
    )
    parser.add_argument(
        "--server-url",
        default="http://127.0.0.1:8000",
        help="Bir server URL used with --send.",
    )
    args = parser.parse_args()

    trace_path = Path(args.trace_path)
    configure(trace_path=trace_path, capture_inputs=True, capture_outputs=True)

    answer = run_callback_lifecycle(args.question)
    latest_trace = load_traces(trace_path)[-1]

    print(answer)
    print(f"wrote {len(latest_trace.events)} events to {trace_path}")
    print(f"trace_id={latest_trace.id}")

    if args.send:
        result = send_events(args.server_url, path=trace_path)
        print(f"sent {result.accepted} events to {args.server_url}")


if __name__ == "__main__":
    main()
