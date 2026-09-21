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
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import {
  Button,
  Input,
  Select,
  Space,
  Tag,
  Tooltip,
  Typography,
} from '@superset-ui/core/components';
import { Icons } from '@superset-ui/core/components/Icons';
import {
  AGGREGATES,
  DatasetOption,
  FieldRef,
  MetricRef,
} from '../types';
import { DragPayload } from './DatasetPanel';

const StyledBand = styled.div<{ highlighted?: boolean }>`
  ${({ theme, highlighted }) => `
    border: 1px dashed
      ${highlighted ? theme.colorPrimary : theme.colorBorder};
    border-radius: ${theme.borderRadius}px;
    padding: ${theme.sizeUnit * 2}px;
    min-height: 56px;
    background: ${highlighted ? theme.colorPrimaryBg : 'transparent'};
    transition: background 0.15s ease;
  `}
`;

const StyledBandTitle = styled.div`
  ${({ theme }) => `
    display: flex;
    align-items: center;
    gap: ${theme.sizeUnit}px;
    margin-bottom: ${theme.sizeUnit}px;
    font-weight: ${theme.fontWeightBold};
  `}
`;

const StyledChip = styled.div`
  ${({ theme }) => `
    display: inline-flex;
    align-items: center;
    gap: ${theme.sizeUnit}px;
    background: ${theme.colorPrimaryBg};
    border: 1px solid ${theme.colorPrimaryBorder};
    border-radius: ${theme.borderRadius}px;
    padding: ${theme.sizeUnit - 2}px ${theme.sizeUnit}px;
    margin: 0 ${theme.sizeUnit}px ${theme.sizeUnit}px 0;
  `}
`;

type Props = {
  datasets: DatasetOption[];
  columns: FieldRef[];
  groupBy: FieldRef[];
  metrics: MetricRef[];
  onAddColumn: (field: FieldRef) => void;
  onRemoveColumn: (index: number) => void;
  onUpdateColumnLabel: (index: number, label: string) => void;
  onAddGroup: (field: FieldRef) => void;
  onRemoveGroup: (index: number) => void;
  onAddMetric: (metric: MetricRef) => void;
  onRemoveMetric: (index: number) => void;
  onUpdateMetric: (index: number, metric: MetricRef) => void;
};

function datasetLabel(datasets: DatasetOption[], id: number): string {
  const ds = datasets.find(item => item.id === id);
  return ds ? ds.table_name : `#${id}`;
}

function DropBand({
  title,
  icon,
  children,
  onDropField,
}: {
  title: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
  onDropField: (payload: DragPayload) => void;
}) {
  return (
    <StyledBand
      onDragOver={event => {
        event.preventDefault();
        event.dataTransfer.dropEffect = 'copy';
      }}
      onDrop={event => {
        event.preventDefault();
        const raw = event.dataTransfer.getData('application/x-report-field');
        if (!raw) return;
        try {
          onDropField(JSON.parse(raw) as DragPayload);
        } catch {
          /* ignore malformed payload */
        }
      }}
    >
      <StyledBandTitle>
        {icon}
        {title}
      </StyledBandTitle>
      {children}
    </StyledBand>
  );
}

export default function FieldsPanel({
  datasets,
  columns,
  groupBy,
  metrics,
  onAddColumn,
  onRemoveColumn,
  onUpdateColumnLabel,
  onAddGroup,
  onRemoveGroup,
  onAddMetric,
  onRemoveMetric,
  onUpdateMetric,
}: Props) {
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <DropBand
        title={t('Detail fields')}
        icon={<Icons.UnorderedListOutlined />}
        onDropField={payload =>
          onAddColumn({
            dataset: payload.dataset,
            column: payload.column,
            label: payload.label,
          })
        }
      >
        {columns.length === 0 && (
          <Typography.Text type="secondary">
            {t('Drag columns here to include them in every row.')}
          </Typography.Text>
        )}
        {columns.map((field, index) => (
          <StyledChip key={`${field.dataset}-${field.column}-${index}`}>
            <Tooltip title={datasetLabel(datasets, field.dataset)}>
              <Tag>{datasetLabel(datasets, field.dataset)}</Tag>
            </Tooltip>
            <Input
              size="small"
              defaultValue={field.label}
              style={{ width: 140 }}
              onBlur={event => onUpdateColumnLabel(index, event.target.value)}
            />
            <Button
              size="small"
              type="text"
              icon={<Icons.CloseOutlined />}
              onClick={() => onRemoveColumn(index)}
            />
          </StyledChip>
        ))}
      </DropBand>

      <DropBand
        title={t('Group by (when metrics are used)')}
        icon={<Icons.GroupOutlined />}
        onDropField={payload =>
          onAddGroup({
            dataset: payload.dataset,
            column: payload.column,
            label: payload.label,
          })
        }
      >
        {groupBy.length === 0 && (
          <Typography.Text type="secondary">
            {t('Drag columns here to group the report. If empty, all detail '
              + 'fields are used when metrics are present.')}
          </Typography.Text>
        )}
        {groupBy.map((field, index) => (
          <StyledChip key={`${field.dataset}-${field.column}-${index}`}>
            <Tag>{datasetLabel(datasets, field.dataset)}</Tag>
            <Typography.Text>{field.column}</Typography.Text>
            <Button
              size="small"
              type="text"
              icon={<Icons.CloseOutlined />}
              onClick={() => onRemoveGroup(index)}
            />
          </StyledChip>
        ))}
      </DropBand>

      <DropBand
        title={t('Metrics (aggregations)')}
        icon={<Icons.Sigma />}
        onDropField={payload =>
          onAddMetric({
            dataset: payload.dataset,
            column: payload.column,
            aggregate: 'SUM',
            label: `SUM(${payload.column})`,
          })
        }
      >
        {metrics.length === 0 && (
          <Typography.Text type="secondary">
            {t('Drag columns here to aggregate them.')}
          </Typography.Text>
        )}
        {metrics.map((metric, index) => (
          <StyledChip key={`${metric.dataset}-${metric.column}-${index}`}>
            <Tag>{datasetLabel(datasets, metric.dataset)}</Tag>
            <Select
              size="small"
              value={metric.aggregate}
              options={AGGREGATES.map(agg => ({ label: agg, value: agg }))}
              style={{ width: 120 }}
              onChange={aggregate =>
                onUpdateMetric(index, {
                  ...metric,
                  aggregate,
                  label: `${aggregate}(${metric.column})`,
                })
              }
            />
            <Input
              size="small"
              defaultValue={metric.label}
              style={{ width: 140 }}
              onBlur={event =>
                onUpdateMetric(index, { ...metric, label: event.target.value })
              }
            />
            <Button
              size="small"
              type="text"
              icon={<Icons.CloseOutlined />}
              onClick={() => onRemoveMetric(index)}
            />
          </StyledChip>
        ))}
      </DropBand>
    </Space>
  );
}
