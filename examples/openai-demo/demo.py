from __future__ import annotations

import argparse
from pathlib import Path

from bir import configure, generation, load_traces, observe, score, send_events, span, tool_call

DEFAULT_TRACE_PATH = Path(__file__).resolve().parent / ".bir" / "traces.jsonl"

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
    with tool_call(
        "search_docs",
        input={"query": question, "authorization": "Bearer demo-secret"},
        metadata={"kind": "retrieval"},
    ) as tool:
        words = {word.strip(".,?!").lower() for word in question.split()}
        matches = [
            document
            for document in DOCUMENTS
            if words.intersection(document["text"].lower().split()) or words.intersection(document["id"].split())
        ]
        if not matches:
            matches = DOCUMENTS[:1]
        tool.set_output({"documents": matches})
        return matches


def draft_answer(question: str, documents: list[dict[str, str]]) -> str:
    context = " ".join(document["text"] for document in documents)
    messages = [
        {"role": "system", "content": "Answer using the provided context."},
        {"role": "user", "content": question},
    ]

    with generation(
        "openai.chat.completions",
        model="demo-gpt-4o-mini",
        input={"messages": messages, "context": context, "api_key": "sk-demo"},
        metadata={"provider": "openai", "mode": "local-simulated"},
    ) as gen:
        answer = f"{context} Answer: {question}"
        gen.set_output({"role": "assistant", "content": answer})
        gen.set_usage(
            input_tokens=sum(len(message["content"].split()) for message in messages) + len(context.split()),
            output_tokens=len(answer.split()),
        )
        return answer


@observe(name="answer_question", capture_inputs=True, capture_outputs=True)
def answer_question(question: str) -> str:
    with span("retrieve_context"):
        documents = retrieve_context(question)

    with span("draft_answer"):
        answer = draft_answer(question, documents)

    score("helpfulness", 0.82)
    return answer


def main() -> None:
    parser = argparse.ArgumentParser(description="Record a local Bir trace for an OpenAI-style LLM workflow.")
    parser.add_argument(
        "--question",
        default="How does Bir help with LLM observability?",
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

    answer = answer_question(args.question)
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
