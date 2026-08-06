export type InvestigationStatus =
  | "pending"
  | "investigating"
  | "needs_human_review"
  | "resolved";

export type EvidenceEntry = {
  source: string;
  finding: string;
  confidence: number;
};

export type Hypothesis = {
  description: string;
  supporting_evidence: string[];
  confidence_score: number;
};

export type InvestigationSummary = {
  id: string;
  issue_description: string;
  status: InvestigationStatus;
  final_root_cause: string | null;
  created_at: string;
  updated_at: string;
};

export type InvestigationDetail = InvestigationSummary & {
  evidence: EvidenceEntry[];
  hypotheses: Hypothesis[];
};

export type InvestigationCreateResponse = {
  id: string;
  status: InvestigationStatus;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Request failed (${response.status})`);
  }

  return response.json() as Promise<T>;
}

export function createInvestigation(
  issue_description: string,
): Promise<InvestigationCreateResponse> {
  return request<InvestigationCreateResponse>("/investigations", {
    method: "POST",
    body: JSON.stringify({ issue_description }),
  });
}

export function listInvestigations(): Promise<InvestigationSummary[]> {
  return request<InvestigationSummary[]>("/investigations");
}

export function getInvestigation(id: string): Promise<InvestigationDetail> {
  return request<InvestigationDetail>(`/investigations/${id}`);
}
