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
  InputNumber,
  Popconfirm,
  Switch,
  Table,
  TableSize,
  Tag,
} from '@superset-ui/core/components';
import { useToasts } from 'src/components/MessageToasts/withToasts';
import {
  describeError,
  fetchHistory,
  fetchSettings,
  HistoryEntry,
  rollbackSetting,
  saveSettings,
  SettingEntry,
} from './api';

/**
 * The service is the authority on every bound, so this page renders what the
 * registry reports rather than re-implementing validation. A control that cannot
 * express an invalid value is better than a control that can and then argues.
 */

const Page = styled.div`
  ${({ theme }) => `
    padding: ${theme.sizeUnit * 4}px;
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit * 4}px;
    max-width: 1200px;
  `}
`;

const Hint = styled.p`
  ${({ theme }) => `
    color: ${theme.colorTextSecondary};
    font-size: ${theme.fontSizeSM}px;
    margin: ${theme.sizeUnit}px 0 0;
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

const SectionTitle = styled.h3`
  ${({ theme }) => `
    margin: 0;
    font-size: ${theme.fontSizeLG}px;
    font-weight: ${theme.fontWeightStrong};
  `}
`;

interface Pending {
  key: string;
  value: unknown;
  version: number;
}

export default function AISettingsPage() {
  const { addDangerToast, addSuccessToast } = useToasts();
  const [entries, setEntries] = useState<SettingEntry[]>([]);
  const [restartRequired, setRestartRequired] = useState<string[]>([]);
  const [pending, setPending] = useState<Record<string, Pending>>({});
  const [reason, setReason] = useState('');
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState('');

  const fail = useCallback(
    async (error: unknown) => {
      addDangerToast(await describeError(error));
    },
    [addDangerToast],
  );

  const refresh = useCallback(async () => {
    setBusy(true);
    try {
      const [body, historyRows] = await Promise.all([
        fetchSettings(),
        fetchHistory(undefined, 20),
      ]);
      setEntries(body.settings);
      setRestartRequired(body.restart_required);
      setHistory(historyRows);
      setPending({});
      setLoadError('');
    } catch (error) {
      setLoadError(await describeError(error));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const change = (entry: SettingEntry, value: unknown) =>
    setPending(current => ({
      ...current,
      [entry.key]: { key: entry.key, value, version: entry.version },
    }));

  const value = (entry: SettingEntry) =>
    pending[entry.key]?.value ?? entry.effective;

  const dirty = Object.keys(pending);

  const onSave = async () => {
    if (!dirty.length) {
      return;
    }
    // Every changed value is sent with the version the page loaded, so a
    // concurrent edit is reported as a conflict instead of overwritten.
    const versions = new Set(dirty.map(key => pending[key].version));
    try {
      await saveSettings({
        values: Object.fromEntries(
          dirty.map(key => [key, pending[key].value]),
        ),
        scope: 'global',
        reason: reason || 'changed in AI Settings',
        expected_version: versions.size === 1 ? [...versions][0] : null,
      });
      addSuccessToast(t('%s setting(s) saved', dirty.length));
      setReason('');
      await refresh();
    } catch (error) {
      await fail(error);
    }
  };

  const byOwner = useMemo(() => {
    const groups = new Map<string, SettingEntry[]>();
    entries.forEach(entry => {
      const list = groups.get(entry.owner) ?? [];
      list.push(entry);
      groups.set(entry.owner, list);
    });
    return [...groups.entries()].sort(([left], [right]) =>
      left.localeCompare(right),
    );
  }, [entries]);

  const renderControl = (entry: SettingEntry) => {
    if (entry.kind === 'bool') {
      return (
        <Switch
          checked={Boolean(value(entry))}
          onChange={(checked: boolean) => change(entry, checked)}
        />
      );
    }
    if (entry.kind === 'int') {
      return (
        <InputNumber
          value={Number(value(entry))}
          min={entry.minimum ?? undefined}
          max={entry.maximum ?? undefined}
          onChange={(next: number | null) => change(entry, next)}
          style={{ width: 160 }}
        />
      );
    }
    return (
      <Input
        value={String(value(entry) ?? '')}
        onChange={(event: { target: { value: string } }) =>
          change(entry, event.target.value)
        }
        style={{ maxWidth: 320 }}
      />
    );
  };

  return (
    <Page data-test="ai-settings-page">
      <Toolbar>
        <SectionTitle>{t('AI settings')}</SectionTitle>
        <Button buttonStyle="secondary" onClick={refresh} loading={busy}>
          {t('Reload')}
        </Button>
        <Input
          placeholder={t('Why is this changing? (recorded in the audit)')}
          value={reason}
          onChange={(event: { target: { value: string } }) =>
            setReason(event.target.value)
          }
          style={{ maxWidth: 380 }}
        />
        <Button buttonStyle="primary" onClick={onSave} disabled={!dirty.length}>
          {dirty.length ? t('Save %s change(s)', dirty.length) : t('Save')}
        </Button>
      </Toolbar>

      <Hint>
        {t(
          'Every value is validated by the service against its declared range. ' +
            'Security ceilings cannot be raised from this page.',
        )}
      </Hint>

      {loadError && (
        <Hint role="alert" style={{ color: 'inherit' }}>
          {loadError}
        </Hint>
      )}

      {restartRequired.length > 0 && (
        <Hint>
          {t('These settings need a restart to take effect: %s', restartRequired.join(', '))}
        </Hint>
      )}

      {byOwner.map(([owner, rows]) => (
        <Card key={owner} title={owner}>
          <Table
            size={TableSize.Small}
            data={rows}
            rowKey="key"
            columns={[
              {
                title: t('Setting'),
                key: 'key',
                render: (_: unknown, row: SettingEntry) => (
                  <div>
                    <code>{row.key}</code>
                    <Hint>{row.summary}</Hint>
                  </div>
                ),
              },
              {
                title: t('Value'),
                key: 'value',
                render: (_: unknown, row: SettingEntry) => renderControl(row),
              },
              {
                title: t('Source'),
                key: 'source',
                render: (_: unknown, row: SettingEntry) => (
                  <Tag color={row.source === 'default' ? 'default' : 'blue'}>
                    {row.source}
                  </Tag>
                ),
              },
              {
                title: t('Bounds'),
                key: 'bounds',
                render: (_: unknown, row: SettingEntry) => {
                  if (row.kind === 'bool') {
                    return t('true / false');
                  }
                  if (row.kind !== 'int') {
                    return '';
                  }
                  return row.ceiling
                    ? t('%s to %s (ceiling)', row.minimum, row.maximum)
                    : t('%s to %s', row.minimum, row.maximum);
                },
              },
              {
                title: t('Change'),
                key: 'change',
                render: (_: unknown, row: SettingEntry) =>
                  pending[row.key] ? (
                    <Tag color="orange">{t('unsaved')}</Tag>
                  ) : (
                    <Tag color={row.restart_required ? 'red' : 'green'}>
                      {row.restart_required ? t('needs restart') : t('applies now')}
                    </Tag>
                  ),
              },
            ]}
          />
        </Card>
      ))}

      <Card title={t('Recent configuration changes')}>
        <Table
          size={TableSize.Small}
          data={history}
          rowKey="id"
          columns={[
            {
              title: t('When'),
              key: 'when',
              render: (_: unknown, row: HistoryEntry) =>
                row.created_at ? new Date(row.created_at).toLocaleString() : '',
            },
            { title: t('Key'), dataIndex: 'key', key: 'key' },
            { title: t('Version'), dataIndex: 'version', key: 'version' },
            {
              title: t('Before → after'),
              key: 'change',
              render: (_: unknown, row: HistoryEntry) =>
                `${JSON.stringify(row.before)} → ${JSON.stringify(row.after)}`,
            },
            { title: t('By'), dataIndex: 'actor', key: 'actor' },
            { title: t('Reason'), dataIndex: 'reason', key: 'reason' },
            {
              title: '',
              key: 'actions',
              render: (_: unknown, row: HistoryEntry) =>
                row.before === null ? (
                  <Hint>{t('no earlier value')}</Hint>
                ) : (
                  <Popconfirm
                    title={t('Restore the previous value as a new version?')}
                    onConfirm={async () => {
                      try {
                        await rollbackSetting(row.id, 'rolled back from settings');
                        addSuccessToast(t('%s restored', row.key));
                        await refresh();
                      } catch (error) {
                        await fail(error);
                      }
                    }}
                  >
                    <Button buttonStyle="link">{t('Restore')}</Button>
                  </Popconfirm>
                ),
            },
          ]}
        />
      </Card>
    </Page>
  );
}
