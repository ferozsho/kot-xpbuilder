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
import { useCallback, useState } from 'react';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import { Button, Card, Input } from '@superset-ui/core/components';
import { SupersetClient } from '@superset-ui/core';
import { useToasts } from 'src/components/MessageToasts/withToasts';
import { describeError } from './api';

/**
 * Tenant offboarding (P11-F06), which had a service endpoint and no way to reach
 * it.
 *
 * It asks for the tenant key to be typed rather than picked from a list, because
 * a dropdown makes an irreversible action one mis-click away. The service refuses
 * the default tenant outright; the typed key is the second guard, not the first.
 */

const Heading = styled.h3`
  ${({ theme }) => `
    margin: 0 0 ${theme.sizeUnit * 2}px;
    font-size: ${theme.fontSizeLG}px;
    font-weight: ${theme.fontWeightStrong};
  `}
`;

const Row = styled.div`
  ${({ theme }) => `
    display: flex;
    gap: ${theme.sizeUnit * 2}px;
    align-items: center;
    flex-wrap: wrap;
    margin-top: ${theme.sizeUnit * 2}px;
  `}
`;

const Warning = styled.p`
  ${({ theme }) => `
    margin: ${theme.sizeUnit * 2}px 0 0;
    padding: ${theme.sizeUnit * 2}px;
    background: ${theme.colorErrorBg};
    border: 1px solid ${theme.colorErrorBorder};
    border-radius: ${theme.borderRadius}px;
    font-size: ${theme.fontSizeSM}px;
  `}
`;

const Result = styled.pre`
  ${({ theme }) => `
    margin: ${theme.sizeUnit * 2}px 0 0;
    padding: ${theme.sizeUnit * 2}px;
    background: ${theme.colorFillSecondary};
    border-radius: ${theme.borderRadius}px;
    font-size: ${theme.fontSizeSM}px;
    max-height: 220px;
    overflow: auto;
  `}
`;

export default function OffboardingCard() {
  const { addDangerToast, addSuccessToast } = useToasts();
  const [tenantKey, setTenantKey] = useState('');
  const [confirmKey, setConfirmKey] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);

  const ready = tenantKey.trim() !== '' && tenantKey.trim() === confirmKey.trim();

  const onOffboard = useCallback(async () => {
    setBusy(true);
    try {
      const { json } = await SupersetClient.post({
        endpoint: `/aiprompt/api/tenants/${encodeURIComponent(
          tenantKey.trim(),
        )}/offboard/`,
        jsonPayload: { reason },
      });
      setResult(json as Record<string, unknown>);
      addSuccessToast(t('%s offboarded', tenantKey.trim()));
    } catch (error) {
      addDangerToast(await describeError(error));
    } finally {
      setBusy(false);
    }
  }, [addDangerToast, addSuccessToast, reason, tenantKey]);

  return (
    <Card data-test="ai-offboarding">
      <Heading>{t('Offboard a tenant')}</Heading>
      <Warning>
        {t(
          'Removes that tenant’s conversations, jobs, usage, patterns, drafts and settings. ' +
            'Global configuration and the default tenant are untouched, and the removal is logged.',
        )}
      </Warning>
      <Row>
        <Input
          placeholder={t('Tenant key')}
          value={tenantKey}
          onChange={(event: { target: { value: string } }) =>
            setTenantKey(event.target.value)
          }
          style={{ maxWidth: 200 }}
        />
        <Input
          placeholder={t('Type the same key to confirm')}
          value={confirmKey}
          onChange={(event: { target: { value: string } }) =>
            setConfirmKey(event.target.value)
          }
          style={{ maxWidth: 240 }}
        />
        <Input
          placeholder={t('Why? (recorded)')}
          value={reason}
          onChange={(event: { target: { value: string } }) =>
            setReason(event.target.value)
          }
          style={{ maxWidth: 280 }}
        />
        <Button
          buttonStyle="danger"
          disabled={!ready || busy}
          loading={busy}
          onClick={onOffboard}
        >
          {t('Offboard')}
        </Button>
      </Row>
      {!ready && (tenantKey || confirmKey) && (
        <Warning>{t('The two keys must match before this is enabled.')}</Warning>
      )}
      {result && <Result>{JSON.stringify(result, null, 2)}</Result>}
    </Card>
  );
}
