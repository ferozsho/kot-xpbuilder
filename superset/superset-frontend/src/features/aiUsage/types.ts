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
export interface UsageTotals {
  requests: number;
  succeeded: number;
  failed: number;
  missing_usage: number;
  fallbacks: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  /** Null means no price is known — deliberately not zero. */
  estimated_cost_micros: number | null;
  priced_requests: number;
  avg_duration_ms: number | null;
  last_attempt?: string | null;
}

export interface UsageGroup extends UsageTotals {
  provider?: string;
  model?: string;
  module?: string;
  status?: string;
  currency?: string;
}

export interface UsageSummary {
  tenant: string;
  group_by: string[];
  groups: UsageGroup[];
  totals: UsageTotals;
  cost_is_estimated: boolean;
  since: string | null;
}

export interface UsageAttempt {
  id: string;
  correlation_id: string;
  module: string;
  feature: string;
  provider: string;
  model: string;
  operation: string;
  attempt_ordinal: number;
  fallback: boolean;
  status: string;
  error_class: string | null;
  error_detail: string;
  provider_request_id: string | null;
  superset_user_id: number | null;
  input_tokens: number | null;
  cached_input_tokens: number | null;
  output_tokens: number | null;
  reasoning_tokens: number | null;
  total_tokens: number | null;
  usage_source: string;
  duration_ms: number | null;
  price_version: number | null;
  currency: string;
  estimated_cost_micros: number | null;
  started_at: string | null;
}

export interface Reconciliation {
  attempts: number;
  missing_usage: number;
  unpriced: number;
  negative_tokens: number;
  unfinished: number;
  inconsistent_totals: number;
  enabled_providers: number;
  price_gaps: string[];
  outbox_pending: number;
  outbox_dead_letter: number;
  healthy: boolean;
  tenant_key?: string;
}

export interface Budget {
  id: string;
  scope: 'global' | 'user' | 'module';
  scope_ref: string;
  period: 'day' | 'month';
  cost_micros_limit: number | null;
  tokens_limit: number | null;
  warn_percent: number;
  hard_stop: boolean;
  enabled: boolean;
  currency: string;
  spent_micros: number;
  reserved_micros: number;
  spent_tokens: number;
  blocked_count: number;
  percent_used: number;
  state: 'ok' | 'warning' | 'exhausted' | 'over' | 'disabled';
  period_start: string | null;
}

export interface BudgetPayload {
  scope: 'global' | 'user' | 'module';
  scope_ref?: string;
  period: 'day' | 'month';
  cost_micros_limit?: number | null;
  tokens_limit?: number | null;
  warn_percent: number;
  hard_stop: boolean;
  enabled: boolean;
  currency?: string;
}

export interface OutboxEvent {
  id: string;
  aggregate_type: string;
  aggregate_id: string;
  event_type: string;
  status: string;
  attempt_count: number;
  last_error: string | null;
  available_at: string | null;
  delivered_at: string | null;
}

export interface OutboxStatus {
  tenant: string;
  counts: Record<string, number>;
  dead_letters: OutboxEvent[];
  count: number;
}

export interface ModelPrice {
  provider: string;
  model: string;
  version: number;
  currency: string;
  unit_tokens: number;
  input_micros_per_unit: number;
  cached_input_micros_per_unit: number | null;
  output_micros_per_unit: number;
  reasoning_micros_per_unit: number | null;
  effective_from: string | null;
  effective_to: string | null;
  source: string;
  source_url: string;
}

export interface BillingImport {
  id: string;
  provider: string;
  period_start: string | null;
  period_end: string | null;
  currency: string;
  source: string;
  row_count: number;
  total_actual_micros: number;
  created_at: string | null;
}

export interface BillingVariance {
  import: BillingImport;
  lines: number;
  matched_lines: number;
  unmatched_lines: number;
  matched_attempts: number;
  unpriced_attempts: number;
  unattributed_attempts: number;
  actual_micros: number;
  estimated_micros: number;
  difference_micros: number;
  difference_percent: number | null;
  estimate_is_preserved: boolean;
  models: {
    model: string;
    lines: number;
    matched: number;
    actual_micros: number;
    estimated_micros: number;
    difference_micros: number;
  }[];
}

export interface BillingSource {
  configured: boolean;
  provider: string;
  currency: string;
  host: string;
}
