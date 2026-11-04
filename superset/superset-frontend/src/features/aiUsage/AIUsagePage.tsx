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
  Popconfirm,
  RawAntdSelect as Select,
  Table,
  TableSize,
  Tag,
} from '@superset-ui/core/components';
import { useToasts } from 'src/components/MessageToasts/withToasts';
import {
  deleteBudget,
  describeError,
  drainOutbox,
  fetchAttempts,
  fetchBillingExport,
  fetchBillingImports,
  fetchBillingSource,
  fetchBillingVariance,
  fetchBudgets,
  fetchOutbox,
  fetchPrices,
  fetchReconciliation,
  fetchSummary,
  saveBudget,
} from './api';
import {
  BillingImport,
  BillingSource,
  BillingVariance,
  Budget,
  ModelPrice,
  OutboxStatus,
  Reconciliation,
  UsageAttempt,
  UsageGroup,
  UsageSummary,
} from './types';

/**
 * Money is stored and returned as integer micros so no float ever rounds it.
 * The only place it becomes a decimal is here, at the edge of the screen.
 */
const MICROS = 1_000_000;

function money(micros: number | null | undefined, currency = 'USD'): string {
  if (micros === null || micros === undefined) {
    return t('unknown');
  }
  return `${(micros / MICROS).toFixed(6)} ${currency}`;
}

function count(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return t('unknown');
  }
  return value.toLocaleString();
}

const MODULES = ['chat', 'sql', 'repair', 'dataset', 'chart', 'dashboard', 'provider_test', 'other'];

const Page = styled.div`
  ${({ theme }) => `
    padding: ${theme.sizeUnit * 4}px;
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit * 4}px;
    max-width: 1400px;
  `}
`;

const Toolbar = styled.div`
  ${({ theme }) => `
    display: flex;
    gap: ${theme.sizeUnit * 2}px;
    align-items: center;
    flex-wrap: wrap;
  `}
`;

const CardGrid = styled.div`
  ${({ theme }) => `
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: ${theme.sizeUnit * 3}px;
  `}
`;

const StatCard = styled.div`
  ${({ theme }) => `
    border: 1px solid ${theme.colorBorderSecondary};
    border-radius: ${theme.borderRadius}px;
    padding: ${theme.sizeUnit * 3}px;
    background: ${theme.colorBgContainer};
  `}
`;

const StatLabel = styled.div`
  ${({ theme }) => `
    color: ${theme.colorTextSecondary};
    font-size: ${theme.fontSizeSM}px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  `}
`;

const StatValue = styled.div`
  ${({ theme }) => `
    font-size: ${theme.fontSizeXL}px;
    font-weight: ${theme.fontWeightStrong};
    margin-top: ${theme.sizeUnit}px;
  `}
`;

const Hint = styled.p`
  ${({ theme }) => `
    color: ${theme.colorTextSecondary};
    font-size: ${theme.fontSizeSM}px;
    margin: ${theme.sizeUnit}px 0 0;
  `}
`;

const State = styled.div<{ tone: 'ok' | 'warn' | 'bad' }>`
  ${({ theme, tone }) => `
    padding: ${theme.sizeUnit * 2}px ${theme.sizeUnit * 3}px;
    border-radius: ${theme.borderRadius}px;
    border: 1px solid ${
      tone === 'ok'
        ? theme.colorSuccessBorder
        : tone === 'warn'
          ? theme.colorWarningBorder
          : theme.colorErrorBorder
    };
    background: ${
      tone === 'ok'
        ? theme.colorSuccessBg
        : tone === 'warn'
          ? theme.colorWarningBg
          : theme.colorErrorBg
    };
    font-size: ${theme.fontSize}px;
  `}
`;

