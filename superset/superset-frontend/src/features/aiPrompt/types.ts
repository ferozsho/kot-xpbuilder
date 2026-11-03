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

export interface MessageRecord {
  id: string;
  conversation_id: string;
  role: MessageRole;
  content: string | null;
  structured_content: {
    kind?: 'answer' | 'error';
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

export interface ChatTurnResponse {
  conversation_id: string;
  correlation_id: string;
  answer: string;
  provider: string;
  model: string;
  usage: MessageUsage;
  attempts: {
    provider: string;
    model: string;
    outcome: string;
    attempt: number;
    error_code: string | null;
  }[];
}
