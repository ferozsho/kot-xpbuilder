/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */
import { SupersetClient } from '@superset-ui/core';
import {
  ChatTurnResponse,
  ContextResetResponse,
  ConversationDetailResponse,
  ConversationListResponse,
  ConversationRecord,
  JobListResponse,
  JobRecord,
  MessageRecord,
} from './types';

const BASE = '/aiprompt/api';

/** What one end-to-end SQL turn reports back to the workspace. */
export interface SqlTurnResponse {
  message: MessageRecord;
  sql: string;
  duration_ms: number;
  limit?: number | null;
  tables?: string[];
  database?: string;
  plan?: string;
  /** ``hit``, ``miss``, ``bypass`` or ``disabled``. */
  cache?: string;
  /** Present when the first statement failed and a correction was re-run. */
  repair?: {
    attempts: number;
    source?: string;
    pattern_id?: string | null;
  } | null;
}

/**
 * The Superset client rejects with the raw `Response`, so surface the JSON
 * `message` the view returned instead of a bare "Request failed".
 */
export async function describeError(error: unknown): Promise<string> {
  const response = error as {
    json?: () => Promise<{ message?: string }>;
    statusText?: string;
    message?: string;
  };
  if (response && typeof response.json === 'function') {
    try {
      const body = await response.json();
      if (body?.message) {
        return body.message;
      }
    } catch {
      // fall through to the generic message
    }
  }
  return response?.message || response?.statusText || 'Unexpected error';
}

export async function fetchConversations(): Promise<ConversationListResponse> {
  const { json } = await SupersetClient.get({
    endpoint: `${BASE}/conversations/`,
  });
  return json as ConversationListResponse;
}

export async function createConversation(
  title?: string,
): Promise<ConversationRecord> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/conversations/`,
    jsonPayload: { title },
  });
  return json as ConversationRecord;
}

export async function fetchConversation(
  id: string,
): Promise<ConversationDetailResponse> {
  const { json } = await SupersetClient.get({
    endpoint: `${BASE}/conversations/${id}/`,
  });
  return json as ConversationDetailResponse;
}

/**
 * Send one turn. `contextVersion` is the generation the client has rendered:
 * the server answers `409` when the context was reset in the meantime instead
 * of answering from history the user has not seen.
 */
export async function sendMessage(
  id: string,
  content: string,
  contextVersion?: number,
): Promise<ChatTurnResponse> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/conversations/${id}/messages/`,
    jsonPayload: { content, context_version: contextVersion },
  });
  return json as ChatTurnResponse;
}

export async function resetContext(
  id: string,
  contextVersion?: number,
): Promise<ContextResetResponse> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/conversations/${id}/reset-context/`,
    jsonPayload: { context_version: contextVersion },
  });
  return json as ContextResetResponse;
}

export async function archiveConversation(id: string): Promise<void> {
  await SupersetClient.post({
    endpoint: `${BASE}/conversations/${id}/archive/`,
    jsonPayload: {},
  });
}

/**
 * Answer a data question end to end: the service writes and validates the SQL,
 * Superset executes it, and the resulting turn is stored with its blocks.
 *
 * ``databaseId`` is only needed when the principal is authorized for several
 * databases; otherwise Superset derives the single one from the slice.
 * ``regenerate`` bypasses the candidate cache so the question is answered fresh.
 */
export async function askWithSql(
  conversationId: string,
  question: string,
  databaseId?: number,
  regenerate?: boolean,
): Promise<SqlTurnResponse> {
  const payload: Record<string, unknown> = { question };
  if (databaseId !== undefined) {
    payload.database_id = databaseId;
  }
  if (regenerate) {
    payload.regenerate = true;
  }
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/conversations/${conversationId}/sql/`,
    jsonPayload: payload,
  });
  return json as SqlTurnResponse;
}

// --- Jobs (P2-F05/F08) -------------------------------------------------

export async function fetchJobs(
  conversationId: string,
): Promise<JobListResponse> {
  const { json } = await SupersetClient.get({
    endpoint: `${BASE}/conversations/${conversationId}/jobs/`,
  });
  return json as JobListResponse;
}

