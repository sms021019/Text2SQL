import { useState, type KeyboardEvent } from "react";

const EXAMPLE_QUESTIONS = [
  "How many orders were placed last month?",
  "List the top 5 customers by total spend.",
  "What is the average order value by region?",
];

export interface SubmitOptions {
  useCache: boolean;
  background: boolean;
}

interface QuestionFormProps {
  question: string;
  loading: boolean;
  onQuestionChange: (question: string) => void;
  onSubmit: (options: SubmitOptions) => void;
}

export function QuestionForm({
  question,
  loading,
  onQuestionChange,
  onSubmit,
}: QuestionFormProps) {
  const [useCache, setUseCache] = useState(true);
  const [background, setBackground] = useState(false);

  function submit() {
    if (!loading && question.trim()) onSubmit({ useCache, background });
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      submit();
    }
  }

  return (
    <form
      className="question-form"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
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
        <div className="question-form-options">
          <label className="option-checkbox">
            <input
              type="checkbox"
              checked={useCache}
              disabled={loading}
              onChange={(e) => setUseCache(e.target.checked)}
            />
            Use cache
          </label>
          <label className="option-checkbox">
            <input
              type="checkbox"
              checked={background}
              disabled={loading}
              onChange={(e) => setBackground(e.target.checked)}
            />
            Run in background
          </label>
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
