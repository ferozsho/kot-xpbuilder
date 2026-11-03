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
  ProviderListResponse,
  ProviderPayload,
  ProviderRecord,
  ProviderTestResult,
  ProviderTypeOption,
} from './types';

const BASE = '/aiprovider/api';

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

export async function fetchProviders(): Promise<ProviderListResponse> {
  const { json } = await SupersetClient.get({ endpoint: `${BASE}/providers/` });
  return json as ProviderListResponse;
}

export async function fetchProviderTypes(): Promise<ProviderTypeOption[]> {
  const { json } = await SupersetClient.get({
    endpoint: `${BASE}/providers/types/`,
  });
  return (json?.result ?? []) as ProviderTypeOption[];
}

export async function createProvider(
  payload: ProviderPayload,
): Promise<ProviderRecord> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/providers/`,
    jsonPayload: payload,
  });
  return json as ProviderRecord;
}

export async function updateProvider(
  id: string,
  payload: ProviderPayload,
): Promise<ProviderRecord> {
  const { json } = await SupersetClient.put({
    endpoint: `${BASE}/providers/${id}/`,
    jsonPayload: payload,
  });
  return json as ProviderRecord;
}

export async function deleteProvider(id: string): Promise<void> {
  await SupersetClient.delete({ endpoint: `${BASE}/providers/${id}/` });
}

export async function testProvider(id: string): Promise<ProviderTestResult> {
  const { json } = await SupersetClient.post({
    endpoint: `${BASE}/providers/${id}/test/`,
    jsonPayload: {},
  });
  return json as ProviderTestResult;
}
