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
  BillingImport,
  BillingSource,
  BillingVariance,
  Budget,
  BudgetPayload,
  ModelPrice,
  OutboxStatus,
  Reconciliation,
  UsageAttempt,
  UsageSummary,
} from './types';

const BASE = '/aiprompt/api/usage';

export interface UsageFilters {
  since?: string;
  module?: string;
  limit?: number;
}

function toQuery(filters: UsageFilters & { group_by?: string } = {}): string {
  const params = new URLSearchParams();
  if (filters.since) {
    params.set('since', filters.since);
  }
  if (filters.module) {
    params.set('module', filters.module);
  }
  if (filters.limit) {
    params.set('limit', String(filters.limit));
  }
  if (filters.group_by) {
    params.set('group_by', filters.group_by);
  }
  const query = params.toString();
  return query ? `?${query}` : '';
}

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

export async function fetchSummary(
  filters: UsageFilters & { group_by?: string } = {},
): Promise<UsageSummary> {
  const { json } = await SupersetClient.get({
    endpoint: `${BASE}/summary/${toQuery(filters)}`,
  });
  return json as UsageSummary;
}

export async function fetchAttempts(
  filters: UsageFilters = { limit: 25 },
): Promise<UsageAttempt[]> {
  const { json } = await SupersetClient.get({
    endpoint: `${BASE}/attempts/${toQuery(filters)}`,
  });
  return (json?.result ?? []) as UsageAttempt[];
}

export async function fetchReconciliation(): Promise<Reconciliation> {
  const { json } = await SupersetClient.get({
    endpoint: `${BASE}/reconciliation/`,
  });
  return json as Reconciliation;
}

export async function fetchBudgets(): Promise<Budget[]> {
  const { json } = await SupersetClient.get({ endpoint: `${BASE}/budgets/` });
  return (json?.budgets ?? []) as Budget[];
}

export async function saveBudget(payload: BudgetPayload): Promise<Budget> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/budgets/`,
    jsonPayload: payload,
  });
  return json as Budget;
}

export async function deleteBudget(budgetId: string): Promise<void> {
  await SupersetClient.delete({ endpoint: `${BASE}/budgets/${budgetId}/` });
}

export async function fetchPrices(): Promise<ModelPrice[]> {
  const { json } = await SupersetClient.get({ endpoint: `${BASE}/prices/` });
  return (json?.result ?? []) as ModelPrice[];
}

export async function publishPrice(
  payload: Partial<ModelPrice>,
): Promise<ModelPrice> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/prices/`,
    jsonPayload: payload,
  });
  return json as ModelPrice;
}

export async function fetchOutbox(): Promise<OutboxStatus> {
  const { json } = await SupersetClient.get({ endpoint: `${BASE}/outbox/` });
  return json as OutboxStatus;
}

export async function drainOutbox(): Promise<Record<string, number>> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/outbox/drain/`,
    jsonPayload: { limit: 25 },
  });
  return json as Record<string, number>;
}

export async function fetchBillingImports(): Promise<BillingImport[]> {
  const { json } = await SupersetClient.get({
    endpoint: `${BASE}/billing/imports/`,
  });
  return (json?.result ?? []) as BillingImport[];
}

export async function importBilling(payload: {
  provider: string;
  currency: string;
  period_start: string;
  period_end: string;
  source?: string;
  rows: Record<string, unknown>[];
}): Promise<BillingImport> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/billing/imports/`,
    jsonPayload: payload,
  });
  return json as BillingImport;
}

export async function fetchBillingVariance(
  importId: string,
): Promise<BillingVariance> {
  const { json } = await SupersetClient.get({
    endpoint: `${BASE}/billing/imports/${importId}/variance/`,
  });
  return json as BillingVariance;
}

export async function fetchBillingSource(): Promise<BillingSource> {
  const { json } = await SupersetClient.get({
    endpoint: `${BASE}/billing/source/`,
  });
  return json as BillingSource;
}

export async function fetchBillingExport(): Promise<{
  rows: number;
  replayed: boolean;
  source: string;
}> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/billing/fetch/`,
    jsonPayload: {},
  });
  return json as { rows: number; replayed: boolean; source: string };
}
