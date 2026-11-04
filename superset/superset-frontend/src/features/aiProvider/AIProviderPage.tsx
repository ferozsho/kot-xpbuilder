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
  RawAntdSelect as Select,
  Space,
  Switch,
  Table,
  TableSize,
  Tag,
  Tooltip,
} from '@superset-ui/core/components';
import { useToasts } from 'src/components/MessageToasts/withToasts';
import {
  createProvider,
  deleteProvider,
  describeError,
  fetchProviders,
  fetchProviderTypes,
  testProvider,
  updateProvider,
} from './api';
import {
  EnvFallbackStatus,
  ProviderPayload,
  ProviderRecord,
  ProviderType,
  ProviderTypeOption,
} from './types';

const TENANT_LABEL = 'Global';

interface FormState {
  name: string;
  provider_type: ProviderType;
  base_url: string;
  api_key: string;
  default_model: string;
  temperature: number;
  max_tokens: number;
  timeout_seconds: number;
  api_version: string;
  enabled: boolean;
  is_default: boolean;
  priority: number;
}

const emptyForm = (type?: ProviderTypeOption): FormState => ({
  name: '',
  provider_type: type?.key ?? 'openai_compatible',
  base_url: type?.default_base_url ?? '',
  api_key: '',
  default_model: type?.default_model ?? '',
  temperature: 0.7,
  max_tokens: 1024,
  timeout_seconds: 60,
  api_version: '',
  enabled: true,
  is_default: false,
  priority: 0,
});

const toFormState = (record: ProviderRecord): FormState => ({
  name: record.name,
  provider_type: record.provider_type,
  base_url: record.base_url,
  api_key: '',
  default_model: record.default_model,
  temperature: record.config.temperature ?? 0.7,
  max_tokens: record.config.max_tokens ?? 1024,
  timeout_seconds: record.config.timeout_seconds ?? 60,
  api_version: record.config.api_version ?? '',
  enabled: record.enabled,
  is_default: record.is_default,
  priority: record.priority,
});

const toPayload = (form: FormState): ProviderPayload => {
  const config: ProviderPayload['config'] = {
    temperature: form.temperature,
    max_tokens: form.max_tokens,
    timeout_seconds: form.timeout_seconds,
  };
  if (form.api_version.trim()) {
    config.api_version = form.api_version.trim();
  }
  const payload: ProviderPayload = {
    name: form.name.trim(),
    provider_type: form.provider_type,
    base_url: form.base_url.trim(),
    default_model: form.default_model.trim(),
    config,
    enabled: form.enabled,
    is_default: form.is_default,
    priority: form.priority,
  };
  if (form.api_key.trim()) {
    payload.api_key = form.api_key.trim();
  }
  return payload;
};

const StyledWrapper = styled.div`
  width: 100%;
  padding: ${({ theme }) => theme.sizeUnit * 6}px;
  max-width: 1500px;
  margin: 0 auto;
`;

const StyledHeader = styled.div`
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: ${({ theme }) => theme.sizeUnit * 4}px;
  margin-bottom: ${({ theme }) => theme.sizeUnit * 4}px;
`;

const StyledTitle = styled.h2`
  margin: 0;
  font-size: ${({ theme }) => theme.fontSizeXL}px;
  font-weight: ${({ theme }) => theme.fontWeightStrong};
`;

const StyledLayout = styled.div`
  display: grid;
  grid-template-columns: minmax(0, 2.2fr) minmax(280px, 1fr);
  gap: ${({ theme }) => theme.sizeUnit * 4}px;
  align-items: start;

  @media (max-width: 1100px) {
    grid-template-columns: minmax(0, 1fr);
  }
`;

const StyledField = styled.div`
  margin-bottom: ${({ theme }) => theme.sizeUnit * 4}px;

  label {
    display: block;
    margin-bottom: ${({ theme }) => theme.sizeUnit}px;
    font-weight: ${({ theme }) => theme.fontWeightStrong};
  }

  .helper {
    display: block;
    margin-top: ${({ theme }) => theme.sizeUnit}px;
    color: ${({ theme }) => theme.colorTextSecondary};
    font-size: ${({ theme }) => theme.fontSizeSM}px;
  }
`;

const StyledRow = styled.div`
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: ${({ theme }) => theme.sizeUnit * 3}px;
`;

/**
 * Superset's Card sets a 4px body padding while its head keeps the antd 24px,
 * which leaves the title indented far more than the content. Pinning both to
 * the same gutter keeps the side cards aligned with the form card.
 */
const StyledCard = styled(Card)`
  .ant-card-head {
    padding: 0 ${({ theme }) => theme.sizeUnit * 4}px;
  }

  .ant-card-body {
    padding: ${({ theme }) => theme.sizeUnit * 4}px;
  }
`;

