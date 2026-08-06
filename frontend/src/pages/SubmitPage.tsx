import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "../components/ui/button";
import { Card, CardDescription, CardTitle } from "../components/ui/card";
import { Textarea } from "../components/ui/textarea";
import { createInvestigation } from "../lib/api";

export function SubmitPage() {
  const navigate = useNavigate();
  const [issueDescription, setIssueDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = issueDescription.trim();
    if (!trimmed) {
      setError("Enter an issue description.");
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      const created = await createInvestigation(trimmed);
      navigate(`/investigations/${created.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start investigation");
      setSubmitting(false);
    }
  }

  return (
    <Card className="max-w-2xl">
      <CardTitle>Start an investigation</CardTitle>
      <CardDescription>
        Describe the data issue. The agents will gather evidence and propose a
        root cause.
      </CardDescription>

      <form className="mt-5 space-y-4" onSubmit={onSubmit}>
        <label className="block space-y-2">
          <span className="text-sm font-medium">Issue description</span>
          <Textarea
            value={issueDescription}
            onChange={(event) => setIssueDescription(event.target.value)}
            placeholder="e.g. Total revenue for 2024-01-20 looks lower than expected..."
            disabled={submitting}
          />
        </label>

        {error ? (
          <p className="m-0 text-sm text-[var(--danger)]" role="alert">
            {error}
          </p>
        ) : null}

        <Button type="submit" disabled={submitting}>
          {submitting ? "Starting…" : "Start investigation"}
        </Button>
      </form>
    </Card>
  );
}
