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

export type SettingKind = 'int' | 'bool' | 'enum' | 'text';

export interface SettingEntry {
  key: string;
  kind: SettingKind;
  default: unknown;
  owner: string;
  summary: string;
  minimum: number | null;
  maximum: number | null;
  allowed: string[];
  scopes: string[];
  dynamic: boolean;
  restart_required: boolean;
  /** True when ``maximum`` is a platform ceiling, not a preference. */
  ceiling: boolean;
  effective: unknown;
  source: 'tenant' | 'global' | 'default';
  version: number;
  global: unknown;
  tenant: unknown;
}

export interface SettingsResponse {
  tenant: string;
  count: number;
  restart_required: string[];
  settings: SettingEntry[];
}

export interface SettingChange {
  key: string;
  scope: string;
  version: number;
  before: unknown;
  after: unknown;
  actor: string;
  reason: string;
  created_at: string | null;
}

export interface HistoryEntry extends SettingChange {
  id: string;
}

const BASE = '/aiprompt/api/settings';

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

export async function fetchSettings(tenant?: string): Promise<SettingsResponse> {
  const query = tenant ? `?tenant=${encodeURIComponent(tenant)}` : '';
  const { json } = await SupersetClient.get({ endpoint: `${BASE}/${query}` });
  return json as SettingsResponse;
}

export async function saveSettings(payload: {
  values: Record<string, unknown>;
  scope: string;
  reason: string;
  expected_version?: number | null;
}): Promise<{ count: number; result: SettingChange[] }> {
  const { json } = await SupersetClient.put({
    endpoint: `${BASE}/`,
    jsonPayload: payload,
  });
  return json as { count: number; result: SettingChange[] };
}

export async function fetchHistory(
  key?: string,
  limit = 50,
): Promise<HistoryEntry[]> {
  const params = new URLSearchParams();
  if (key) {
    params.set('key', key);
  }
  params.set('limit', String(limit));
  const { json } = await SupersetClient.get({
    endpoint: `${BASE}/history/?${params.toString()}`,
  });
  return (json?.result ?? []) as HistoryEntry[];
}

export async function rollbackSetting(
  auditId: string,
  reason: string,
): Promise<SettingChange> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/rollback/${auditId}/`,
    jsonPayload: { reason },
  });
  return json as SettingChange;
}

export interface HeldAtCeiling {
  key: string;
  stored: unknown;
  in_force: unknown;
}

export interface SettingsHealth {
  tenant: string;
  stored: number;
  declared: number;
  restart_required: string[];
  held_at_ceiling: HeldAtCeiling[];
  limits: {
    max_rows: number;
    timeout_ms: number;
    max_concurrent: number;
    max_bytes: number;
  };
  alerts: { enabled: boolean; webhook: boolean; breaches: number };
  sql_slots_active: number;
  healthy: boolean;
}

export interface RetentionPreview {
  windows: Record<string, number>;
  would_delete: Record<string, number>;
}

export interface RetentionResult {
  windows: Record<string, number>;
  removed: Record<string, number>;
  total: number;
}

export async function fetchHealth(tenant?: string): Promise<SettingsHealth> {
  const query = tenant ? `?tenant=${encodeURIComponent(tenant)}` : '';
  const { json } = await SupersetClient.get({
    endpoint: `${BASE}/health/${query}`,
  });
  return json as SettingsHealth;
}

export async function fetchRetention(tenant?: string): Promise<RetentionPreview> {
  const query = tenant ? `?tenant=${encodeURIComponent(tenant)}` : '';
  const { json } = await SupersetClient.get({
    endpoint: `/aiprompt/api/retention/${query}`,
  });
  return json as RetentionPreview;
}

export async function applyRetention(tenant?: string): Promise<RetentionResult> {
  const { json } = await SupersetClient.post({
    endpoint: '/aiprompt/api/retention/',
    jsonPayload: tenant ? { tenant } : {},
  });
  return json as RetentionResult;
}
