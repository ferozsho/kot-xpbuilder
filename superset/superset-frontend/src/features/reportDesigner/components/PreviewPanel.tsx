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
import { useMemo, useState } from 'react';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import { Alert } from '@apache-superset/core/components';
import {
  Button,
  Space,
  Table,
  Tag,
  Typography,
} from '@superset-ui/core/components';
import { Icons } from '@superset-ui/core/components/Icons';
import { PreviewResult } from '../types';

const StyledWrapper = styled.div`
  ${({ theme }) => `
    border-top: 1px solid ${theme.colorBorderSecondary};
    padding-top: ${theme.sizeUnit * 2}px;
    margin-top: ${theme.sizeUnit * 3}px;
  `}
`;

const StyledSql = styled.pre`
  ${({ theme }) => `
    background: ${theme.colorFillAlter};
    border: 1px solid ${theme.colorBorder};
    border-radius: ${theme.borderRadius}px;
    padding: ${theme.sizeUnit * 2}px;
    overflow: auto;
    max-height: 240px;
    font-size: ${theme.fontSizeSM}px;
  `}
`;

type Props = {
  result: PreviewResult | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
};

function stringify(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

export default function PreviewPanel({
  result,
  loading,
  error,
  onRefresh,
}: Props) {
  const [showSql, setShowSql] = useState(false);

  const columns = useMemo(
    () =>
      result?.columns.map(col => ({
        title: col,
        dataIndex: col,
        key: col,
        ellipsis: true,
        render: (value: unknown) => stringify(value),
      })) ?? [],
    [result],
  );

  const rows = useMemo(
    () =>
      (result?.rows ?? []).map((row, index) => ({
        key: index,
        ...row,
      })),
    [result],
  );

  return (
    <StyledWrapper>
      <Space style={{ marginBottom: 8 }} align="center">
        <Typography.Title level={5} style={{ margin: 0 }}>
          {t('Live preview')}
        </Typography.Title>
        <Button
          icon={<Icons.PlayCircleOutlined />}
          loading={loading}
          onClick={onRefresh}
        >
          {t('Run preview')}
        </Button>
        {result && (
          <>
            <Tag color="green">{result.count} rows</Tag>
            {result.truncated && <Tag color="orange">{t('truncated')}</Tag>}
            <Button
              type="link"
              size="small"
              onClick={() => setShowSql(!showSql)}
            >
              {showSql ? t('Hide SQL') : t('Show SQL')}
            </Button>
          </>
        )}
      </Space>

      {error && (
        <Alert
          type="error"
          showIcon
          message={t('Preview failed')}
          description={error}
          style={{ marginBottom: 8 }}
        />
      )}

      {result && showSql && <StyledSql>{result.sql}</StyledSql>}

      <Table
        dataSource={rows}
        columns={columns}
        loading={loading}
        size="small"
        scroll={{ x: 'max-content', y: 420 }}
        pagination={{ pageSize: 50, showSizeChanger: true }}
      />
    </StyledWrapper>
  );
}