const StyledTableCard = styled(Card)`
  .ant-card-body {
    padding: 0;
  }
`;

const StyledSubLabel = styled.div`
  display: block;
  margin-bottom: ${({ theme }) => theme.sizeUnit}px;
`;

const StyledSubtitle = styled.div`
  margin-top: ${({ theme }) => theme.sizeUnit}px;
  color: ${({ theme }) => theme.colorTextSecondary};
  font-size: ${({ theme }) => theme.fontSizeSM}px;
`;

const StyledSwitchRow = styled.div`
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: ${({ theme }) => theme.sizeUnit * 2}px 0;

  & + & {
    border-top: 1px solid ${({ theme }) => theme.colorBorderSecondary};
  }
`;

const StyledNotice = styled.div`
  margin-bottom: ${({ theme }) => theme.sizeUnit * 3}px;
  padding: ${({ theme }) => theme.sizeUnit * 2}px ${({ theme }) => theme.sizeUnit * 3}px;
  border-radius: ${({ theme }) => theme.borderRadius}px;
  background: ${({ theme }) => theme.colorFillAlter};
  color: ${({ theme }) => theme.colorTextSecondary};
`;

const StyledError = styled.div`
  margin-top: ${({ theme }) => theme.sizeUnit * 2}px;
  color: ${({ theme }) => theme.colorError};
`;

