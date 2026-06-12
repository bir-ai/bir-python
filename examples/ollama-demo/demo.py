"""Real local LLM tracing demo for the Bir SDK using Ollama."""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from bir import configure, generation, load_traces, observe, retrieval, score, send_events, span

DEFAULT_TRACE_PATH = Path(__file__).resolve().parent / ".bir" / "traces.jsonl"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "llama3.2:1b"

DOCUMENTS = [
    {
        "id": "sdk",
        "text": "Bir records local traces with @observe, spans, generations, tool calls, and scores.",
    },
    {
        "id": "privacy",
        "text": "Input and output capture is opt-in, and common secret-like fields are redacted.",
    },
    {
        "id": "server",
        "text": "The FastAPI server accepts JSONL trace events at /v1/events and exposes /v1/traces.",
    },
]


def retrieve_context(question: str) -> list[dict[str, str]]:
    """Return matching local documents while recording a retrieval event."""

    with retrieval("search_docs", query=question, metadata={"provider": "local"}) as result:
        words = {word.strip(".,?!").lower() for word in question.split()}
        matches = [
            document
            for document in DOCUMENTS
            if words.intersection(document["text"].lower().split())
        ]
        if not matches:
            matches = DOCUMENTS[:1]
        result.set_documents(matches)
        return matches


def chat(ollama_url: str, model: str, messages: list[dict[str, str]]) -> dict:
    """Call the local Ollama chat API and return the parsed response."""

    request = urllib.request.Request(
        f"{ollama_url}/api/chat",
        data=json.dumps({"model": model, "messages": messages, "stream": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def draft_answer(
    question: str,
    documents: list[dict[str, str]],
    *,
    ollama_url: str,
    model: str,
) -> str:
    """Answer with a real local LLM call while recording a generation event."""

    context = " ".join(document["text"] for document in documents)
    messages = [
        {"role": "system", "content": f"Answer briefly using this context: {context}"},
        {"role": "user", "content": question},
    ]

    with generation(
        "ollama.chat",
        model=model,
        input={"messages": messages},
        metadata={"provider": "ollama", "url": ollama_url},
    ) as gen:
        response = chat(ollama_url, model, messages)
        answer = response["message"]["content"]
        gen.set_output({"role": "assistant", "content": answer})
        gen.set_usage(
            input_tokens=response.get("prompt_eval_count", 0),
            output_tokens=response.get("eval_count", 0),
        )
        return answer


@observe(name="answer_question", capture_inputs=True, capture_outputs=True)
def answer_question(question: str, *, ollama_url: str, model: str) -> str:
    """Answer a question with real retrieval, generation, spans, and an eval score."""

    with span("retrieve_context"):
        documents = retrieve_context(question)

    with span("draft_answer"):
        answer = draft_answer(question, documents, ollama_url=ollama_url, model=model)

    matched = sum(1 for document in documents if document["text"].lower() in answer.lower())
    score("context_used", matched / len(documents) if documents else 0.0)
    score("answered", 1.0 if answer.strip() else 0.0)
    return answer


def main() -> None:
    """Run the demo from the command line."""

    parser = argparse.ArgumentParser(description="Record a Bir trace for a real local Ollama LLM call.")
    parser.add_argument(
        "--question",
        default="How does Bir help with LLM observability?",
        help="Question to answer in the demo trace.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Ollama model name.",
    )
    parser.add_argument(
        "--ollama-url",
        default=DEFAULT_OLLAMA_URL,
        help="Ollama server URL.",
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

    answer = answer_question(args.question, ollama_url=args.ollama_url, model=args.model)
    traces = load_traces(trace_path)
    latest_trace = traces[-1]

    print(answer)
    print(f"wrote {len(latest_trace.events)} events to {trace_path}")
    print(f"trace_id={latest_trace.id}")

    if args.send:
        result = send_events(args.server_url, path=trace_path)
        print(f"sent {result.accepted} events to {args.server_url}")


if __name__ == "__main__":
    main()
