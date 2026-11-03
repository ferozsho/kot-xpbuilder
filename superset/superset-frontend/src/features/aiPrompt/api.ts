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
 */
export async function askWithSql(
  conversationId: string,
  question: string,
  databaseId?: number,
): Promise<SqlTurnResponse> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/conversations/${conversationId}/sql/`,
    jsonPayload:
      databaseId === undefined ? { question } : { question, database_id: databaseId },
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