export default function AIProviderPage() {
  const { addDangerToast, addSuccessToast } = useToasts();
  const [providers, setProviders] = useState<ProviderRecord[]>([]);
  const [types, setTypes] = useState<ProviderTypeOption[]>([]);
  const [envFallback, setEnvFallback] = useState<EnvFallbackStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [editing, setEditing] = useState<ProviderRecord | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState<FormState>(emptyForm());
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    fetchProviders()
      .then(response => {
        setProviders(response.result ?? []);
        setEnvFallback(response.env_fallback ?? null);
      })
      .catch(async error => {
        const status = (error as { status?: number })?.status;
        if (status === 403) {
          setForbidden(true);
        } else {
          addDangerToast(await describeError(error));
        }
      })
      .finally(() => setLoading(false));
  }, [addDangerToast]);

  useEffect(() => {
    load();
    fetchProviderTypes()
      .then(setTypes)
      .catch(() => {
        // The catalog only pre-fills defaults; the page still works without it.
      });
  }, [load]);

  const typeOptions = useMemo(
    () =>
      types.map(option => ({
        value: option.key,
        label: option.label,
      })),
    [types],
  );

  const selectedType =
    types.find(option => option.key === form.provider_type) ?? types[0];

  const openCreate = () => {
    setEditing(null);
    setForm(emptyForm(types[0]));
    setFormError(null);
    setFormOpen(true);
  };

  const openEdit = (record: ProviderRecord) => {
    setEditing(record);
    setForm(toFormState(record));
    setFormError(null);
    setFormOpen(true);
  };

  const changeType = (value: ProviderType) => {
    const option = types.find(item => item.key === value);
    setForm(current => ({
      ...current,
      provider_type: value,
      base_url: option?.default_base_url ?? current.base_url,
      default_model: option?.default_model ?? current.default_model,
    }));
  };

  const save = async () => {
    if (!form.name.trim()) {
      setFormError(t('Display name is required'));
      return;
    }
    if (!editing && !form.api_key.trim()) {
      setFormError(t('API key is required'));
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      if (editing) {
        await updateProvider(editing.id, toPayload(form));
        addSuccessToast(t('Provider saved'));
      } else {
        await createProvider(toPayload(form));
        addSuccessToast(t('Provider added'));
      }
      setFormOpen(false);
      load();
    } catch (error) {
      setFormError(await describeError(error));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (record: ProviderRecord) => {
    try {
      await deleteProvider(record.id);
      addSuccessToast(t('Provider deleted'));
      load();
    } catch (error) {
      addDangerToast(await describeError(error));
    }
  };

  const runTest = async (record: ProviderRecord) => {
    setTestingId(record.id);
    try {
      const result = await testProvider(record.id);
      if (result.status === 'succeeded') {
        addSuccessToast(
          `${t('Connection succeeded')} (${result.duration_ms ?? 0} ms)`,
        );
      } else {
        addDangerToast(
          `${t('Connection failed')}: ${result.error_code ?? t('unknown error')}`,
        );
      }
      load();
    } catch (error) {
      addDangerToast(await describeError(error));
    } finally {
      setTestingId(null);
    }
  };

  const columns = useMemo(
    () => [
      {
        title: t('Display name'),
        dataIndex: 'name',
        key: 'name',
        // Wide enough that the header itself never wraps; the model and key are
        // shown in the edit form instead of as table columns.
        width: 240,
        render: (name: string, record: ProviderRecord) => (
          <Space size={4} wrap>
            <strong>{name}</strong>
            {record.is_default && <Tag color="purple">{t('Default')}</Tag>}
            {!record.enabled && <Tag>{t('Disabled')}</Tag>}
          </Space>
        ),
      },
      {
        title: t('Provider type'),
        dataIndex: 'provider_label',
        key: 'provider_label',
        width: 170,
      },
      {
        title: t('Priority'),
        dataIndex: 'priority',
        key: 'priority',
        width: 90,
        align: 'center' as const,
      },
      {
        title: t('Last test'),
        key: 'last_test',
        width: 140,
        render: (_: unknown, record: ProviderRecord) => {
          if (!record.last_test.status) {
            return '—';
          }
          return record.last_test.status === 'succeeded' ? (
            <Tag color="green">{t('Succeeded')}</Tag>
          ) : (
            <Tag color="red">{record.last_test.error || t('Failed')}</Tag>
          );
        },
      },
      {
        title: t('Health'),
        key: 'health',
        width: 150,
        render: (_: unknown, record: ProviderRecord) => {
          const health = record.health;
          if (!health || health.state !== 'open') {
            return <Tag color="green">{t('Healthy')}</Tag>;
          }
          // The breaker is skipping this provider until `open_until`; the
          // tooltip carries the redacted reason so no prompt data is exposed.
          return (
            <Tooltip
              title={`${t('Skipped until')} ${health.open_until ?? '—'}${
                health.last_error ? ` — ${health.last_error}` : ''
              }`}
            >
              <Tag color="orange">
                {t('Cooling down')}
                {health.consecutive_failures
                  ? ` (${health.consecutive_failures})`
                  : ''}
              </Tag>
            </Tooltip>
          );
        },
      },
      {
        title: t('Actions'),
        key: 'actions',
        width: 300,
        render: (_: unknown, record: ProviderRecord) => (
          <Space size={4}>
            <Button
              buttonSize="small"
              loading={testingId === record.id}
              onClick={() => runTest(record)}
            >
              {t('Test connection')}
            </Button>
            <Button buttonSize="small" onClick={() => openEdit(record)}>
              {t('Edit')}
            </Button>
            <Popconfirm
              title={t('Delete this provider?')}
              onConfirm={() => remove(record)}
            >
              <Button buttonSize="small" buttonStyle="danger">
                {t('Delete')}
              </Button>
            </Popconfirm>
          </Space>
        ),
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [testingId, t],
  );

  if (forbidden) {
    return (
      <StyledWrapper>
        <StyledTitle>{t('AI Provider')}</StyledTitle>
        <StyledNotice>
          {t(
            'You need administrator permissions to manage AI providers. Ask an ' +
              'administrator of this XPBuilder deployment.',
          )}
        </StyledNotice>
      </StyledWrapper>
    );
  }

  return (
    <StyledWrapper>
      <StyledHeader>
        <div>
          <StyledTitle>
            {formOpen
              ? editing
                ? t('Edit provider')
                : t('Connect a provider')
              : t('AI Provider')}
          </StyledTitle>
          <StyledSubtitle>
            {t(
              'Keys are encrypted at rest in the AI service database and are ' +
                'never returned by the API after saving.',
            )}
          </StyledSubtitle>
        </div>
        {!formOpen && (
          <Button buttonStyle="primary" onClick={openCreate}>
            {t('Connect a provider')}
          </Button>
        )}
      </StyledHeader>

      {formOpen ? (
        <StyledLayout>
          <StyledCard>
            <StyledField>
              <label htmlFor="provider-name">{t('Display name')} *</label>
              <Input
                id="provider-name"
                value={form.name}
                placeholder="OpenAI (prod)"
                onChange={event =>
                  setForm({ ...form, name: event.target.value })
                }
              />
            </StyledField>

            <StyledRow>
              <StyledField>
                <label htmlFor="provider-type">{t('Provider type')} *</label>
                <Select
                  id="provider-type"
                  value={form.provider_type}
                  options={typeOptions}
                  style={{ width: '100%' }}
                  onChange={changeType}
                />
              </StyledField>
              <StyledField>
                <label htmlFor="provider-model">{t('Default model')}</label>
                <Input
                  id="provider-model"
                  value={form.default_model}
                  placeholder={selectedType?.model_placeholder}
                  onChange={event =>
                    setForm({ ...form, default_model: event.target.value })
                  }
                />
              </StyledField>
            </StyledRow>

            <StyledField>
              <label htmlFor="provider-base-url">{t('Base URL')}</label>
              <Input
                id="provider-base-url"
                value={form.base_url}
                placeholder={selectedType?.default_base_url}
                onChange={event =>
                  setForm({ ...form, base_url: event.target.value })
                }
              />
              <span className="helper">
                {selectedType?.requires_base_url
                  ? t('Required for this provider type.')
                  : t('Leave empty to use the provider default.')}
              </span>
            </StyledField>

            <StyledField>
              <label htmlFor="provider-api-key">{t('API key')} *</label>
              <Input.Password
                id="provider-api-key"
                value={form.api_key}
                placeholder="sk-..."
                autoComplete="new-password"
                onChange={event =>
                  setForm({ ...form, api_key: event.target.value })
                }
              />
              <span className="helper">
                {editing
                  ? t(
                      'Leave blank to keep the stored key ' +
                        `(${editing.api_key.last_four}).`,
                    )
                  : t('Encrypted at rest and never returned by the API.')}
              </span>
            </StyledField>

            <StyledField>
              <label>{t('Advanced (config)')}</label>
              <StyledRow>
                <div>
                  <StyledSubLabel>{t('Temperature')}</StyledSubLabel>
                  <InputNumber
                    min={0}
                    max={2}
                    step={0.1}
                    style={{ width: '100%' }}
                    value={form.temperature}
                    onChange={value =>
                      setForm({ ...form, temperature: Number(value ?? 0.7) })
                    }
                  />
                </div>
                <div>
                  <StyledSubLabel>{t('Max tokens')}</StyledSubLabel>
                  <InputNumber
                    min={1}
                    max={131072}
                    style={{ width: '100%' }}
                    value={form.max_tokens}
                    onChange={value =>
                      setForm({ ...form, max_tokens: Number(value ?? 1024) })
                    }
                  />
                </div>
                <div>
                  <StyledSubLabel>{t('Timeout (s)')}</StyledSubLabel>
                  <InputNumber
                    min={1}
                    max={600}
                    style={{ width: '100%' }}
                    value={form.timeout_seconds}
                    onChange={value =>
                      setForm({
                        ...form,
                        timeout_seconds: Number(value ?? 60),
                      })
                    }
                  />
                </div>
                {selectedType?.supports_api_version && (
                  <div>
                    <StyledSubLabel>{t('API version')}</StyledSubLabel>
                    <Input
                      value={form.api_version}
                      placeholder="2024-10-21"
                      onChange={event =>
                        setForm({ ...form, api_version: event.target.value })
                      }
                    />
                  </div>
                )}
              </StyledRow>
            </StyledField>
          </StyledCard>

          <div>
            <StyledCard title={t('Tenant')}>
              <StyledField>
                <Input value={TENANT_LABEL} disabled />
                <span className="helper">
                  {t('Global configures every tenant.')}
                </span>
              </StyledField>
            </StyledCard>
            <StyledCard title={t('Status')} style={{ marginTop: 16 }}>
              <StyledSwitchRow>
                <span>{t('Enabled')}</span>
                <Switch
                  checked={form.enabled}
                  onChange={checked => setForm({ ...form, enabled: checked })}
                />
              </StyledSwitchRow>
              <StyledSwitchRow>
                <span>{t('Set as default')}</span>
                <Switch
                  checked={form.is_default}
                  onChange={checked =>
                    setForm({ ...form, is_default: checked })
                  }
                />
              </StyledSwitchRow>
              <StyledField style={{ marginTop: 12, marginBottom: 0 }}>
                <label htmlFor="provider-priority">{t('Priority')}</label>
                <InputNumber
                  id="provider-priority"
                  min={-1000}
                  max={1000}
                  style={{ width: '100%' }}
                  value={form.priority}
                  onChange={value =>
                    setForm({ ...form, priority: Number(value ?? 0) })
                  }
                />
              </StyledField>
            </StyledCard>
            <StyledCard title={t('Connection test')} style={{ marginTop: 16 }}>
              <span className="helper">
                {editing
                  ? t('Use “Test connection” in the table after saving.')
                  : t('Save the provider first to test its connection.')}
              </span>
            </StyledCard>
            {formError && <StyledError>{formError}</StyledError>}
            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <Space>
                <Button
                  buttonStyle="tertiary"
                  onClick={() => setFormOpen(false)}
                >
                  {t('Cancel')}
                </Button>
                <Button buttonStyle="primary" loading={saving} onClick={save}>
                  {t('Save provider')}
                </Button>
              </Space>
            </div>
          </div>
        </StyledLayout>
      ) : (
        <>
          {envFallback?.gemini_configured || envFallback?.deepseek_configured ? (
            <StyledNotice>
              {t(
                'A provider key is also present in the deployment .env file. ' +
                  'Providers saved here take precedence; the .env keys are ' +
                  'only used while no enabled provider exists.',
              )}
            </StyledNotice>
          ) : null}
          <StyledTableCard>
            <Table
              loading={loading}
              data={providers}
              columns={columns}
              rowKey="id"
              size={TableSize.Middle}
              pagination={false}
              locale={{ emptyText: t('No AI providers configured yet.') }}
            />
          </StyledTableCard>
        </>
      )}
    </StyledWrapper>
  );
}
