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
import { useCallback, useEffect, useMemo, useState } from 'react';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import {
  Button,
  Card,
  Input,
  Popconfirm,
  RawAntdSelect as Select,
  Table,
  TableSize,
  Tag,
} from '@superset-ui/core/components';
import { useToasts } from 'src/components/MessageToasts/withToasts';
import { SupersetClient } from '@superset-ui/core';

/**
 * Plan, check, preview and publish a dashboard (P8), which had a backend and no
 * way to reach it from the browser.
 *
 * The service owns the planning rules, so this panel never decides what belongs
 * on a dashboard: it sends the goal and the datasets the principal may already
 * open, and renders what comes back — including the slots that must still be
 * generated, which is the part a caller cannot skip before publishing.
 */

const Panel = styled.div`
  ${({ theme }) => `
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit * 2}px;
  `}
`;

const Row = styled.div`
  ${({ theme }) => `
    display: flex;
    gap: ${theme.sizeUnit * 2}px;
    align-items: center;
    flex-wrap: wrap;
  `}
`;

const Hint = styled.p`
  ${({ theme }) => `
    margin: 0;
    color: ${theme.colorTextSecondary};
    font-size: ${theme.fontSizeSM}px;
  `}
`;

const Raw = styled.pre`
  ${({ theme }) => `
    margin: 0;
    padding: ${theme.sizeUnit * 2}px;
    background: ${theme.colorFillSecondary};
    border-radius: ${theme.borderRadius}px;
    font-size: ${theme.fontSizeSM}px;
    max-height: 220px;
    overflow: auto;
  `}
`;

interface DatasetOption {
  value: number;
  label: string;
}

interface DashboardOption {
  value: number;
  label: string;
}

interface Slot {
  id: string;
  title: string;
  family: string;
  section: string;
  dataset_name: string;
  viz_type: string;
  chart_id: number | null;
  rationale: string;
}

interface PlanResponse {
  title?: string;
  count?: number;
  reuse_count?: number;
  generate_count?: number;
  slots?: Slot[];
  needs?: { kind?: string; section?: string; reason?: string }[];
  excluded?: { name?: string; reason?: string }[];
  quality?: Record<string, number>;
  notes?: string[];
  columns?: Record<string, string>;
  message?: string;
}

const BASE = '/aiprompt/api';

async function post<T>(endpoint: string, payload: unknown): Promise<T> {
  const { json } = await SupersetClient.post({
    endpoint,
    jsonPayload: payload as Record<string, unknown>,
  });
  return json as T;
}

async function describe(error: unknown): Promise<string> {
  const candidate = error as {
    json?: () => Promise<{ message?: string }>;
    statusText?: string;
    message?: string;
  };
  if (candidate && typeof candidate.json === 'function') {
    try {
      const body = await candidate.json();
      if (body?.message) {
        return body.message;
      }
    } catch {
      // fall through
    }
  }
  return candidate?.message || candidate?.statusText || 'Unexpected error';
}

