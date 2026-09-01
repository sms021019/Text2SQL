/** Pill shown while a background job (`POST /query/async`) is still
 * pending. The parent stops rendering this the moment the job's result
 * arrives or an error is shown -- see `App.tsx`'s `jobStatus` state. */

const LABELS: Record<"queued" | "in_progress", string> = {
  queued: "Queued…",
  in_progress: "Running…",
};

interface JobStatusProps {
  status: "queued" | "in_progress";
}

export function JobStatus({ status }: JobStatusProps) {
  return (
    <div className="job-status" role="status">
      {LABELS[status]}
    </div>
  );
}