export async function submitJob(
  conversationId: string,
  payload: {
    job_type: string;
    idempotency_key: string;
    request?: Record<string, unknown>;
    steps?: string[];
  },
): Promise<JobRecord> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/conversations/${conversationId}/jobs/`,
    jsonPayload: payload,
  });
  return json as JobRecord;
}

export type JobAction = 'cancel' | 'retry' | 'confirm' | 'reject';

export async function actOnJob(
  jobId: string,
  action: JobAction,
): Promise<JobRecord> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/jobs/${jobId}/${action}/`,
    jsonPayload: {},
  });
  return json as JobRecord;
}

// --- Repair knowledge (P5-F10) ----------------------------------------

/** Counts an operator needs; no statement and no question is in here. */
export interface RepairMetricsResponse {
  tenant: string;
  metrics: {
    states: Record<string, number>;
    attempts: {
      attempted: number;
      succeeded: number;
      failed: number;
      suppressed: number;
    };
    success_rate: number;
    kinds: { kind: string; total: number }[];
    usable: number;
    disabled: number;
  };
  promotion: {
    evidence: number;
    auto_promote: boolean;
    validator_version: string;
    policy_version: string;
  };
  budget: {
    max_attempts: number;
    max_elapsed_ms: number;
    max_tokens: number;
    suppress_failed_fixes: boolean;
  };
}

/** One stored correction, as an operator may see it: no SQL, ever. */
export interface RepairPatternRecord {
  id: string;
  state: string;
  version: number;
  dialect: string;
  kind: string;
  failure_fingerprint: string;
  successes: number;
  failures: number;
  confidence: number;
  promotion_policy: string;
  evidence_count: number;
  superseded_by?: string | null;
  reason: string;
  updated_at?: string | null;
  sql_hidden: boolean;
}

export interface RepairPatternListResponse {
  tenant: string;
  count: number;
  result: RepairPatternRecord[];
}

export type PatternAction = 'promote' | 'deprecate' | 'quarantine' | 'restore';

export async function fetchRepairMetrics(): Promise<RepairMetricsResponse> {
  const { json } = await SupersetClient.get({ endpoint: `${BASE}/repair/metrics/` });
  return json as RepairMetricsResponse;
}

export async function fetchRepairPatterns(
  state?: string,
): Promise<RepairPatternListResponse> {
  const query = state ? `?state=${encodeURIComponent(state)}` : '';
  const { json } = await SupersetClient.get({
    endpoint: `${BASE}/repair/patterns/${query}`,
  });
  return json as RepairPatternListResponse;
}

export async function actOnPattern(
  patternId: string,
  action: PatternAction,
): Promise<{ id: string; state: string }> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/repair/patterns/${patternId}/${action}/`,
    jsonPayload: {},
  });
  return json as { id: string; state: string };
}

// --- Dataset drafts (P6-F02/F03/F05) ----------------------------------

/** One stored dataset proposal, with what was measured about it. */
export interface DatasetDraftRecord {
  id: string;
  name: string;
  kind: string;
  database: string;
  status: string;
  version: number;
  quality_ok: boolean;
  quality: {
    codes?: string[];
    blocking?: boolean;
    requires_confirmation?: boolean;
    warnings?: { code: string; message: string; column?: string }[];
  };
  fingerprint: string;
  superset_dataset_id?: number | null;
  sql_hidden: boolean;
}

export interface DatasetQualityWarning {
  code: string;
  severity: string;
  column?: string;
  message: string;
}

export interface DatasetDraftResponse {
  draft: DatasetDraftRecord;
  quality: {
    ok?: boolean;
    blocking?: boolean;
    codes?: string[];
    warnings?: DatasetQualityWarning[];
  };
  profile?: {
    row_count?: number;
    sampled?: boolean;
    duplicate_grain?: number;
    columns?: number;
  };
}

export interface DatasetSavedResponse {
  dataset: {
    dataset_id: number;
    name: string;
    schema?: string;
    columns: number;
    metrics: string[];
    verified: boolean;
  };
  blocks: unknown[];
}

/**
 * Build a draft from a statement that already ran. The proxy previews it through
 * the guarded path, so the draft is measured rather than assumed.
 */
export async function createDatasetDraft(payload: {
  question: string;
  sql: string;
  database?: string;
  conversationId?: string;
  idempotencyKey: string;
}): Promise<DatasetDraftResponse> {
  const spec: Record<string, unknown> = {
    name: payload.question.slice(0, 100) || 'dataset',
    kind: 'virtual',
    database: payload.database,
    sql: payload.sql,
  };
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/datasets/drafts/`,
    jsonPayload: {
      spec,
      conversation_id: payload.conversationId,
      idempotency_key: payload.idempotencyKey,
    },
  });
  return json as DatasetDraftResponse;
}

