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
export type MessageRole = 'user' | 'assistant' | 'tool' | 'system';

/**
 * The seven outcomes of the deterministic intent router (P2-F04).
 * `clarification` means the router asked a question instead of guessing.
 */
export type IntentName =
  | 'query'
  | 'dataset'
  | 'chart'
  | 'dashboard'
  | 'explanation'
  | 'modification'
  | 'clarification';

export interface ConversationRecord {
  id: string;
  tenant: string;
  superset_user_id: number;
  title: string;
  status: 'active' | 'archived';
  context_version: number;
  last_message_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface MessageUsage {
  input_tokens?: number | null;
  cached_input_tokens?: number | null;
  output_tokens?: number | null;
  reasoning_tokens?: number | null;
  total_tokens?: number | null;
  source?: string;
}

/**
 * Structured response blocks (P2-F06). The backend normalizes and bounds every
 * block, so the renderer can assume the shapes below; anything else is dropped
 * before it reaches the browser.
 */
export interface ClarificationBlock {
  type: 'clarification';
  question: string;
}

export interface PlanBlock {
  type: 'plan';
  title: string;
  items: string[];
}

export interface AssumptionsBlock {
  type: 'assumptions';
  items: string[];
}

export interface SqlPreviewBlock {
  type: 'sql_preview';
  sql: string;
  dialect: string;
  truncated: boolean;
}

export interface ResultPreviewBlock {
  type: 'result_preview';
  columns: string[];
  rows: string[][];
  row_count: number;
  truncated: boolean;
}

export interface DatasetProfileBlock {
  type: 'dataset_profile';
  table: string;
  columns: { name: string; type: string; nullable: boolean }[];
  row_count: number | null;
}

export interface ChartRecommendationBlock {
  type: 'chart_recommendation';
  chart_type: string;
  x: string;
  y: string[];
  reason: string;
}

export interface DashboardProposalBlock {
  type: 'dashboard_proposal';
  title: string;
  sections: { title: string; items: string[] }[];
}

export interface WarningsBlock {
  type: 'warnings';
  items: string[];
}

export interface RepairBlock {
  type: 'repair';
  outcome: 'fixed' | 'failed' | 'unchanged' | 'skipped';
  attempts: number;
  notes: string;
}

/**
 * Native Superset links (P2-F09). Only the object identity is sent — never a
 * URL — so the page resolves it against this deployment's own routes.
 */
export interface LinksBlock {
  type: 'links';
  items: {
    label: string;
    entity_type: 'dataset' | 'chart' | 'dashboard';
    entity_id: string;
  }[];
}

export type ResponseBlock =
  | ClarificationBlock
  | PlanBlock
  | AssumptionsBlock
  | SqlPreviewBlock
  | ResultPreviewBlock
  | DatasetProfileBlock
  | ChartRecommendationBlock
  | DashboardProposalBlock
  | WarningsBlock
  | RepairBlock
  | LinksBlock;

export interface MessageRecord {
  id: string;
  conversation_id: string;
  role: MessageRole;
  content: string | null;
  //: Generation this message belongs to; only the conversation's current one
  //: is replayed to the AI. A change marks an explicit context reset.
  context_version?: number;
  structured_content: {
    kind?: 'answer' | 'error' | 'clarification';
    intent?: IntentName;
    target_intent?: IntentName | null;
    pending_intent?: IntentName | null;
    intent_reasons?: string[];
    intent_confidence?: number;
    blocks?: ResponseBlock[];
    correlation_id?: string;
    provider?: string;
    model?: string;
    finish_reason?: string | null;
    usage?: MessageUsage;
    error_code?: string;
    retryable?: boolean;
  };
  created_at: string | null;
}

export interface ConversationListResponse {
  tenant: string;
  count: number;
  result: ConversationRecord[];
}

export interface ConversationDetailResponse {
  conversation: ConversationRecord;
  messages: MessageRecord[];
}

export interface ContextResetResponse {
  conversation: ConversationRecord;
  context_version: number;
}

export type JobStatus =
  | 'queued'
  | 'running'
  | 'clarification'
  | 'succeeded'
  | 'failed'
  | 'cancelled';

export interface JobStep {
  step_key: string;
  ordinal: number;
  status: string;
  attempt_count: number;
  error_code: string | null;
}

export interface JobRecord {
  id: string;
  conversation_id: string;
  job_type: string;
  status: JobStatus;
  attempt_count: number;
  max_attempts: number;
  context_version: number;
  cancel_requested: boolean;
  requires_confirmation: boolean;
  error_code: string | null;
  replayed: boolean;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  steps: JobStep[];
}

export interface JobListResponse {
  count: number;
  result: JobRecord[];
}

export interface ChatTurnResponse {
  conversation_id: string;
  correlation_id: string;
  answer: string;
  provider: string;
  model: string;
  intent: IntentName;
  clarification: boolean;
  target_intent: IntentName | null;
  intent_reasons: string[];
  usage: MessageUsage;
  attempts: {
    provider: string;
    model: string;
    outcome: string;
    attempt: number;
    error_code: string | null;
  }[];
}
