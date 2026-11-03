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
export type ProviderType =
  | 'openai_compatible'
  | 'anthropic'
  | 'gemini'
  | 'azure_openai'
  | 'deepseek';

export interface ProviderTypeOption {
  key: ProviderType;
  label: string;
  default_base_url: string;
  default_model: string;
  requires_base_url: boolean;
  supports_api_version: boolean;
  model_placeholder: string;
}

export interface ProviderAdvancedConfig {
  temperature?: number;
  max_tokens?: number;
  timeout_seconds?: number;
  api_version?: string;
}

export interface ProviderApiKeyInfo {
  configured: boolean;
  fingerprint: string;
  last_four: string;
}

export interface ProviderTestInfo {
  status: string | null;
  error: string | null;
  at: string | null;
}

/**
 * Passive health of one provider row. `state` is `open` while the circuit
 * breaker is skipping the provider, so the table can explain missing traffic.
 */
export interface ProviderHealthInfo {
  state: 'open' | 'closed';
  consecutive_failures: number;
  open_until: string | null;
  last_error: string | null;
  last_failure_at: string | null;
}

export interface ProviderRecord {
  id: string;
  tenant: string;
  name: string;
  provider_type: ProviderType;
  provider_label: string;
  base_url: string;
  default_model: string;
  config: ProviderAdvancedConfig;
  enabled: boolean;
  is_default: boolean;
  priority: number;
  api_key: ProviderApiKeyInfo;
  last_test: ProviderTestInfo;
  health: ProviderHealthInfo;
  created_at: string | null;
  updated_at: string | null;
}

export interface EnvFallbackStatus {
  gemini_configured: boolean;
  deepseek_configured: boolean;
  note: string;
}

export interface ProviderListResponse {
  tenant: string;
  count: number;
  result: ProviderRecord[];
  env_fallback?: EnvFallbackStatus;
}

/** Payload accepted by POST/PUT /aiprovider/api/providers/. */
export interface ProviderPayload {
  name: string;
  provider_type: ProviderType;
  base_url: string;
  api_key?: string;
  default_model: string;
  config: ProviderAdvancedConfig;
  enabled: boolean;
  is_default: boolean;
  priority: number;
}

export interface ProviderTestResult {
  status: 'succeeded' | 'failed';
  model?: string;
  duration_ms?: number;
  error_code?: string;
  retryable?: boolean;
  message?: string;
  /** Server-side correlation id for the attempt, quoted in support requests. */
  correlation_id?: string;
}