export default function DashboardPanel() {
  const { addDangerToast, addSuccessToast } = useToasts();
  const [datasets, setDatasets] = useState<DatasetOption[]>([]);
  const [chosen, setChosen] = useState<number[]>([]);
  const [goal, setGoal] = useState('');
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [layout, setLayout] = useState<Record<string, unknown> | null>(null);
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null);
  const [published, setPublished] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState('');
  // Where the plan should land. ``null`` means "a new dashboard"; a number means
  // "add these charts to that one". The choice is the user's, because only they
  // know whether the answer belongs beside what they already publish.
  const [dashboards, setDashboards] = useState<DashboardOption[]>([]);
  const [targetDashboardId, setTargetDashboardId] = useState<number | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const { json } = await SupersetClient.get({
          endpoint: `${BASE}/schema/`,
        });
        const tables = (json?.tables ?? []) as {
          dataset_id?: number;
          name?: string;
          database?: string;
        }[];
        setDatasets(
          tables
            .filter(table => typeof table.dataset_id === 'number')
            .map(table => ({
              value: table.dataset_id as number,
              label: `${table.database || ''}.${table.name || ''}`,
            })),
        );
      } catch (error) {
        addDangerToast(await describe(error));
      }
    };
    load();
    // The authorized slice does not change while the panel is open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const loadDashboards = async () => {
      try {
        const { json } = await SupersetClient.get({
          endpoint: '/api/v1/dashboard/?q=(page_size:100)',
        });
        const rows = (json?.result ?? []) as {
          id?: number;
          dashboard_title?: string;
        }[];
        setDashboards(
          rows
            .filter(row => typeof row.id === 'number')
            .map(row => ({
              value: row.id as number,
              label: row.dashboard_title || `Dashboard ${row.id}`,
            })),
        );
      } catch {
        // Publishing into a new dashboard still works; an empty list just
        // means "no existing target to offer".
      }
    };
    loadDashboards();
  }, []);

  const slots = useMemo<Slot[]>(() => plan?.slots ?? [], [plan]);
  const needsGeneration = slots.filter(slot => slot.chart_id === null).length;

  const onPlan = useCallback(async () => {
    if (!goal.trim()) {
      addDangerToast(t('Describe the dashboard first'));
      return;
    }
    setBusy('plan');
    try {
      const body = await post<PlanResponse>(`${BASE}/dashboards/plan/`, {
        goal,
        dataset_ids: chosen.length ? chosen : undefined,
      });
      setPlan(body);
      setLayout(null);
      setPreview(null);
      setPublished(null);
      addSuccessToast(t('Planned %s slot(s)', body.count ?? 0));
    } catch (error) {
      addDangerToast(await describe(error));
    } finally {
      setBusy('');
    }
  }, [addDangerToast, addSuccessToast, chosen, goal]);

  const onLayout = useCallback(async () => {
    if (!plan) {
      return;
    }
    setBusy('layout');
    try {
      setLayout(
        await post(`${BASE}/dashboards/layout/`, {
          title: plan.title || goal,
          slots,
          columns: plan.columns ?? {},
        }),
      );
    } catch (error) {
      addDangerToast(await describe(error));
    } finally {
      setBusy('');
    }
  }, [addDangerToast, goal, plan, slots]);

  const onPreview = useCallback(async () => {
    if (!plan) {
      return;
    }
    setBusy('preview');
    try {
      setPreview(
        await post(`${BASE}/dashboards/preview/`, {
          title: plan.title || goal,
          goal,
          audience: 'unspecified',
          slots,
        }),
      );
      addSuccessToast(t('Every slot answered with real data'));
    } catch (error) {
      addDangerToast(await describe(error));
    } finally {
      setBusy('');
    }
  }, [addDangerToast, addSuccessToast, goal, plan, slots]);

  const onPublish = useCallback(async () => {
    if (!plan) {
      return;
    }
    const updating = targetDashboardId !== null;
    setBusy('publish');
    try {
      const body = await post<Record<string, unknown>>(
        `${BASE}/dashboards/publish/`,
        {
          // ``update`` adds the planned tiles to the target's existing layout
          // (nothing already on that dashboard is removed); ``create`` builds a
          // new dashboard. Both go through the same ledger, so a failed update
          // compensates the same way a failed create does.
          mode: updating ? 'update' : 'create',
          ...(updating ? { dashboard_id: targetDashboardId } : {}),
          title: plan.title || goal,
          goal,
          slots,
          visibility: 'private',
          idempotency_key: `panel-${updating ? targetDashboardId : 'new'}-${
            plan.title || goal
          }`.slice(0, 120),
        },
      );
      setPublished(body);
      addSuccessToast(t('Dashboard published'));
    } catch (error) {
      addDangerToast(await describe(error));
    } finally {
      setBusy('');
    }
  }, [addDangerToast, addSuccessToast, goal, plan, slots, targetDashboardId]);

  return (
    <Card data-test="ai-dashboard-panel">
      <Panel>
        <Row>
          <Input
            placeholder={t('What is this dashboard for?')}
            value={goal}
            onChange={(event: { target: { value: string } }) => setGoal(event.target.value)}
            style={{ minWidth: 320 }}
          />
          <Select
            mode="multiple"
            allowClear
            placeholder={t('Datasets (default: everything you may open)')}
            style={{ minWidth: 280 }}
            value={chosen}
            onChange={(value: number[]) => setChosen(value)}
            options={datasets}
          />
          <Button buttonStyle="primary" onClick={onPlan} loading={busy === 'plan'}>
            {t('Plan')}
          </Button>
        </Row>

        {plan && (
          <>
            <Row>
              <Tag color="blue">{t('%s slot(s)', plan.count ?? 0)}</Tag>
              <Tag color={needsGeneration === 0 ? 'green' : 'orange'}>
                {t('%s reused, %s to generate', plan.reuse_count ?? 0, needsGeneration)}
              </Tag>
              <Button
                buttonStyle="secondary"
                onClick={onLayout}
                loading={busy === 'layout'}
              >
                {t('Check the layout')}
              </Button>
              <Button
                buttonStyle="secondary"
                onClick={onPreview}
                loading={busy === 'preview'}
              >
                {t('Preview with real data')}
              </Button>
              <Select
                allowClear
                placeholder={t('Publish into: a new dashboard')}
                style={{ minWidth: 240 }}
                value={targetDashboardId ?? undefined}
                onChange={(value: number | undefined) =>
                  setTargetDashboardId(value ?? null)
                }
                options={dashboards}
              />
              <Popconfirm
                title={
                  targetDashboardId === null
                    ? t('Publish this dashboard?')
                    : t('Add these charts to the chosen dashboard?')
                }
                description={t(
                  'Generated slots become real charts. The plan is not previewed first if you skip it.',
                )}
                onConfirm={onPublish}
                okText={t('Publish')}
                cancelText={t('Cancel')}
                disabled={needsGeneration > 0}
              >
                <Button
                  buttonStyle="primary"
                  disabled={needsGeneration > 0}
                  loading={busy === 'publish'}
                >
                  {targetDashboardId === null
                    ? t('Publish')
                    : t('Add to dashboard')}
                </Button>
              </Popconfirm>
            </Row>

            {needsGeneration > 0 && (
              <Hint>
                {t(
                  'Publishing is blocked until every slot exists: %s still need to be generated.',
                  needsGeneration,
                )}
              </Hint>
            )}

            <Table
              size={TableSize.Small}
              data={slots}
              rowKey="id"
              columns={[
                { title: t('Section'), dataIndex: 'section' },
                { title: t('Title'), dataIndex: 'title' },
                { title: t('Dataset'), dataIndex: 'dataset_name' },
                { title: t('Chart type'), dataIndex: 'viz_type' },
                {
                  title: t('Source'),
                  dataIndex: 'chart_id',
                  render: (value: number | null) =>
                    value === null ? (
                      <Tag color="orange">{t('generate')}</Tag>
                    ) : (
                      <Tag color="green">{t('chart %s', value)}</Tag>
                    ),
                },
              ]}
            />

            {(plan.needs ?? []).length > 0 && (
              <Hint>
                {t('Needs: %s', (plan.needs ?? [])
                  .map(need => need.kind || need.reason || '')
                  .filter(Boolean)
                  .join(', '))}
              </Hint>
            )}
            {(plan.excluded ?? []).length > 0 && (
              <Hint>
                {t('Excluded: %s', (plan.excluded ?? [])
                  .map(entry => `${entry.name || ''} (${entry.reason || ''})`)
                  .join(', '))}
              </Hint>
            )}
            {plan.notes && plan.notes.length > 0 && (
              <Hint>{plan.notes.join(' · ')}</Hint>
            )}
          </>
        )}

        {layout && <Raw>{JSON.stringify(layout, null, 2)}</Raw>}
        {preview && <Raw>{JSON.stringify(preview, null, 2)}</Raw>}
        {published && <Raw>{JSON.stringify(published, null, 2)}</Raw>}
      </Panel>
    </Card>
  );
}