const Bar = styled.div<{ percent: number; tone: 'ok' | 'warn' | 'bad' }>`
  ${({ theme, percent, tone }) => `
    height: ${theme.sizeUnit * 1.5}px;
    border-radius: 999px;
    background: ${theme.colorFillSecondary};
    overflow: hidden;
    min-width: 80px;

    & > span {
      display: block;
      height: 100%;
      width: ${Math.min(100, Math.max(0, percent))}%;
      background: ${
        tone === 'ok'
          ? theme.colorSuccess
          : tone === 'warn'
            ? theme.colorWarning
            : theme.colorError
      };
    }
  `}
`;

const SectionTitle = styled.h3`
  ${({ theme }) => `
    margin: 0 0 ${theme.sizeUnit * 2}px;
    font-size: ${theme.fontSizeLG}px;
    font-weight: ${theme.fontWeightStrong};
  `}
`;

export default function AIUsagePage() {
  const { addDangerToast, addSuccessToast } = useToasts();
  const [summary, setSummary] = useState<UsageSummary | null>(null);
  const [attempts, setAttempts] = useState<UsageAttempt[]>([]);
  const [health, setHealth] = useState<Reconciliation | null>(null);
  const [budgets, setBudgets] = useState<Budget[]>([]);
  const [prices, setPrices] = useState<ModelPrice[]>([]);
  const [outbox, setOutbox] = useState<OutboxStatus | null>(null);
  const [imports, setImports] = useState<BillingImport[]>([]);
  const [variance, setVariance] = useState<BillingVariance | null>(null);
  const [source, setSource] = useState<BillingSource | null>(null);
  const [moduleFilter, setModuleFilter] = useState<string>('');
  const [busy, setBusy] = useState(false);

  const fail = useCallback(
    async (error: unknown) => {
      addDangerToast(await describeError(error));
    },
    [addDangerToast],
  );

  const refresh = useCallback(async () => {
    setBusy(true);
    try {
      const [summaryBody, attemptRows, healthBody, budgetRows, priceRows, outboxBody, importRows, sourceBody] =
        await Promise.all([
          fetchSummary({ module: moduleFilter || undefined, group_by: 'module' }),
          fetchAttempts({ limit: 25, module: moduleFilter || undefined }),
          fetchReconciliation(),
          fetchBudgets(),
          fetchPrices(),
          fetchOutbox(),
          fetchBillingImports(),
          fetchBillingSource(),
        ]);
      setSummary(summaryBody);
      setAttempts(attemptRows);
      setHealth(healthBody);
      setBudgets(budgetRows);
      setPrices(priceRows);
      setOutbox(outboxBody);
      setImports(importRows);
      setSource(sourceBody);
    } catch (error) {
      await fail(error);
    } finally {
      setBusy(false);
    }
  }, [fail, moduleFilter]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const totals = summary?.totals;
  const failureRate = useMemo(() => {
    if (!totals?.requests) {
      return 0;
    }
    return Math.round(((totals.failed ?? 0) * 1000) / totals.requests) / 10;
  }, [totals]);

  const onDrain = async () => {
    try {
      const outcome = await drainOutbox();
      addSuccessToast(
        t('%s event(s) delivered, %s failed', outcome.delivered ?? 0, outcome.failed ?? 0),
      );
      await refresh();
    } catch (error) {
      await fail(error);
    }
  };

  const onFetchBilling = async () => {
    try {
      const outcome = await fetchBillingExport();
      addSuccessToast(
        t('%s billing row(s) imported from %s', outcome.rows, outcome.source),
      );
      await refresh();
    } catch (error) {
      await fail(error);
    }
  };

  const onShowVariance = async (importId: string) => {
    try {
      setVariance(await fetchBillingVariance(importId));
    } catch (error) {
      await fail(error);
    }
  };

  return (
    <Page data-test="ai-usage-page">
      <Toolbar>
        <SectionTitle style={{ margin: 0 }}>{t('AI usage and cost')}</SectionTitle>
        <Select
          aria-label={t('Module')}
          value={moduleFilter}
          onChange={(value: string) => setModuleFilter(value)}
          options={[
            { value: '', label: t('All modules') },
            ...MODULES.map(module => ({ value: module, label: module })),
          ]}
          css={{ minWidth: 180 }}
        />
        <Button buttonStyle="secondary" onClick={refresh} loading={busy}>
          {t('Refresh')}
        </Button>
      </Toolbar>

      <Hint>
        {t(
          'Costs are estimated from the published price list, not read from a provider invoice. ' +
            'Unknown token counts leave the cost unknown rather than zero.',
        )}
      </Hint>

      <CardGrid>
        <StatCard>
          <StatLabel>{t('Requests')}</StatLabel>
          <StatValue>{count(totals?.requests)}</StatValue>
        </StatCard>
        <StatCard>
          <StatLabel>{t('Tokens')}</StatLabel>
          <StatValue>{count(totals?.total_tokens)}</StatValue>
        </StatCard>
        <StatCard>
          <StatLabel>{t('Estimated spend')}</StatLabel>
          <StatValue>{money(totals?.estimated_cost_micros)}</StatValue>
          <Hint>
            {t('%s of %s requests priced', count(totals?.priced_requests), count(totals?.requests))}
          </Hint>
        </StatCard>
        <StatCard>
          <StatLabel>{t('Failures')}</StatLabel>
          <StatValue>{`${failureRate}%`}</StatValue>
        </StatCard>
        <StatCard>
          <StatLabel>{t('Fallbacks')}</StatLabel>
          <StatValue>{count(totals?.fallbacks)}</StatValue>
        </StatCard>
        <StatCard>
          <StatLabel>{t('Avg latency')}</StatLabel>
          <StatValue>
            {totals?.avg_duration_ms ? `${Math.round(totals.avg_duration_ms)} ms` : t('n/a')}
          </StatValue>
        </StatCard>
        <StatCard>
          <StatLabel>{t('Missing usage')}</StatLabel>
          <StatValue>{count(totals?.missing_usage)}</StatValue>
        </StatCard>
      </CardGrid>

      <Card title={t('Ledger health')}>
        {health && (
          <State tone={health.healthy ? 'ok' : 'bad'} data-test="usage-health">
            {health.healthy
              ? t('The ledger is consistent: no negative counts, nothing left open.')
              : t(
                  'The ledger needs attention: %s negative, %s unfinished, %s inconsistent, %s dead-lettered.',
                  health.negative_tokens,
                  health.unfinished,
                  health.inconsistent_totals,
                  health.outbox_dead_letter,
                )}
            <Hint>
              {t('%s attempts, %s without usage, %s unpriced, %s waiting to be delivered',
                health.attempts, health.missing_usage, health.unpriced, health.outbox_pending)}
            </Hint>
            {health.price_gaps.length > 0 && (
              <Hint>
                {t('No price published for: %s', health.price_gaps.join(', '))}
              </Hint>
            )}
          </State>
        )}
      </Card>

      <Card
        title={t('Budgets')}
        extra={
          <Popconfirm
            title={t('Create a daily global budget of $1.00?')}
            onConfirm={async () => {
              try {
                await saveBudget({
                  scope: 'global',
                  period: 'day',
                  cost_micros_limit: MICROS,
                  warn_percent: 80,
                  hard_stop: true,
                  enabled: true,
                  currency: 'USD',
                });
                addSuccessToast(t('Budget created'));
                await refresh();
              } catch (error) {
                await fail(error);
              }
            }}
          >
            <Button buttonStyle="secondary">{t('Add a $1/day budget')}</Button>
          </Popconfirm>
        }
      >
        <Table
          size={TableSize.Small}
          data={budgets}
          rowKey="id"
          columns={[
            { title: t('Scope'), dataIndex: 'scope', key: 'scope' },
            {
              title: t('Period'),
              dataIndex: 'period',
              key: 'period',
              render: (period: string, row: Budget) =>
                row.scope === 'global' ? period : `${period} · ${row.scope_ref}`,
            },
            {
              title: t('Limit'),
              dataIndex: 'cost_micros_limit',
              key: 'limit',
              render: (limit: number | null, row: Budget) => money(limit, row.currency || 'USD'),
            },
            {
              title: t('Used'),
              key: 'used',
              render: (_: unknown, row: Budget) => (
                <div>
                  <Bar
                    percent={row.percent_used}
                    tone={
                      row.state === 'exhausted' || row.state === 'over'
                        ? 'bad'
                        : row.state === 'warning'
                          ? 'warn'
                          : 'ok'
                    }
                  >
                    <span />
                  </Bar>
                  <Hint>
                    {money(row.spent_micros, row.currency || 'USD')}
                    {row.reserved_micros > 0
                      ? ` ${t('(+%s reserved)', money(row.reserved_micros, row.currency || 'USD'))}`
                      : ''}
                  </Hint>
                </div>
              ),
            },
            {
              title: t('State'),
              dataIndex: 'state',
              key: 'state',
              render: (state: string) => (
                <Tag color={state === 'ok' ? 'green' : state === 'warning' ? 'orange' : 'red'}>
                  {state}
                </Tag>
              ),
            },
            {
              title: t('Refused'),
              dataIndex: 'blocked_count',
              key: 'blocked',
            },
            {
              title: '',
              key: 'actions',
              render: (_: unknown, row: Budget) => (
                <Popconfirm
                  title={t('Delete this budget?')}
                  onConfirm={async () => {
                    try {
                      await deleteBudget(row.id);
                      await refresh();
                    } catch (error) {
                      await fail(error);
                    }
                  }}
                >
                  <Button buttonStyle="link">{t('Delete')}</Button>
                </Popconfirm>
              ),
            },
          ]}
        />
      </Card>

      <Card title={t('By module')}>
        <Table
          size={TableSize.Small}
          data={summary?.groups ?? []}
          rowKey={row => `${row.module ?? 'all'}`}
          columns={[
            { title: t('Module'), dataIndex: 'module', key: 'module' },
            { title: t('Requests'), dataIndex: 'requests', key: 'requests' },
            { title: t('Tokens'), dataIndex: 'total_tokens', key: 'tokens' },
            {
              title: t('Estimated cost'),
              key: 'cost',
              render: (_: unknown, row: UsageGroup) =>
                money(row.estimated_cost_micros),
            },
            {
              title: t('Failures'),
              dataIndex: 'failed',
              key: 'failed',
            },
          ]}
        />
      </Card>

      <Card
        title={t('Recent attempts')}
        extra={
          <Button buttonStyle="secondary" onClick={onDrain}>
            {t('Deliver queued usage' +
              (outbox?.counts?.pending ? ` (${outbox.counts.pending})` : ''))}
          </Button>
        }
      >
        <Table
          size={TableSize.Small}
          data={attempts}
          rowKey="id"
          columns={[
            {
              title: t('When'),
              dataIndex: 'started_at',
              key: 'started_at',
              render: (value: string | null) => (value ? new Date(value).toLocaleString() : ''),
            },
            { title: t('Module'), dataIndex: 'module', key: 'module' },
            {
              title: t('Provider'),
              key: 'provider',
              render: (_: unknown, row: UsageAttempt) => `${row.provider} · ${row.model}`,
            },
            {
              title: t('Tokens'),
              key: 'tokens',
              render: (_: unknown, row: UsageAttempt) =>
                row.usage_source === 'reported' ? count(row.total_tokens) : t('not reported'),
            },
            {
              title: t('Estimated cost'),
              key: 'cost',
              render: (_: unknown, row: UsageAttempt) =>
                money(row.estimated_cost_micros, row.currency || 'USD'),
            },
            {
              title: t('Outcome'),
              key: 'outcome',
              render: (_: unknown, row: UsageAttempt) => (
                <Tag color={row.status === 'succeeded' ? 'green' : 'red'}>
                  {row.error_class ? `${row.status}: ${row.error_class}` : row.status}
                </Tag>
              ),
            },
            {
              title: t('Latency'),
              key: 'latency',
              render: (_: unknown, row: UsageAttempt) =>
                row.duration_ms === null ? '' : `${row.duration_ms} ms`,
            },
          ]}
        />
      </Card>

      <Card title={t('Published prices')}>
        <Table
          size={TableSize.Small}
          data={prices}
          rowKey={row => `${row.provider}/${row.model}/v${row.version}`}
          columns={[
            { title: t('Provider'), dataIndex: 'provider', key: 'provider' },
            { title: t('Model'), dataIndex: 'model', key: 'model' },
            { title: t('Version'), dataIndex: 'version', key: 'version' },
            {
              title: t('Input / output per 1M'),
              key: 'rates',
              render: (_: unknown, row: ModelPrice) =>
                `${money(row.input_micros_per_unit, row.currency)} / ${money(
                  row.output_micros_per_unit,
                  row.currency,
                )}`,
            },
            {
              title: t('Cached'),
              key: 'cached',
              render: (_: unknown, row: ModelPrice) =>
                row.cached_input_micros_per_unit === null
                  ? t('same as input')
                  : money(row.cached_input_micros_per_unit, row.currency),
            },
            { title: t('Source'), dataIndex: 'source', key: 'source' },
            {
              title: t('Effective from'),
              key: 'from',
              render: (_: unknown, row: ModelPrice) =>
                row.effective_from ? new Date(row.effective_from).toLocaleDateString() : '',
            },
          ]}
        />
      </Card>

      <Card
        title={t('Provider billing')}
        extra={
          <Toolbar>
            <Hint style={{ margin: 0 }}>
              {source?.configured
                ? t('Fetched from %s', source.host)
                : t('No export source configured; imports are manual')}
            </Hint>
            <Button
              buttonStyle="secondary"
              onClick={onFetchBilling}
              disabled={!source?.configured}
            >
              {t('Fetch now')}
            </Button>
          </Toolbar>
        }
      >
        <Table
          size={TableSize.Small}
          data={imports}
          rowKey="id"
          columns={[
            { title: t('Provider'), dataIndex: 'provider', key: 'provider' },
            {
              title: t('Period'),
              key: 'period',
              render: (_: unknown, row: BillingImport) =>
                `${row.period_start?.slice(0, 10) ?? ''} → ${row.period_end?.slice(0, 10) ?? ''}`,
            },
            { title: t('Rows'), dataIndex: 'row_count', key: 'rows' },
            {
              title: t('Billed'),
              key: 'billed',
              render: (_: unknown, row: BillingImport) =>
                money(row.total_actual_micros, row.currency),
            },
            {
              title: '',
              key: 'actions',
              render: (_: unknown, row: BillingImport) => (
                <Button buttonStyle="link" onClick={() => onShowVariance(row.id)}>
                  {t('Compare')}
                </Button>
              ),
            },
          ]}
        />
        {variance && (
          <State tone={variance.unmatched_lines ? 'warn' : 'ok'} data-test="billing-variance">
            {t('Billed %s against an estimate of %s',
              money(variance.actual_micros, variance.import.currency),
              money(variance.estimated_micros, variance.import.currency))}
            <Hint>
              {t('%s of %s lines matched to a recorded attempt; %s unmatched; %s recorded attempts were not on the invoice.',
                variance.matched_lines,
                variance.lines,
                variance.unmatched_lines,
                variance.unattributed_attempts)}
            </Hint>
            <Hint>
              {t('The estimate is preserved: an invoice is evidence, not a correction.')}
            </Hint>
          </State>
        )}
      </Card>
    </Page>
  );
}
