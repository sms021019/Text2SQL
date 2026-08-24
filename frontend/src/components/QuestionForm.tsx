import type { KeyboardEvent } from "react";

const EXAMPLE_QUESTIONS = [
  "How many orders were placed last month?",
  "List the top 5 customers by total spend.",
  "What is the average order value by region?",
];

interface QuestionFormProps {
  question: string;
  loading: boolean;
  onQuestionChange: (question: string) => void;
  onSubmit: () => void;
}

export function QuestionForm({
  question,
  loading,
  onQuestionChange,
  onSubmit,
}: QuestionFormProps) {
  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      if (!loading && question.trim()) onSubmit();
    }
  }

  return (
    <form
      className="question-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (!loading && question.trim()) onSubmit();
      }}
    >
      <textarea
        className="question-input"
        placeholder="Ask a question about your data…"
        value={question}
        onChange={(e) => onQuestionChange(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={loading}
        rows={4}
      />
      <div className="question-form-row">
        <div className="example-chips">
          {EXAMPLE_QUESTIONS.map((example) => (
            <button
              key={example}
              type="button"
              className="chip"
              disabled={loading}
              onClick={() => onQuestionChange(example)}
            >
              {example}
            </button>
          ))}
        </div>
        <button
          type="submit"
          className="submit-button"
          disabled={loading || !question.trim()}
        >
          {loading ? "Asking…" : "Ask (Ctrl+Enter)"}
        </button>
      </div>
    </form>
  );
}
