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
import {
  Button,
  Input,
  Select,
  Space,
  Tag,
  Typography,
} from '@superset-ui/core/components';
import { Icons } from '@superset-ui/core/components/Icons';
import { DatasetOption, FILTER_OPS, FilterRef } from '../types';
import { DragPayload } from './DatasetPanel';

const StyledRow = styled.div`
  ${({ theme }) => `
    display: flex;
    align-items: center;
    gap: ${theme.sizeUnit}px;
    padding: ${theme.sizeUnit}px 0;
    border-bottom: 1px solid ${theme.colorBorderSecondary};
  `}
`;

type Props = {
  datasets: DatasetOption[];
  filters: FilterRef[];
  onChange: (filters: FilterRef[]) => void;
};

function datasetName(datasets: DatasetOption[], id: number): string {
  const ds = datasets.find(item => item.id === id);
  return ds ? ds.table_name : `#${id}`;
}

export default function FiltersPanel({ datasets, filters, onChange }: Props) {
  const [dataset, setDataset] = useState<number | undefined>();
  const [column, setColumn] = useState<string | undefined>();
  const [op, setOp] = useState('=');
  const [value, setValue] = useState('');

  const datasetOptions = useMemo(
    () => datasets.map(ds => ({ label: ds.table_name, value: ds.id })),
    [datasets],
  );

  const columns = useMemo(
    () =>
      datasets
        .find(ds => ds.id === dataset)
        ?.columns.map(col => ({ label: col.column_name, value: col.column_name })) ?? [],
    [datasets, dataset],
  );

  const needsValue = !['IS NULL', 'IS NOT NULL'].includes(op);
  const isMulti = op === 'IN' || op === 'NOT IN';
  const isBetween = op === 'BETWEEN';

  const handleAdd = () => {
    if (dataset === undefined || column === undefined) return;
    if (needsValue && value.trim() === '') return;

    let filterValue: string | string[];
    if (isBetween) {
      const parts = value.split(',').map(part => part.trim());
      filterValue = parts.length === 2 ? parts : [value];
    } else if (isMulti) {
      filterValue = value
        .split(',')
        .map(part => part.trim())
        .filter(Boolean);
    } else {
      filterValue = value;
    }

    const filter: FilterRef = {
      dataset,
      column,
      op,
      value: filterValue,
    };
    onChange([...filters, filter]);
    setValue('');
    setColumn(undefined);
  };

  const handleDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    const raw = event.dataTransfer.getData('application/x-report-field');
    if (!raw) return;
    try {
      const payload = JSON.parse(raw) as DragPayload;
      setDataset(payload.dataset);
      setColumn(payload.column);
      setOp('=');
    } catch {
      /* ignore */
    }
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {filters.length === 0 && (
        <Typography.Text type="secondary">
          {t('No filters. Drag a column here to filter the report.')}
        </Typography.Text>
      )}
      {filters.map((filter, index) => (
        <StyledRow key={`${index}-${filter.dataset}-${filter.column}`}>
          <Tag>{datasetName(datasets, filter.dataset)}</Tag>
          <Typography.Text code>{filter.column}</Typography.Text>
          <Tag color="blue">{filter.op}</Tag>
          <Typography.Text>
            {Array.isArray(filter.value)
              ? filter.value.join(', ')
              : String(filter.value)}
          </Typography.Text>
          <Button
            type="text"
            danger
            size="small"
            icon={<Icons.DeleteOutlined />}
            onClick={() => onChange(filters.filter((_, i) => i !== index))}
          />
        </StyledRow>
      ))}

      <StyledRow
        onDragOver={event => event.preventDefault()}
        onDrop={handleDrop}
      >
        <Select
          placeholder={t('Table')}
          options={datasetOptions}
          value={dataset ?? null}
          style={{ width: 150 }}
          onChange={setDataset}
        />
        <Select
          placeholder={t('Column')}
          options={columns}
          value={column ?? null}
          style={{ width: 140 }}
          showSearch
          onChange={setColumn}
        />
        <Select
          options={FILTER_OPS.map(opItem => ({ label: opItem, value: opItem }))}
          value={op}
          style={{ width: 110 }}
          onChange={setOp}
        />
        {needsValue && (
          <Input
            placeholder={
              isBetween
                ? t('low, high')
                : isMulti
                  ? t('comma separated values')
                  : t('value')
            }
            value={value}
            style={{ width: 200 }}
            onChange={event => setValue(event.target.value)}
            onPressEnter={handleAdd}
          />
        )}
        <Button
          type="primary"
          icon={<Icons.PlusOutlined />}
          disabled={dataset === undefined || column === undefined}
          onClick={handleAdd}
        >
          {t('Add filter')}
        </Button>
      </StyledRow>
    </Space>
  );
}
