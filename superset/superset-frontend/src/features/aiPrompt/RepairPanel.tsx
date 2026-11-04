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
import { useCallback, useEffect, useState } from 'react';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import { Button, Empty, Space, Tag, Tooltip } from '@superset-ui/core/components';
import { useToasts } from 'src/components/MessageToasts/withToasts';
import type {
  PatternAction,
  RepairMetricsResponse,
  RepairPatternRecord,
} from './api';
import {
  actOnPattern,
  describeError,
  fetchRepairMetrics,
  fetchRepairPatterns,
} from './api';

/**
 * What automatic repair has learned, and the brake on it (P5-F10).
 *
 * The panel is deliberately about *outcomes*: how many corrections exist, how
 * many earned reuse, how many were suppressed or switched off — never the
 * statements themselves, which stay sealed in the service. An operator can
 * quarantine a correction here, which takes effect on the next turn.
 */

const STATE_COLORS: Record<string, string> = {
  candidate: 'default',
  validating: 'blue',
  validated: 'geekblue',
  promoted: 'green',
  deprecated: 'orange',
  quarantined: 'red',
};

const STATES = [
  'promoted',
  'validated',
  'validating',
  'candidate',
  'deprecated',
  'quarantined',
];

const StyledPanel = styled.section`
  ${({ theme }) => `
    border-top: 1px solid ${theme.colorBorder};
    padding-top: ${theme.sizeUnit * 2}px;
    margin-top: ${theme.sizeUnit * 3}px;
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit}px;
  `}
`;

const StyledRow = styled.div`
  ${({ theme }) => `
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: ${theme.sizeUnit * 2}px;
    padding: ${theme.sizeUnit}px 0;
    border-bottom: 1px solid ${theme.colorBorderSecondary};
    font-size: ${theme.fontSizeSM}px;
  `}
`;

const StyledMeta = styled.span`
  ${({ theme }) => `
    color: ${theme.colorTextSecondary};
  `}
`;

export default function RepairPanel() {
  const { addDangerToast, addSuccessToast } = useToasts();
  const [metrics, setMetrics] = useState<RepairMetricsResponse | null>(null);
  const [records, setRecords] = useState<RepairPatternRecord[]>([]);
  const [state, setState] = useState('promoted');
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(
    async (wanted: string) => {
      try {
        const [summary, list] = await Promise.all([
          fetchRepairMetrics(),
          fetchRepairPatterns(wanted),
        ]);
        setMetrics(summary);
        setRecords(list.result || []);
      } catch (error) {
        addDangerToast(await describeError(error));
      }
    },
    [addDangerToast],
  );

  useEffect(() => {
    load(state);
  }, [load, state]);

  const act = useCallback(
    async (record: RepairPatternRecord, action: PatternAction) => {
      setBusy(record.id);
      try {
        await actOnPattern(record.id, action);
        addSuccessToast(t('Correction %(action)s', { action }));
        await load(state);
      } catch (error) {
        addDangerToast(await describeError(error));
      } finally {
        setBusy(null);
      }
    },
    [addDangerToast, addSuccessToast, load, state],
  );

  const attempts = metrics?.metrics.attempts;

  return (
    <StyledPanel aria-label={t('Repair knowledge')}>
      <strong>{t('Repair knowledge')}</strong>
      <StyledMeta>
        {metrics
          ? t(
              '%(usable)s reusable · %(disabled)s disabled · %(attempts)s attempts · %(rate)s%% healed',
              {
                usable: metrics.metrics.usable,
                disabled: metrics.metrics.disabled,
                attempts:
                  (attempts?.attempted || 0) +
                  (attempts?.succeeded || 0) +
                  (attempts?.failed || 0),
                rate: Math.round((metrics.metrics.success_rate || 0) * 100),
              },
            )
          : t('Loading…')}
      </StyledMeta>
      {metrics && (
        <StyledMeta>
          {t(
            'Promotion needs %(evidence)s independent validations · automatic: %(auto)s · max %(attempts)s attempts per failure',
            {
              evidence: metrics.promotion.evidence,
              auto: metrics.promotion.auto_promote ? t('yes') : t('no'),
              attempts: metrics.budget.max_attempts,
            },
          )}
        </StyledMeta>
      )}
      <Space wrap>
        {STATES.map(name => (
          <Button
            key={name}
            buttonStyle={state === name ? 'primary' : 'tertiary'}
            buttonSize="small"
            onClick={() => setState(name)}
          >
            {`${name} (${metrics?.metrics.states[name] ?? 0})`}
          </Button>
        ))}
      </Space>
      {records.length === 0 ? (
        <Empty description={t('No corrections in this state')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        records.map(record => (
          <StyledRow key={record.id}>
            <span>
              <Tag color={STATE_COLORS[record.state] || 'default'}>
                {record.state}
              </Tag>
              <Tooltip title={t('Failure fingerprint (no content)')}>
                <span>{record.kind}</span>
              </Tooltip>{' '}
              <StyledMeta>
                {t(
                  'v%(version)s · %(successes)s ok / %(failures)s failed · confidence %(confidence)s',
                  {
                    version: record.version,
                    successes: record.successes,
                    failures: record.failures,
                    confidence: record.confidence,
                  },
                )}
              </StyledMeta>
            </span>
            <Space>
              {record.state !== 'promoted' && record.successes > 0 && (
                <Button
                  buttonSize="small"
                  loading={busy === record.id}
                  onClick={() => act(record, 'promote')}
                >
                  {t('Promote')}
                </Button>
              )}
              {record.state === 'promoted' && (
                <Button
                  buttonSize="small"
                  danger
                  loading={busy === record.id}
                  onClick={() => act(record, 'quarantine')}
                >
                  {t('Quarantine')}
                </Button>
              )}
              {(record.state === 'deprecated' || record.state === 'quarantined') && (
                <Button
                  buttonSize="small"
                  loading={busy === record.id}
                  onClick={() => act(record, 'restore')}
                >
                  {t('Restore')}
                </Button>
              )}
            </Space>
          </StyledRow>
        ))
      )}
    </StyledPanel>
  );
}