export async function saveDatasetDraft(
  draftId: string,
): Promise<DatasetSavedResponse> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/datasets/drafts/${draftId}/save/`,
    jsonPayload: {},
  });
  return json as DatasetSavedResponse;
}

export async function scrapDatasetDraft(
  draftId: string,
  reason: string,
): Promise<void> {
  await SupersetClient.post({
    endpoint: `${BASE}/datasets/drafts/${draftId}/scrap/`,
    jsonPayload: { reason },
  });
}

// --- Charts (P7-F03/F05/F06/F08) ---------------------------------------

/** One visualization the rules (and optionally the model) proposed. */
export interface ChartCandidate {
  id: string;
  viz_type: string;
  family: string;
  title: string;
  form_data: Record<string, unknown>;
  rationale: string;
  score: number;
  factors?: Record<string, number>;
}

export interface ChartRecommendationResponse {
  candidates: ChartCandidate[];
  eliminated: { viz_type: string; family: string; reason: string }[];
  catalog: { available: string[]; denied: string[] };
  dataset: { dataset_id: number; name: string };
  /** Whether the deployment could measure cardinalities from a sample. */
  measured: boolean;
  ranking: { order: string[]; source: string; notes: string };
}

export interface ChartPreviewResponse {
  columns: string[];
  rows: string[][];
  row_count: number;
  truncated: boolean;
  viz_type?: string;
  blocks: unknown[];
}

export interface ChartDraftEnvelope {
  draft: {
    id: string;
    viz_type: string;
    title: string;
    version: number;
    status: string;
  };
}

export interface ChartSavedResponse {
  chart: { slice_id: number; name: string; viz_type: string };
  blocks: unknown[];
}

export async function recommendCharts(payload: {
  dataset_id: number;
  question?: string;
  rank_with_model?: boolean;
}): Promise<ChartRecommendationResponse> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/charts/recommend/`,
    jsonPayload: payload,
  });
  return json as ChartRecommendationResponse;
}

export async function createChartDraft(payload: {
  dataset_id: number;
  candidate: ChartCandidate;
  rankingSource?: string;
  conversationId?: string;
  idempotencyKey: string;
}): Promise<ChartDraftEnvelope> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/charts/drafts/`,
    jsonPayload: {
      dataset_id: payload.dataset_id,
      candidate: payload.candidate,
      ranking_source: payload.rankingSource,
      conversation_id: payload.conversationId,
      idempotency_key: payload.idempotencyKey,
    },
  });
  return json as ChartDraftEnvelope;
}

/** Render the draft's data through Superset, as the current user. */
export async function previewChart(
  draftId: string,
): Promise<ChartPreviewResponse> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/charts/preview/`,
    jsonPayload: { draft_id: draftId },
  });
  return json as ChartPreviewResponse;
}

export async function saveChart(draftId: string): Promise<ChartSavedResponse> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/charts/drafts/${draftId}/save/`,
    jsonPayload: {},
  });
  return json as ChartSavedResponse;
}

export async function scrapChartDraft(
  draftId: string,
  reason: string,
): Promise<void> {
  await SupersetClient.post({
    endpoint: `${BASE}/charts/drafts/${draftId}/scrap/`,
    jsonPayload: { reason },
  });
}
